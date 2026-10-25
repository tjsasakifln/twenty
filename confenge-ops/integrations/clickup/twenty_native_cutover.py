#!/usr/bin/env python3
"""Finish the ClickUp-to-Twenty cutover without reading ClickUp again.

The one-shot ``configure`` command maps the source assignment evidence to the
single target workspace member, converts legacy list-text view filters to real
relations, and makes the migrated task fields usable in Twenty.  It emits the
target-bound application-variable values used by the native Twenty database
event logic functions; no ClickUp connection or polling service remains.

This module contains no tenant IDs, record IDs, credentials, or source data.
All target-bound values live in root-readable deployment configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


IMPORTER_MARKER = "Managed by confenge-clickup-native-importer/v1"
ACKNOWLEDGEMENT = "TWENTY_NATIVE_CUTOVER"
NATIVE_STATUSES = ("TODO", "IN_PROGRESS", "DONE")
EXPECTED_TWENTY_HOSTNAME = "gestao.confenge.com.br"
IMPORTER_NAMESPACE = uuid.UUID("ee96e59e-46b7-43d6-b3a1-f6da63f31049")


def deterministic_uuid(*parts: object) -> str:
    return str(uuid.uuid5(IMPORTER_NAMESPACE, "\x1f".join(str(part) for part in parts)))


CONTRACTUAL_MILESTONES_VIEW_ID = deterministic_uuid(
    "view", "global:contractual-milestones"
)
OPERATIONAL_CALENDAR_VIEW_ID = deterministic_uuid("view", "global:deadline-calendar")
CUTOVER_STAGE_MEMORY_FIELD_UID = "38c98da3-3395-458a-a9f6-eafbda739e50"
CUTOVER_API_BASE_URL = "http://server:3000"


class CutoverError(RuntimeError):
    """A safety or API invariant failed."""


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def unwrap_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise CutoverError("unexpected list response")
    data = value.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        lists = [item for item in data.values() if isinstance(item, list)]
        if len(lists) == 1:
            return lists[0]
    for key in ("objects", "fields", "views", "viewFilters", "viewFields"):
        if isinstance(value.get(key), list):
            return value[key]
    raise CutoverError("unexpected list response")


def unwrap_one(value: Any) -> dict[str, Any]:
    current = value
    while isinstance(current, dict):
        data = current.get("data")
        if isinstance(data, dict) and data.get("id") not in (None, ""):
            current = data
            continue
        if isinstance(data, dict) and len(data) == 1:
            current = next(iter(data.values()))
            continue
        break
    if not isinstance(current, dict):
        raise CutoverError("unexpected record response")
    return current


class TwentyClient:
    """Small paced Twenty REST/metadata client."""

    def __init__(self, base_url: str, token: str, *, request_interval: float = 0.75):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.request_interval = request_interval
        self.last_request_at: float | None = None
        self.read_count = 0
        self.write_count = 0

    def request(self, path: str, payload: Any = None, method: str = "GET") -> Any:
        method = method.upper()
        if self.last_request_at is not None and self.request_interval > 0:
            remaining = self.request_interval - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()
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
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(response_body)
            except json.JSONDecodeError:
                detail = response_body[:500]
            raise CutoverError(f"Twenty {method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CutoverError(f"Twenty {method} {path} failed: {exc.reason}") from exc
        if method == "GET":
            self.read_count += 1
        else:
            self.write_count += 1
        return json.loads(raw) if raw else {}


    def list_rest(self, path: str, *, limit: int = 200) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            separator = "&" if "?" in path else "?"
            url = f"{path}{separator}limit={limit}"
            if cursor:
                url += "&starting_after=" + urllib.parse.quote(cursor)
            response = self.request(url)
            records.extend(unwrap_list(response))
            page = response.get("pageInfo") if isinstance(response, dict) else None
            if not isinstance(page, dict) or not page.get("hasNextPage"):
                return records
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CutoverError("invalid REST pagination cursor")
            seen.add(cursor)

    def workspace(self) -> dict[str, Any]:
        response = self.request(
            "/metadata",
            {
                "query": "query CutoverPreflight { currentWorkspace { id activationStatus isPublicInviteLinkEnabled } }",
                "variables": {},
            },
            "POST",
        )
        if response.get("errors"):
            raise CutoverError(f"workspace preflight failed: {response['errors']}")
        workspace = (response.get("data") or {}).get("currentWorkspace")
        if not isinstance(workspace, dict):
            raise CutoverError("workspace preflight returned no workspace")
        # A metadata query is read-only even though GraphQL uses HTTP POST.
        self.write_count -= 1
        self.read_count += 1
        return workspace


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"cannot read JSON document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"JSON document must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def source_tasks(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in document.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        task = item.get("data") if isinstance(item.get("data"), dict) else item
        if isinstance(task, dict) and task.get("id") not in (None, ""):
            result.append(task)
    return result


def assignment_evidence(document: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return exact task->source-user assignment evidence.

    Source display names and emails are intentionally ignored.  The cutover is
    valid only for the observed one-source-user/one-target-member topology.
    """

    assigned: dict[str, str] = {}
    for task in source_tasks(document):
        task_id = str(task["id"])
        user_ids = {
            str(user.get("userid") or user.get("id"))
            for user in task.get("users") or []
            if isinstance(user, dict)
            and normalized(user.get("type")) == "assigned"
            and (user.get("userid") or user.get("id")) not in (None, "")
        }
        if len(user_ids) > 1:
            raise CutoverError(f"task {task_id} has multiple assigned source users")
        if user_ids:
            assigned[task_id] = next(iter(user_ids))
    distinct = set(assigned.values())
    if len(distinct) != 1:
        raise CutoverError(
            f"cutover requires exactly one distinct assigned source user; found {len(distinct)}"
        )
    return assigned, next(iter(distinct))


