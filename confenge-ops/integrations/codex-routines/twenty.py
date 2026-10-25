"""Twenty metadata/catalog planner and idempotent routine event sink."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from contract import ContractError, TERMINAL_STATUSES, canonical_json, payload_hash

MARKER = "managed-by=confenge-codex-routines/v1"
ROUTINE_OBJECT = {
    "nameSingular": "codexRoutine",
    "namePlural": "codexRoutines",
    "labelSingular": "Rotina Codex",
    "labelPlural": "Rotinas Codex",
    "icon": "IconCalendarRepeat",
    "description": MARKER,
    "isSearchable": True,
}
RUN_OBJECT = {
    "nameSingular": "codexRoutineRun",
    "namePlural": "codexRoutineRuns",
    "labelSingular": "Execução de rotina",
    "labelPlural": "Execuções de rotina",
    "icon": "IconHistory",
    "description": MARKER,
    "isSearchable": False,
}
ROUTINE_FIELDS = (
    ("automationId", "ID da automação", "TEXT", True),
    ("kind", "Tipo do agendamento", "TEXT", False),
    ("schedule", "Agenda", "TEXT", False),
    ("scheduleDescription", "Frequência", "TEXT", False),
    ("schedulerStatus", "Estado do agendamento", "TEXT", False),
    ("timezone", "Fuso", "TEXT", False),
    ("allowedDomainObjects", "Destinos permitidos", "RAW_JSON", False),
    ("allowExtraSync", "Pode sincronizar Extra", "BOOLEAN", False),
    ("configHash", "Hash da configuração", "TEXT", False),
    ("nextExpectedAt", "Próxima execução esperada", "DATE_TIME", False),
    ("lastRunKey", "Chave da última execução", "TEXT", False),
    ("lastScheduledFor", "Agenda da última execução", "DATE_TIME", False),
    ("lastObservedAt", "Última observação recebida", "DATE_TIME", False),
    ("lastRunStatus", "Estado da última execução", "TEXT", False),
    ("lastCoverageStatus", "Cobertura da última execução", "TEXT", False),
    ("lastStartedAt", "Último início", "DATE_TIME", False),
    ("lastFinishedAt", "Último término", "DATE_TIME", False),
    ("lastSucceededAt", "Último sucesso", "DATE_TIME", False),
    ("lastMaterialChange", "Última execução teve mudança", "BOOLEAN", False),
    ("lastDomainRecordCount", "Referências na última execução", "NUMBER", False),
    ("lastErrorCode", "Último código de erro", "TEXT", False),
    ("lastSummary", "Resumo da última execução", "TEXT", False),
    ("lastSyncedAt", "Última sincronização", "DATE_TIME", False),
)
RUN_FIELDS = (
    ("runKey", "Chave da execução", "TEXT", True),
    ("automationId", "ID da automação", "TEXT", False),
    ("eventType", "Tipo do evento", "TEXT", False),
    ("scheduledFor", "Prevista para", "DATE_TIME", False),
    ("trigger", "Origem do disparo", "TEXT", False),
    ("outcomeStatus", "Estado apurado", "TEXT", False),
    ("coverageStatus", "Cobertura", "TEXT", False),
    ("startedAt", "Início real", "DATE_TIME", False),
    ("finishedAt", "Término real", "DATE_TIME", False),
    ("materialChange", "Mudança material", "BOOLEAN", False),
    ("summary", "Resumo seguro", "TEXT", False),
    ("errorCode", "Código de erro", "TEXT", False),
    ("aggregateMetrics", "Métricas agregadas", "RAW_JSON", False),
    ("artifactRefs", "Referências de artefatos", "RAW_JSON", False),
    ("domainRecordRefs", "Referências de domínio", "RAW_JSON", False),
    ("payloadHash", "Hash do evento", "TEXT", False),
    ("lastObservedAt", "Última observação", "DATE_TIME", False),
    ("correctionReason", "Motivo da correção", "TEXT", False),
)
TASK_FIELDS = (
    ("codexRoutineItemKey", "Chave da rotina Codex", "TEXT", True),
    ("codexRoutineAutomationId", "Automação Codex", "TEXT", False),
    ("codexRoutineRunKey", "Execução Codex", "TEXT", False),
    ("codexRoutineItemStatus", "Estado da pendência", "TEXT", False),
    ("codexRoutineItemKind", "Tipo do item", "TEXT", False),
    ("codexRoutineRefs", "Referências da rotina", "RAW_JSON", False),
)
ROUTINE_TABLE_ORDER = (
    "name",
    "schedulerStatus",
    "scheduleDescription",
    "lastSummary",
    "lastCoverageStatus",
    "lastFinishedAt",
)
RUN_TABLE_ORDER = (
    "name",
    "outcomeStatus",
    "summary",
    "coverageStatus",
    "finishedAt",
)
ROUTINE_MAIN_FIELDS = set(ROUTINE_TABLE_ORDER) - {"name"}
RUN_MAIN_FIELDS = set(RUN_TABLE_ORDER) - {"name"}


class ApiError(RuntimeError):
    pass


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value).strip())
    except ValueError as exc:
        raise ApiError("invalid Twenty base URL") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ApiError("Twenty base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ApiError("Twenty base URL must not contain a path")
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ApiError("invalid Twenty base URL port") from exc
    if parsed.scheme == "https" and host == "gestao.confenge.com.br" and port in {None, 443}:
        return "https://gestao.confenge.com.br"
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        port_text = f":{port}" if port is not None else ""
        rendered_host = f"[{host}]" if ":" in host else host
        return f"http://{rendered_host}{port_text}"
    raise ApiError("Twenty base URL must be canonical HTTPS or explicit HTTP loopback")


def schedule_description(schedule: str, timezone_name: str) -> str:
    fallback = "Agendamento personalizado no Codex"
    text = str(schedule or "").strip()
    if text.startswith("RRULE:"):
        text = text[6:]
    parts: dict[str, str] = {}
    for item in text.split(";"):
        if "=" not in item:
            return fallback
        key, value = item.split("=", 1)
        if not key or not value or key in parts:
            return fallback
        parts[key] = value
    allowed = {"FREQ", "INTERVAL", "COUNT", "BYDAY", "BYHOUR", "BYMINUTE", "BYSECOND"}
    if not parts or set(parts) - allowed:
        return fallback
    frequency = parts.get("FREQ")
    if frequency == "MINUTELY":
        if set(parts) - {"FREQ", "INTERVAL"}:
            return fallback
        interval = parts.get("INTERVAL")
        if not interval or not interval.isdigit() or int(interval) < 1:
            return fallback
        return f"A cada {int(interval)} minutos"
    if frequency not in {"DAILY", "WEEKLY"}:
        return fallback
    compatible = (
        {"FREQ", "COUNT", "BYHOUR", "BYMINUTE", "BYSECOND"}
        if frequency == "DAILY"
        else {"FREQ", "COUNT", "BYDAY", "BYHOUR", "BYMINUTE", "BYSECOND"}
    )
    if set(parts) - compatible:
        return fallback
    second = parts.get("BYSECOND", "0")
    if not second.isdigit() or int(second) != 0:
        return fallback
    hour = parts.get("BYHOUR")
    minute = parts.get("BYMINUTE", "0")
    if not hour or not hour.isdigit() or not minute.isdigit():
        return fallback
    hour_number, minute_number = int(hour), int(minute)
    if not 0 <= hour_number <= 23 or not 0 <= minute_number <= 59:
        return fallback
    time_text = f"{hour_number:02d}:{minute_number:02d}"
    if frequency == "DAILY":
        prefix = "Todos os dias"
    else:
        day_names = {
            "MO": "segundas",
            "TU": "terças",
            "WE": "quartas",
            "TH": "quintas",
            "FR": "sextas",
            "SA": "sábados",
            "SU": "domingos",
        }
        codes = parts.get("BYDAY", "").split(",")
        if not codes or any(code not in day_names for code in codes):
            return fallback
        names = [day_names[code] for code in codes]
        prefix = names[0].capitalize() if len(names) == 1 else ", ".join(names[:-1]).capitalize() + " e " + names[-1]
    description = f"{prefix} às {time_text} ({timezone_name})"
    count = parts.get("COUNT")
    if count:
        if not count.isdigit() or int(count) < 1:
            return fallback
        description += f", {int(count)} ocorrências"
    return description


def unwrap_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("data"), list):
            return value["data"]
        for item in value.values():
            if isinstance(item, list):
                return item
        data = value.get("data")
        if isinstance(data, dict):
            for item in data.values():
                if isinstance(item, list):
                    return item
    raise ApiError("unexpected list response")


def unwrap_one(value: Any) -> dict[str, Any]:
    current = value
    while isinstance(current, dict) and isinstance(current.get("data"), dict) and len(current["data"]) == 1:
        current = next(iter(current["data"].values()))
    if not isinstance(current, dict):
        raise ApiError("unexpected record response")
    return current


class TwentyClient:
    def __init__(self, base_url: str, token: str, *, request_interval: float = 0.75) -> None:
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.request_interval = request_interval
        self.last_request_at: float | None = None

    def request(self, path: str, payload: Any = None, method: str = "GET") -> Any:
        method = method.upper()
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        attempts = 5 if method == "GET" else 1
        for attempt in range(attempts):
            if self.last_request_at is not None:
                remaining = self.request_interval - (time.monotonic() - self.last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            self.last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                message = exc.read().decode("utf-8", "replace")[:1000]
                if method != "GET" or exc.code not in {429, 502, 503, 504} or attempt + 1 == attempts:
                    raise ApiError(f"HTTP {exc.code}: {message}") from exc
            except urllib.error.URLError as exc:
                if attempt + 1 == attempts:
                    raise ApiError(f"connection failed: {exc.reason}") from exc
            time.sleep(min(2**attempt, 16))
        raise ApiError("request failed")

    def list_rest(self, path: str, *, limit: int = 500) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            separator = "&" if "?" in path else "?"
            url = f"{path}{separator}limit={limit}"
            if cursor:
                url += "&starting_after=" + urllib.parse.quote(cursor)
            response = self.request(url)
            records.extend(unwrap_list(response))
            page_info = response.get("pageInfo") if isinstance(response, dict) else None
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                return records
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise ApiError("invalid REST pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def current_workspace_id(self) -> str:
        response = self.request(
            "/metadata",
            {
                "query": "query CodexRoutinePreflight { currentWorkspace { id } }",
                "variables": {},
            },
            "POST",
        )
        if not isinstance(response, dict) or response.get("errors"):
            raise ApiError("could not verify the current Twenty workspace")
        data = response.get("data")
        workspace = data.get("currentWorkspace") if isinstance(data, dict) else None
        workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ApiError("currentWorkspace.id is unavailable")
        return workspace_id

    def preflight_workspace(self, expected_workspace_id: str) -> dict[str, str]:
        expected = str(expected_workspace_id or "").strip()
        if not expected:
            raise ApiError("credential workspace id is missing")
        actual = self.current_workspace_id()
        if actual != expected:
            raise ApiError("Twenty token workspace does not match the credential workspace")
        return {"workspaceId": actual}

    def relation_field(self, field_id: str) -> dict[str, Any]:
        response = self.request(
            "/metadata",
            {
                "query": """
                    query CodexRoutineRelation($id: UUID!) {
                      field(id: $id) {
                        id name type settings
                        relation {
                          type
                          sourceObjectMetadata { id nameSingular }
                          targetObjectMetadata { id nameSingular }
                          sourceFieldMetadata { id name }
                          targetFieldMetadata { id name }
                        }
                      }
                    }
                """,
                "variables": {"id": field_id},
            },
            "POST",
        )
        if not isinstance(response, dict) or response.get("errors"):
            raise ApiError("could not hydrate relation metadata")
        data = response.get("data")
        field = data.get("field") if isinstance(data, dict) else None
        if not isinstance(field, dict):
            raise ApiError("relation field metadata is unavailable")
        return field


def _field_payload(object_id: str, spec: tuple[str, str, str, bool]) -> dict[str, Any]:
    name, label, field_type, unique = spec
    payload: dict[str, Any] = {
        "objectMetadataId": object_id,
        "type": field_type,
        "name": name,
        "label": label,
        "isNullable": True,
        "isUnique": unique,
        "isActive": True,
        "description": MARKER,
    }
    if field_type == "BOOLEAN":
        payload["defaultValue"] = False
    if unique:
        payload["isUIEditable"] = False
    return payload


@dataclass
class SchemaPlan:
    objects_to_create: list[dict[str, Any]]
    fields_to_create: list[tuple[str, tuple[str, str, str, bool]]]
    relation_to_create: bool

    def summary(self) -> dict[str, Any]:
        return {
            "objectsToCreate": [item["nameSingular"] for item in self.objects_to_create],
            "fieldsToCreate": [f"{object_name}.{spec[0]}" for object_name, spec in self.fields_to_create],
            "relationToCreate": self.relation_to_create,
            "viewVisibilityPolicy": {
                "taskTechnicalFields": "hidden_in_all_views_and_widgets",
                "codexRoutineMainFields": sorted(ROUTINE_MAIN_FIELDS),
                "codexRoutineRunMainFields": sorted(RUN_MAIN_FIELDS),
                "technicalKeysHashesAndJson": "hidden_in_main_table",
            },
        }


class RoutineGateway:
    def __init__(self, client: TwentyClient, catalog: dict[str, Any]) -> None:
        self.client = client
        self.catalog = catalog

    def schema_plan(self) -> SchemaPlan:
        objects = self.client.list_rest("/rest/metadata/objects")
        by_name = {item.get("nameSingular"): item for item in objects}
        objects_to_create = []
        for desired in (ROUTINE_OBJECT, RUN_OBJECT):
            current = by_name.get(desired["nameSingular"])
            if current is None:
                objects_to_create.append(desired)
            elif current.get("description") != MARKER:
                raise ApiError(f"refusing unowned object collision: {desired['nameSingular']}")
        fields_to_create: list[tuple[str, tuple[str, str, str, bool]]] = []
        for object_name, specs in (
            ("codexRoutine", ROUTINE_FIELDS),
            ("codexRoutineRun", RUN_FIELDS),
            ("task", TASK_FIELDS),
        ):
            current = by_name.get(object_name)
            if current is None:
                if object_name == "task":
                    raise ApiError("native Task metadata was not found")
                fields_to_create.extend((object_name, spec) for spec in specs)
                continue
            current_fields = {field.get("name"): field for field in current.get("fields") or []}
            for spec in specs:
                field = current_fields.get(spec[0])
                if field is None:
                    fields_to_create.append((object_name, spec))
                elif field.get("description") != MARKER or field.get("type") != spec[2] or bool(field.get("isUnique")) != spec[3]:
                    raise ApiError(f"refusing incompatible field collision: {object_name}.{spec[0]}")
        run = by_name.get("codexRoutineRun")
        routine = by_name.get("codexRoutine")
        relation_to_create = run is None or routine is None
        if run is not None and routine is not None:
            expected_description = self._relation_description(routine["id"])
            relation = next(
                (field for field in run.get("fields") or [] if field.get("name") == "codexRoutine"),
                None,
            )
            if relation is None:
                relation_to_create = True
            else:
                if relation.get("type") != "RELATION" or relation.get("description") != expected_description:
                    raise ApiError("relation ownership mismatch: codexRoutineRun.codexRoutine")
                source_graph = self.client.relation_field(str(relation["id"]))
                inverse_field_id = self._validate_relation_graph(
                    source_graph,
                    expected_type="MANY_TO_ONE",
                    expected_source_object_id=run["id"],
                    expected_target_object_id=routine["id"],
                    expected_source_field_id=relation["id"],
                    expected_target_field_id=None,
                    field_name="codexRoutineRun.codexRoutine",
                )
                inverse_graph = self.client.relation_field(inverse_field_id)
                self._validate_relation_graph(
                    inverse_graph,
                    expected_type="ONE_TO_MANY",
                    expected_source_object_id=routine["id"],
                    expected_target_object_id=run["id"],
                    expected_source_field_id=inverse_field_id,
                    expected_target_field_id=relation["id"],
                    field_name="codexRoutine inverse",
                )
                relation_to_create = False
        return SchemaPlan(objects_to_create, fields_to_create, relation_to_create)

    @staticmethod
    def _relation_description(target_id: str) -> str:
        return f"{MARKER};target={target_id};type=MANY_TO_ONE;inverse=codexRoutineRuns"

    @staticmethod
    def _validate_relation_graph(
        field: dict[str, Any],
        *,
        expected_type: str,
        expected_source_object_id: str,
        expected_target_object_id: str,
        expected_source_field_id: str,
        expected_target_field_id: str | None,
        field_name: str,
    ) -> str:
        settings = field.get("settings") if isinstance(field.get("settings"), dict) else {}
        relation = field.get("relation") if isinstance(field.get("relation"), dict) else {}
        source_object = relation.get("sourceObjectMetadata")
        target_object = relation.get("targetObjectMetadata")
        source_field = relation.get("sourceFieldMetadata")
        target_field = relation.get("targetFieldMetadata")
        if (
            field.get("type") != "RELATION"
            or settings.get("relationType") != expected_type
            or relation.get("type") != expected_type
        ):
            raise ApiError(f"incompatible relation field: {field_name}")
        if not isinstance(source_object, dict) or source_object.get("id") != expected_source_object_id:
            raise ApiError(f"relation source object mismatch: {field_name}")
        if not isinstance(target_object, dict) or target_object.get("id") != expected_target_object_id:
            raise ApiError(f"relation target object mismatch: {field_name}")
        if not isinstance(source_field, dict) or source_field.get("id") != expected_source_field_id:
            raise ApiError(f"relation source field mismatch: {field_name}")
        if not isinstance(target_field, dict) or not target_field.get("id"):
            raise ApiError(f"relation target field is missing: {field_name}")
        if expected_target_field_id is not None and target_field.get("id") != expected_target_field_id:
            raise ApiError(f"relation cross-pair mismatch: {field_name}")
        return str(target_field["id"])

    def apply_schema(self) -> dict[str, Any]:
        plan = self.schema_plan()
        for payload in plan.objects_to_create:
            self.client.request("/rest/metadata/objects", payload, "POST")
        objects = self.client.list_rest("/rest/metadata/objects")
        by_name = {item.get("nameSingular"): item for item in objects}
        for object_name, spec in plan.fields_to_create:
            current = by_name.get(object_name)
            if current is None:
                objects = self.client.list_rest("/rest/metadata/objects")
                by_name = {item.get("nameSingular"): item for item in objects}
                current = by_name[object_name]
            self.client.request("/rest/metadata/fields", _field_payload(current["id"], spec), "POST")
        if plan.relation_to_create:
            objects = self.client.list_rest("/rest/metadata/objects")
            by_name = {item.get("nameSingular"): item for item in objects}
            run = by_name["codexRoutineRun"]
            routine = by_name["codexRoutine"]
            self.client.request(
                "/rest/metadata/fields",
                {
                    "objectMetadataId": run["id"],
                    "type": "RELATION",
                    "name": "codexRoutine",
                    "label": "Rotina Codex",
                    "isNullable": True,
                    "isActive": True,
                    "description": self._relation_description(routine["id"]),
                    "relationCreationPayload": {
                        "type": "MANY_TO_ONE",
                        "targetObjectMetadataId": routine["id"],
                        "targetFieldLabel": "Execuções",
                        "targetFieldIcon": "IconHistory",
                    },
                },
                "POST",
            )
        summary = plan.summary()
        summary["viewVisibilityApplied"] = self.apply_view_visibility()
        readback = self.schema_plan()
        if (
            readback.objects_to_create
            or readback.fields_to_create
            or readback.relation_to_create
        ):
            raise ApiError("schema readback did not converge")
        summary["readbackConverged"] = True
        return summary

    def apply_view_visibility(self) -> dict[str, int]:
        objects = self.client.list_rest("/rest/metadata/objects")
        by_name = {item.get("nameSingular"): item for item in objects}
        required = {"task", "codexRoutine", "codexRoutineRun"}
        if not required.issubset(by_name):
            raise ApiError("routine objects are missing before view visibility apply")
        views = self.client.list_rest("/rest/metadata/views")
        view_fields = self.client.list_rest("/rest/metadata/viewFields")
        fields_by_id: dict[str, tuple[str, str]] = {}
        fields_by_object_and_name: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for object_name in required:
            fields_by_object_and_name[object_name] = {}
            for field in by_name[object_name].get("fields") or []:
                field_id = field.get("id")
                field_name = field.get("name")
                if field_id and field_name:
                    fields_by_id[str(field_id)] = (object_name, str(field_name))
                    fields_by_object_and_name[object_name].setdefault(
                        str(field_name), []
                    ).append(field)
        table_orders = {
            "codexRoutine": ROUTINE_TABLE_ORDER,
            "codexRoutineRun": RUN_TABLE_ORDER,
        }
        target_table_ids: dict[str, str] = {}
        for object_name, order in table_orders.items():
            table_views = [
                view
                for view in views
                if view.get("objectMetadataId") == by_name[object_name]["id"]
                and view.get("type") == "TABLE"
                and view.get("key") == "INDEX"
            ]
            if len(table_views) != 1:
                raise ApiError(
                    f"expected exactly one owned INDEX TABLE view for {object_name}; found {len(table_views)}"
                )
            target_table_ids[object_name] = str(table_views[0]["id"])
            table_fields = [
                item
                for item in view_fields
                if item.get("viewId") == table_views[0]["id"]
            ]
            counts_by_metadata_id: dict[str, int] = {}
            for item in table_fields:
                metadata_id = str(item.get("fieldMetadataId") or "")
                counts_by_metadata_id[metadata_id] = counts_by_metadata_id.get(metadata_id, 0) + 1
            for field_name in order:
                metadata = fields_by_object_and_name[object_name].get(field_name) or []
                if len(metadata) != 1:
                    raise ApiError(
                        f"expected exactly one metadata field: {object_name}.{field_name}"
                    )
                count = counts_by_metadata_id.get(str(metadata[0]["id"]), 0)
                if count != 1:
                    raise ApiError(
                        f"expected exactly one TABLE viewField: {object_name}.{field_name}; found {count}"
                    )
        task_technical = {spec[0] for spec in TASK_FIELDS}
        task_technical_ids = {
            str(field["id"])
            for field_name in task_technical
            for field in fields_by_object_and_name["task"].get(field_name, [])
        }
        task_pairs: dict[tuple[str, str], int] = {}
        for item in view_fields:
            metadata_id = str(item.get("fieldMetadataId") or "")
            if metadata_id in task_technical_ids:
                pair = (str(item.get("viewId") or ""), metadata_id)
                task_pairs[pair] = task_pairs.get(pair, 0) + 1
        if any(count != 1 for count in task_pairs.values()):
            raise ApiError("duplicate Task technical viewField")
        counts = {"hidden": 0, "shown": 0, "reordered": 0, "renamed": 0, "preserved": 0}
        for object_name in ("codexRoutine", "codexRoutineRun"):
            name_field = fields_by_object_and_name[object_name]["name"][0]
            if name_field.get("label") != "Nome":
                self.client.request(
                    f"/rest/metadata/fields/{name_field['id']}",
                    {"label": "Nome"},
                    "PATCH",
                )
                counts["renamed"] += 1
        for view_field in view_fields:
            field_identity = fields_by_id.get(str(view_field.get("fieldMetadataId") or ""))
            if field_identity is None:
                continue
            object_name, field_name = field_identity
            desired: bool | None = None
            desired_position: int | None = None
            if object_name == "task" and field_name in task_technical:
                desired = False
            elif (
                object_name in table_orders
                and str(view_field.get("viewId")) == target_table_ids[object_name]
            ):
                order = table_orders[object_name]
                if order:
                    desired = field_name in order
                    if desired:
                        desired_position = order.index(field_name)
            update: dict[str, Any] = {}
            if desired is not None and bool(view_field.get("isVisible")) != desired:
                update["isVisible"] = desired
            if desired_position is not None and view_field.get("position") != desired_position:
                update["position"] = desired_position
            if not update:
                counts["preserved"] += 1
                continue
            self.client.request(
                f"/rest/metadata/viewFields/{view_field['id']}",
                update,
                "PATCH",
            )
            if "isVisible" in update:
                counts["shown" if desired else "hidden"] += 1
            if "position" in update:
                counts["reordered"] += 1
        return counts

    def _by_unique(self, plural: str, field: str, value: str) -> dict[str, Any] | None:
        matches = [item for item in self.client.list_rest(f"/rest/{plural}") if item.get(field) == value]
        if len(matches) > 1:
            raise ApiError(f"duplicate unique value: {plural}.{field}={value}")
        return matches[0] if matches else None

    def sync_catalog(self) -> dict[str, int]:
        counts = {"created": 0, "updated": 0, "preserved": 0, "notFound": 0}
        now = datetime.now(timezone.utc).isoformat()
        catalog_ids = set(self.catalog["byId"])
        for routine in self.catalog["routines"]:
            config = {key: routine.get(key) for key in sorted(routine)}
            payload = {
                "name": routine["name"],
                "automationId": routine["automationId"],
                "kind": routine["kind"],
                "schedule": routine["schedule"],
                "scheduleDescription": schedule_description(
                    routine["schedule"], self.catalog["timezone"]
                ),
                "schedulerStatus": routine["schedulerStatus"],
                "timezone": self.catalog["timezone"],
                "allowedDomainObjects": routine.get("allowedDomainObjects") or [],
                "allowExtraSync": bool(routine.get("allowExtraSync")),
                "configHash": payload_hash(config),
            }
            current = self._by_unique("codexRoutines", "automationId", routine["automationId"])
            if current is None:
                self.client.request(
                    "/rest/codexRoutines", {**payload, "lastSyncedAt": now}, "POST"
                )
                counts["created"] += 1
                continue
            update = {
                key: value
                for key, value in payload.items()
                if key != "automationId" and canonical_json(current.get(key)) != canonical_json(value)
            }
            if update:
                update["lastSyncedAt"] = now
                self.client.request(f"/rest/codexRoutines/{current['id']}", update, "PATCH")
                counts["updated"] += 1
            else:
                counts["preserved"] += 1
        for current in self.client.list_rest("/rest/codexRoutines"):
            automation_id = current.get("automationId")
            if not automation_id or automation_id in catalog_ids:
                continue
            if current.get("schedulerStatus") == "NOT_FOUND":
                continue
            self.client.request(
                f"/rest/codexRoutines/{current['id']}",
                {"schedulerStatus": "NOT_FOUND", "lastSyncedAt": now},
                "PATCH",
            )
            counts["notFound"] += 1
        return counts

    @staticmethod
    def _body(item_key: str, summary: str | None) -> dict[str, str] | None:
        if not summary:
            return None
        block = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-routine-item:{item_key}")),
            "type": "paragraph",
            "props": {"textColor": "default", "backgroundColor": "default", "textAlignment": "left"},
            "content": [{"type": "text", "text": summary, "styles": {}}],
            "children": [],
        }
        return {"blocknote": json.dumps([block], ensure_ascii=False), "markdown": summary}

    def _upsert_operational_items(self, event: dict[str, Any]) -> dict[str, int]:
        result = {"created": 0, "updated": 0, "preserved": 0}
        for item in event.get("operationalItems") or []:
            key = f"{event['automationId']}:{item['externalKey']}"
            payload = {
                "title": item["title"],
                "bodyV2": self._body(key, item.get("summary")),
                "dueAt": item.get("dueAt"),
                "codexRoutineItemKey": key,
                "codexRoutineAutomationId": event["automationId"],
                "codexRoutineRunKey": event["runKey"],
                "codexRoutineItemStatus": item["status"],
                "codexRoutineItemKind": item["kind"],
                "codexRoutineRefs": item.get("refs") or [],
            }
            current = self._by_unique("tasks", "codexRoutineItemKey", key)
            if current is None:
                self.client.request("/rest/tasks", payload, "POST")
                result["created"] += 1
                continue
            owned_update_fields = {
                "codexRoutineAutomationId",
                "codexRoutineRunKey",
                "codexRoutineItemStatus",
                "codexRoutineItemKind",
                "codexRoutineRefs",
            }
            update = {
                field: value
                for field, value in payload.items()
                if field in owned_update_fields
                and canonical_json(current.get(field)) != canonical_json(value)
            }
            if update:
                self.client.request(f"/rest/tasks/{current['id']}", update, "PATCH")
                result["updated"] += 1
            else:
                result["preserved"] += 1
        return result

    def apply_event(self, event: dict[str, Any]) -> dict[str, Any]:
        routine = self._by_unique("codexRoutines", "automationId", event["automationId"])
        if routine is None:
            raise ApiError("routine catalog has not been synchronized")
        current = self._by_unique("codexRoutineRuns", "runKey", event["runKey"])
        duplicate = False
        superseded_start = False
        if current is not None and current.get("outcomeStatus") in TERMINAL_STATUSES:
            if current.get("payloadHash") == event["payloadHash"]:
                duplicate = True
            elif event["eventType"] == "START":
                duplicate = True
                superseded_start = True
            elif event["eventType"] != "CORRECTION":
                raise ContractError("terminal run replay differs and requires CORRECTION")
        if current is not None and current.get("outcomeStatus") == "RUNNING" and event["status"] == "RUNNING" and current.get("payloadHash") != event["payloadHash"]:
            raise ContractError("conflicting START replay")
        run_payload = {
            "name": f"{event['automationId']} — {event['scheduledFor']}",
            "runKey": event["runKey"],
            "automationId": event["automationId"],
            "eventType": event["eventType"],
            "scheduledFor": event["scheduledFor"],
            "trigger": event["trigger"],
            "outcomeStatus": event["status"],
            "coverageStatus": event["coverageStatus"],
            "startedAt": event.get("startedAt") or (event["observedAt"] if event["eventType"] == "START" else None),
            "finishedAt": event.get("finishedAt") or (event["observedAt"] if event["status"] in TERMINAL_STATUSES else None),
            "materialChange": event["materialChange"],
            "summary": event.get("summary"),
            "errorCode": event.get("errorCode"),
            "aggregateMetrics": event["aggregateMetrics"],
            "artifactRefs": event.get("artifactRefs") or [],
            "domainRecordRefs": event["domainRefs"],
            "payloadHash": event["payloadHash"],
            "lastObservedAt": event["observedAt"],
            "correctionReason": event.get("correctionReason"),
            "codexRoutineId": routine["id"],
        }
        created = current is None
        if created:
            current = unwrap_one(self.client.request("/rest/codexRoutineRuns", run_payload, "POST"))
        elif not duplicate:
            update = {
                key: value
                for key, value in run_payload.items()
                if key != "runKey" and canonical_json(current.get(key)) != canonical_json(value)
            }
            if update:
                current = {**current, **unwrap_one(self.client.request(f"/rest/codexRoutineRuns/{current['id']}", update, "PATCH"))}
        item_result = self._upsert_operational_items(event)
        routine_update: dict[str, Any] = {}
        incoming_order = self._event_order(event["scheduledFor"], event["observedAt"])
        current_order = self._event_order(
            routine.get("lastScheduledFor"), routine.get("lastObservedAt")
        )
        if current_order is None or incoming_order >= current_order:
            routine_update.update(
                {
                    "lastRunKey": event["runKey"],
                    "lastScheduledFor": event["scheduledFor"],
                    "lastObservedAt": event["observedAt"],
                    "lastRunStatus": event["status"],
                    "lastCoverageStatus": event["coverageStatus"],
                    "lastStartedAt": run_payload["startedAt"],
                    "lastFinishedAt": run_payload["finishedAt"],
                    "lastMaterialChange": event["materialChange"],
                    "lastDomainRecordCount": len(event["domainRefs"]),
                    "lastErrorCode": event.get("errorCode"),
                    "lastSummary": event.get("summary"),
                }
            )
        if event["status"] in {"SUCCEEDED", "NO_MATERIAL_CHANGE"} and run_payload["finishedAt"]:
            incoming_success = self._instant(run_payload["finishedAt"])
            current_success = self._instant(routine.get("lastSucceededAt"))
            if current_success is None or incoming_success > current_success:
                routine_update["lastSucceededAt"] = run_payload["finishedAt"]
        routine_update = {
            key: value
            for key, value in routine_update.items()
            if canonical_json(routine.get(key)) != canonical_json(value)
        }
        if routine_update:
            routine_update["lastSyncedAt"] = datetime.now(timezone.utc).isoformat()
            self.client.request(f"/rest/codexRoutines/{routine['id']}", routine_update, "PATCH")
        return {
            "confirmed": True,
            "created": created,
            "duplicate": duplicate,
            "supersededStart": superseded_start,
            "payloadHash": event["payloadHash"],
            "runId": current["id"],
            "operationalItems": item_result,
        }

    @staticmethod
    def _instant(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError("stored routine timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise ApiError("stored routine timestamp lacks timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _event_order(cls, scheduled_for: Any, observed_at: Any) -> tuple[datetime, datetime] | None:
        scheduled = cls._instant(scheduled_for)
        observed = cls._instant(observed_at)
        if scheduled is None and observed is None:
            return None
        if scheduled is None or observed is None:
            raise ApiError("stored routine ordering fields are incomplete")
        return scheduled, observed


def run_extra_sync(command: list[str]) -> dict[str, Any]:
    process = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    if process.returncode != 0:
        raise ApiError(f"Extra sync failed with exit code {process.returncode}")
    return {"status": "completed", "exitCode": 0}
