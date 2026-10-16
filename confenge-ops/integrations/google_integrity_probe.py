#!/usr/bin/env python3
"""Hash the complete Extra grid using the Google Sheets readonly API.

The resulting evidence deliberately contains no spreadsheet values or formulas:
only a canonical SHA-256, requested range, dimensions, and non-empty-cell count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
RANGE = "Propostas!A1:CF1000"
FIELDS = "properties.title,sheets(properties(sheetId,title,gridProperties),data.rowData.values.userEnteredValue)"


def write_secure(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path("/etc/confenge-gestao/google-readonly.json"))
    parser.add_argument("--spreadsheet-id", default=os.environ.get("EXTRA_SPREADSHEET_ID", ""),
                        help="Google Sheet id (or set EXTRA_SPREADSHEET_ID)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.spreadsheet_id or "/" in args.spreadsheet_id:
        raise ValueError("invalid spreadsheet id")
    credentials = service_account.Credentials.from_service_account_file(str(args.credentials), scopes=[SCOPE])
    response = AuthorizedSession(credentials).get(
        "https://sheets.googleapis.com/v4/spreadsheets/" + quote(args.spreadsheet_id, safe=""),
        params={"ranges": RANGE, "includeGridData": "true", "fields": FIELDS}, timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError("sheets_http_%d" % response.status_code)
    value = response.json()
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    rows = value.get("sheets", [{}])[0].get("data", [{}])[0].get("rowData", [])
    nonempty = sum(1 for row in rows for cell in row.get("values", []) if cell.get("userEnteredValue"))
    write_secure(args.output, {
        "status": "ok",
        "api": "Google Sheets v4 GET",
        "scope": SCOPE,
        "range": RANGE,
        "requested_dimensions": {"rows": 1000, "columns": 84},
        "returned_row_data": len(rows),
        "nonempty_cells": nonempty,
        "canonical_sha256": hashlib.sha256(encoded).hexdigest(),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
