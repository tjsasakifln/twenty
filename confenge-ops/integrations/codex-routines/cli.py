#!/usr/bin/env python3
"""CLI for catalog planning, durable enqueue, and confirmed Twenty delivery."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from catalog import reconcile_catalog, write_private_catalog
from contract import ContractError, load_catalog, validate_catalog_document, validate_event
from outbox import Outbox
from twenty import ApiError, RoutineGateway, TwentyClient, run_extra_sync

ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = Path(
    os.environ.get("CODEX_ROUTINES_CATALOG") or ROOT / "catalog.json"
)
DEFAULT_LOCAL_OUTBOX = Path(os.environ.get("CODEX_ROUTINES_OUTBOX") or Path.home() / ".codex" / "codex-routines" / "outbox.sqlite3")
DEFAULT_REMOTE_OUTBOX = Path("/var/lib/confenge-gestao/codex-routines/outbox.sqlite3")
DEFAULT_CREDENTIAL_FILE = Path("/etc/confenge-gestao/admin.json")
DEFAULT_EXTRA_SYNC = ["/opt/confenge-gestao/scripts/infra/extra-sync.sh"]


def read_json(path: str | None) -> dict[str, Any]:
    if path == "-" or path is None:
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def gateway(args: argparse.Namespace, catalog: dict[str, Any]) -> RoutineGateway:
    credentials = json.loads(Path(args.credential_file).read_text(encoding="utf-8"))
    base_url = args.base_url or credentials.get("base_url") or "http://127.0.0.1:18300"
    client = TwentyClient(base_url, credentials["api_key"])
    client.preflight_workspace(credentials.get("workspace_id"))
    return RoutineGateway(client, catalog)


def remote_deliver(
    event: dict[str, Any], args: argparse.Namespace, catalog: dict[str, Any]
) -> dict[str, Any]:
    if not args.ssh_host:
        raise RuntimeError(
            "SSH delivery requires --ssh-host or CODEX_ROUTINES_SSH_HOST"
        )
    command = [
        "wsl.exe",
        "-d",
        args.wsl_distribution,
        "--",
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        args.ssh_host,
        args.remote_python,
        args.remote_cli,
        "ingest",
        "--apply",
        "--input",
        "-",
        "--catalog-envelope",
    ]
    public_catalog = {key: value for key, value in catalog.items() if key != "byId"}
    process = subprocess.run(
        command,
        input=json.dumps(
            {"catalog": public_catalog, "event": event}, ensure_ascii=False
        ).encode("utf-8"),
        capture_output=True,
        timeout=args.timeout,
        check=False,
    )
    if process.returncode != 0:
        error = process.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(
            f"remote delivery failed with exit code {process.returncode}: {error}"
        )
    return json.loads(process.stdout.decode("utf-8"))


def local_deliver(event: dict[str, Any], args: argparse.Namespace, catalog: dict[str, Any]) -> dict[str, Any]:
    if event.get("requestExtraSync"):
        run_extra_sync(args.extra_sync_command)
    return gateway(args, catalog).apply_event(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--base-url")
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema = subparsers.add_parser("schema")
    schema.add_argument("--apply", action="store_true")

    catalog_refresh = subparsers.add_parser("catalog-refresh")
    catalog_refresh.add_argument(
        "--automations-dir",
        type=Path,
        default=Path.home() / ".codex" / "automations",
    )

    catalog_sync = subparsers.add_parser("catalog-sync")
    catalog_sync.add_argument("--apply", action="store_true")

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--input", default="-")
    enqueue.add_argument("--outbox", type=Path, default=DEFAULT_LOCAL_OUTBOX)

    flush = subparsers.add_parser("flush")
    flush.add_argument("--outbox", type=Path, default=DEFAULT_LOCAL_OUTBOX)
    flush.add_argument("--apply", action="store_true")
    flush.add_argument("--transport", choices=("ssh", "local"), default="ssh")
    flush.add_argument("--wsl-distribution", default="Ubuntu")
    flush.add_argument(
        "--ssh-host",
        default=os.environ.get("CODEX_ROUTINES_SSH_HOST"),
        help="private SSH host alias; required for SSH delivery",
    )
    flush.add_argument("--remote-python", default="/opt/confenge-gestao/.venv/bin/python")
    flush.add_argument("--remote-cli", default="/opt/confenge-gestao/integrations/codex-routines/cli.py")
    flush.add_argument("--timeout", type=int, default=180)
    flush.add_argument("--extra-sync-command", nargs="+", default=DEFAULT_EXTRA_SYNC)

    status = subparsers.add_parser("status")
    status.add_argument("--outbox", type=Path, default=DEFAULT_LOCAL_OUTBOX)

    requeue = subparsers.add_parser("requeue")
    requeue.add_argument("--outbox", type=Path, default=DEFAULT_LOCAL_OUTBOX)
    choice = requeue.add_mutually_exclusive_group(required=True)
    choice.add_argument("--event-key")
    choice.add_argument("--all", action="store_true")

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--input", default="-")
    ingest.add_argument("--outbox", type=Path, default=DEFAULT_REMOTE_OUTBOX)
    ingest.add_argument("--apply", action="store_true")
    ingest.add_argument("--catalog-envelope", action="store_true")
    ingest.add_argument("--extra-sync-command", nargs="+", default=DEFAULT_EXTRA_SYNC)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "catalog-refresh":
        catalog = reconcile_catalog(args.automations_dir, args.catalog)
        print(
            json.dumps(
                {
                    "catalog": str(args.catalog),
                    "routineCount": len(catalog["routines"]),
                    "automationIds": sorted(catalog["byId"]),
                    "promptsStored": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    envelope: dict[str, Any] | None = None
    if args.command == "ingest" and args.catalog_envelope:
        envelope = read_json(args.input)
        if set(envelope) != {"catalog", "event"}:
            raise ContractError("catalog envelope must contain exactly catalog and event")
        catalog = validate_catalog_document(envelope["catalog"])
        write_private_catalog(args.catalog, catalog)
    else:
        catalog = load_catalog(args.catalog)
    if args.command == "schema":
        target = gateway(args, catalog)
        result = target.apply_schema() if args.apply else target.schema_plan().summary()
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "schema": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "catalog-sync":
        if not args.apply:
            print(json.dumps({"mode": "dry-run", "routineCount": len(catalog["routines"]), "automationIds": sorted(catalog["byId"])}, ensure_ascii=False, indent=2))
            return 0
        result = gateway(args, catalog).sync_catalog()
        print(json.dumps({"mode": "apply", "catalog": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "enqueue":
        event = validate_event(read_json(args.input), catalog)
        box = Outbox(args.outbox)
        try:
            result = box.enqueue(event)
            print(json.dumps({"status": result, "eventKey": event["eventKey"], "payloadHash": event["payloadHash"], "outbox": box.status()}, ensure_ascii=False))
        finally:
            box.close()
        return 0
    if args.command == "status":
        box = Outbox(args.outbox)
        try:
            print(json.dumps(box.status(), ensure_ascii=False, indent=2))
        finally:
            box.close()
        return 0
    if args.command == "requeue":
        box = Outbox(args.outbox)
        try:
            count = box.requeue_dead(
                event_key=args.event_key,
                requeue_all=args.all,
            )
            print(
                json.dumps(
                    {"requeued": count, "outbox": box.status()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            box.close()
        return 0
    if args.command == "flush":
        box = Outbox(args.outbox)
        try:
            if not args.apply:
                print(json.dumps({"mode": "dry-run", "outbox": box.status()}, ensure_ascii=False, indent=2))
                return 0
            deliver = (
                (lambda event: remote_deliver(event, args, catalog))
                if args.transport == "ssh"
                else (lambda event: local_deliver(event, args, catalog))
            )
            print(json.dumps({"mode": "apply", "flush": box.flush(deliver), "outbox": box.status()}, ensure_ascii=False, indent=2))
            final = box.status()
            if final["pending"] or final["dead"]:
                return 3
        finally:
            box.close()
        return 0
    if args.command == "ingest":
        raw_event = envelope["event"] if envelope is not None else read_json(args.input)
        event = validate_event(raw_event, catalog)
        box = Outbox(args.outbox)
        try:
            queued = box.enqueue(event)
            if not args.apply:
                print(json.dumps({"confirmed": False, "queued": queued, "payloadHash": event["payloadHash"]}, ensure_ascii=False))
                return 0
            target = gateway(args, catalog)
            catalog_result: dict[str, int] | None = None

            def deliver_ingested(value: dict[str, Any]) -> dict[str, Any]:
                nonlocal catalog_result
                if catalog_result is None:
                    catalog_result = target.sync_catalog()
                if value.get("requestExtraSync"):
                    run_extra_sync(args.extra_sync_command)
                return target.apply_event(value)

            summary = box.flush(deliver_ingested)
            if box.event_status(event["eventKey"]) != "SENT":
                raise RuntimeError("event remains queued for retry")
            print(json.dumps({"confirmed": True, "queued": queued, "payloadHash": event["payloadHash"], "catalog": catalog_result, "flush": summary}, ensure_ascii=False))
        finally:
            box.close()
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, ContractError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
