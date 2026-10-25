#!/usr/bin/env python3
"""Plan and install a native Twenty operational overview.

The default mode is read-only.  Apply mode creates only owned metadata: one
empty operational-goal object, owned table-widget views, one dashboard tab and
owned widgets.  It never creates goals, changes task records, or removes user
metadata.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


MARKER = "Managed by confenge-operational-overview/v1"
ACKNOWLEDGEMENT = "APPLY_OPERATIONAL_OVERVIEW"
DEFAULT_DASHBOARD_TITLE = "CONFENGE | Hoje"
DEFAULT_TAB_TITLE = "Operação"
DEFAULT_EXPECTED_HOSTNAME = "gestao.confenge.com.br"
GOAL_TASKS_FIELD_NAME = "tarefas"
NAMESPACE = uuid.UUID("d753d7d8-830b-49af-a088-3d83df4dce53")


def deterministic_uuid(*parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, "\x1f".join(str(part) for part in parts)))


FOCUS_VIEW_ID = deterministic_uuid("view", "task-focus")
OVERDUE_VIEW_ID = deterministic_uuid("view", "task-overdue")
GOALS_VIEW_ID = deterministic_uuid("view", "operational-goals")
ROUTINES_VIEW_ID = deterministic_uuid("view", "codex-routines")


class OverviewError(RuntimeError):
    """A safety, ownership or Twenty API invariant failed."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def unwrap_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise OverviewError("unexpected list response")
    if isinstance(value.get("data"), list):
        return value["data"]
    if isinstance(value.get("data"), dict):
        candidates = [item for item in value["data"].values() if isinstance(item, list)]
        if len(candidates) == 1:
            return candidates[0]
    for key in (
        "objects",
        "fields",
        "views",
        "viewFields",
        "viewFilters",
        "viewFilterGroups",
        "viewSorts",
        "pageLayoutTabs",
        "pageLayoutWidgets",
    ):
        if isinstance(value.get(key), list):
            return value[key]
    raise OverviewError("unexpected list response")


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
        raise OverviewError("unexpected record response")
    return current


