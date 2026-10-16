#!/usr/bin/env python3
"""Read-only importer for the Extra procurement-control spreadsheet.

This module creates an auditable staging JSON file.  It deliberately has no
Twenty endpoint assumptions and never deletes records from an existing state.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
ID_HEADER = "ID PNCP / chave do certame"       # Spreadsheet column T
LOT_HEADER = "Unidade / lote"                  # Spreadsheet column BA
MANUAL_KEYS = ("manual", "manual_fields")

LOG = logging.getLogger("extra_readonly")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_brl(value: Any, field: str, issues: list[dict[str, str]]) -> str | None:
    """Parse a Brazilian currency cell without turning unknown values into zero."""
    text = _text(value)
    if not text:
        return None
    if "#REF!" in text.upper():
        issues.append({"code": "formula_ref", "field": field})
        return None
    normalized = text.replace("R$", "").replace(" ", "")
    # Only unambiguous BRL numerals are values; prose such as "não definido" is unknown.
    if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:\.\d{3})*|\d+)(?:,\d{1,2})?", normalized):
        return None
    try:
        return format(Decimal(normalized.replace(".", "").replace(",", ".")).quantize(Decimal(".01")), "f")
    except InvalidOperation:
        issues.append({"code": "invalid_brl", "field": field})
        return None


def parse_deadline(value: Any, field: str, issues: list[dict[str, str]]) -> str | None:
    """Return only an explicit source date/time; prose deadlines stay unparsed."""
    text = _text(value)
    if not text:
        return None
    match = re.match(r"^(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}))?$", text)
    if not match:
        return None
    try:
        fmt = "%d/%m/%Y %H:%M" if match.group(2) else "%d/%m/%Y"
        parsed = datetime.strptime(text, fmt)
        if match.group(2):
            return parsed.replace(tzinfo=SAO_PAULO).isoformat()
        return parsed.date().isoformat()
    except ValueError:
        issues.append({"code": "invalid_deadline", "field": field})
        return None


def _row_dict(headers: list[str], row: list[Any]) -> dict[str, str]:
    return {header: _text(row[index]) if index < len(row) else "" for index, header in enumerate(headers)}


def _observed_at(raw: dict[str, str]) -> str | None:
    issues: list[dict[str, str]] = []
    return parse_deadline(raw.get("Data/hora da fonte oficial", ""), "official_observed_at", issues)


def records_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    values = snapshot.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("snapshot must contain a non-empty values matrix")
    headers = [_text(value) for value in values[0]]
    if ID_HEADER not in headers or LOT_HEADER not in headers:
        raise ValueError("snapshot does not have the required T/BA headers")
    records: list[dict[str, Any]] = []
    for number, row in enumerate(values[1:], start=2):
        if not isinstance(row, list):
            continue
        raw = _row_dict(headers, row)
        source_id = raw[ID_HEADER]
        if not source_id:  # Presentation rows do not have an official identifier.
            continue
        issues: list[dict[str, str]] = []
        lot = raw[LOT_HEADER]
        key = "T:" + source_id + "|BA:" + lot
        fields = {
            "procurement_id": source_id,
            "unit_or_lot": lot or None,
            "contracting_org": raw["Órgão contratante"] or None,
            "tender_id": raw["ID Edital"] or None,
            "object": raw["Objeto"] or None,
            "reference_value_brl": parse_brl(raw["Referência validada / estado"], "reference_value_brl", issues),
            "source_value_brl": parse_brl(raw["Valor"], "source_value_brl", issues),
            "source_value_nature": raw["Natureza do valor"] or None,
            "protocol_deadline": parse_deadline(raw["Prazo protocolo"], "protocol_deadline", issues),
            "challenge_deadline": parse_deadline(raw["Prazo impugnação"], "challenge_deadline", issues),
            "official_observed_at": _observed_at(raw),
            "official_status": raw["Situação oficial conferida"] or None,
            "management_group": raw["Grupo gerencial"] or None,
            "official_source_url": raw["Fonte oficial URL"] or None,
            # These are intentionally distinct from reference value and contract value.
            "prepared_price_brl": parse_brl(raw["Preço preparado"], "prepared_price_brl", issues),
            "approved_price_brl": parse_brl(raw["Preço aprovado"], "approved_price_brl", issues),
            "submitted_price_brl": parse_brl(raw["Preço apresentado"], "submitted_price_brl", issues),
            "contract_value_brl": None,
        }
        records.append({"key": key, "source_row": number, "raw": raw, "fields": fields, "issues": issues})
    return records


def merge_records(incoming: Iterable[dict[str, Any]], previous: dict[str, Any] | None, source_failed: bool = False) -> dict[str, Any]:
    """Idempotently merge source records, retaining manual data and all prior keys."""
    previous = previous or {}
    old_records = {record["key"]: record for record in previous.get("records", []) if "key" in record}
    result = dict(old_records)
    if source_failed:
        for record in result.values():
            record = record.setdefault("issues", [])
            if not any(issue.get("code") == "source_stale" for issue in record):
                record.append({"code": "source_stale"})
        return {"records": list(result.values()), "source_failed": True}
    for candidate in incoming:
        key = candidate["key"]
        old = old_records.get(key)
        if old and _is_older(candidate, old):
            preserved = dict(old)
            preserved.setdefault("issues", []).append({"code": "source_older_than_current"})
            result[key] = preserved
            continue
        merged = dict(candidate)
        if old:
            for manual_key in MANUAL_KEYS:
                if manual_key in old:
                    merged[manual_key] = old[manual_key]
        result[key] = merged
    return {"records": [result[key] for key in sorted(result)], "source_failed": False}


def _is_older(candidate: dict[str, Any], old: dict[str, Any]) -> bool:
    new_time = candidate.get("fields", {}).get("official_observed_at")
    old_time = old.get("fields", {}).get("official_observed_at")
    return bool(new_time and old_time and new_time < old_time)


class FileLock:
    def __init__(self, path: Path, timeout: float = 10.0): self.path, self.timeout, self.fd = path, timeout, None
    def __enter__(self):
        until = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.monotonic() >= until: raise TimeoutError("state lock is busy")
                time.sleep(.1)
    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd)
        try: self.path.unlink()
        except FileNotFoundError: pass


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle: return json.load(handle)


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def import_snapshot(snapshot_path: Path, state_path: Path) -> dict[str, Any]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(state_path.with_suffix(state_path.suffix + ".lock")):
        prior = _read_json(state_path) if state_path.exists() else None
        records = records_from_snapshot(_read_json(snapshot_path))
        state = merge_records(records, prior)
        state.update({"mode": "snapshot", "provenance": "manual official-sheet consultation", "generated_at": datetime.now(SAO_PAULO).isoformat(), "source_snapshot": snapshot_path.name})
        write_state(state_path, state)
    LOG.info("snapshot_imported records=%d issues=%d", len(state["records"]), sum(len(r.get("issues", [])) for r in state["records"]))
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Create read-only Extra staging state from a saved official snapshot.")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    import_snapshot(args.snapshot, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
