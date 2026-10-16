#!/usr/bin/env python3
"""Read the Extra spreadsheet through the Google Sheets API, without writes.

The module deliberately keeps authentication imports inside ``live_session`` so
the parsing and transport behaviour can be unit-tested on hosts that do not
have the production Google dependencies installed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

LOG = logging.getLogger("google_extra_reader")
API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
SPREADSHEET_ID = os.environ.get("EXTRA_SPREADSHEET_ID", "")
SHEET_NAME = "Propostas"
EXPECTED_TITLE = "Controle Global Propostas - Extra"
READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_CREDENTIALS = "/etc/confenge-gestao/google-readonly.json"
try:
    SAO_PAULO = ZoneInfo("America/Sao_Paulo")
except Exception:  # Minimal Windows Python installations may omit the IANA database.
    SAO_PAULO = timezone(timedelta(hours=-3), "America/Sao_Paulo")
WIDTH, ROWS, FIRST_PAGE_ROWS, CHUNK_ROWS = 84, 1000, 100, 400
CRITICAL_HEADERS = {3: "Valor", 19: "ID PNCP / chave do certame", 42: "Referência validada / estado", 52: "Unidade / lote"}


class ReaderError(RuntimeError):
    """A safe, non-secret-bearing source error."""


class Response(Protocol):
    status_code: int
    headers: dict[str, str]
    def json(self) -> dict[str, Any]: ...


class Session(Protocol):
    def get(self, url: str, *, params: dict[str, str] | None = None, timeout: int = 20) -> Response: ...


def now_iso() -> str:
    return datetime.now(SAO_PAULO).isoformat()


def _safe_error(response: Response | None = None, exc: Exception | None = None) -> str:
    if response is not None:
        return "http_%d" % response.status_code
    return "network_error" if exc is not None else "source_error"


def _retry_delay(response: Response | None, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "") if response else ""
    try:
        return min(5.0, max(0.0, float(retry_after)))
    except ValueError:
        return min(5.0, 0.25 * (2 ** attempt))


def get_json(session: Session, path: str, *, params: dict[str, str] | None = None, sleep=time.sleep) -> dict[str, Any]:
    """Issue at most three HTTPS GETs against the fixed Sheets API origin."""
    if not path.startswith("/") or ":" in path.split("/")[0]:
        raise ValueError("invalid API path")
    url = API_ROOT + path
    last_error = "source_error"
    for attempt in range(3):
        response: Response | None = None
        try:
            response = session.get(url, params=params, timeout=20)
            if 200 <= response.status_code < 300:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                raise ReaderError("invalid_json")
            last_error = _safe_error(response=response)
            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
        except ReaderError:
            raise
        except Exception as exc:
            last_error = _safe_error(exc=exc)
            retryable = True
        if not retryable:
            break
        if attempt < 2:
            sleep(_retry_delay(response, attempt))
    raise ReaderError(last_error)


def _sheet_properties(metadata: dict[str, Any]) -> dict[str, Any]:
    if metadata.get("properties", {}).get("title") != EXPECTED_TITLE:
        raise ReaderError("unexpected_spreadsheet")
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == SHEET_NAME:
            grid = properties.get("gridProperties", {})
            if properties.get("sheetId") != 0 or grid.get("columnCount") != WIDTH or grid.get("rowCount") != ROWS:
                raise ReaderError("unexpected_sheet_bounds")
            return properties
    raise ReaderError("missing_propostas_sheet")


def validate_headers(values: list[list[Any]]) -> list[Any]:
    if not values or not isinstance(values[0], list) or len(values[0]) != WIDTH:
        raise ReaderError("invalid_header_width")
    headers = values[0]
    for index, expected in CRITICAL_HEADERS.items():
        if headers[index] != expected:
            raise ReaderError("invalid_header_%d" % index)
    return headers


def _rows(payload: dict[str, Any]) -> list[list[Any]]:
    values = payload.get("values", [])
    if not isinstance(values, list):
        raise ReaderError("invalid_values")
    if not all(isinstance(row, list) for row in values):
        raise ReaderError("invalid_values")
    return values


def _merge(matrix: list[list[Any]], offset: int, rows: list[list[Any]]) -> None:
    required = min(ROWS, offset + len(rows))
    if len(matrix) < required:
        matrix.extend([] for _ in range(required - len(matrix)))
    for position, row in enumerate(rows):
        target = offset + position
        if target >= ROWS:
            break
        matrix[target] = row[:]  # retain explicit nulls and empty presentation rows


def _last_nonempty_row(matrix: list[list[Any]]) -> int:
    for index in range(len(matrix) - 1, -1, -1):
        if matrix[index]:
            return index + 1
    return 0


def _validation(values: list[list[Any]]) -> dict[str, Any]:
    headers = validate_headers(values)
    ids: dict[str, set[str]] = {}
    for row in values[1:]:
        if len(row) <= 19 or not row[19]:
            continue
        identifier = str(row[19])
        lot = "" if len(row) <= 52 or row[52] is None else str(row[52])
        ids.setdefault(identifier, set()).add(lot)
    ambiguous = sum(1 for lots in ids.values() if len(lots) > 1)
    return {"header_count": len(headers), "canonical_id_count": len(ids), "duplicate_ids_different_lots": ambiguous}


def read_snapshot(session: Session, spreadsheet_id: str = SPREADSHEET_ID, *, sleep=time.sleep) -> dict[str, Any]:
    """Fetch the bounded initial range and expand only if column T needs it."""
    if not spreadsheet_id or "/" in spreadsheet_id:
        raise ValueError("invalid spreadsheet id")
    encoded_id = quote(spreadsheet_id, safe="")
    metadata = get_json(session, "/" + encoded_id, params={"fields": "properties(title,timeZone),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"}, sleep=sleep)
    properties = _sheet_properties(metadata)
    initial = get_json(session, "/" + encoded_id + "/values/" + quote("Propostas!A1:CF100", safe=""), params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}, sleep=sleep)
    matrix = _rows(initial)
    validate_headers(matrix)
    sentinel = get_json(session, "/" + encoded_id + "/values/" + quote("Propostas!T101:T1000", safe=""), params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}, sleep=sleep)
    sentinel_rows = _rows(sentinel)
    has_more_ids = any(bool(row and row[0]) for row in sentinel_rows)
    if has_more_ids:
        for start in range(101, ROWS + 1, CHUNK_ROWS):
            end = min(ROWS, start + CHUNK_ROWS - 1)
            page = get_json(session, "/" + encoded_id + "/values/" + quote("Propostas!A%d:CF%d" % (start, end), safe=""), params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}, sleep=sleep)
            _merge(matrix, start - 1, _rows(page))
    last = _last_nonempty_row(matrix)
    values = matrix[:last]
    validation = _validation(values)
    fetched_at = now_iso()
    return {
        "range": "Propostas!A1:CF%d" % max(FIRST_PAGE_ROWS, last),
        "majorDimension": "ROWS",
        "values": values,
        "metadata": {"spreadsheet_title": metadata["properties"]["title"], "sheet_id": properties["sheetId"], "sheet_title": properties["title"], "row_count": ROWS, "column_count": WIDTH},
        "source_fetched_at": fetched_at,
        "source_status": "FRESH",
        "read_only": True,
        "validation": validation,
    }


def write_json_secure(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _old_last_success(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_success")
    except (OSError, ValueError, AttributeError):
        return None


def _write_status(path: Path, *, status: str, last_success: str | None, error: str | None, counts: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"status": status, "checked_at": now_iso(), "last_success": last_success}
    if error:
        payload["error_code"] = error
    if counts:
        payload["counts"] = counts
    write_json_secure(path, payload)


def live_session(credentials_path: Path) -> Session:
    """Build Google-maintained auth transport with the sole readonly scope."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_file(str(credentials_path), scopes=[READONLY_SCOPE])
    return AuthorizedSession(credentials)


