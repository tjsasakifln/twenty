"""Reconcile the private routine catalog from Codex automation metadata."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from contract import CATALOG_SCHEMA_VERSION, validate_catalog_document

DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_POLICY = {"allowedDomainObjects": ["task"], "allowExtraSync": False}


def _automation_entry(document: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    policy = previous if previous is not None else DEFAULT_POLICY
    entry: dict[str, Any] = {
        "automationId": document.get("id"),
        "name": document.get("name"),
        "kind": document.get("kind"),
        "schedule": document.get("rrule"),
        "schedulerStatus": document.get("status"),
        "allowedDomainObjects": list(policy["allowedDomainObjects"]),
        "allowExtraSync": bool(policy.get("allowExtraSync")),
    }
    if document.get("model"):
        entry["model"] = document["model"]
    if document.get("reasoning_effort"):
        entry["reasoningEffort"] = document["reasoning_effort"]
    return entry


def reconcile_catalog(
    automations_dir: Path,
    catalog_path: Path,
    *,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    if catalog_path.exists():
        previous = validate_catalog_document(
            json.loads(catalog_path.read_text(encoding="utf-8"))
        )
        timezone = str(previous.get("timezone") or timezone)
    previous_by_id = {
        routine["automationId"]: routine for routine in previous.get("routines", [])
    }
    routines: list[dict[str, Any]] = []
    for automation_file in sorted(automations_dir.glob("*/automation.toml")):
        document = tomllib.loads(automation_file.read_text(encoding="utf-8"))
        automation_id = str(document.get("id") or "")
        routines.append(_automation_entry(document, previous_by_id.get(automation_id)))
    catalog = validate_catalog_document(
        {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "timezone": timezone,
            "routines": routines,
        }
    )
    write_private_catalog(catalog_path, catalog)
    return catalog


def write_private_catalog(path: Path, catalog: dict[str, Any]) -> None:
    validated = validate_catalog_document(
        {key: value for key, value in catalog.items() if key != "byId"}
    )
    document = {key: value for key, value in validated.items() if key != "byId"}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
