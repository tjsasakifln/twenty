"""Small, strict wire contract for scheduled Codex routine observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "confenge.codex-routine.v1"
CATALOG_SCHEMA_VERSION = "confenge.codex-routine-catalog.v1"
EVENT_TYPES = {"START", "OUTCOME", "CORRECTION"}
TRIGGERS = {"SCHEDULED", "INTERACTIVE", "RECOVERY"}
RUN_STATUSES = {
    "RUNNING",
    "SUCCEEDED",
    "NO_MATERIAL_CHANGE",
    "PARTIAL",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
}
TERMINAL_STATUSES = RUN_STATUSES - {"RUNNING"}
COVERAGE_STATUSES = {"COMPLETE", "PARTIAL", "UNAVAILABLE", "NOT_APPLICABLE"}
TASK_STATUSES = {"TODO", "IN_PROGRESS", "WAITING", "DONE", "CANCELLED"}
TASK_KINDS = {"TASK", "PENDING"}
SAFE_DOMAIN_OBJECTS = {
    "company",
    "opportunity",
    "proposta",
    "contrato",
    "trabalho",
    "entrega",
    "marcoFinanceiro",
    "recebimento",
    "licitacaoExtra",
    "interacao",
    "fonte",
    "restricaoComercial",
    "task",
}
FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "contact_email",
    "email",
    "message_body",
    "password",
    "phone",
    "raw_body",
    "secret",
    "token",
}
SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~-]{12,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)
EMAIL_PATTERN = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
METRIC_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
CATALOG_STATUS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{0,39}$")
CATALOG_TOP_LEVEL_FIELDS = {"schemaVersion", "timezone", "routines"}
CATALOG_ROUTINE_FIELDS = {
    "automationId",
    "name",
    "kind",
    "schedule",
    "schedulerStatus",
    "allowedDomainObjects",
    "allowExtraSync",
    "model",
    "reasoningEffort",
}
TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "automationId",
    "eventType",
    "scheduledFor",
    "observedAt",
    "trigger",
    "status",
    "coverageStatus",
    "domainRunId",
    "runKey",
    "eventKey",
    "payloadHash",
    "startedAt",
    "finishedAt",
    "materialChange",
    "summary",
    "errorCode",
    "correctionReason",
    "aggregateMetrics",
    "artifactRefs",
    "domainRefs",
    "operationalItems",
    "requestExtraSync",
}
DOMAIN_REF_FIELDS = {"object", "id", "externalKey", "url", "label", "hash"}
ARTIFACT_REF_FIELDS = {"id", "url", "label", "hash", "kind"}
OPERATIONAL_ITEM_FIELDS = {"externalKey", "title", "status", "kind", "summary", "dueAt", "refs"}
OPERATIONAL_REF_FIELDS = {"id", "url", "label", "hash", "kind", "object", "externalKey"}


class ContractError(ValueError):
    """The routine event is unsafe or inconsistent with the declared catalog."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_datetime(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"missing required field: {field}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid datetime: {field}") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"datetime must include an offset: {field}")
    return text