def run(output: Path, status_path: Path, credentials_path: Path, spreadsheet_id: str) -> dict[str, Any]:
    try:
        snapshot = read_snapshot(live_session(credentials_path), spreadsheet_id)
        write_json_secure(output, snapshot)
        validation = snapshot["validation"]
        _write_status(status_path, status="FRESH", last_success=snapshot["source_fetched_at"], error=None, counts=validation)
        LOG.info("google_sheet_read status=FRESH rows=%d ids=%d ambiguous_ids=%d", len(snapshot["values"]) - 1, validation["canonical_id_count"], validation["duplicate_ids_different_lots"])
        return snapshot
    except (ReaderError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, ReaderError) else "source_error"
        _write_status(status_path, status="STALE", last_success=_old_last_success(status_path), error=code)
        LOG.warning("google_sheet_read status=STALE error=%s", code)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Read the Extra sheet using readonly Google Sheets API calls.")
    parser.add_argument("--output", type=Path, default=Path("/var/lib/confenge-gestao/extra-snapshot.json"))
    parser.add_argument("--status-path", type=Path, default=Path("/var/lib/confenge-gestao/extra-source-status.json"))
    parser.add_argument("--credentials", type=Path, default=Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_CREDENTIALS)))
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID,
                        help="Google Sheet id (or set EXTRA_SPREADSHEET_ID)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(args.credentials))
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        run(args.output, args.status_path, args.credentials, args.spreadsheet_id)
    except (ReaderError, OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
