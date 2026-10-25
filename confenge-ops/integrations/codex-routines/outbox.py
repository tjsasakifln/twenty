"""SQLite outbox: enqueue durably before attempting any network delivery."""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from contract import ContractError, canonical_json


class Outbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','DEAD')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                last_error TEXT,
                last_dead_error TEXT,
                requeue_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                sent_at REAL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_lock (
                lock_id INTEGER PRIMARY KEY CHECK(lock_id = 1),
                owner TEXT,
                lease_until REAL NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO delivery_lock(lock_id,owner,lease_until) VALUES(1,NULL,0)"
        )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "last_dead_error" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN last_dead_error TEXT")
        if "requeue_count" not in columns:
            self.connection.execute(
                "ALTER TABLE events ADD COLUMN requeue_count INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def enqueue(self, event: dict[str, Any], *, now: float | None = None) -> str:
        event_key = str(event["eventKey"])
        body = canonical_json(event)
        digest = str(event["payloadHash"])
        existing = self.connection.execute(
            "SELECT payload_hash FROM events WHERE event_key = ?", (event_key,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ContractError(f"conflicting replay for eventKey: {event_key}")
            return "duplicate"
        timestamp = time.time() if now is None else now
        self.connection.execute(
            "INSERT INTO events(event_key,payload_json,payload_hash,status,next_attempt_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (event_key, body, digest, "PENDING", timestamp, timestamp),
        )
        self.connection.commit()
        return "queued"

    def pending(self, *, now: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else now
        rows = self.connection.execute(
            "SELECT * FROM events WHERE status='PENDING' AND next_attempt_at <= ? "
            "ORDER BY created_at,event_key LIMIT ?",
            (timestamp, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def flush(
        self,
        deliver: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        now: float | None = None,
        max_attempts: int = 8,
        jitter: Callable[[], float] | None = None,
        lease_seconds: float = 7200.0,
    ) -> dict[str, int]:
        timestamp = time.time() if now is None else now
        jitter = jitter or random.random
        summary = {"sent": 0, "retried": 0, "dead": 0, "busy": 0}
        owner = str(uuid.uuid4())
        if not self._acquire_delivery_lock(owner, timestamp, lease_seconds):
            summary["busy"] = 1
            return summary
        try:
            for row in self.pending(now=timestamp):
                self._renew_delivery_lock(owner, time.time(), lease_seconds)
                event = json.loads(row["payload_json"])
                try:
                    receipt = deliver(event)
                    if receipt.get("confirmed") is not True or receipt.get("payloadHash") != row["payload_hash"]:
                        raise RuntimeError("delivery did not return a matching confirmed payload hash")
                    self.connection.execute(
                        "UPDATE events SET status='SENT',sent_at=?,last_error=NULL "
                        "WHERE event_key=? AND status='PENDING'",
                        (timestamp, row["event_key"]),
                    )
                    summary["sent"] += 1
                except Exception as exc:  # delivery failures remain durable
                    attempts = int(row["attempts"]) + 1
                    error = str(exc)[:1000]
                    if attempts >= max_attempts:
                        self.connection.execute(
                            "UPDATE events SET status='DEAD',attempts=?,last_error=?,last_dead_error=? "
                            "WHERE event_key=? AND status='PENDING'",
                            (attempts, error, error, row["event_key"]),
                        )
                        summary["dead"] += 1
                    else:
                        delay = min(3600.0, float(2 ** min(attempts, 10))) + float(jitter())
                        self.connection.execute(
                            "UPDATE events SET attempts=?,next_attempt_at=?,last_error=? "
                            "WHERE event_key=? AND status='PENDING'",
                            (attempts, timestamp + delay, error, row["event_key"]),
                        )
                        summary["retried"] += 1
                self.connection.commit()
            return summary
        finally:
            self._release_delivery_lock(owner)

    def _acquire_delivery_lock(
        self, owner: str, now: float, lease_seconds: float
    ) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT owner,lease_until FROM delivery_lock WHERE lock_id=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("delivery lock row is missing")
            if row["owner"] and float(row["lease_until"]) > now:
                self.connection.commit()
                return False
            cursor = self.connection.execute(
                "UPDATE delivery_lock SET owner=?,lease_until=? WHERE lock_id=1 "
                "AND (owner IS NULL OR lease_until <= ?)",
                (owner, now + lease_seconds, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self.connection.rollback()
            raise

    def _renew_delivery_lock(
        self, owner: str, now: float, lease_seconds: float
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE delivery_lock SET lease_until=? WHERE lock_id=1 AND owner=?",
            (now + lease_seconds, owner),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("delivery lease was lost")

    def _release_delivery_lock(self, owner: str) -> None:
        self.connection.execute(
            "UPDATE delivery_lock SET owner=NULL,lease_until=0 WHERE lock_id=1 AND owner=?",
            (owner,),
        )
        self.connection.commit()

    def status(self) -> dict[str, Any]:
        counts = {
            row["status"]: row["count"]
            for row in self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        }
        oldest = self.connection.execute(
            "SELECT MIN(created_at) AS value FROM events WHERE status='PENDING'"
        ).fetchone()["value"]
        return {
            "pending": counts.get("PENDING", 0),
            "sent": counts.get("SENT", 0),
            "dead": counts.get("DEAD", 0),
            "oldestPendingAt": oldest,
        }

    def event_status(self, event_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM events WHERE event_key = ?", (event_key,)
        ).fetchone()
        return str(row["status"]) if row is not None else None

    def requeue_dead(
        self,
        *,
        event_key: str | None = None,
        requeue_all: bool = False,
        now: float | None = None,
    ) -> int:
        if (event_key is None) == (not requeue_all):
            raise ValueError("provide exactly one of event_key or requeue_all=True")
        timestamp = time.time() if now is None else now
        where = "status='DEAD'"
        parameters: list[Any] = [timestamp]
        if event_key is not None:
            where += " AND event_key=?"
            parameters.append(event_key)
        cursor = self.connection.execute(
            "UPDATE events SET status='PENDING',attempts=0,next_attempt_at=?,"
            "last_error='manual_requeue',sent_at=NULL,requeue_count=requeue_count+1 "
            f"WHERE {where}",
            parameters,
        )
        self.connection.commit()
        return int(cursor.rowcount)