def validate_catalog_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ContractError("routine catalog must be an object")
    unknown_top = set(document) - CATALOG_TOP_LEVEL_FIELDS
    if unknown_top:
        raise ContractError(f"routine catalog contains unknown fields: {sorted(unknown_top)}")
    if document.get("schemaVersion") != CATALOG_SCHEMA_VERSION:
        raise ContractError("unsupported routine catalog schema")
    timezone_name = _bounded_text(document.get("timezone"), "catalog.timezone", 100)
    if not timezone_name:
        raise ContractError("routine catalog timezone is missing")
    routines = document.get("routines")
    if not isinstance(routines, list) or not routines:
        raise ContractError("routine catalog is empty")
    by_id: dict[str, dict[str, Any]] = {}
    normalized_routines: list[dict[str, Any]] = []
    for index, routine in enumerate(routines):
        if not isinstance(routine, dict):
            raise ContractError(f"catalog.routines[{index}] must be an object")
        unknown_fields = set(routine) - CATALOG_ROUTINE_FIELDS
        if unknown_fields:
            raise ContractError(
                f"catalog routine contains unknown fields: {sorted(unknown_fields)}"
            )
        automation_id = _bounded_text(
            routine.get("automationId"), f"catalog.routines[{index}].automationId", 160
        )
        if not automation_id or automation_id in by_id:
            raise ContractError("routine catalog has a missing or duplicate automationId")
        name = _bounded_text(routine.get("name"), f"catalog.routines[{index}].name", 240)
        kind = _bounded_text(routine.get("kind"), f"catalog.routines[{index}].kind", 40)
        schedule = _bounded_text(
            routine.get("schedule"), f"catalog.routines[{index}].schedule", 500
        )
        status = _bounded_text(
            routine.get("schedulerStatus"),
            f"catalog.routines[{index}].schedulerStatus",
            40,
        )
        if not name or not kind or not schedule or not status:
            raise ContractError("catalog routine is missing required scheduling metadata")
        if kind not in {"cron", "heartbeat"}:
            raise ContractError(f"unsupported catalog routine kind: {kind}")
        status = status.upper()
        if not CATALOG_STATUS_PATTERN.fullmatch(status):
            raise ContractError("invalid catalog routine status")
        allowed = routine.get("allowedDomainObjects")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ContractError("allowedDomainObjects must be a string array")
        allowed = list(dict.fromkeys(allowed))
        unknown = set(allowed) - SAFE_DOMAIN_OBJECTS
        if unknown:
            raise ContractError(f"catalog contains unsafe domain objects: {sorted(unknown)}")
        allow_extra_sync = routine.get("allowExtraSync")
        if not isinstance(allow_extra_sync, bool):
            raise ContractError("allowExtraSync must be a boolean")
        normalized = {
            "automationId": automation_id,
            "name": name,
            "kind": kind,
            "schedule": schedule,
            "schedulerStatus": status,
            "allowedDomainObjects": allowed,
            "allowExtraSync": allow_extra_sync,
        }
        for source, target in (("model", "model"), ("reasoningEffort", "reasoningEffort")):
            value = _bounded_text(
                routine.get(source), f"catalog.routines[{index}].{source}", 100
            )
            if value:
                normalized[target] = value
        normalized_routines.append(normalized)
        by_id[automation_id] = normalized
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "timezone": timezone_name,
        "routines": normalized_routines,
        "byId": by_id,
    }


def load_catalog(path: Path) -> dict[str, Any]:
    return validate_catalog_document(json.loads(path.read_text(encoding="utf-8")))


def stable_run_key(automation_id: str, scheduled_for: str, trigger: str, domain_run_id: str | None = None) -> str:
    if domain_run_id:
        suffix = str(domain_run_id).strip()
        if not suffix or len(suffix) > 160:
            raise ContractError("invalid domainRunId")
        return f"{automation_id}:{suffix}"
    seed = canonical_json([automation_id, scheduled_for, trigger])
    return f"{automation_id}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"