def choose_stage_options(
    options: Iterable[dict[str, Any]],
    explicit_label_map: dict[str, str],
    requested_labels: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    requested_labels = requested_labels or {}
    normalized_map = {normalized(label): status for label, status in explicit_label_map.items()}
    invalid = sorted({status for status in normalized_map.values() if status not in NATIVE_STATUSES})
    if invalid:
        raise CutoverError("explicit stage map has invalid native statuses: " + ", ".join(invalid))
    stage_to_status: dict[str, str] = {}
    status_candidates: dict[str, list[tuple[int, str]]] = {key: [] for key in NATIVE_STATUSES}
    preferences = {
        "TODO": ("a organizar", "aberto", "to do"),
        "IN_PROGRESS": ("em execucao", "em andamento", "in progress"),
        "DONE": ("concluida", "concluido", "done", "complete"),
    }
    for index, option in enumerate(options):
        value = str(option.get("value") or "")
        label = str(option.get("label") or "")
        if not value:
            continue
        label_key = normalized(label)
        status = normalized_map.get(label_key)
        if status is None:
            raise CutoverError(f"explicit stage map has no exact entry for label: {label}")
        stage_to_status[value] = status
        requested = normalized(requested_labels.get(status))
        if requested and label_key == requested:
            rank = -1000
        else:
            rank = next(
                (position for position, candidate in enumerate(preferences[status]) if candidate in label_key),
                100 + index,
            )
        status_candidates[status].append((rank, value))
    missing = [status for status, candidates in status_candidates.items() if not candidates]
    if missing:
        raise CutoverError("Etapa options do not cover native statuses: " + ", ".join(missing))
    status_to_stage = {
        status: min(candidates, key=lambda item: item[0])[1]
        for status, candidates in status_candidates.items()
    }
    return stage_to_status, status_to_stage


def parse_explicit_stage_map(document: dict[str, Any]) -> dict[str, str]:
    mapping = document.get("labels") if isinstance(document.get("labels"), dict) else document
    if not isinstance(mapping, dict) or not mapping:
        raise CutoverError("stage map must contain a non-empty label-to-status object")
    result = {str(label): str(status) for label, status in mapping.items()}
    normalized_labels = [normalized(label) for label in result]
    if len(set(normalized_labels)) != len(normalized_labels):
        raise CutoverError("stage map contains duplicate normalized labels")
    return result


def relation_filter_value(record_id: str) -> str:
    return json.dumps(
        {"isCurrentRecordSelected": False, "selectedRecordIds": [record_id]},
        separators=(",", ":"),
        sort_keys=True,
    )


LABELS = {
    "assignee": "Responsável",
    "status": "Situação geral",
    "dueAt": "Prazo com horário",
    "clickUpStatus": "Etapa",
    "clickUpPriority": "Prioridade",
    "clickUpStartOn": "Início (data)",
    "clickUpStartAt": "Início com horário",
    "clickUpDueOn": "Prazo (data)",
    "clickUpList": "Lista",
    "clickUpParentTask": "Tarefa pai",
    "subtarefasClickup": "Subtarefas",
    "dependenciasDeSaidaClickup": "Depende de",
    "dependenciasDeEntradaClickup": "Bloqueia",
    "clickUpSourceUrl": "Origem no ClickUp",
}


VISIBLE_WIDGET_FIELDS = {
    "assignee",
    "status",
    "dueAt",
    "clickUpStatus",
    "clickUpPriority",
    "clickUpStartOn",
    "clickUpStartAt",
    "clickUpDueOn",
    "clickUpList",
    "clickUpParentTask",
    "subtarefasClickup",
    "dependenciasDeSaidaClickup",
    "dependenciasDeEntradaClickup",
}


@dataclass
class ConfigurationPlan:
    assignments: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    filters: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    field_labels: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    widget_fields: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    metadata_views: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    assignment_evidence_count: int = 0
    relation_filter_count: int = 0
    already_assigned_count: int = 0
    application_variables: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "assignment_evidence": self.assignment_evidence_count,
            "assignment_patches": len(self.assignments),
            "assignments_already_correct": self.already_assigned_count,
            "relation_filters_total": self.relation_filter_count,
            "relation_filter_patches": len(self.filters),
            "field_label_patches": len(self.field_labels),
            "widget_visibility_patches": len(self.widget_fields),
            "view_metadata_patches": len(self.metadata_views),
            "writes_planned": sum(
                len(items)
                for items in (
                    self.assignments,
                    self.filters,
                    self.field_labels,
                    self.widget_fields,
                    self.metadata_views,
                )
            ),
        }