def contains_expected(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains_expected(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and canonical(actual) == canonical(expected)
    return actual == expected


class TwentyClient:
    """Small paced Twenty REST/GraphQL client with read/write accounting."""

    def __init__(self, base_url: str, token: str, *, request_interval: float = 0.35):
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
            raise OverviewError(
                f"Twenty {method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OverviewError(f"Twenty {method} {path} failed: {exc.reason}") from exc
        if method == "GET":
            self.read_count += 1
        else:
            self.write_count += 1
        return json.loads(raw) if raw else {}

    def list_rest(self, path: str, *, limit: int = 200) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            separator = "&" if "?" in path else "?"
            url = f"{path}{separator}limit={limit}"
            if cursor:
                url += "&starting_after=" + urllib.parse.quote(cursor)
            response = self.request(url)
            result.extend(unwrap_list(response))
            page = response.get("pageInfo") if isinstance(response, dict) else None
            if not isinstance(page, dict) or not page.get("hasNextPage"):
                return result
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise OverviewError("invalid REST pagination cursor")
            seen.add(cursor)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.request(
            "/metadata", {"query": query, "variables": variables}, "POST"
        )
        # GraphQL reads use POST but are still read-only.
        self.write_count -= 1
        self.read_count += 1
        if response.get("errors"):
            raise OverviewError(f"metadata query failed: {response['errors']}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise OverviewError("metadata query returned no data")
        return data

    def relation_field(self, field_id: str) -> dict[str, Any]:
        data = self.graphql(
            """
            query OperationalGoalRelation($id: UUID!) {
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
            {"id": field_id},
        )
        field_metadata = data.get("field")
        if not isinstance(field_metadata, dict):
            raise OverviewError("metadata GraphQL did not return the relation field")
        return field_metadata

    def workspace_query(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request(
            "/graphql", {"query": query, "variables": variables}, "POST"
        )
        self.write_count -= 1
        self.read_count += 1
        if response.get("errors"):
            raise OverviewError(f"workspace query failed: {response['errors']}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise OverviewError("workspace query returned no data")
        return data

    def workspace(self) -> dict[str, Any]:
        data = self.graphql(
            "query OperationalOverviewPreflight { currentWorkspace { id activationStatus isPublicInviteLinkEnabled } }",
            {},
        )
        workspace = data.get("currentWorkspace")
        if not isinstance(workspace, dict):
            raise OverviewError("workspace preflight returned no workspace")
        return workspace

    def client_config(self) -> dict[str, Any]:
        # The endpoint is public by design; using the same client keeps all live
        # reads visible in the plan accounting.
        return self.request("/client-config")


@dataclass(frozen=True)
class OverviewSettings:
    dashboard_title: str = DEFAULT_DASHBOARD_TITLE
    dashboard_id: str | None = None
    tab_title: str = DEFAULT_TAB_TITLE
    front_field_label: str = "Frente"
    focus_field_label: str = "Foco atual"
    routines_object_name: str = "codexRoutine"


@dataclass
class Inspection:
    workspace: dict[str, Any]
    objects: dict[str, dict[str, Any]]
    dashboard: dict[str, Any]
    tabs: list[dict[str, Any]]
    widgets: list[dict[str, Any]]
    task: dict[str, Any]
    front_field: dict[str, Any]
    focus_field: dict[str, Any]
    task_counts: dict[str, Any]
    ai: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def exact_field(
    object_metadata: dict[str, Any], *, name: str | None = None, label: str | None = None
) -> dict[str, Any]:
    matches = [
        item
        for item in object_metadata.get("fields") or []
        if (name is not None and item.get("name") == name)
        or (label is not None and item.get("label") == label)
    ]
    if len(matches) != 1:
        selector = f"name={name!r}" if name is not None else f"label={label!r}"
        raise OverviewError(
            f"expected exactly one field on {object_metadata.get('nameSingular')} with {selector}; found {len(matches)}"
        )
    return matches[0]


def task_record_counts(
    client: TwentyClient,
    *,
    front_field_name: str,
    focus_field_name: str,
) -> dict[str, Any]:
    for name in (front_field_name, focus_field_name):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise OverviewError(f"unsafe task field metadata name: {name!r}")
    query = f"""
      query OperationalOverviewTaskCounts($first: Int!, $after: String) {{
        tasks(first: $first, after: $after) {{
          edges {{
            node {{
              id
              status
              dueAt
              clickUpDueOn
              {front_field_name}
              {focus_field_name}
            }}
          }}
          pageInfo {{ hasNextPage endCursor }}
        }}
      }}
    """
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        data = client.workspace_query(
            query, {"first": 200, "after": cursor}
        )
        connection = data.get("tasks")
        if not isinstance(connection, dict):
            raise OverviewError("task evidence query returned no task connection")
        for edge in connection.get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                records.append(node)
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise OverviewError("invalid task evidence pagination cursor")
        seen.add(cursor)
    return {
        "total": len(records),
        "status": {
            value: sum(item.get("status") == value for item in records)
            for value in ("TODO", "IN_PROGRESS", "DONE")
        },
        "front_non_empty": sum(
            item.get(front_field_name) not in (None, "") for item in records
        ),
        "focus_non_empty": sum(
            item.get(focus_field_name) not in (None, "") for item in records
        ),
        "due_at_non_empty": sum(item.get("dueAt") not in (None, "") for item in records),
        "due_on_non_empty": sum(
            item.get("clickUpDueOn") not in (None, "") for item in records
        ),
        "any_deadline_non_empty": sum(
            bool(item.get("dueAt") or item.get("clickUpDueOn")) for item in records
        ),
    }


def inspect_live(client: TwentyClient, settings: OverviewSettings) -> Inspection:
    workspace = client.workspace()
    objects = client.list_rest("/rest/metadata/objects", limit=500)
    objects_by_name = {item["nameSingular"]: item for item in objects}
    task = objects_by_name.get("task")
    if not isinstance(task, dict):
        raise OverviewError("native Task metadata is missing")
    front_field = exact_field(task, label=settings.front_field_label)
    focus_field = exact_field(task, label=settings.focus_field_label)
    if front_field.get("type") != "SELECT" or focus_field.get("type") != "SELECT":
        raise OverviewError("Frente and Foco atual must be native SELECT fields")

    dashboards = client.list_rest("/rest/dashboards", limit=200)
    if settings.dashboard_id:
        candidates = [item for item in dashboards if item.get("id") == settings.dashboard_id]
    else:
        candidates = [
            item for item in dashboards if item.get("title") == settings.dashboard_title
        ]
    if len(candidates) != 1:
        selector = settings.dashboard_id or settings.dashboard_title
        raise OverviewError(
            f"expected exactly one existing dashboard {selector!r}; found {len(candidates)}"
        )
    dashboard = candidates[0]
    if dashboard.get("title") != settings.dashboard_title:
        raise OverviewError(
            f"dashboard id belongs to {dashboard.get('title')!r}, not {settings.dashboard_title!r}"
        )
    page_layout_id = dashboard.get("pageLayoutId")
    if not isinstance(page_layout_id, str) or not page_layout_id:
        raise OverviewError("target dashboard has no native page layout")
    tabs = client.list_rest(
        "/rest/metadata/pageLayoutTabs?pageLayoutId="
        + urllib.parse.quote(page_layout_id),
        limit=100,
    )
    operation_tabs = [item for item in tabs if item.get("title") == settings.tab_title]
    if len(operation_tabs) > 1:
        raise OverviewError(f"duplicate dashboard tabs named {settings.tab_title!r}")
    widgets: list[dict[str, Any]] = []
    if operation_tabs:
        widgets = client.list_rest(
            "/rest/metadata/pageLayoutWidgets?pageLayoutTabId="
            + urllib.parse.quote(str(operation_tabs[0]["id"])),
            limit=100,
        )

    try:
        client_config = client.client_config()
        ai_models = client_config.get("aiModels") or []
        evaluation_models = client_config.get("aiEvaluationModels") or []
        ai = {
            "feature_present": True,
            "chat_model_count": len(ai_models),
            "configured_tier_count": len(client_config.get("aiModelTiers") or []),
            "available_evaluation_model_count": sum(
                1
                for model in evaluation_models
                if isinstance(model, dict) and model.get("isAvailable") is True
            ),
            "configured": bool(ai_models),
            "business_data_sent": False,
        }
    except OverviewError as exc:
        ai = {
            "feature_present": True,
            "configured": None,
            "business_data_sent": False,
            "inspection_error": str(exc),
        }

    try:
        task_counts = task_record_counts(
            client,
            front_field_name=str(front_field["name"]),
            focus_field_name=str(focus_field["name"]),
        )
    except OverviewError as exc:
        task_counts = {"inspection_error": str(exc)}

    return Inspection(
        workspace=workspace,
        objects=objects_by_name,
        dashboard=dashboard,
        tabs=tabs,
        widgets=widgets,
        task=task,
        front_field=front_field,
        focus_field=focus_field,
        task_counts=task_counts,
        ai=ai,
    )


def source_snapshot_evidence(
    tasks_document: dict[str, Any], field_document: dict[str, Any]
) -> dict[str, Any]:
    tasks = []
    for item in tasks_document.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        task = item.get("data") if isinstance(item.get("data"), dict) else item
        if isinstance(task, dict):
            tasks.append(task)
    definitions = {
        str(item.get("id")): item
        for item in field_document.get("fields") or []
        if isinstance(item, dict) and item.get("id") not in (None, "")
    }
    by_label = {str(item.get("name")): item for item in definitions.values()}
    front = by_label.get("Frente")
    focus = by_label.get("Foco atual")

    def option_counts(definition: dict[str, Any] | None) -> dict[str, int]:
        if not definition:
            return {}
        option_labels = {
            str(option.get("id")): str(option.get("name"))
            for option in (definition.get("type_config") or {}).get("options") or []
            if isinstance(option, dict)
        }
        counts: Counter[str] = Counter()
        for task in tasks:
            value = next(
                (
                    field_value.get("value")
                    for field_value in task.get("fields") or []
                    if isinstance(field_value, dict)
                    and str(field_value.get("field_id")) == str(definition.get("id"))
                ),
                None,
            )
            counts[option_labels.get(str(value), "<empty>") if value else "<empty>"] += 1
        return dict(sorted(counts.items()))

    return {
        "captured_at": tasks_document.get("captured_at"),
        "task_count": len(tasks),
        "field_definition_count": len(definitions),
        "tasks_with_due_date": sum(
            1 for item in tasks if (item.get("time_mgmt") or {}).get("due_date")
        ),
        "front_counts": option_counts(front),
        "focus_counts": option_counts(focus),
    }


def plan_report(
    inspection: Inspection,
    source_evidence: dict[str, Any] | None,
    settings: OverviewSettings | None = None,
) -> dict[str, Any]:
    settings = settings or OverviewSettings()
    goal = inspection.objects.get("operationalGoal")
    routine = inspection.objects.get("codexRoutine")
    operation_tabs = [
        item for item in inspection.tabs if item.get("title") == settings.tab_title
    ]
    existing_widget_titles = sorted(
        str(item.get("title")) for item in inspection.widgets if item.get("title")
    )
    report = {
        "mode": "read-only-plan",
        "workspace": {
            "id": inspection.workspace.get("id"),
            "activation_status": inspection.workspace.get("activationStatus"),
            "public_invite_disabled": inspection.workspace.get(
                "isPublicInviteLinkEnabled"
            )
            is False,
        },
        "dashboard": {
            "id": inspection.dashboard.get("id"),
            "title": inspection.dashboard.get("title"),
            "existing_tab_count": len(inspection.tabs),
            "operation_tab_exists": bool(operation_tabs),
            "existing_managed_widget_titles": existing_widget_titles,
        },
        "task_metadata": {
            "front_field": {
                "name": inspection.front_field.get("name"),
                "option_count": len(inspection.front_field.get("options") or []),
            },
            "focus_field": {
                "name": inspection.focus_field.get("name"),
                "option_count": len(inspection.focus_field.get("options") or []),
            },
            "due_fields": [
                item.get("name")
                for item in inspection.task.get("fields") or []
                if item.get("name") in {"dueAt", "clickUpDueOn"}
            ],
            "record_counts": inspection.task_counts,
        },
        "goals": {
            "object_exists": goal is not None,
            "records_to_create": 0,
            "policy": "schema and native records UI only; users supply targets",
        },
        "scheduled_routines": {
            "canonical_object": "codexRoutine",
            "object_exists": routine is not None,
            "dashboard_widget": "planned when object exists; otherwise skipped",
        },
        "ai": inspection.ai,
        "planned_components": [
            "reuse the existing dashboard and preserve every existing tab/widget",
            "create or reuse a separate grid tab named Operação",
            "native focus task table using Foco atual and excluding DONE",
            "native overdue task table using dueAt OR clickUpDueOn and excluding DONE",
            "native stacked task count by Frente and Situação geral",
            "empty operational-goal object related many-tasks-to-one-goal",
            "native goals table with no fabricated records or targets",
            "optional Codex routine health table backed by codexRoutine",
        ],
        "source_snapshot": source_evidence,
        "writes_performed": inspection.workspace.get("_writes", 0),
    }
    return report


GOAL_OPTIONS = [
    ("PLANNED", "Planejada", "gray"),
    ("ACTIVE", "Em andamento", "blue"),
    ("AT_RISK", "Em risco", "orange"),
    ("ACHIEVED", "Concluída", "green"),
    ("PAUSED", "Pausada", "yellow"),
]


class Configurator:
    def __init__(self, client: TwentyClient, inspection: Inspection, settings: OverviewSettings):
        self.client = client
        self.inspection = inspection
        self.settings = settings
        self.changes: Counter[str] = Counter()
        self.warnings: list[str] = []
        self.objects = inspection.objects

    def refresh_objects(self) -> None:
        objects = self.client.list_rest("/rest/metadata/objects", limit=500)
        self.objects = {item["nameSingular"]: item for item in objects}

    def ensure_object(self) -> dict[str, Any]:
        current = self.objects.get("operationalGoal")
        desired = {
            "nameSingular": "operationalGoal",
            "namePlural": "operationalGoals",
            "labelSingular": "Meta",
            "labelPlural": "Metas",
            "icon": "IconTargetArrow",
            "description": MARKER,
            "isSearchable": True,
        }
        if current is None:
            current = unwrap_one(
                self.client.request("/rest/metadata/objects", desired, "POST")
            )
            self.changes["objects_created"] += 1
            self.objects["operationalGoal"] = current
            return current
        if current.get("description") != MARKER:
            raise OverviewError("refusing unowned object collision: operationalGoal")
        update = {
            key: value
            for key, value in desired.items()
            if key not in {"nameSingular", "namePlural"}
            and canonical(current.get(key)) != canonical(value)
        }
        if update:
            patched = unwrap_one(
                self.client.request(
                    f"/rest/metadata/objects/{current['id']}", update, "PATCH"
                )
            )
            current = {**current, **patched}
            self.objects["operationalGoal"] = current
            self.changes["objects_updated"] += 1
        return current

    def ensure_field(
        self,
        object_metadata: dict[str, Any],
        name: str,
        label: str,
        field_type: str,
        *,
        options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        current = next(
            (
                item
                for item in object_metadata.get("fields") or []
                if item.get("name") == name
            ),
            None,
        )
        if current is None:
            payload: dict[str, Any] = {
                "objectMetadataId": object_metadata["id"],
                "name": name,
                "label": label,
                "type": field_type,
                "isNullable": True,
                "isActive": True,
                "isUnique": False,
                "description": MARKER,
            }
            if options is not None:
                payload["options"] = options
            current = unwrap_one(
                self.client.request("/rest/metadata/fields", payload, "POST")
            )
            object_metadata.setdefault("fields", []).append(current)
            self.changes["fields_created"] += 1
            return current
        if current.get("description") != MARKER:
            raise OverviewError(
                f"refusing unowned field collision: {object_metadata.get('nameSingular')}.{name}"
            )
        if current.get("type") != field_type:
            raise OverviewError(
                f"field type mismatch: {object_metadata.get('nameSingular')}.{name}"
            )
        update: dict[str, Any] = {}
        if current.get("label") != label:
            update["label"] = label
        if options is not None:
            existing_ids = {
                item.get("value"): item.get("id") for item in current.get("options") or []
            }
            merged = [
                {
                    **option,
                    "id": existing_ids.get(option["value"]) or option["id"],
                    "position": position,
                }
                for position, option in enumerate(options)
            ]
            if canonical(current.get("options") or []) != canonical(merged):
                update["options"] = merged
        if update:
            patched = unwrap_one(
                self.client.request(
                    f"/rest/metadata/fields/{current['id']}", update, "PATCH"
                )
            )
            current = {**current, **patched}
            self.changes["fields_updated"] += 1
        return current

    def ensure_goal_name_label(self, goal: dict[str, Any]) -> dict[str, Any]:
        """Localize the owned goal object's generated primary field."""

        candidates = [
            item for item in goal.get("fields") or [] if item.get("name") == "name"
        ]
        if len(candidates) != 1:
            raise OverviewError("operationalGoal must have exactly one primary name field")
        current = candidates[0]
        if current.get("type") != "TEXT" or current.get("objectMetadataId") not in {
            None,
            goal["id"],
        }:
            raise OverviewError("operationalGoal primary name field has unexpected identity")
        if current.get("label") == "Nome":
            return current
        patched = unwrap_one(
            self.client.request(
                f"/rest/metadata/fields/{current['id']}", {"label": "Nome"}, "PATCH"
            )
        )
        current = {**current, **patched}
        self.changes["goal_name_labels_updated"] += 1
        return current

    def ensure_goal_schema(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        existing_goal = self.objects.get("operationalGoal")
        if existing_goal is not None:
            self.validate_goal_relation_graph(
                self.objects["task"], existing_goal, allow_absent=True
            )
        goal = self.ensure_object()
        self.ensure_goal_name_label(goal)
        options = [
            {
                "id": deterministic_uuid("goal-status", value),
                "value": value,
                "label": label,
                "color": color,
                "position": position,
            }
            for position, (value, label, color) in enumerate(GOAL_OPTIONS)
        ]
        for name, label, field_type, field_options in (
            ("goalStatus", "Situação", "SELECT", options),
            ("targetOn", "Data-alvo", "DATE", None),
            ("progressPercent", "Progresso (%)", "NUMBER", None),
            ("description", "Descrição", "TEXT", None),
        ):
            self.ensure_field(goal, name, label, field_type, options=field_options)
        self.refresh_objects()
        goal = self.objects["operationalGoal"]
        task = self.objects["task"]
        relation_marker = f"{MARKER};target={goal['id']};type=MANY_TO_ONE"
        current = next(
            (
                item
                for item in task.get("fields") or []
                if item.get("name") == "operationalGoal"
            ),
            None,
        )
        if current is None:
            self.client.request(
                "/rest/metadata/fields",
                {
                    "objectMetadataId": task["id"],
                    "name": "operationalGoal",
                    "label": "Meta",
                    "type": "RELATION",
                    "isNullable": True,
                    "isActive": True,
                    "description": relation_marker,
                    "relationCreationPayload": {
                        "type": "MANY_TO_ONE",
                        "targetObjectMetadataId": goal["id"],
                        "targetFieldLabel": "Tarefas",
                        "targetFieldIcon": "IconCheckbox",
                    },
                },
                "POST",
            )
            self.changes["relations_created"] += 1
            self.refresh_objects()
            goal = self.objects["operationalGoal"]
            task = self.objects["task"]
        self.validate_goal_relation_graph(task, goal, allow_absent=False)
        required_goal_fields = {
            "name",
            "goalStatus",
            "targetOn",
            "progressPercent",
            "description",
            GOAL_TASKS_FIELD_NAME,
        }
        goal_field_names = {item.get("name") for item in goal.get("fields") or []}
        missing = sorted(required_goal_fields - goal_field_names)
        if missing:
            raise OverviewError(
                "operationalGoal schema is incomplete after relation creation: "
                + ", ".join(missing)
            )
        return goal, {item["name"]: item for item in goal.get("fields") or []}

    def validate_goal_relation_graph(
        self,
        task: dict[str, Any],
        goal: dict[str, Any],
        *,
        allow_absent: bool,
    ) -> None:
        """Require the complete bidirectional relation created by this integration."""

        source_fields = [
            item
            for item in task.get("fields") or []
            if item.get("name") == "operationalGoal"
        ]
        inverse_fields = [
            item
            for item in goal.get("fields") or []
            if item.get("name") == GOAL_TASKS_FIELD_NAME
        ]
        if not source_fields and not inverse_fields and allow_absent:
            return
        if len(source_fields) != 1 or len(inverse_fields) != 1:
            raise OverviewError(
                "operationalGoal relation must have exactly one task source and one goal inverse"
            )

        source = source_fields[0]
        inverse = inverse_fields[0]
        relation_marker = f"{MARKER};target={goal['id']};type=MANY_TO_ONE"
        if not source.get("id") or not inverse.get("id") or source["id"] == inverse["id"]:
            raise OverviewError("operationalGoal relation fields need distinct stable ids")
        source_hydrated = (
            source if source.get("relation") else self.client.relation_field(source["id"])
        )
        inverse_hydrated = (
            inverse
            if inverse.get("relation")
            else self.client.relation_field(inverse["id"])
        )
        source_relation = source_hydrated.get("relation") or {}
        inverse_relation = inverse_hydrated.get("relation") or {}

        source_expected = {
            "type": "MANY_TO_ONE",
            "sourceObjectMetadata": {"id": task["id"]},
            "targetObjectMetadata": {"id": goal["id"]},
            "sourceFieldMetadata": {
                "id": source.get("id"),
                "name": "operationalGoal",
            },
            "targetFieldMetadata": {
                "id": inverse.get("id"),
                "name": GOAL_TASKS_FIELD_NAME,
            },
        }
        inverse_expected = {
            "type": "ONE_TO_MANY",
            "sourceObjectMetadata": {"id": goal["id"]},
            "targetObjectMetadata": {"id": task["id"]},
            "sourceFieldMetadata": {
                "id": inverse.get("id"),
                "name": GOAL_TASKS_FIELD_NAME,
            },
            "targetFieldMetadata": {
                "id": source.get("id"),
                "name": "operationalGoal",
            },
        }
        if not (
            source.get("description") == relation_marker
            and source.get("type") == "RELATION"
            and (source.get("settings") or {}).get("relationType") == "MANY_TO_ONE"
            and contains_expected(source_relation, source_expected)
            and inverse.get("type") == "RELATION"
            and (inverse.get("settings") or {}).get("relationType") == "ONE_TO_MANY"
            and contains_expected(inverse_relation, inverse_expected)
        ):
            raise OverviewError(
                "operationalGoal relation graph has wrong target, direction, or inverse"
            )

    def ensure_view(
        self,
        *,
        view_id: str,
        stable_key: str,
        name: str,
        object_metadata: dict[str, Any],
        fields: dict[str, dict[str, Any]],
        visible_fields: list[str],
        filters: list[dict[str, Any]],
        sorts: list[dict[str, Any]],
        filter_groups: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        views = self.client.list_rest(
            f"/rest/metadata/views?objectMetadataId={object_metadata['id']}", limit=500
        )
        current = next((item for item in views if item.get("id") == view_id), None)
        payload = {
            "id": view_id,
            "name": name,
            "objectMetadataId": object_metadata["id"],
            "type": "TABLE_WIDGET",
            "icon": "IconTable",
            "position": 0,
            "visibility": "WORKSPACE",
        }
        groups = filter_groups or []
        if current is None:
            child_state: dict[str, list[dict[str, Any]]] = {
                "filter_groups": [],
                "filters": [],
                "sorts": [],
            }
            current = unwrap_one(
                self.client.request("/rest/metadata/views", payload, "POST")
            )
            self.changes["views_created"] += 1
        else:
            if current.get("objectMetadataId") != object_metadata["id"] or current.get(
                "type"
            ) != "TABLE_WIDGET":
                raise OverviewError(f"refusing view id collision: {view_id}")
            child_state = self.preflight_owned_view_children(
                view_id=view_id,
                stable_key=stable_key,
                fields=fields,
                filter_groups=groups,
                filters=filters,
                sorts=sorts,
            )
            update = {
                key: value
                for key, value in payload.items()
                if key not in {"id", "objectMetadataId", "type"}
                and canonical(current.get(key)) != canonical(value)
            }
            if update:
                current = unwrap_one(
                    self.client.request(
                        f"/rest/metadata/views/{view_id}", update, "PATCH"
                    )
                )
                self.changes["views_updated"] += 1
        self.ensure_view_filter_groups(
            view_id, stable_key, groups, existing=child_state["filter_groups"]
        )
        self.ensure_view_filters(
            view_id,
            stable_key,
            fields,
            filters,
            existing=child_state["filters"],
        )
        self.ensure_view_fields(view_id, stable_key, fields, visible_fields)
        self.ensure_view_sorts(
            view_id, stable_key, fields, sorts, existing=child_state["sorts"]
        )
        return current

    @staticmethod
    def reject_unexpected_owned_view_children(
        *,
        view_id: str,
        stable_key: str,
        child_kind: str,
        existing: list[dict[str, Any]],
        expected_ids: set[str],
    ) -> None:
        scoped = [item for item in existing if item.get("viewId") == view_id]
        actual_ids = [item.get("id") for item in scoped]
        unexpected = sorted(
            str(identifier)
            for identifier in actual_ids
            if identifier not in expected_ids
        )
        if len(actual_ids) != len(set(actual_ids)):
            unexpected.append("<duplicate-id>")
        if unexpected:
            raise OverviewError(
                f"owned view {stable_key} has unexpected {child_kind} children: "
                + ", ".join(unexpected)
            )

    def preflight_owned_view_children(
        self,
        *,
        view_id: str,
        stable_key: str,
        fields: dict[str, dict[str, Any]],
        filter_groups: list[dict[str, Any]],
        filters: list[dict[str, Any]],
        sorts: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        state = {
            "filter_groups": self.client.list_rest(
                f"/rest/metadata/viewFilterGroups?viewId={view_id}", limit=100
            ),
            "filters": self.client.list_rest(
                f"/rest/metadata/viewFilters?viewId={view_id}", limit=100
            ),
            "sorts": self.client.list_rest(
                f"/rest/metadata/viewSorts?viewId={view_id}", limit=100
            ),
        }
        expected = {
            "filter_groups": {
                deterministic_uuid("view-filter-group", stable_key, spec["key"])
                for spec in filter_groups
            },
            "filters": {
                deterministic_uuid(
                    "view-filter", stable_key, spec.get("key", index)
                )
                for index, spec in enumerate(filters)
            },
            "sorts": {
                deterministic_uuid("view-sort", stable_key, index)
                for index, spec in enumerate(sorts)
                if spec["fieldName"] in fields
            },
        }
        for child_kind, existing in state.items():
            self.reject_unexpected_owned_view_children(
                view_id=view_id,
                stable_key=stable_key,
                child_kind=child_kind,
                existing=existing,
                expected_ids=expected[child_kind],
            )
        return state

    def ensure_view_filter_groups(
        self,
        view_id: str,
        stable_key: str,
        specs: list[dict[str, Any]],
        *,
        existing: list[dict[str, Any]] | None = None,
    ) -> None:
        if existing is None:
            existing = self.client.list_rest(
                f"/rest/metadata/viewFilterGroups?viewId={view_id}", limit=100
            )
        by_id = {item["id"]: item for item in existing if item.get("viewId") == view_id}
        for index, spec in enumerate(specs):
            group_id = deterministic_uuid("view-filter-group", stable_key, spec["key"])
            parent_key = spec.get("parentKey")
            payload = {
                "id": group_id,
                "viewId": view_id,
                "logicalOperator": spec.get("logicalOperator", "AND"),
                "positionInViewFilterGroup": index,
                "parentViewFilterGroupId": None,
            }
            if parent_key:
                payload["parentViewFilterGroupId"] = deterministic_uuid(
                    "view-filter-group", stable_key, parent_key
                )
            current = by_id.get(group_id)
            if current is None:
                self.client.request("/rest/metadata/viewFilterGroups", payload, "POST")
                self.changes["view_filter_groups_created"] += 1
            else:
                update = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"id", "viewId"}
                    and canonical(current.get(key)) != canonical(value)
                }
                if update:
                    self.client.request(
                        f"/rest/metadata/viewFilterGroups/{group_id}", update, "PATCH"
                    )
                    self.changes["view_filter_groups_updated"] += 1

    def ensure_view_filters(
        self,
        view_id: str,
        stable_key: str,
        fields: dict[str, dict[str, Any]],
        specs: list[dict[str, Any]],
        *,
        existing: list[dict[str, Any]] | None = None,
    ) -> None:
        if existing is None:
            existing = self.client.list_rest(
                f"/rest/metadata/viewFilters?viewId={view_id}", limit=100
            )
        by_id = {item["id"]: item for item in existing if item.get("viewId") == view_id}
        for index, spec in enumerate(specs):
            metadata = fields.get(spec["fieldName"])
            if metadata is None:
                raise OverviewError(
                    f"owned view {stable_key} requires missing field {spec['fieldName']}"
                )
            filter_id = deterministic_uuid("view-filter", stable_key, spec.get("key", index))
            value = spec.get("value", "")
            if metadata.get("type") in {"SELECT", "MULTI_SELECT"} and spec[
                "operand"
            ] in {"IS", "IS_NOT"}:
                value = value if isinstance(value, list) else [value]
            payload: dict[str, Any] = {
                "id": filter_id,
                "viewId": view_id,
                "fieldMetadataId": metadata["id"],
                "operand": spec["operand"],
                "value": value,
                "positionInViewFilterGroup": spec.get("position", index),
                "viewFilterGroupId": None,
                "subFieldName": None,
                "relationTargetFieldMetadataId": None,
            }
            group_key = spec.get("groupKey")
            if group_key:
                payload["viewFilterGroupId"] = deterministic_uuid(
                    "view-filter-group", stable_key, group_key
                )
            current = by_id.get(filter_id)
            if current is None:
                self.client.request("/rest/metadata/viewFilters", payload, "POST")
                self.changes["view_filters_created"] += 1
            else:
                if current.get("fieldMetadataId") != metadata["id"]:
                    raise OverviewError(f"owned view filter field drift: {stable_key}")
                update = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"id", "viewId", "fieldMetadataId"}
                    and canonical(current.get(key)) != canonical(value)
                }
                if update:
                    self.client.request(
                        f"/rest/metadata/viewFilters/{filter_id}", update, "PATCH"
                    )
                    self.changes["view_filters_updated"] += 1

    def ensure_view_fields(
        self,
        view_id: str,
        stable_key: str,
        fields: dict[str, dict[str, Any]],
        field_names: list[str],
    ) -> None:
        existing = self.client.list_rest(
            f"/rest/metadata/viewFields?viewId={view_id}", limit=300
        )
        by_field = {
            item.get("fieldMetadataId"): item
            for item in existing
            if item.get("viewId") == view_id
        }
        desired_ids = {fields[name]["id"] for name in field_names if name in fields}
        for position, field_name in enumerate(field_names):
            metadata = fields.get(field_name)
            if metadata is None:
                continue
            desired_size = 360 if field_name in {"title", "name"} else 180
            current = by_field.get(metadata["id"])
            payload = {
                "isVisible": True,
                "position": position,
                "size": desired_size,
            }
            if current is None:
                self.client.request(
                    "/rest/metadata/viewFields",
                    {
                        "id": deterministic_uuid("view-field", stable_key, field_name),
                        "viewId": view_id,
                        "fieldMetadataId": metadata["id"],
                        **payload,
                    },
                    "POST",
                )
                self.changes["view_fields_created"] += 1
            else:
                update = {
                    key: value
                    for key, value in payload.items()
                    if canonical(current.get(key)) != canonical(value)
                }
                if update:
                    self.client.request(
                        f"/rest/metadata/viewFields/{current['id']}", update, "PATCH"
                    )
                    self.changes["view_fields_updated"] += 1
        for current in existing:
            if (
                current.get("viewId") == view_id
                and current.get("fieldMetadataId") not in desired_ids
                and current.get("isVisible") is not False
            ):
                self.client.request(
                    f"/rest/metadata/viewFields/{current['id']}",
                    {"isVisible": False},
                    "PATCH",
                )
                self.changes["view_fields_hidden"] += 1

    def ensure_view_sorts(
        self,
        view_id: str,
        stable_key: str,
        fields: dict[str, dict[str, Any]],
        specs: list[dict[str, Any]],
        *,
        existing: list[dict[str, Any]] | None = None,
    ) -> None:
        if existing is None:
            existing = self.client.list_rest(
                f"/rest/metadata/viewSorts?viewId={view_id}", limit=100
            )
        by_id = {item["id"]: item for item in existing if item.get("viewId") == view_id}
        for index, spec in enumerate(specs):
            metadata = fields.get(spec["fieldName"])
            if metadata is None:
                continue
            sort_id = deterministic_uuid("view-sort", stable_key, index)
            payload = {
                "id": sort_id,
                "viewId": view_id,
                "fieldMetadataId": metadata["id"],
                "direction": spec.get("direction", "ASC"),
                "subFieldName": None,
            }
            current = by_id.get(sort_id)
            if current is None:
                self.client.request("/rest/metadata/viewSorts", payload, "POST")
                self.changes["view_sorts_created"] += 1
            elif current.get("fieldMetadataId") != metadata["id"]:
                raise OverviewError(f"owned view sort field drift: {stable_key}")
            else:
                update = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"id", "viewId", "fieldMetadataId"}
                    and canonical(current.get(key)) != canonical(value)
                }
                if not update:
                    continue
                self.client.request(
                    f"/rest/metadata/viewSorts/{sort_id}",
                    update,
                    "PATCH",
                )
                self.changes["view_sorts_updated"] += 1

    def ensure_tab(self) -> dict[str, Any]:
        candidates = [
            item
            for item in self.inspection.tabs
            if item.get("title") == self.settings.tab_title
        ]
        if len(candidates) > 1:
            raise OverviewError(f"duplicate tabs named {self.settings.tab_title!r}")
        if candidates:
            current = candidates[0]
            if current.get("layoutMode") != "GRID":
                raise OverviewError("existing operation tab is not a GRID layout")
            return current
        current = unwrap_one(
            self.client.request(
                "/rest/metadata/pageLayoutTabs",
                {
                    "title": self.settings.tab_title,
                    "position": len(self.inspection.tabs),
                    "pageLayoutId": self.inspection.dashboard["pageLayoutId"],
                    "layoutMode": "GRID",
                },
                "POST",
            )
        )
        self.changes["tabs_created"] += 1
        return current

    def ensure_widget(
        self,
        tab: dict[str, Any],
        *,
        title: str,
        widget_type: str,
        object_metadata_id: str,
        configuration: dict[str, Any],
        position: dict[str, Any],
    ) -> dict[str, Any]:
        widgets = self.client.list_rest(
            "/rest/metadata/pageLayoutWidgets?pageLayoutTabId="
            + urllib.parse.quote(str(tab["id"])),
            limit=100,
        )
        candidates = [item for item in widgets if item.get("title") == title]
        if len(candidates) > 1:
            raise OverviewError(f"duplicate dashboard widgets named {title!r}")
        if candidates:
            current = candidates[0]
            if (
                current.get("type") != widget_type
                or current.get("objectMetadataId") != object_metadata_id
                or not contains_expected(current.get("configuration"), configuration)
            ):
                raise OverviewError(
                    f"refusing to overwrite dashboard widget collision: {title}"
                )
            # Position is intentionally preserved after creation so manual
            # dashboard rearrangement remains authoritative.
            return current
        current = unwrap_one(
            self.client.request(
                "/rest/metadata/pageLayoutWidgets",
                {
                    "pageLayoutTabId": tab["id"],
                    "title": title,
                    "type": widget_type,
                    "objectMetadataId": object_metadata_id,
                    "configuration": configuration,
                    "position": {"layoutMode": "GRID", **position},
                },
                "POST",
            )
        )
        self.changes["widgets_created"] += 1
        return current

    def apply(self) -> dict[str, Any]:
        goal, goal_fields = self.ensure_goal_schema()
        self.refresh_objects()
        task = self.objects["task"]
        task_fields = {item["name"]: item for item in task.get("fields") or []}
        front = exact_field(task, label=self.settings.front_field_label)
        focus = exact_field(task, label=self.settings.focus_field_label)
        task_fields[front["name"]] = front
        task_fields[focus["name"]] = focus
        focus_values = [
            item["value"]
            for item in focus.get("options") or []
            if item.get("value") not in (None, "")
        ]
        if not focus_values:
            raise OverviewError("Foco atual has no selectable options")

        self.ensure_view(
            view_id=FOCUS_VIEW_ID,
            stable_key="task-focus",
            name="Operação | Foco atual",
            object_metadata=task,
            fields=task_fields,
            visible_fields=[
                "title",
                focus["name"],
                front["name"],
                "status",
                "clickUpStatus",
                "dueAt",
                "clickUpDueOn",
            ],
            filters=[
                {
                    "key": "focus",
                    "fieldName": focus["name"],
                    "operand": "IS",
                    "value": focus_values,
                },
                {
                    "key": "open",
                    "fieldName": "status",
                    "operand": "IS_NOT",
                    "value": ["DONE"],
                },
            ],
            sorts=[{"fieldName": focus["name"], "direction": "ASC"}],
        )
        overdue_groups = [
            {"key": "root", "logicalOperator": "AND"},
            {"key": "due", "parentKey": "root", "logicalOperator": "OR"},
        ]
        self.ensure_view(
            view_id=OVERDUE_VIEW_ID,
            stable_key="task-overdue",
            name="Operação | Pendências vencidas",
            object_metadata=task,
            fields=task_fields,
            visible_fields=[
                "title",
                front["name"],
                "status",
                "clickUpStatus",
                "dueAt",
                "clickUpDueOn",
                "assignee",
            ],
            filter_groups=overdue_groups,
            filters=[
                {
                    "key": "open",
                    "fieldName": "status",
                    "operand": "IS_NOT",
                    "value": ["DONE"],
                    "groupKey": "root",
                    "position": 0,
                },
                {
                    "key": "due-at",
                    "fieldName": "dueAt",
                    "operand": "IS_IN_PAST",
                    "value": "",
                    "groupKey": "due",
                    "position": 0,
                },
                {
                    "key": "due-on",
                    "fieldName": "clickUpDueOn",
                    "operand": "IS_IN_PAST",
                    "value": "",
                    "groupKey": "due",
                    "position": 1,
                },
            ],
            sorts=[{"fieldName": "clickUpDueOn", "direction": "ASC"}],
        )
        self.ensure_view(
            view_id=GOALS_VIEW_ID,
            stable_key="operational-goals",
            name="Operação | Metas",
            object_metadata=goal,
            fields=goal_fields,
            visible_fields=[
                "name",
                "goalStatus",
                "progressPercent",
                "targetOn",
                GOAL_TASKS_FIELD_NAME,
                "description",
            ],
            filters=[],
            sorts=[{"fieldName": "targetOn", "direction": "ASC"}],
        )

        routine = self.objects.get(self.settings.routines_object_name)
        routine_view_created = False
        if routine is not None:
            routine_fields = {item["name"]: item for item in routine.get("fields") or []}
            expected = [
                "name",
                "scheduleDescription",
                "schedulerStatus",
                "lastRunStatus",
                "lastCoverageStatus",
                "lastFinishedAt",
                "lastSummary",
            ]
            missing = [name for name in expected if name not in routine_fields]
            if missing:
                self.warnings.append(
                    "codexRoutine widget skipped; missing fields: " + ", ".join(missing)
                )
            else:
                self.ensure_view(
                    view_id=ROUTINES_VIEW_ID,
                    stable_key="codex-routines",
                    name="Operação | Rotinas Codex",
                    object_metadata=routine,
                    fields=routine_fields,
                    visible_fields=expected,
                    filters=[],
                    sorts=[{"fieldName": "name", "direction": "ASC"}],
                )
                routine_view_created = True

        tab = self.ensure_tab()
        table_configuration = lambda view_id, limit: {
            "configurationType": "RECORD_TABLE",
            "viewId": view_id,
            "recordLimit": limit,
            "isUIEditable": False,
        }
        self.ensure_widget(
            tab,
            title="Operação | Foco atual",
            widget_type="RECORD_TABLE",
            object_metadata_id=task["id"],
            configuration=table_configuration(FOCUS_VIEW_ID, 6),
            position={"row": 0, "column": 0, "rowSpan": 6, "columnSpan": 6},
        )
        self.ensure_widget(
            tab,
            title="Operação | Pendências vencidas",
            widget_type="RECORD_TABLE",
            object_metadata_id=task["id"],
            configuration=table_configuration(OVERDUE_VIEW_ID, 8),
            position={"row": 0, "column": 6, "rowSpan": 6, "columnSpan": 6},
        )
        self.ensure_widget(
            tab,
            title="Operação | Progresso por frente",
            widget_type="GRAPH",
            object_metadata_id=task["id"],
            configuration={
                "configurationType": "BAR_CHART",
                "layout": "HORIZONTAL",
                "aggregateFieldMetadataId": task_fields["title"]["id"],
                "aggregateOperation": "COUNT",
                "primaryAxisGroupByFieldMetadataId": front["id"],
                "primaryAxisOrderBy": "VALUE_DESC",
                "secondaryAxisGroupByFieldMetadataId": task_fields["status"]["id"],
                "secondaryAxisOrderBy": "FIELD_POSITION_ASC",
                "displayDataLabel": True,
                "displayLegend": True,
                "axisNameDisplay": "NONE",
                "omitNullValues": True,
                "groupMode": "STACKED",
                "description": "Contagem de tarefas por frente e situação geral.",
            },
            position={"row": 6, "column": 0, "rowSpan": 6, "columnSpan": 12},
        )
        self.ensure_widget(
            tab,
            title="Operação | Metas",
            widget_type="RECORD_TABLE",
            object_metadata_id=goal["id"],
            configuration=table_configuration(GOALS_VIEW_ID, 8),
            position={"row": 12, "column": 0, "rowSpan": 6, "columnSpan": 6},
        )
        if routine_view_created and routine is not None:
            self.ensure_widget(
                tab,
                title="Operação | Rotinas Codex",
                widget_type="RECORD_TABLE",
                object_metadata_id=routine["id"],
                configuration=table_configuration(ROUTINES_VIEW_ID, 8),
                position={"row": 12, "column": 6, "rowSpan": 6, "columnSpan": 6},
            )
        return {
            "changes": dict(sorted(self.changes.items())),
            "writes": self.client.write_count,
            "warnings": self.warnings,
            "goal_records_created": 0,
            "task_records_updated": 0,
            "existing_dashboard_preserved": self.inspection.dashboard["id"],
        }


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OverviewError(f"cannot read JSON document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OverviewError(f"JSON document must be an object: {path}")
    return value


def credentials(path: Path) -> tuple[TwentyClient, str]:
    document = load_json(path)
    token = document.get("api_key")
    workspace_id = document.get("workspace_id")
    base_url = str(document.get("base_url") or "").rstrip("/")
    if not isinstance(token, str) or not token:
        raise OverviewError("credential file has no api_key")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise OverviewError("credential file has no workspace_id")
    parsed = urllib.parse.urlparse(base_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OverviewError("base_url must be a credential-free canonical origin")
    loopback = False
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if not (
        (parsed.scheme == "http" and loopback)
        or (
            parsed.scheme == "https"
            and hostname == DEFAULT_EXPECTED_HOSTNAME
            and parsed.port in {None, 443}
            and parsed.path in {"", "/"}
        )
    ):
        raise OverviewError(
            "base_url must be HTTP loopback or the fixed canonical HTTPS host"
        )
    return TwentyClient(base_url, token), workspace_id


def verify_apply_preflight(
    inspection: Inspection, *, credential_workspace_id: str, confirmed_workspace_id: str
) -> None:
    live_workspace_id = inspection.workspace.get("id")
    if not (
        live_workspace_id == credential_workspace_id == confirmed_workspace_id
    ):
        raise OverviewError("workspace confirmation mismatch")
    if inspection.workspace.get("activationStatus") != "ACTIVE":
        raise OverviewError("target workspace is not ACTIVE")
    if inspection.workspace.get("isPublicInviteLinkEnabled") is not False:
        raise OverviewError("public workspace invite must remain disabled")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--dashboard-title", default=DEFAULT_DASHBOARD_TITLE)
    parser.add_argument("--dashboard-id")
    parser.add_argument("--tab-title", default=DEFAULT_TAB_TITLE)
    parser.add_argument("--source-tasks", type=Path)
    parser.add_argument("--source-fields", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-workspace-id")
    parser.add_argument("--ack")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.source_tasks) != bool(args.source_fields):
        raise OverviewError("--source-tasks and --source-fields must be supplied together")
    client, credential_workspace_id = credentials(args.credential_file)
    settings = OverviewSettings(
        dashboard_title=args.dashboard_title,
        dashboard_id=args.dashboard_id,
        tab_title=args.tab_title,
    )
    inspection = inspect_live(client, settings)
    source_evidence = None
    if args.source_tasks and args.source_fields:
        source_evidence = source_snapshot_evidence(
            load_json(args.source_tasks), load_json(args.source_fields)
        )
    if not args.apply:
        report = plan_report(inspection, source_evidence, settings)
        report["reads_performed"] = client.read_count
        report["writes_performed"] = client.write_count
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.ack != ACKNOWLEDGEMENT:
        raise OverviewError(f"--ack must be exactly {ACKNOWLEDGEMENT}")
    if not args.confirm_workspace_id:
        raise OverviewError("--confirm-workspace-id is required with --apply")
    verify_apply_preflight(
        inspection,
        credential_workspace_id=credential_workspace_id,
        confirmed_workspace_id=args.confirm_workspace_id,
    )
    result = Configurator(client, inspection, settings).apply()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OverviewError as exc:
        raise SystemExit(f"error: {exc}") from exc