def _walk_safe(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in FORBIDDEN_KEYS:
                raise ContractError(f"forbidden sensitive field: {path}.{key}")
            _walk_safe(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if SECRET_PATTERN.search(value):
            raise ContractError(f"possible credential in {path}")
        if EMAIL_PATTERN.search(value):
            raise ContractError(f"possible personal email in {path}")


def _bounded_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    text = value.strip()
    if len(text) > maximum:
        raise ContractError(f"{field} exceeds {maximum} characters")
    return text or None


def _strict_ref(
    ref: Any,
    *,
    field: str,
    allowed_fields: set[str],
    identity_fields: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(ref, dict):
        raise ContractError(f"each {field} entry must be an object")
    unexpected = set(ref) - allowed_fields
    if unexpected:
        raise ContractError(f"{field} contains unsupported fields: {sorted(unexpected)}")
    normalized: dict[str, Any] = {}
    for key, value in ref.items():
        normalized_value = _bounded_text(value, f"{field}.{key}", 1000 if key == "url" else 240)
        if normalized_value is not None:
            normalized[key] = normalized_value
    if not any(normalized.get(key) for key in identity_fields):
        raise ContractError(f"{field} entries require {', '.join(identity_fields)}")
    return normalized


def validate_event(event: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict) or event.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("unsupported routine event schema")
    unexpected_top_level = set(event) - TOP_LEVEL_FIELDS
    if unexpected_top_level:
        raise ContractError(f"event contains unsupported fields: {sorted(unexpected_top_level)}")
    automation_id = str(event.get("automationId") or "").strip()
    routine = catalog["byId"].get(automation_id)
    if routine is None:
        raise ContractError(f"automation is not catalogued: {automation_id}")
    event_type = str(event.get("eventType") or "").upper()
    trigger = str(event.get("trigger") or "").upper()
    status = str(event.get("status") or "").upper()
    coverage = str(event.get("coverageStatus") or "NOT_APPLICABLE").upper()
    if event_type not in EVENT_TYPES:
        raise ContractError("invalid eventType")
    if trigger not in TRIGGERS:
        raise ContractError("invalid trigger")
    if status not in RUN_STATUSES:
        raise ContractError("invalid status")
    if coverage not in COVERAGE_STATUSES:
        raise ContractError("invalid coverageStatus")
    if event_type == "START" and status != "RUNNING":
        raise ContractError("START must report RUNNING")
    if event_type in {"OUTCOME", "CORRECTION"} and status not in TERMINAL_STATUSES:
        raise ContractError(f"{event_type} must report a terminal status")
    scheduled_for = parse_datetime(event.get("scheduledFor"), "scheduledFor")
    parse_datetime(event.get("observedAt"), "observedAt")
    if event.get("startedAt") is not None:
        parse_datetime(event["startedAt"], "startedAt")
    if event.get("finishedAt") is not None:
        parse_datetime(event["finishedAt"], "finishedAt")
    run_key = stable_run_key(automation_id, scheduled_for, trigger, event.get("domainRunId"))
    supplied_run_key = str(event.get("runKey") or run_key)
    if supplied_run_key != run_key:
        raise ContractError("runKey does not match the deterministic contract")

    domain_refs_input = event.get("domainRefs") or []
    domain_refs: list[dict[str, Any]] = []
    if not isinstance(domain_refs_input, list) or len(domain_refs_input) > 100:
        raise ContractError("domainRefs must be a list with at most 100 entries")
    allowed = set(routine.get("allowedDomainObjects") or [])
    for raw_ref in domain_refs_input:
        ref = _strict_ref(
            raw_ref,
            field="domainRefs",
            allowed_fields=DOMAIN_REF_FIELDS,
            identity_fields=("id", "externalKey", "url"),
        )
        object_name = str(ref.get("object") or "")
        if object_name not in allowed:
            raise ContractError(f"domain object is not allowed for {automation_id}: {object_name}")
        domain_refs.append(ref)

    artifact_refs_input = event.get("artifactRefs") or []
    artifact_refs: list[dict[str, Any]] = []
    if not isinstance(artifact_refs_input, list) or len(artifact_refs_input) > 100:
        raise ContractError("artifactRefs must be a list with at most 100 entries")
    for raw_ref in artifact_refs_input:
        artifact_refs.append(
            _strict_ref(
                raw_ref,
                field="artifactRefs",
                allowed_fields=ARTIFACT_REF_FIELDS,
                identity_fields=("id", "url", "hash"),
            )
        )

    operational_items_input = event.get("operationalItems") or []
    operational_items: list[dict[str, Any]] = []
    if not isinstance(operational_items_input, list) or len(operational_items_input) > 25:
        raise ContractError("operationalItems must be a list with at most 25 entries")
    seen_item_keys: set[str] = set()
    for raw_item in operational_items_input:
        if not isinstance(raw_item, dict):
            raise ContractError("each operational item must be an object")
        unexpected = set(raw_item) - OPERATIONAL_ITEM_FIELDS
        if unexpected:
            raise ContractError(f"operationalItems contains unsupported fields: {sorted(unexpected)}")
        item_key = str(raw_item.get("externalKey") or "").strip()
        if not item_key or item_key in seen_item_keys or len(item_key) > 160:
            raise ContractError("operationalItems require unique bounded externalKey values")
        seen_item_keys.add(item_key)
        item_status = str(raw_item.get("status") or "").upper()
        item_kind = str(raw_item.get("kind") or "").upper()
        if item_status not in TASK_STATUSES:
            raise ContractError("invalid operational item status")
        if item_kind not in TASK_KINDS:
            raise ContractError("invalid operational item kind")
        title = _bounded_text(raw_item.get("title"), "operationalItems.title", 240)
        if title is None:
            raise ContractError("operationalItems.title is required")
        summary = _bounded_text(raw_item.get("summary"), "operationalItems.summary", 2000)
        due_at = None
        if raw_item.get("dueAt") is not None:
            due_at = parse_datetime(raw_item["dueAt"], "operationalItems.dueAt")
        refs_input = raw_item.get("refs") or []
        refs: list[dict[str, Any]] = []
        if not isinstance(refs_input, list) or len(refs_input) > 25:
            raise ContractError("operationalItems.refs must contain at most 25 entries")
        for raw_ref in refs_input:
            refs.append(
                _strict_ref(
                    raw_ref,
                    field="operationalItems.refs",
                    allowed_fields=OPERATIONAL_REF_FIELDS,
                    identity_fields=("id", "url", "hash", "externalKey"),
                )
            )
        normalized_item: dict[str, Any] = {
            "externalKey": item_key,
            "title": title,
            "status": item_status,
            "kind": item_kind,
            "refs": refs,
        }
        if summary is not None:
            normalized_item["summary"] = summary
        if due_at is not None:
            normalized_item["dueAt"] = due_at
        operational_items.append(normalized_item)

    metrics = event.get("aggregateMetrics") or {}
    if not isinstance(metrics, dict) or len(metrics) > 50:
        raise ContractError("aggregateMetrics must be an object with at most 50 values")
    for key, value in metrics.items():
        if not isinstance(key, str) or not METRIC_KEY_PATTERN.fullmatch(key):
            raise ContractError("aggregateMetrics contains an invalid key")
        if not isinstance(value, (int, float, bool, type(None))) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ContractError("aggregateMetrics accepts numeric, boolean, or null values only")

    if "materialChange" in event and not isinstance(event["materialChange"], bool):
        raise ContractError("materialChange must be a boolean")
    if "requestExtraSync" in event and not isinstance(event["requestExtraSync"], bool):
        raise ContractError("requestExtraSync must be a boolean")
    request_extra_sync = event.get("requestExtraSync", False)
    if request_extra_sync and not routine.get("allowExtraSync"):
        raise ContractError(f"Extra sync is not allowed for {automation_id}")
    summary = _bounded_text(event.get("summary"), "summary", 2000)
    error_code = _bounded_text(event.get("errorCode"), "errorCode", 120)
    correction_reason = _bounded_text(event.get("correctionReason"), "correctionReason", 500)
    if event_type == "CORRECTION" and not correction_reason:
        raise ContractError("CORRECTION requires correctionReason")
    _walk_safe(event)
    normalized = {
        **event,
        "automationId": automation_id,
        "eventType": event_type,
        "trigger": trigger,
        "status": status,
        "coverageStatus": coverage,
        "scheduledFor": scheduled_for,
        "runKey": run_key,
        "materialChange": bool(event.get("materialChange", False)),
        "requestExtraSync": request_extra_sync,
        "domainRefs": domain_refs,
        "artifactRefs": artifact_refs,
        "operationalItems": operational_items,
        "aggregateMetrics": metrics,
        "summary": summary,
        "errorCode": error_code,
        "correctionReason": correction_reason,
    }
    normalized = {key: value for key, value in normalized.items() if value is not None}
    computed_event_key = f"{run_key}:{event_type}"
    if event.get("eventKey") not in (None, computed_event_key):
        raise ContractError("eventKey does not match the deterministic contract")
    normalized["eventKey"] = computed_event_key
    computed_payload_hash = payload_hash({k: v for k, v in normalized.items() if k != "payloadHash"})
    if event.get("payloadHash") not in (None, computed_payload_hash):
        raise ContractError("payloadHash does not match the canonical event")
    normalized["payloadHash"] = computed_payload_hash
    return normalized


def build_event(
    *,
    automation_id: str,
    event_type: str,
    scheduled_for: str,
    observed_at: str,
    trigger: str,
    status: str,
    catalog: dict[str, Any],
    **optional: Any,
) -> dict[str, Any]:
    return validate_event(
        {
            "schemaVersion": SCHEMA_VERSION,
            "automationId": automation_id,
            "eventType": event_type,
            "scheduledFor": scheduled_for,
            "observedAt": observed_at,
            "trigger": trigger,
            "status": status,
            **optional,
        },
        catalog,
    )