def task_object(client: TwentyClient) -> dict[str, Any]:
    objects = client.list_rest("/rest/metadata/objects", limit=500)
    matches = [item for item in objects if item.get("nameSingular") == "task"]
    if len(matches) != 1:
        raise CutoverError(f"expected one Task object; found {len(matches)}")
    return matches[0]


def original_fingerprint(record: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    result = {key: record.get(key) for key in keys}
    if record.get("updatedAt") not in (None, ""):
        result["updatedAt"] = record["updatedAt"]
    return result


def preflight(client: TwentyClient, expected_workspace_id: str) -> dict[str, Any]:
    workspace = client.workspace()
    if workspace.get("id") != expected_workspace_id:
        raise CutoverError("target workspace id mismatch")
    if workspace.get("activationStatus") != "ACTIVE":
        raise CutoverError("target workspace is not ACTIVE")
    if workspace.get("isPublicInviteLinkEnabled") is not False:
        raise CutoverError("public workspace invite link must be disabled")
    members = client.list_rest("/rest/workspaceMembers", limit=20)
    if len(members) != 1:
        raise CutoverError(f"cutover requires exactly one Twenty workspace member; found {len(members)}")
    return members[0]


def build_configuration_plan(
    client: TwentyClient,
    source_document: dict[str, Any],
    *,
    expected_workspace_id: str,
    default_list_external_id: str,
    explicit_stage_map: dict[str, str],
    requested_stage_labels: dict[str, str] | None = None,
) -> ConfigurationPlan:
    assignments, _source_user_id = assignment_evidence(source_document)
    member = preflight(client, expected_workspace_id)
    member_id = str(member.get("id") or "")
    if not member_id:
        raise CutoverError("workspace member has no id")
    task_meta = task_object(client)
    field_by_name = {item.get("name"): item for item in task_meta.get("fields") or []}
    required = {"clickUpId", "clickUpListExternalId", "clickUpList", "clickUpStatus"}
    missing = sorted(required - set(field_by_name))
    if missing:
        raise CutoverError("missing Task fields: " + ", ".join(missing))

    tasks = client.list_rest("/rest/tasks", limit=200)
    by_external_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        external_id = task.get("clickUpId")
        if external_id in (None, ""):
            continue
        key = str(external_id)
        if key in by_external_id:
            raise CutoverError(f"duplicate imported task external id: {key}")
        by_external_id[key] = task
    unresolved = sorted(set(assignments) - set(by_external_id))
    if unresolved:
        raise CutoverError(f"{len(unresolved)} assigned source tasks are absent in Twenty")

    plan = ConfigurationPlan(assignment_evidence_count=len(assignments))
    for external_id in sorted(assignments):
        task = by_external_id[external_id]
        current = task.get("assigneeId")
        if current in (None, ""):
            expected_updated_at = str(task.get("updatedAt") or "")
            if not expected_updated_at:
                raise CutoverError("an assigned imported task has no updatedAt for CAS")
            plan.assignments.append(
                (str(task["id"]), expected_updated_at, {"assigneeId": member_id})
            )
        elif str(current) == member_id:
            plan.already_assigned_count += 1
        else:
            raise CutoverError("an assigned imported task points to an unexpected Twenty member")

    list_rows = client.list_rest("/rest/clickUpLists", limit=200)
    list_by_external: dict[str, str] = {}
    for row in list_rows:
        external_id = str(row.get("externalId") or "")
        row_id = str(row.get("id") or "")
        if external_id and row_id:
            if external_id in list_by_external:
                raise CutoverError(f"duplicate list external id: {external_id}")
            list_by_external[external_id] = row_id
    default_list_id = list_by_external.get(default_list_external_id)
    if not default_list_id:
        raise CutoverError("default native list external id was not found")

    filters = client.list_rest("/rest/metadata/viewFilters", limit=500)
    legacy_field_id = field_by_name["clickUpListExternalId"]["id"]
    relation_field_id = field_by_name["clickUpList"]["id"]
    for view_filter in filters:
        if view_filter.get("fieldMetadataId") != legacy_field_id:
            continue
        external_id = str(view_filter.get("value") or "")
        if external_id not in list_by_external:
            raise CutoverError("a legacy list view filter refers to an unknown list")
        plan.relation_filter_count += 1
        desired = {
            "fieldMetadataId": relation_field_id,
            "operand": "IS",
            "value": relation_filter_value(list_by_external[external_id]),
        }
        if any(view_filter.get(key) != value for key, value in desired.items()):
            plan.filters.append(
                (
                    str(view_filter["id"]),
                    original_fingerprint(
                        view_filter, ("fieldMetadataId", "operand", "value")
                    ),
                    desired,
                )
            )

    for name, desired_label in LABELS.items():
        metadata = field_by_name.get(name)
        if not metadata or metadata.get("label") == desired_label:
            continue
        description = str(metadata.get("description") or "")
        if name not in {"assignee", "status", "dueAt"} and not description.startswith(IMPORTER_MARKER):
            raise CutoverError(f"refusing to relabel an unowned field: {name}")
        plan.field_labels.append(
            (
                str(metadata["id"]),
                original_fingerprint(metadata, ("label",)),
                {"label": desired_label},
            )
        )

    task_views = client.list_rest(
        f"/rest/metadata/views?objectMetadataId={urllib.parse.quote(str(task_meta['id']))}",
        limit=200,
    )
    task_views_by_id = {str(view.get("id") or ""): view for view in task_views}
    for variable, view_id in (
        ("CONTRACTUAL_MILESTONES_VIEW_ID", CONTRACTUAL_MILESTONES_VIEW_ID),
        ("OPERATIONAL_CALENDAR_VIEW_ID", OPERATIONAL_CALENDAR_VIEW_ID),
    ):
        if view_id not in task_views_by_id:
            raise CutoverError(f"{variable} does not identify a Task view")

    calendar_view = task_views_by_id[OPERATIONAL_CALENDAR_VIEW_ID]
    desired_calendar_name = "Datas sem horário (legado)"
    if calendar_view.get("name") != desired_calendar_name:
        plan.metadata_views.append(
            (
                OPERATIONAL_CALENDAR_VIEW_ID,
                original_fingerprint(calendar_view, ("name",)),
                {"name": desired_calendar_name},
            )
        )

    widget_views = [
        view
        for view in task_views
        if view.get("type") == "FIELDS_WIDGET" and view.get("isActive") is not False
    ]
    if len(widget_views) != 1:
        raise CutoverError(f"expected one Task FIELDS_WIDGET view; found {len(widget_views)}")
    view_fields = client.list_rest(
        f"/rest/metadata/viewFields?viewId={urllib.parse.quote(str(widget_views[0]['id']))}",
        limit=300,
    )
    name_by_id = {str(value["id"]): name for name, value in field_by_name.items()}
    found_widget_names: set[str] = set()
    for view_field in view_fields:
        name = name_by_id.get(str(view_field.get("fieldMetadataId") or ""))
        if name in VISIBLE_WIDGET_FIELDS:
            found_widget_names.add(name)
            if view_field.get("isVisible") is not True:
                plan.widget_fields.append(
                    (
                        str(view_field["id"]),
                        original_fingerprint(view_field, ("isVisible",)),
                        {"isVisible": True},
                    )
                )
        elif name == "clickUpSourceUrl":
            found_widget_names.add(name)
            if view_field.get("isVisible") is not False:
                plan.widget_fields.append(
                    (
                        str(view_field["id"]),
                        original_fingerprint(view_field, ("isVisible",)),
                        {"isVisible": False},
                    )
                )
    required_widget = {
        name for name in VISIBLE_WIDGET_FIELDS if name in field_by_name
    } | {"clickUpSourceUrl"}
    missing_widget = required_widget - found_widget_names
    if missing_widget:
        raise CutoverError("FIELDS_WIDGET is missing fields: " + ", ".join(sorted(missing_widget)))

    memory_fields = [
        item
        for item in task_meta.get("fields") or []
        if item.get("universalIdentifier") == CUTOVER_STAGE_MEMORY_FIELD_UID
    ]
    if len(memory_fields) != 1:
        raise CutoverError(
            "expected exactly one app-owned cutover stage memory Task field"
        )
    memory_field_id = str(memory_fields[0]["id"])
    memory_view_fields = [
        item
        for item in client.list_rest(
            "/rest/metadata/viewFields?fieldMetadataId="
            + urllib.parse.quote(memory_field_id),
            limit=500,
        )
        if str(item.get("fieldMetadataId") or "") == memory_field_id
    ]
    if len(memory_view_fields) != 2:
        raise CutoverError(
            "expected exactly two view fields for the app-owned stage memory field"
        )
    for view_field in memory_view_fields:
        if view_field.get("isVisible") is not False:
            plan.widget_fields.append(
                (
                    str(view_field["id"]),
                    original_fingerprint(view_field, ("isVisible",)),
                    {"isVisible": False},
                )
            )

    stage_options = field_by_name["clickUpStatus"].get("options") or []
    stage_to_status, status_to_stage = choose_stage_options(
        stage_options, explicit_stage_map, requested_stage_labels
    )
    plan.application_variables = {
        "CUTOVER_API_BASE_URL": CUTOVER_API_BASE_URL,
        "CUTOVER_MEMBER_ID": member_id,
        "CUTOVER_DEFAULT_LIST_ID": default_list_id,
        "CUTOVER_STAGE_STATUS_MAP": json.dumps(stage_to_status, separators=(",", ":"), sort_keys=True),
        "CUTOVER_STAGE_TODO": status_to_stage["TODO"],
        "CUTOVER_STAGE_IN_PROGRESS": status_to_stage["IN_PROGRESS"],
        "CUTOVER_STAGE_DONE": status_to_stage["DONE"],
    }
    plan.application_variables["CONTRACTUAL_MILESTONES_VIEW_ID"] = (
        CONTRACTUAL_MILESTONES_VIEW_ID
    )
    plan.application_variables["OPERATIONAL_CALENDAR_VIEW_ID"] = (
        OPERATIONAL_CALENDAR_VIEW_ID
    )
    return plan


def apply_configuration(
    client: TwentyClient, plan: ConfigurationPlan, *, journal_path: Path | None = None
) -> None:
    if journal_path is not None and journal_path.exists():
        journal = load_json(journal_path)
        journal["status"] = "applying"
        journal["resume_count"] = int(journal.get("resume_count") or 0) + 1
        journal["remaining_plan"] = plan.summary()
    else:
        journal = {
            "version": 1,
            "status": "applying",
            "planned": plan.summary(),
            "application_variables_prepared": True,
            "completed": [],
            "resume_count": 0,
        }

    def save_journal() -> None:
        if journal_path is not None:
            atomic_write_json(journal_path, journal)

    def completed(kind: str, record_id: str) -> None:
        journal["completed"].append({"kind": kind, "id": record_id})
        save_journal()

    def patch_metadata_if_unchanged(
        path: str,
        expected: dict[str, Any],
        desired: dict[str, Any],
        *,
        kind: str,
        record_id: str,
    ) -> None:
        current = unwrap_one(client.request(path))
        actual = {key: current.get(key) for key in expected}
        if actual != expected:
            raise CutoverError(
                f"{kind} changed after planning; refusing to overwrite a concurrent edit"
            )
        client.request(path, desired, "PATCH")
        completed(kind, record_id)

    save_journal()
    try:
        for record_id, expected_updated_at, payload in plan.assignments:
            response = client.request(
                "/graphql",
                {
                    "query": """
                        mutation CutoverAssignTask($filter: TaskFilterInput!, $data: TaskUpdateInput!) {
                          updateTasks(filter: $filter, data: $data) { id updatedAt }
                        }
                    """,
                    "variables": {
                        "filter": {
                            "id": {"eq": record_id},
                            "updatedAt": {"eq": expected_updated_at},
                            "assigneeId": {"is": "NULL"},
                        },
                        "data": payload,
                    },
                },
                "POST",
            )
            if response.get("errors"):
                raise CutoverError("assignment CAS failed")
            updated = (response.get("data") or {}).get("updateTasks")
            if (
                not isinstance(updated, list)
                or len(updated) != 1
                or updated[0].get("id") != record_id
            ):
                raise CutoverError(
                    "assignment CAS found a concurrent task edit; rerun the dry-run"
                )
            completed("assignment", record_id)
        for filter_id, expected, payload in plan.filters:
            patch_metadata_if_unchanged(
                f"/rest/metadata/viewFilters/{filter_id}",
                expected,
                payload,
                kind="relation_filter",
                record_id=filter_id,
            )
        for field_id, expected, payload in plan.field_labels:
            patch_metadata_if_unchanged(
                f"/rest/metadata/fields/{field_id}",
                expected,
                payload,
                kind="field_label",
                record_id=field_id,
            )
        for view_field_id, expected, payload in plan.widget_fields:
            patch_metadata_if_unchanged(
                f"/rest/metadata/viewFields/{view_field_id}",
                expected,
                payload,
                kind="widget_visibility",
                record_id=view_field_id,
            )
        for view_id, expected, payload in plan.metadata_views:
            patch_metadata_if_unchanged(
                f"/rest/metadata/views/{view_id}",
                expected,
                payload,
                kind="view_metadata",
                record_id=view_id,
            )
    except BaseException as exc:
        journal["status"] = "failed"
        journal["error_type"] = type(exc).__name__
        save_journal()
        raise
    journal["status"] = "writes-complete-awaiting-verification"
    save_journal()


def complete_journal(path: Path, verification: dict[str, Any]) -> None:
    journal = load_json(path)
    journal["status"] = "complete"
    journal["verification"] = verification
    atomic_write_json(path, journal)


def fail_verification_journal(path: Path, exc: BaseException) -> None:
    journal = load_json(path)
    journal["status"] = "verification-failed"
    journal["error_type"] = type(exc).__name__
    atomic_write_json(path, journal)


def verify_marker(
    path: Path, expected_workspace_id: str, *, required_phase: str
) -> dict[str, Any]:
    marker = load_json(path)
    if marker.get("target_workspace_id") != expected_workspace_id:
        raise CutoverError("cutover marker target workspace id mismatch")
    if marker.get("phase") != required_phase:
        raise CutoverError(
            f"cutover marker phase must be exactly {required_phase}; found {marker.get('phase')!r}"
        )
    return marker


def exclude_completed_operations(
    plan: ConfigurationPlan, journal: dict[str, Any]
) -> ConfigurationPlan:
    completed = {
        (str(item.get("kind")), str(item.get("id")))
        for item in journal.get("completed") or []
        if isinstance(item, dict)
    }
    plan.assignments = [
        item for item in plan.assignments if ("assignment", item[0]) not in completed
    ]
    plan.filters = [
        item for item in plan.filters if ("relation_filter", item[0]) not in completed
    ]
    plan.field_labels = [
        item for item in plan.field_labels if ("field_label", item[0]) not in completed
    ]
    plan.widget_fields = [
        item
        for item in plan.widget_fields
        if ("widget_visibility", item[0]) not in completed
    ]
    plan.metadata_views = [
        item
        for item in plan.metadata_views
        if ("view_metadata", item[0]) not in completed
    ]
    return plan


def transition_marker(
    path: Path, marker: dict[str, Any], *, phase: str, journal_path: Path
) -> None:
    updated = dict(marker)
    updated["phase"] = phase
    updated["native_configuration_journal"] = str(journal_path)
    atomic_write_json(path, updated)


def validated_base_url(value: Any) -> str:
    base_url = str(value or "").rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    canonical_https = (
        parsed.scheme == "https" and parsed.hostname == EXPECTED_TWENTY_HOSTNAME
    )
    if not canonical_https and not (parsed.scheme == "http" and loopback):
        raise CutoverError(
            "credentials base_url must use the canonical HTTPS origin or explicit HTTP loopback"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CutoverError("credentials base_url contains unsupported URL components")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CutoverError("credentials base_url has an invalid port") from exc
    if (canonical_https and port not in (None, 443)) or parsed.path not in ("", "/"):
        raise CutoverError("credentials base_url must be the canonical Twenty origin")
    return base_url


def credentials(path: Path) -> tuple[TwentyClient, str]:
    document = load_json(path)
    for key in ("base_url", "api_key", "workspace_id"):
        if not document.get(key):
            raise CutoverError(f"credentials file is missing {key}")
    base_url = validated_base_url(document["base_url"])
    return TwentyClient(base_url, str(document["api_key"])), str(document["workspace_id"])


def configure_command(args: argparse.Namespace) -> int:
    client, expected_workspace_id = credentials(args.credentials)
    source = load_json(args.source_tasks)
    explicit_stage_map = parse_explicit_stage_map(load_json(args.stage_map))
    requested = {
        "TODO": args.todo_stage,
        "IN_PROGRESS": args.in_progress_stage,
        "DONE": args.done_stage,
    }
    plan = build_configuration_plan(
        client,
        source,
        expected_workspace_id=expected_workspace_id,
        default_list_external_id=args.default_list_external_id,
        explicit_stage_map=explicit_stage_map,
        requested_stage_labels=requested,
    )
    result = {"mode": "apply" if args.apply else "dry-run", **plan.summary()}
    if args.apply:
        if args.ack != ACKNOWLEDGEMENT:
            raise CutoverError(f"--ack must be exactly {ACKNOWLEDGEMENT}")
        marker = verify_marker(
            args.cutover_marker, expected_workspace_id, required_phase="preparing"
        )
        if args.resume:
            if not args.journal.exists():
                raise CutoverError("--resume requires an existing cutover journal")
            previous_journal = load_json(args.journal)
            if previous_journal.get("status") == "complete":
                if plan.summary()["writes_planned"] != 0:
                    raise CutoverError(
                        "completed journal recovery requires a fully converged configuration"
                    )
                transition_marker(
                    args.cutover_marker,
                    marker,
                    phase="native-configured",
                    journal_path=args.journal,
                )
                result["recovered_completed_journal"] = True
                result["post_apply_writes_planned"] = 0
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            if previous_journal.get("status") not in {
                "failed",
                "verification-failed",
                "writes-complete-awaiting-verification",
            }:
                raise CutoverError("--resume requires a failed or interrupted journal")
            plan = exclude_completed_operations(plan, previous_journal)
        elif args.journal.exists():
            raise CutoverError(
                "a cutover journal already exists; use --resume after reviewing it"
            )
        atomic_write_json(args.application_variables, plan.application_variables)
        if load_json(args.application_variables) != plan.application_variables:
            raise CutoverError("application variables failed their read-back validation")
        apply_configuration(client, plan, journal_path=args.journal)
        try:
            verification = build_configuration_plan(
                client,
                source,
                expected_workspace_id=expected_workspace_id,
                default_list_external_id=args.default_list_external_id,
                explicit_stage_map=explicit_stage_map,
                requested_stage_labels=requested,
            )
            verification_summary = verification.summary()
            if verification_summary["writes_planned"] != 0:
                raise CutoverError(
                    "post-apply verification found unconverged changes; rerun safely with the journal"
                )
        except BaseException as exc:
            fail_verification_journal(args.journal, exc)
            raise
        complete_journal(args.journal, verification_summary)
        transition_marker(
            args.cutover_marker,
            marker,
            phase="native-configured",
            journal_path=args.journal,
        )
        result["writes_applied"] = client.write_count
        result["application_variables_written"] = True
        result["post_apply_writes_planned"] = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="plan or apply the one-shot native cutover")
    configure.add_argument("--credentials", type=Path, required=True)
    configure.add_argument("--source-tasks", type=Path, required=True)
    configure.add_argument("--default-list-external-id", required=True)
    configure.add_argument("--stage-map", type=Path, required=True)
    configure.add_argument("--application-variables", type=Path, required=True)
    configure.add_argument("--journal", type=Path, required=True)
    configure.add_argument("--cutover-marker", type=Path, required=True)
    configure.add_argument("--todo-stage", default="a organizar")
    configure.add_argument("--in-progress-stage", default="em execução")
    configure.add_argument("--done-stage", default="concluído")
    configure.add_argument("--apply", action="store_true")
    configure.add_argument("--resume", action="store_true")
    configure.add_argument("--ack")
    configure.set_defaults(func=configure_command)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CutoverError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
