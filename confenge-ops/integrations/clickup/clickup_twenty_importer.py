#!/usr/bin/env python3
"""Idempotently project a private ClickUp snapshot into native Twenty.

The default mode is an offline dry-run.  No network client is constructed until
the three explicit production-write guards have been satisfied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


IMPORTER_NAMESPACE = uuid.UUID("ee96e59e-46b7-43d6-b3a1-f6da63f31049")
APPLY_ACK = "APPLY_CLICKUP_TO_TWENTY"
IMPORTER_MARKER = "Managed by confenge-clickup-native-importer/v1"
COLORS = (
    "blue",
    "green",
    "orange",
    "purple",
    "red",
    "yellow",
    "turquoise",
    "sky",
    "pink",
    "gray",
)


class ContractError(RuntimeError):
    """The private snapshot does not satisfy the importer contract."""


class ApiError(RuntimeError):
    """A redacted Twenty API failure."""


@dataclass(frozen=True)
class ListSpec:
    external_id: str
    name: str
    space_external_id: str
    space_name: str
    folder_external_id: str | None
    folder_name: str | None
    root_position: int
    folder_position: int
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class FolderSpec:
    external_id: str
    name: str
    position: int


@dataclass(frozen=True)
class HierarchySpec:
    workspace_external_id: str
    workspace_name: str
    space_external_id: str
    space_name: str
    folders: tuple[FolderSpec, ...]
    lists: tuple[ListSpec, ...]


@dataclass(frozen=True)
class StatusSpec:
    list_external_id: str
    status_external_id: str
    label: str
    color: str
    list_position: int
    value: str
    option_id: str


@dataclass
class SnapshotPlan:
    hierarchy: HierarchySpec
    tasks: list[dict[str, Any]]
    status_specs: list[StatusSpec]
    field_definitions: dict[str, dict[str, Any]]
    global_views: list[dict[str, Any]]
    warnings: list[str]
    coverage: dict[str, Any]

    def has_complete_task_coverage(self) -> bool:
        try:
            return (
                int(self.coverage.get("found_count"))
                == int(self.coverage.get("requested_count"))
                == len(self.tasks)
                and int(self.coverage.get("missing_count") or 0) == 0
            )
        except (TypeError, ValueError):
            return False

    def summary(self, *, dark_theme: bool) -> dict[str, Any]:
        return {
            "mode": "dry-run",
            "network_requests": 0,
            "workspace_id": self.hierarchy.workspace_external_id,
            "hierarchy": {
                "space": self.hierarchy.space_name,
                "folders": len(self.hierarchy.folders),
                "lists": len(self.hierarchy.lists),
                "direct_lists": sum(
                    item.folder_external_id is None for item in self.hierarchy.lists
                ),
            },
            "records": {
                "list_records": len(self.hierarchy.lists),
                "tasks_available": len(self.tasks),
                "status_options": len({item.value for item in self.status_specs}),
                "list_status_assignments": len(self.status_specs),
                "custom_field_values": sum(
                    len(as_list(task.get("fields"))) for task in self.tasks
                ),
                "dependency_edges_available": len(build_dependency_edges(self.tasks)),
            },
            "views": {
                "table": len(self.hierarchy.lists),
                "kanban": sum(
                    str(item.get("type") or "TABLE").upper() == "KANBAN"
                    for item in self.global_views
                ),
                "calendar": sum(
                    str(item.get("type") or "TABLE").upper() == "CALENDAR"
                    for item in self.global_views
                ),
                "navigation_folders": len(self.hierarchy.folders),
                "navigation_list_links": len(self.hierarchy.lists),
                "global_named_views": len(self.global_views),
                "navigation_global_links": len(self.global_views),
            },
            "appearance": {"dark_theme_planned": dark_theme},
            "coverage": self.coverage,
            "warnings": self.warnings,
            "safety": {
                "deletes": 0,
                "missing_source_records_archived": 0,
                "existing_crm_records_replaced": 0,
            },
        }


def deterministic_uuid(*parts: object) -> str:
    return str(uuid.uuid5(IMPORTER_NAMESPACE, "\x1f".join(str(part) for part in parts)))


def stable_hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def raw_or_none(value: Any) -> Any:
    return None if value in (None, "", [], {}) else value


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: line {exc.lineno}") from exc


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    return value.strip()


def parse_hierarchy(document: dict[str, Any]) -> HierarchySpec:
    if not isinstance(document, dict):
        raise ContractError("hierarchy snapshot must be an object")
    hierarchy = document.get("hierarchy")
    root = hierarchy.get("root") if isinstance(hierarchy, dict) else None
    if not isinstance(root, dict):
        raise ContractError("hierarchy.root is missing")

    seen: set[str] = set()

    def identity(node: dict[str, Any], path: str) -> tuple[str, str]:
        external_id = require_string(node.get("id"), f"{path}.id")
        name = require_string(node.get("name"), f"{path}.name")
        if external_id in seen:
            raise ContractError(f"duplicate hierarchy id: {external_id}")
        seen.add(external_id)
        return external_id, name

    workspace_id, workspace_name = identity(root, "hierarchy.root")
    spaces = [
        child
        for child in as_list(root.get("children"))
        if isinstance(child, dict) and child.get("type") == "space"
    ]
    if len(spaces) != 1:
        raise ContractError(f"expected exactly one space, found {len(spaces)}")
    space = spaces[0]
    space_id, space_name = identity(space, "space")

    folders: list[FolderSpec] = []
    lists: list[ListSpec] = []
    for root_position, node in enumerate(as_list(space.get("children"))):
        if not isinstance(node, dict):
            raise ContractError("space children must be objects")
        node_type = node.get("type")
        if node_type == "folder":
            folder_id, folder_name = identity(node, f"folder[{root_position}]")
            folders.append(FolderSpec(folder_id, folder_name, root_position))
            for folder_position, list_node in enumerate(as_list(node.get("children"))):
                if not isinstance(list_node, dict) or list_node.get("type") != "list":
                    raise ContractError(
                        f"folder {folder_id} contains a non-list child"
                    )
                list_id, list_name = identity(
                    list_node, f"folder[{root_position}].list[{folder_position}]"
                )
                lists.append(
                    ListSpec(
                        list_id,
                        list_name,
                        space_id,
                        space_name,
                        folder_id,
                        folder_name,
                        root_position,
                        folder_position,
                        list_node,
                    )
                )
        elif node_type == "list":
            list_id, list_name = identity(node, f"list[{root_position}]")
            lists.append(
                ListSpec(
                    list_id,
                    list_name,
                    space_id,
                    space_name,
                    None,
                    None,
                    root_position,
                    0,
                    node,
                )
            )
        else:
            raise ContractError(f"unsupported space child type: {node_type!r}")
    if not lists:
        raise ContractError("hierarchy has no lists")
    return HierarchySpec(
        workspace_id,
        workspace_name,
        space_id,
        space_name,
        tuple(folders),
        tuple(lists),
    )


def parse_tasks(document: dict[str, Any], hierarchy: HierarchySpec) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(document, dict):
        raise ContractError("task snapshot must be an object")
    workspace_id = document.get("workspace_id")
    if workspace_id is not None and str(workspace_id) != hierarchy.workspace_external_id:
        raise ContractError("task and hierarchy workspace ids do not match")
    known_lists = {item.external_id for item in hierarchy.lists}
    seen: set[str] = set()
    tasks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, source_item in enumerate(as_list(document.get("tasks"))):
        if not isinstance(source_item, dict):
            raise ContractError(f"tasks[{index}] must be an object")
        if isinstance(source_item.get("data"), dict):
            task = dict(source_item["data"])
            task["_source_projection"] = {
                "object_id": source_item.get("object_id"),
                "status": source_item.get("status"),
            }
            task.setdefault("text", task.get("text_content"))
            task.setdefault("timing", task.get("time_mgmt"))
            task.setdefault("home_list", task.get("lists"))
            task_status = task.get("task_status")
            embedded_status = task.get("status")
            if not isinstance(task_status, dict):
                task_status = embedded_status if isinstance(embedded_status, dict) else {}
            task["task_status"] = {
                "id": task_status.get("id") or task.get("status_id"),
                "status": task_status.get("status")
                or (embedded_status if isinstance(embedded_status, str) else None),
            }
            source_status = source_item.get("status")
        else:
            task = dict(source_item)
            source_status = task.get("status")
        task_id = require_string(task.get("id"), f"tasks[{index}].id")
        if task_id in seen:
            raise ContractError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        if source_status not in (None, "found") and not isinstance(task.get("task_status"), dict):
            warnings.append(f"task {task_id} skipped because status={source_status!r}")
            continue
        require_string(task.get("name"), f"tasks[{index}].name")
        list_id = get_home_list_id(task)
        if list_id is None:
            warnings.append(f"task {task_id} has no home list")
        elif list_id not in known_lists:
            warnings.append(f"task {task_id} references unknown list {list_id}")
        tasks.append(task)
    return tasks, warnings


def get_home_list_id(task: dict[str, Any]) -> str | None:
    homes = [item for item in as_list(task.get("home_list")) if isinstance(item, dict)]
    preferred = next((item for item in homes if item.get("type") == "home"), None)
    selected = preferred or (homes[0] if homes else None)
    value = selected.get("list_id") if selected else task.get("list_id")
    return str(value) if value not in (None, "") else None


def normalize_color(raw: Any, seed: str) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        aliases = {"grey": "gray", "violet": "purple", "cyan": "turquoise"}
        value = aliases.get(value, value)
        if value in COLORS:
            return value
        rgb: tuple[int, int, int] | None = None
        if re.fullmatch(r"#[0-9a-f]{3}", value):
            rgb = tuple(int(char * 2, 16) for char in value[1:])  # type: ignore[assignment]
        elif re.fullmatch(r"#[0-9a-f]{6}", value):
            rgb = tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[assignment]
        else:
            match = re.fullmatch(
                r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,[^)]*)?\s*\)",
                value,
            )
            if match:
                rgb = tuple(min(255, int(item)) for item in match.groups())  # type: ignore[assignment]
        if rgb is not None:
            palette = {
                "blue": (37, 99, 235),
                "green": (34, 197, 94),
                "orange": (249, 115, 22),
                "purple": (124, 58, 237),
                "red": (239, 68, 68),
                "yellow": (234, 179, 8),
                "turquoise": (20, 184, 166),
                "sky": (14, 165, 233),
                "pink": (236, 72, 153),
                "gray": (107, 114, 128),
            }
            return min(
                palette,
                key=lambda name: sum(
                    (rgb[index] - palette[name][index]) ** 2 for index in range(3)
                ),
            )
    return COLORS[int(stable_hash(seed, 8), 16) % len(COLORS)]


def iter_status_inputs(
    hierarchy: HierarchySpec,
    tasks: list[dict[str, Any]],
    status_document: dict[str, Any] | None,
    space_status_document: dict[str, Any] | None,
) -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(space_status_document, dict):
        if str(space_status_document.get("space_id")) != hierarchy.space_external_id:
            raise ContractError("space status profile does not match hierarchy space")
        applies_to = {
            str(value) for value in as_list(space_status_document.get("applies_to_lists"))
        }
        known_lists = {item.external_id for item in hierarchy.lists}
        unknown = applies_to - known_lists
        if unknown:
            raise ContractError(f"space status profile references unknown lists: {sorted(unknown)}")
        inherited_ids: dict[str, str] = {}
        if isinstance(status_document, dict):
            prefix = f"p{hierarchy.space_external_id}_"
            for value in as_list(status_document.get("statuses")):
                if not isinstance(value, dict):
                    continue
                status_id = str(value.get("status_id") or value.get("id") or "")
                label = str(value.get("status") or value.get("name") or "").strip().casefold()
                if status_id.startswith(prefix) and label:
                    prior = inherited_ids.setdefault(label, status_id)
                    if prior != status_id:
                        raise ContractError(f"ambiguous inherited status id for {label}")
        for list_id in sorted(applies_to):
            for value in as_list(space_status_document.get("statuses")):
                if not isinstance(value, dict):
                    continue
                label = str(value.get("status") or value.get("name") or "").strip().casefold()
                status_id = inherited_ids.get(label)
                if not status_id:
                    raise ContractError(f"no observed inherited status id for {label}")
                normalized = dict(value)
                normalized["id"] = status_id
                yield list_id, normalized
    if isinstance(status_document, dict):
        observed = status_document.get("statuses")
        if isinstance(observed, list):
            for value in observed:
                if isinstance(value, dict) and value.get("list_id") not in (None, ""):
                    normalized = dict(value)
                    normalized.setdefault("id", normalized.get("status_id"))
                    yield str(value["list_id"]), normalized
        by_list = status_document.get("lists", status_document)
        if isinstance(by_list, dict):
            for list_id, values in by_list.items():
                for value in as_list(values):
                    if isinstance(value, dict):
                        yield str(list_id), value
    for item in hierarchy.lists:
        for value in as_list(item.raw.get("statuses")):
            if isinstance(value, dict):
                yield item.external_id, value
    for task in tasks:
        list_id = get_home_list_id(task)
        task_status = task.get("task_status")
        if list_id and isinstance(task_status, dict):
            yield list_id, task_status


def build_status_specs(
    hierarchy: HierarchySpec,
    tasks: list[dict[str, Any]],
    status_document: dict[str, Any] | None,
    space_status_document: dict[str, Any] | None = None,
) -> list[StatusSpec]:
    known_lists = {item.external_id for item in hierarchy.lists}
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for list_id, raw in iter_status_inputs(
        hierarchy, tasks, status_document, space_status_document
    ):
        if list_id not in known_lists:
            continue
        label = raw.get("status") or raw.get("name") or raw.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        external_id = str(raw.get("id") or stable_hash(label.strip().casefold(), 20))
        unique.setdefault((list_id, external_id), raw)

    result: list[StatusSpec] = []
    by_list_position: Counter[str] = Counter()
    identity_by_status_id: dict[str, tuple[str, str]] = {}
    grouped_statuses: dict[str, list[dict[str, Any]]] = {}
    for (_, status_id), raw in unique.items():
        grouped_statuses.setdefault(status_id, []).append(raw)
    for status_id, raw_values in grouped_statuses.items():
        labels = {
            str(raw.get("status") or raw.get("name") or raw.get("label")).strip().casefold()
            for raw in raw_values
        }
        explicit_colors = {
            normalize_color(raw.get("color"), status_id)
            for raw in raw_values
            if raw.get("color") not in (None, "")
        }
        if len(labels) != 1 or len(explicit_colors) > 1:
            raise ContractError(f"status id {status_id} has conflicting labels or colors")
        color = next(iter(explicit_colors), normalize_color(None, status_id))
        identity_by_status_id[status_id] = (next(iter(labels)), color)
    for global_position, ((list_id, status_id), raw) in enumerate(sorted(unique.items())):
        label = str(raw.get("status") or raw.get("name") or raw.get("label")).strip()
        color = identity_by_status_id[status_id][1]
        explicit_position = raw.get("orderindex", raw.get("position"))
        try:
            list_position = int(explicit_position)
        except (TypeError, ValueError):
            list_position = by_list_position[list_id]
        by_list_position[list_id] += 1
        value = f"S_{stable_hash(status_id)}"
        result.append(
            StatusSpec(
                list_id,
                status_id,
                label,
                color,
                list_position,
                value,
                deterministic_uuid("status-option", status_id),
            )
        )
    # Twenty requires globally unique option positions.  Per-list ordering is
    # separately represented by each board's view groups.
    result.sort(key=lambda item: (item.list_external_id, item.list_position, item.status_external_id))
    return result


def build_plan(
    hierarchy_document: dict[str, Any],
    task_document: dict[str, Any],
    status_document: dict[str, Any] | None = None,
    field_document: dict[str, Any] | None = None,
    global_view_document: dict[str, Any] | None = None,
    task_links_document: list[dict[str, Any]] | dict[str, Any] | None = None,
    space_status_document: dict[str, Any] | None = None,
    rich_content_document: list[dict[str, Any]] | dict[str, Any] | None = None,
    source_view_document: dict[str, Any] | None = None,
) -> SnapshotPlan:
    hierarchy = parse_hierarchy(hierarchy_document)
    list_ids = [item.external_id for item in hierarchy.lists]
    overlapping_list_ids = [
        (left, right)
        for left in list_ids
        for right in list_ids
        if left != right and left in right
    ]
    if overlapping_list_ids:
        left, right = overlapping_list_ids[0]
        raise ContractError(
            "list external ids must not contain one another because Twenty TEXT "
            f"view filters use CONTAINS: {left} in {right}"
        )
    tasks, warnings = parse_tasks(task_document, hierarchy)
    if rich_content_document is not None:
        rich_rows = (
            rich_content_document.get("tasks")
            if isinstance(rich_content_document, dict)
            else rich_content_document
        )
        rich_by_id: dict[str, Any] = {}
        for item in as_list(rich_rows):
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                raise ContractError("rich content entries require an id")
            task_id = str(item["id"])
            if task_id in rich_by_id:
                raise ContractError(f"duplicate rich content task id: {task_id}")
            rich_by_id[task_id] = item.get("content")
        task_ids = {str(task["id"]) for task in tasks}
        if set(rich_by_id) != task_ids:
            raise ContractError(
                f"rich content coverage mismatch: {len(task_ids - set(rich_by_id))} missing, "
                f"{len(set(rich_by_id) - task_ids)} extra"
            )
        for task in tasks:
            task["rich_content"] = rich_by_id[str(task["id"])]
    if task_links_document is not None:
        raw_links = (
            task_links_document.get("links")
            if isinstance(task_links_document, dict)
            else task_links_document
        )
        link_by_id: dict[str, str] = {}
        for item in as_list(raw_links):
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ContractError("task link entries require a url")
            parsed = urllib.parse.urlparse(item["url"])
            task_id = parsed.path.rstrip("/").split("/")[-1]
            if not task_id:
                raise ContractError("task link has no terminal task id")
            if task_id in link_by_id:
                raise ContractError(f"duplicate task link id: {task_id}")
            link_by_id[task_id] = item["url"]
        task_ids = {str(task["id"]) for task in tasks}
        if set(link_by_id) != task_ids:
            missing = len(task_ids - set(link_by_id))
            extra = len(set(link_by_id) - task_ids)
            raise ContractError(f"task link coverage mismatch: {missing} missing, {extra} extra")
        for task in tasks:
            task["source_url"] = link_by_id[str(task["id"])]
    status_specs = build_status_specs(
        hierarchy, tasks, status_document, space_status_document
    )
    statuses_by_list = {item.list_external_id for item in status_specs}
    for list_spec in hierarchy.lists:
        if list_spec.external_id not in statuses_by_list:
            warnings.append(
                f"list {list_spec.external_id} has no structured status definitions; "
                "its board will retain global status groups until definitions are supplied"
            )
    coverage = task_document.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {
            "found_count": task_document.get("found_count"),
            "requested_count": len(as_list(task_document.get("requested_ids"))),
            "source_link_count": task_document.get("source_link_count"),
            "missing_count": len(as_list(task_document.get("missing"))),
        }
    expected = coverage.get("expected_total") or coverage.get("total_tasks")
    found = coverage.get("found_count")
    requested = coverage.get("requested_count")
    complete_by_reconciliation = (
        found is not None
        and requested is not None
        and int(found) == int(requested) == len(tasks)
        and int(coverage.get("missing_count") or 0) == 0
    )
    if not complete_by_reconciliation and (expected is None or int(expected or 0) > len(tasks)):
        warnings.append("task snapshot coverage is partial or unproven; absent tasks will be untouched")
    fields_input = field_document.get("fields", field_document) if isinstance(field_document, dict) else {}
    if isinstance(fields_input, list):
        fields = {
            str(item["id"]): item
            for item in fields_input
            if isinstance(item, dict) and item.get("id") not in (None, "")
        }
    elif isinstance(fields_input, dict):
        fields = fields_input
    else:
        raise ContractError("field definitions must be an object or list")
    global_views = (
        as_list(global_view_document.get("views"))
        if isinstance(global_view_document, dict)
        else []
    )
    if any(not isinstance(item, dict) or not item.get("name") for item in global_views):
        raise ContractError("global view definitions must contain named objects")
    global_views = [dict(item) for item in global_views]
    if source_view_document is not None:
        source_rows = as_list(source_view_document.get("views"))
        source_url_by_name: dict[str, str] = {}
        for item in source_rows:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ContractError("source UI view entries require a name")
            url = item.get("url")
            if not isinstance(url, str) or not url:
                raise ContractError(f"source UI view {item['name']} requires a url")
            if item["name"] in source_url_by_name:
                raise ContractError(f"duplicate source UI view name: {item['name']}")
            source_url_by_name[item["name"]] = url
        global_names = {str(item["name"]) for item in global_views}
        if set(source_url_by_name) != global_names:
            raise ContractError(
                "source UI/global view coverage mismatch: "
                f"{len(global_names - set(source_url_by_name))} missing, "
                f"{len(set(source_url_by_name) - global_names)} extra"
            )
        for item in global_views:
            item["sourceUrl"] = source_url_by_name[str(item["name"])]
    return SnapshotPlan(hierarchy, tasks, status_specs, fields, global_views, warnings, coverage)


def epoch_ms_to_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = int(str(value))
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def epoch_ms_to_date(value: Any, zone_name: str, has_time: bool = False) -> str | None:
    instant = epoch_ms_to_datetime(value)
    if instant is None:
        return None
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    # ClickUp encodes date-only values at UTC midnight.  Applying the workspace
    # offset would incorrectly move them to the previous calendar day.
    if not has_time:
        return parsed.date().isoformat()
    return parsed.astimezone(resolve_timezone(zone_name)).date().isoformat()


def resolve_timezone(zone_name: str):
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        # Portability fallback for Windows Python builds without tzdata.
        # Brazil has not observed daylight saving time since 2019.
        if zone_name == "America/Sao_Paulo":
            return timezone(timedelta(hours=-3), name="America/Sao_Paulo")
        raise ContractError(f"time zone data is unavailable for {zone_name}")


def rich_text_payload(task_id: str, text: Any) -> dict[str, str] | None:
    if not isinstance(text, str) or not text:
        return None
    paragraphs = text.splitlines() or [text]
    blocks = []
    for index, paragraph in enumerate(paragraphs):
        blocks.append(
            {
                "id": deterministic_uuid("task-body", task_id, index),
                "type": "paragraph",
                "props": {
                    "textColor": "default",
                    "backgroundColor": "default",
                    "textAlignment": "left",
                },
                "content": ([{"type": "text", "text": paragraph, "styles": {}}] if paragraph else []),
                "children": [],
            }
        )
    return {"blocknote": json.dumps(blocks, ensure_ascii=False), "markdown": text}


def normalized_clickup_link_key(url: str) -> tuple[str, str] | None:
    """Return a stable host/path key only for links owned by ClickUp."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"app.clickup.com", "www.app.clickup.com"}:
        return None
    return host.removeprefix("www."), parsed.path.rstrip("/") or "/"


def build_native_link_resolver(
    plan: SnapshotPlan,
    task_records: dict[str, dict[str, Any]],
) -> Callable[[str], str]:
    """Map known ClickUp task/list/view links to deterministic Twenty routes.

    Unknown ClickUp URLs and all external URLs are returned byte-for-byte.  The
    original source URL and raw Quill snapshot remain stored in their dedicated
    source fields; this resolver affects only the native body projection.
    """
    exact_paths: dict[tuple[str, str], str] = {}
    for spec in plan.global_views:
        source_url = spec.get("sourceUrl")
        if not isinstance(source_url, str) or not source_url:
            continue
        source_key = normalized_clickup_link_key(source_url)
        if source_key is None:
            raise ContractError(f"global view source URL is not a ClickUp URL: {spec['name']}")
        stable_key = f"global:{spec.get('key') or stable_hash(str(spec['name']))}"
        exact_paths[source_key] = (
            f"/objects/tasks?viewId={deterministic_uuid('view', stable_key)}"
        )

    list_ids = {item.external_id for item in plan.hierarchy.lists}
    task_target_by_external_id = {
        str(external_id): f"/object/task/{record['id']}"
        for external_id, record in task_records.items()
        if isinstance(record, dict) and record.get("id")
    }

    def resolve(url: str) -> str:
        key = normalized_clickup_link_key(url)
        if key is None:
            return url
        if key in exact_paths:
            return exact_paths[key]
        _, path = key
        segments = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if "t" in segments:
            task_id = segments[-1] if segments else ""
            target = task_target_by_external_id.get(task_id)
            if target:
                return target
        if "li" in segments:
            marker_index = segments.index("li")
            list_id = segments[marker_index + 1] if marker_index + 1 < len(segments) else ""
            if list_id in list_ids:
                view_id = deterministic_uuid("view", f"list:{list_id}:table")
                return f"/objects/tasks?viewId={view_id}"
        return url

    return resolve


def quill_body_payload(
    task_id: str,
    content: Any,
    fallback_text: Any,
    link_resolver: Callable[[str], str] | None = None,
) -> dict[str, str] | None:
    if content in (None, ""):
        return rich_text_payload(task_id, fallback_text)
    try:
        delta = json.loads(content) if isinstance(content, str) else content
        ops = delta.get("ops") if isinstance(delta, dict) else None
        if not isinstance(ops, list):
            raise ValueError("missing ops")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ContractError(f"invalid Quill content for task {task_id}") from exc

    blocks: list[dict[str, Any]] = []
    inline: list[dict[str, Any]] = []
    markdown_inline: list[str] = []
    markdown_lines: list[str] = []

    def flush(attributes: dict[str, Any]) -> None:
        nonlocal inline, markdown_inline
        list_kind = attributes.get("list")
        if isinstance(list_kind, dict):
            list_kind = list_kind.get("list")
        header = attributes.get("header")
        if header:
            block_type = "heading"
            props = {"level": max(1, min(3, int(header)))}
            prefix = "#" * props["level"] + " "
        elif list_kind == "bullet":
            block_type, props, prefix = "bulletListItem", {}, "- "
        elif list_kind == "ordered":
            block_type, props, prefix = "numberedListItem", {}, "1. "
        else:
            block_type, props, prefix = "paragraph", {}, ""
        blocks.append(
            {
                "id": deterministic_uuid("task-body", task_id, len(blocks)),
                "type": block_type,
                "props": props,
                "content": inline,
                "children": [],
            }
        )
        markdown_lines.append(prefix + "".join(markdown_inline))
        inline, markdown_inline = [], []

    for op in ops:
        if not isinstance(op, dict):
            continue
        inserted = op.get("insert")
        if isinstance(inserted, dict):
            embed_type = next(iter(inserted), "embed")
            embed_value = inserted.get(embed_type)
            if embed_type == "divider":
                inline.append({"type": "text", "text": "────────", "styles": {}})
                markdown_inline.append("---")
            else:
                url = embed_value if isinstance(embed_value, str) else None
                if isinstance(embed_value, dict):
                    url = embed_value.get("url") or embed_value.get("src")
                label = "Imagem da origem" if embed_type == "image" else "Conteúdo incorporado"
                node = {"type": "text", "text": label, "styles": {}}
                if isinstance(url, str) and url:
                    if link_resolver is not None:
                        url = link_resolver(url)
                    inline.append({"type": "link", "href": url, "content": [node]})
                    markdown_inline.append(f"[{label}]({url})")
                else:
                    inline.append(node)
                    markdown_inline.append(f"[{label} preservado no snapshot]")
            flush({})
            continue
        if not isinstance(inserted, str):
            continue
        attributes = op.get("attributes") if isinstance(op.get("attributes"), dict) else {}
        parts = inserted.split("\n")
        for index, segment in enumerate(parts):
            if segment:
                styles = {
                    key: True
                    for key in ("bold", "italic", "underline", "strike")
                    if attributes.get(key) is True
                }
                text_node: dict[str, Any] = {"type": "text", "text": segment, "styles": styles}
                link = attributes.get("link")
                if isinstance(link, str) and link:
                    if link_resolver is not None:
                        link = link_resolver(link)
                    inline.append({"type": "link", "href": link, "content": [text_node]})
                    rendered = f"[{segment}]({link})"
                else:
                    inline.append(text_node)
                    rendered = segment
                if attributes.get("bold"):
                    rendered = f"**{rendered}**"
                if attributes.get("italic"):
                    rendered = f"*{rendered}*"
                markdown_inline.append(rendered)
            if index < len(parts) - 1:
                flush(attributes)
    if inline or markdown_inline:
        flush({})
    if not blocks:
        return rich_text_payload(task_id, fallback_text)
    return {
        "blocknote": json.dumps(blocks, ensure_ascii=False),
        "markdown": "\n".join(markdown_lines),
    }


def canonical(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if item == "":
            return None
        if isinstance(item, str):
            stripped = item.strip()
            if not stripped:
                return None
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, (dict, list)):
                    return normalize(decoded)
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                if parsed.tzinfo:
                    return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
            return stripped
        if isinstance(item, dict):
            return {key: normalize(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [normalize(value) for value in item]
        return item

    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def summarize_value(value: Any, options: Any = None) -> str:
    option_map: dict[str, str] = {}
    for option in as_list(options):
        if isinstance(option, dict):
            option_id = option.get("id") or option.get("value")
            label = option.get("name") or option.get("label") or option.get("value")
            if option_id is not None and label is not None:
                option_map[str(option_id)] = str(label)
    if isinstance(value, list):
        return ", ".join(option_map.get(str(item), str(item)) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return option_map.get(str(value), str(value)) if value is not None else ""


def number_or_none(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def currency_or_none(value: Any, currency: str) -> dict[str, Any] | None:
    number = number_or_none(value)
    if number is None:
        return None
    return {"amountMicros": int(round(float(number) * 1_000_000)), "currencyCode": currency}


def summarize_collection(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                label = (
                    item.get("name")
                    or item.get("username")
                    or item.get("url")
                    or item.get("id")
                    or item.get("task_id")
                )
                parts.append(str(label) if label is not None else json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return ", ".join(parts) or None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def extract_parent_id(task: dict[str, Any]) -> str | None:
    for key in ("direct_parent", "parent", "parent_id"):
        value = task.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("task_id") or value.get("taskId")
        if value not in (None, ""):
            return str(value)
    return None


def build_parent_links(tasks: list[dict[str, Any]]) -> dict[str, str]:
    links: dict[str, str] = {}
    for task in tasks:
        task_id = str(task["id"])
        parent_id = extract_parent_id(task)
        if parent_id:
            links[task_id] = parent_id
        for child_id in extract_reference_ids(task.get("subtask_ids")):
            existing = links.get(child_id)
            if existing is not None and existing != task_id:
                raise ContractError(f"subtask {child_id} has conflicting parents")
            links[child_id] = task_id
    return links


def extract_reference_ids(value: Any) -> list[str]:
    if isinstance(value, (int, float)):
        return []
    result: list[str] = []
    for item in as_list(value):
        if isinstance(item, str) and item:
            result.append(item)
        elif isinstance(item, dict):
            candidate = item.get("id") or item.get("task_id") or item.get("taskId")
            if candidate not in (None, ""):
                result.append(str(candidate))
    return result


def build_dependency_edges(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = str(task["id"])
        dependencies = task.get("dependencies")
        if isinstance(dependencies, dict):
            for target in extract_reference_ids(dependencies.get("dependsOn")):
                edge_id = f"DEPENDS_ON:{task_id}:{target}"
                edges[edge_id] = {"externalId": edge_id, "from": task_id, "to": target, "type": "DEPENDS_ON"}
            for dependent in extract_reference_ids(dependencies.get("dependedBy")):
                edge_id = f"DEPENDS_ON:{dependent}:{task_id}"
                edges[edge_id] = {"externalId": edge_id, "from": dependent, "to": task_id, "type": "DEPENDS_ON"}
        for related in extract_reference_ids(task.get("related_tasks")):
            left, right = sorted((task_id, related))
            edge_id = f"RELATED:{left}:{right}"
            edges[edge_id] = {"externalId": edge_id, "from": left, "to": right, "type": "RELATED"}
    return list(edges.values())


def unwrap_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ApiError("unexpected list response")
    data = value.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for inner in data.values():
            if isinstance(inner, list):
                return inner
    for key in ("objects", "fields", "views", "viewFilters", "viewFields", "viewGroups"):
        if isinstance(value.get(key), list):
            return value[key]
    raise ApiError("unexpected list response")


def unwrap_one(value: Any) -> dict[str, Any]:
    current = value
    while isinstance(current, dict):
        data = current.get("data")
        if isinstance(data, dict) and len(data) == 1:
            current = next(iter(data.values()))
            continue
        break
    if not isinstance(current, dict):
        raise ApiError("unexpected record response")
    return current


class TwentyClient:
    def __init__(self, base_url: str, token: str, *, write_delay: float = 0.75):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # Twenty enforces a workspace API-key budget across reads and writes.
        # Pace every attempt (not only successful writes) below 100 req/min.
        self.request_interval = write_delay
        self.last_request_at: float | None = None
        self.request_count = 0
        self.write_count = 0

    def request(
        self,
        path: str,
        payload: Any = None,
        method: str = "GET",
        attempts: int | None = None,
        *,
        retry_safe: bool | None = None,
        count_write: bool | None = None,
    ) -> Any:
        method = method.upper()
        if retry_safe is None:
            retry_safe = method == "GET"
        if attempts is None:
            attempts = 5 if retry_safe else 1
        if attempts > 1 and not retry_safe:
            raise ValueError("multiple attempts require retry_safe=True")
        if count_write is None:
            count_write = method != "GET"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        for attempt in range(attempts):
            if self.last_request_at is not None and self.request_interval > 0:
                remaining = self.request_interval - (time.monotonic() - self.last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            self.last_request_at = time.monotonic()
            self.request_count += 1
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    raw = response.read()
                if count_write:
                    self.write_count += 1
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", "replace")
                try:
                    decoded = json.loads(response_body)
                    message = (
                        decoded.get("message")
                        or decoded.get("messages")
                        or decoded.get("summary")
                        or decoded.get("errors")
                        or decoded.get("error")
                        or f"HTTP {exc.code}"
                    )
                except json.JSONDecodeError:
                    message = f"HTTP {exc.code}"
                retryable = exc.code in (429, 502, 503, 504)
                if not retryable or attempt + 1 == attempts:
                    raise ApiError(str(message)) from exc
            except urllib.error.URLError as exc:
                if attempt + 1 == attempts:
                    raise ApiError(f"connection failed: {exc.reason}") from exc
            time.sleep(min(2**attempt, 16))
        raise ApiError("request failed")

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        is_read = query.lstrip().startswith("query")
        result = self.request(
            "/metadata",
            {"query": query, "variables": variables or {}},
            "POST",
            retry_safe=is_read,
            count_write=not is_read,
        )
        if result.get("errors"):
            messages = [str(item.get("message", "GraphQL error")) for item in result["errors"]]
            raise ApiError("; ".join(messages))
        return result.get("data") or {}

    def list_rest(self, path: str, *, limit: int = 200) -> list[dict[str, Any]]:
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

    def preflight(self, expected_workspace_id: str) -> dict[str, Any]:
        data = self.graphql(
            """
            query ImporterPreflight {
              currentWorkspace { id displayName activationStatus isPublicInviteLinkEnabled }
            }
            """
        )
        workspace = data.get("currentWorkspace")
        if not isinstance(workspace, dict):
            raise ApiError("preflight could not read current Twenty workspace")
        if workspace.get("id") != expected_workspace_id:
            raise ApiError("preflight target Twenty workspace id mismatch")
        if workspace.get("activationStatus") != "ACTIVE":
            raise ApiError("preflight target Twenty workspace is not ACTIVE")
        members = self.list_rest("/rest/workspaceMembers", limit=20)
        if len(members) != 1:
            raise ApiError(f"preflight requires exactly one workspace member; found {len(members)}")
        if workspace.get("isPublicInviteLinkEnabled") is not False:
            raise ApiError("preflight requires the public workspace invite link to be disabled")
        return {
            "workspace_id": workspace["id"],
            "workspace_name": workspace.get("displayName"),
            "member_count": len(members),
            "public_invite_disabled": True,
        }


LIST_FIELD_SPECS = (
    ("importerKey", "Chave do importador", "TEXT", True),
    ("isSourceActive", "Ativo na origem", "BOOLEAN", False),
    ("externalId", "ID ClickUp", "TEXT", True),
    ("spaceExternalId", "ID do espaço ClickUp", "TEXT", False),
    ("spaceName", "Espaço ClickUp", "TEXT", False),
    ("folderExternalId", "ID da pasta ClickUp", "TEXT", False),
    ("folderName", "Pasta ClickUp", "TEXT", False),
    ("sourcePosition", "Posição na origem", "NUMBER", False),
    ("sourceSnapshot", "Snapshot da lista", "RAW_JSON", False),
)

TASK_SIMPLE_FIELD_SPECS = (
    ("clickUpImporterKey", "Chave do importador ClickUp", "TEXT", True),
    ("clickUpId", "ID ClickUp", "TEXT", True),
    ("clickUpListExternalId", "ID da lista ClickUp", "TEXT", False),
    ("clickUpStatusExternalId", "ID do status ClickUp", "TEXT", False),
    ("clickUpStatusLabel", "Status original ClickUp", "TEXT", False),
    ("clickUpPriority", "Prioridade ClickUp", "TEXT", False),
    ("clickUpParentExternalId", "ID da tarefa pai ClickUp", "TEXT", False),
    ("clickUpSourceUrl", "URL ClickUp", "TEXT", False),
    ("clickUpStartOn", "Início ClickUp", "DATE", False),
    ("clickUpStartAt", "Início com horário ClickUp", "DATE_TIME", False),
    ("clickUpDueOn", "Prazo ClickUp", "DATE", False),
    ("clickUpCreatedAt", "Criada no ClickUp", "DATE_TIME", False),
    ("clickUpUpdatedAt", "Atualizada no ClickUp", "DATE_TIME", False),
    ("clickUpClosedAt", "Encerrada no ClickUp", "DATE_TIME", False),
    ("clickUpTimeEstimateMs", "Estimativa ClickUp (ms)", "NUMBER", False),
    ("clickUpTimeSpentMs", "Tempo registrado ClickUp (ms)", "NUMBER", False),
    ("clickUpIsPrivate", "Privada no ClickUp", "BOOLEAN", False),
    ("clickUpIsArchived", "Arquivada no ClickUp", "BOOLEAN", False),
    ("clickUpIsDeleted", "Excluída na origem", "BOOLEAN", False),
    ("clickUpCustomFieldsSummary", "Campos ClickUp", "TEXT", False),
    ("clickUpDependenciesSummary", "Dependências ClickUp", "TEXT", False),
    ("clickUpCommentsSummary", "Comentários ClickUp", "TEXT", False),
    ("clickUpAssigneesSummary", "Responsáveis ClickUp", "TEXT", False),
    ("clickUpAttachmentsSummary", "Anexos ClickUp", "TEXT", False),
    ("clickUpSubtaskIds", "IDs das subtarefas ClickUp", "RAW_JSON", False),
    ("clickUpCustomFieldsRaw", "Campos personalizados brutos", "RAW_JSON", False),
    ("clickUpDependenciesRaw", "Dependências brutas", "RAW_JSON", False),
    ("clickUpRelatedTasksRaw", "Tarefas relacionadas brutas", "RAW_JSON", False),
    ("clickUpAttachmentsRaw", "Anexos brutos", "RAW_JSON", False),
    ("clickUpCommentsRaw", "Comentários brutos", "RAW_JSON", False),
    ("clickUpAssigneesRaw", "Responsáveis brutos", "RAW_JSON", False),
    ("clickUpSourceSnapshot", "Snapshot bruto ClickUp", "RAW_JSON", False),
)

DEPENDENCY_FIELD_SPECS = (
    ("importerKey", "Chave do importador", "TEXT", True),
    ("isSourceActive", "Ativo na origem", "BOOLEAN", False),
    ("externalId", "ID externo", "TEXT", True),
    ("relationType", "Tipo de vínculo", "SELECT", False),
    ("fromTaskExternalId", "ID da tarefa dependente", "TEXT", False),
    ("toTaskExternalId", "ID da tarefa requerida", "TEXT", False),
    ("sourceSnapshot", "Snapshot do vínculo", "RAW_JSON", False),
)

FIELD_VALUE_FIELD_SPECS = (
    ("importerKey", "Chave do importador", "TEXT", True),
    ("isSourceActive", "Ativo na origem", "BOOLEAN", False),
    ("externalId", "ID externo", "TEXT", True),
    ("taskExternalId", "ID da tarefa ClickUp", "TEXT", False),
    ("fieldExternalId", "ID do campo ClickUp", "TEXT", False),
    ("fieldName", "Campo ClickUp", "TEXT", False),
    ("fieldTypeCode", "Tipo do campo ClickUp", "NUMBER", False),
    ("valueText", "Valor legível", "TEXT", False),
    ("rawValue", "Valor bruto", "RAW_JSON", False),
)


class TwentyImporter:
    def __init__(self, client: TwentyClient, plan: SnapshotPlan, *, zone_name: str):
        self.client = client
        self.plan = plan
        self.zone_name = zone_name
        self.changes: Counter[str] = Counter()
        self.runtime_warnings: list[str] = []
        self.objects_by_name: dict[str, dict[str, Any]] = {}
        self.dynamic_fields: dict[str, dict[str, Any]] = {}
        self.global_view_records: list[dict[str, Any]] = []

    def refresh_objects(self) -> None:
        objects = self.client.list_rest("/rest/metadata/objects", limit=500)
        self.objects_by_name = {item["nameSingular"]: item for item in objects}

    def ensure_object(
        self,
        name_singular: str,
        name_plural: str,
        label_singular: str,
        label_plural: str,
        icon: str,
        *,
        searchable: bool = True,
    ) -> dict[str, Any]:
        current = self.objects_by_name.get(name_singular)
        if current:
            if current.get("description") != IMPORTER_MARKER:
                raise ApiError(
                    f"refusing unowned object metadata collision: {name_singular}"
                )
            if bool(current.get("isSearchable")) != searchable:
                updated = unwrap_one(
                    self.client.request(
                        f"/rest/metadata/objects/{current['id']}",
                        {"isSearchable": searchable},
                        "PATCH",
                    )
                )
                current = {**current, **updated}
                self.objects_by_name[name_singular] = current
                self.changes["objects_updated"] += 1
            return current
        payload = {
            "nameSingular": name_singular,
            "namePlural": name_plural,
            "labelSingular": label_singular,
            "labelPlural": label_plural,
            "icon": icon,
            "description": IMPORTER_MARKER,
            "isSearchable": searchable,
        }
        created = unwrap_one(self.client.request("/rest/metadata/objects", payload, "POST"))
        self.changes["objects_created"] += 1
        self.objects_by_name[name_singular] = created
        return created

    def ensure_field(
        self,
        object_metadata: dict[str, Any],
        name: str,
        label: str,
        field_type: str,
        *,
        unique: bool = False,
        options: list[dict[str, Any]] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = next(
            (field for field in object_metadata.get("fields", []) if field.get("name") == name),
            None,
        )
        if current:
            if current.get("description") != IMPORTER_MARKER:
                raise ApiError(
                    f"refusing unowned field metadata collision: "
                    f"{object_metadata.get('nameSingular')}.{name}"
                )
            if current.get("type") != field_type:
                raise ApiError(
                    f"field type mismatch for {object_metadata.get('nameSingular')}.{name}: "
                    f"expected {field_type}, found {current.get('type')}"
                )
            if bool(current.get("isUnique")) != bool(unique):
                raise ApiError(
                    f"field uniqueness mismatch for {object_metadata.get('nameSingular')}.{name}"
                )
            update: dict[str, Any] = {}
            if options is not None:
                merged = self.merge_owned_options(current.get("options") or [], options)
                if canonical(current.get("options") or []) != canonical(merged):
                    update["options"] = merged
            if settings is not None and canonical(current.get("settings")) != canonical(settings):
                update["settings"] = settings
            if update:
                updated = unwrap_one(
                    self.client.request(
                        f"/rest/metadata/fields/{current['id']}",
                        update,
                        "PATCH",
                    )
                )
                current = {**current, **updated}
                self.changes["fields_updated"] += 1
            return current
        payload: dict[str, Any] = {
            "objectMetadataId": object_metadata["id"],
            "type": field_type,
            "name": name,
            "label": label,
            "isNullable": True,
            "isUnique": unique,
            "isActive": True,
            "description": IMPORTER_MARKER,
        }
        if field_type == "BOOLEAN":
            payload["defaultValue"] = False
        if options is not None:
            payload["options"] = options
        if settings is not None:
            payload["settings"] = settings
        created = unwrap_one(self.client.request("/rest/metadata/fields", payload, "POST"))
        self.changes["fields_created"] += 1
        return created

    @staticmethod
    def merge_owned_options(
        existing: list[dict[str, Any]], desired: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        existing_id_by_value = {item.get("value"): item.get("id") for item in existing}
        merged = []
        for position, item in enumerate(desired):
            merged.append(
                {
                    **item,
                    "id": existing_id_by_value.get(item.get("value")) or item["id"],
                    "position": position,
                }
            )
        return merged

    def ensure_relation(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        name: str,
        label: str,
        target_label: str,
        *,
        inverse_name: str | None = None,
    ) -> dict[str, Any] | None:
        relation_marker = f"{IMPORTER_MARKER};target={target['id']};type=MANY_TO_ONE"
        current = next(
            (field for field in source.get("fields", []) if field.get("name") == name),
            None,
        )
        if current:
            if current.get("description") != relation_marker or current.get("type") != "RELATION":
                raise ApiError(
                    f"refusing unowned relation metadata collision: "
                    f"{source.get('nameSingular')}.{name}"
                )
            relation_type = (current.get("settings") or {}).get("relationType")
            if relation_type not in {"MANY_TO_ONE", None}:
                raise ApiError(
                    f"relation type mismatch for {source.get('nameSingular')}.{name}"
                )
            return current
        payload: dict[str, Any] = {
            "objectMetadataId": source["id"],
            "type": "RELATION",
            "name": name,
            "label": label,
            "isNullable": True,
            "isActive": True,
            "description": relation_marker,
            "relationCreationPayload": {
                "type": "MANY_TO_ONE",
                "targetObjectMetadataId": target["id"],
                "targetFieldLabel": target_label,
                "targetFieldIcon": "IconLink",
            },
        }
        if inverse_name:
            payload["relationCreationPayload"]["targetFieldName"] = inverse_name
        created = unwrap_one(self.client.request("/rest/metadata/fields", payload, "POST"))
        self.changes["relations_created"] += 1
        return created

    def ensure_schema(self) -> None:
        self.refresh_objects()
        clickup_list = self.ensure_object(
            "clickUpList",
            "clickUpLists",
            "Lista ClickUp",
            "Listas ClickUp",
            "IconList",
            searchable=False,
        )
        clickup_dependency = self.ensure_object(
            "clickUpDependency",
            "clickUpDependencies",
            "Dependência ClickUp",
            "Dependências ClickUp",
            "IconArrowsSplit",
            searchable=False,
        )
        clickup_field_value = self.ensure_object(
            "clickUpFieldValue",
            "clickUpFieldValues",
            "Valor de campo ClickUp",
            "Valores de campos ClickUp",
            "IconForms",
            searchable=False,
        )
        self.refresh_objects()
        task = self.objects_by_name.get("task")
        if task is None:
            raise ApiError("native Task metadata was not found")
        clickup_list = self.objects_by_name["clickUpList"]
        clickup_dependency = self.objects_by_name["clickUpDependency"]
        clickup_field_value = self.objects_by_name["clickUpFieldValue"]

        for name, label, field_type, unique in LIST_FIELD_SPECS:
            self.ensure_field(clickup_list, name, label, field_type, unique=unique)
        for name, label, field_type, unique in TASK_SIMPLE_FIELD_SPECS:
            self.ensure_field(task, name, label, field_type, unique=unique)
        unique_statuses: dict[str, StatusSpec] = {}
        for item in self.plan.status_specs:
            unique_statuses.setdefault(item.value, item)
        status_options = [
            {
                "id": item.option_id,
                "value": item.value,
                "label": item.label,
                "position": position,
                "color": item.color,
            }
            for position, item in enumerate(unique_statuses.values())
        ]
        if not status_options:
            status_options = [
                {
                    "id": deterministic_uuid("status-option", "unknown"),
                    "value": "CLICKUP_STATUS_UNKNOWN",
                    "label": "Status ClickUp desconhecido",
                    "position": 0,
                    "color": "gray",
                }
            ]
        self.ensure_field(
            task,
            "clickUpStatus",
            "Status ClickUp",
            "SELECT",
            options=status_options,
        )
        self.ensure_dynamic_task_fields(task)
        for name, label, field_type, unique in DEPENDENCY_FIELD_SPECS:
            options = None
            if name == "relationType":
                options = [
                    {
                        "id": deterministic_uuid("dependency-type", value),
                        "value": value,
                        "label": label_value,
                        "position": position,
                        "color": color,
                    }
                    for position, (value, label_value, color) in enumerate(
                        (
                            ("DEPENDS_ON", "Depende de", "orange"),
                            ("RELATED", "Relacionada", "blue"),
                        )
                    )
                ]
            self.ensure_field(
                clickup_dependency,
                name,
                label,
                field_type,
                unique=unique,
                options=options,
            )
        for name, label, field_type, unique in FIELD_VALUE_FIELD_SPECS:
            self.ensure_field(clickup_field_value, name, label, field_type, unique=unique)

        self.refresh_objects()
        task = self.objects_by_name["task"]
        clickup_list = self.objects_by_name["clickUpList"]
        clickup_dependency = self.objects_by_name["clickUpDependency"]
        clickup_field_value = self.objects_by_name["clickUpFieldValue"]
        self.ensure_relation(task, clickup_list, "clickUpList", "Lista ClickUp", "Tarefas ClickUp")
        self.ensure_relation(
            task, task, "clickUpParentTask", "Tarefa pai ClickUp", "Subtarefas ClickUp",
            inverse_name="clickUpSubtasks",
        )
        self.ensure_relation(
            clickup_dependency, task, "fromTask", "Tarefa dependente", "Dependências de saída ClickUp"
        )
        self.ensure_relation(
            clickup_dependency, task, "toTask", "Tarefa requerida", "Dependências de entrada ClickUp"
        )
        self.ensure_relation(
            clickup_field_value, task, "task", "Tarefa", "Valores de campos ClickUp"
        )
        self.refresh_objects()

    def ensure_dynamic_task_fields(self, task: dict[str, Any]) -> None:
        """Create editable Twenty fields from private ClickUp definitions."""
        for external_id, definition in sorted(self.plan.field_definitions.items()):
            source_type = str(definition.get("type") or "short_text").lower()
            field_name = f"clickUpCf{stable_hash(external_id, 12)}"
            label = str(definition.get("name") or f"Campo ClickUp {external_id}")
            options: list[dict[str, Any]] | None = None
            option_value_by_id: dict[str, str] = {}
            if source_type in {"drop_down", "dropdown", "select"}:
                field_type = "SELECT"
                options = []
                source_options = as_list((definition.get("type_config") or {}).get("options"))
                for position, source_option in enumerate(source_options):
                    if not isinstance(source_option, dict):
                        continue
                    option_id = str(
                        source_option.get("id")
                        or source_option.get("value")
                        or stable_hash(str(source_option), 20)
                    )
                    value = f"O_{stable_hash(external_id + ':' + option_id)}"
                    option_value_by_id[option_id] = value
                    options.append(
                        {
                            "id": deterministic_uuid("custom-field-option", external_id, option_id),
                            "value": value,
                            "label": str(
                                source_option.get("name")
                                or source_option.get("label")
                                or source_option.get("value")
                                or option_id
                            ),
                            "position": position,
                            "color": normalize_color(
                                source_option.get("color"), f"{external_id}:{option_id}"
                            ),
                        }
                    )
                if not options:
                    options = [
                        {
                            "id": deterministic_uuid("custom-field-option", external_id, "unknown"),
                            "value": "OPTION_UNKNOWN",
                            "label": "Opção desconhecida",
                            "position": 0,
                            "color": "gray",
                        }
                    ]
            elif source_type in {"currency", "money"}:
                field_type = "CURRENCY"
            elif source_type in {"date", "date_time", "datetime"}:
                # ClickUp exposes a per-value time flag. The source fields in
                # this snapshot are business dates, so a DATE preserves them
                # without inventing midnight in another time zone.
                field_type = "DATE"
            elif source_type in {"url", "link"}:
                field_type = "LINKS"
            elif source_type in {"checkbox", "boolean"}:
                field_type = "BOOLEAN"
            elif source_type in {"number", "rating", "progress"}:
                field_type = "NUMBER"
            elif source_type in {"labels", "multi_select", "multiselect"}:
                field_type = "MULTI_SELECT"
                options = []
                for position, source_option in enumerate(
                    as_list((definition.get("type_config") or {}).get("options"))
                ):
                    if not isinstance(source_option, dict):
                        continue
                    option_id = str(source_option.get("id") or source_option.get("value"))
                    value = f"O_{stable_hash(external_id + ':' + option_id)}"
                    option_value_by_id[option_id] = value
                    options.append(
                        {
                            "id": deterministic_uuid("custom-field-option", external_id, option_id),
                            "value": value,
                            "label": str(source_option.get("name") or source_option.get("value") or option_id),
                            "position": position,
                            "color": normalize_color(source_option.get("color"), option_id),
                        }
                    )
            else:
                field_type = "TEXT"
            self.ensure_field(
                task,
                field_name,
                label,
                field_type,
                options=options,
                settings=(
                    {"format": "full", "decimals": 2}
                    if field_type == "CURRENCY"
                    else None
                ),
            )
            self.dynamic_fields[external_id] = {
                "name": field_name,
                "type": field_type,
                "source_type": source_type,
                "option_value_by_id": option_value_by_id,
                "currency": str(
                    (definition.get("type_config") or {}).get("currency_type") or "BRL"
                ),
            }

    def list_object_records(self, plural: str) -> list[dict[str, Any]]:
        return self.client.list_rest(f"/rest/{plural}", limit=200)

    def upsert_owned_records(
        self,
        plural: str,
        key_field: str,
        desired: list[dict[str, Any]],
        owned_fields: set[str],
        *,
        marker_field: str,
        deactivate_missing: bool = False,
    ) -> dict[str, dict[str, Any]]:
        desired_by_key: dict[str, dict[str, Any]] = {}
        for payload in desired:
            key = str(payload[key_field])
            if key in desired_by_key:
                raise ApiError(f"duplicate desired {plural}.{key_field}: {key}")
            desired_by_key[key] = payload

        existing_rows = self.list_object_records(plural)
        by_key: dict[str, dict[str, Any]] = {}
        for row in existing_rows:
            key = row.get(key_field)
            if key in (None, ""):
                continue
            key = str(key)
            if key in by_key:
                raise ApiError(f"duplicate {plural}.{key_field}: {key}")
            expected_marker = deterministic_uuid("record", plural, key)
            if row.get(marker_field) != expected_marker:
                raise ApiError(f"refusing unowned record collision: {plural}.{key_field}={key}")
            by_key[key] = row

        # Helper rows have no user-authored payload.  Twenty v2.45 exposes an
        # official atomic batch upsert for them; using it only for absent rows
        # preserves the ownership checks above and cuts thousands of ordinary
        # single-record round trips.  A lost response is never retried here: a
        # later run first GETs/reconciles the committed rows by unique key.
        if plural in {"clickUpFieldValues", "clickUpDependencies"}:
            missing = [
                payload
                for key, payload in desired_by_key.items()
                if key not in by_key
            ]
            for offset in range(0, len(missing), 200):
                chunk = missing[offset : offset + 200]
                response = self.client.request(
                    f"/rest/batch/{plural}?upsert=true",
                    chunk,
                    "POST",
                )
                created_rows = unwrap_list(response)
                returned_by_key: dict[str, dict[str, Any]] = {}
                for row in created_rows:
                    key = str(row.get(key_field) or "")
                    if key not in {str(item[key_field]) for item in chunk}:
                        raise ApiError(f"batch {plural} returned an unexpected key")
                    if row.get(marker_field) != deterministic_uuid("record", plural, key):
                        raise ApiError(f"batch {plural} returned an unowned record")
                    if key in returned_by_key:
                        raise ApiError(f"batch {plural} returned a duplicate key: {key}")
                    returned_by_key[key] = row
                expected_keys = {str(item[key_field]) for item in chunk}
                if set(returned_by_key) != expected_keys:
                    raise ApiError(
                        f"batch {plural} response coverage mismatch: "
                        f"expected {len(expected_keys)}, received {len(returned_by_key)}"
                    )
                by_key.update(returned_by_key)
                self.changes[f"{plural}_created"] += len(returned_by_key)
        for payload in desired:
            key = str(payload[key_field])
            current = by_key.get(key)
            if current is None:
                created = unwrap_one(self.client.request(f"/rest/{plural}", payload, "POST"))
                by_key[key] = created
                self.changes[f"{plural}_created"] += 1
                continue
            update = {
                field_name: payload.get(field_name)
                for field_name in owned_fields
                if field_name in payload
                and canonical(current.get(field_name)) != canonical(payload.get(field_name))
            }
            if update:
                updated = unwrap_one(
                    self.client.request(f"/rest/{plural}/{current['id']}", update, "PATCH")
                )
                by_key[key] = {**current, **updated}
                self.changes[f"{plural}_updated"] += 1
            else:
                self.changes[f"{plural}_preserved"] += 1
        if deactivate_missing:
            desired_keys = {str(payload[key_field]) for payload in desired}
            for key, current in by_key.items():
                if key in desired_keys or current.get("isSourceActive") is False:
                    continue
                updated = unwrap_one(
                    self.client.request(
                        f"/rest/{plural}/{current['id']}",
                        {"isSourceActive": False},
                        "PATCH",
                    )
                )
                by_key[key] = {**current, **updated}
                self.changes[f"{plural}_deactivated"] += 1
        return by_key

    def import_lists(self) -> dict[str, dict[str, Any]]:
        payloads = [
            {
                "name": item.name,
                "importerKey": deterministic_uuid("record", "clickUpLists", item.external_id),
                "isSourceActive": True,
                "externalId": item.external_id,
                "spaceExternalId": item.space_external_id,
                "spaceName": item.space_name,
                "folderExternalId": item.folder_external_id,
                "folderName": item.folder_name,
                "sourcePosition": item.folder_position,
                "sourceSnapshot": item.raw,
            }
            for item in self.plan.hierarchy.lists
        ]
        owned = set(payloads[0]) if payloads else set()
        return self.upsert_owned_records(
            "clickUpLists", "externalId", payloads, owned,
            marker_field="importerKey", deactivate_missing=True,
        )

    def status_by_task(self, task: dict[str, Any], list_id: str | None) -> StatusSpec | None:
        value = task.get("task_status")
        if not isinstance(value, dict):
            return None
        status_id = value.get("id") or task.get("status_id")
        label = value.get("status") or task.get("status")
        for candidate in self.plan.status_specs:
            if candidate.list_external_id == list_id and (
                (status_id is not None and candidate.status_external_id == str(status_id))
                or (label is not None and candidate.label == str(label))
            ):
                return candidate
        return None

    @staticmethod
    def normalized_native_status(label: Any) -> str:
        text = str(label or "").strip().casefold()
        if any(token in text for token in ("conclu", "done", "complete", "closed", "encerr")):
            return "DONE"
        if any(token in text for token in ("execu", "progress", "ativo", "active", "revis")):
            return "IN_PROGRESS"
        return "TODO"

    def task_payload(
        self,
        task: dict[str, Any],
        list_records: dict[str, dict[str, Any]],
        link_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        task_id = str(task["id"])
        list_id = get_home_list_id(task)
        status_value = task.get("task_status") if isinstance(task.get("task_status"), dict) else {}
        status_spec = self.status_by_task(task, list_id)
        timing = task.get("timing") if isinstance(task.get("timing"), dict) else {}
        privacy = task.get("privacy") if isinstance(task.get("privacy"), dict) else {}
        states = task.get("states") if isinstance(task.get("states"), dict) else {}
        fields = [item for item in as_list(task.get("fields")) if isinstance(item, dict)]
        definitions = self.plan.field_definitions
        field_lines = []
        for item in fields:
            field_id = str(item.get("field_id") or "")
            definition = definitions.get(field_id, {})
            label = definition.get("name") or field_id
            value = summarize_value(item.get("value"), definition.get("type_config", {}).get("options"))
            field_lines.append(f"{label}: {value}")
        comments = task.get("comments")
        assignees = task.get("assignees", task.get("users"))
        attachments = task.get("attachments")
        dependencies = task.get("dependencies")
        source_url = task.get("url") or task.get("source_url")
        parent_id = extract_parent_id(task)
        body = quill_body_payload(
            task_id,
            task.get("rich_content"),
            task.get("text"),
            link_resolver,
        )
        payload: dict[str, Any] = {
            "title": str(task["name"]),
            "clickUpImporterKey": deterministic_uuid("record", "tasks", task_id),
            "clickUpId": task_id,
            "clickUpListExternalId": list_id,
            "clickUpStatusExternalId": status_value.get("id") or task.get("status_id"),
            "clickUpStatusLabel": status_value.get("status") or task.get("status"),
            "clickUpStatus": status_spec.value if status_spec else None,
            "clickUpPriority": str(task.get("priority")) if task.get("priority") is not None else None,
            "clickUpParentExternalId": parent_id,
            "clickUpSourceUrl": source_url,
            "clickUpStartOn": epoch_ms_to_date(
                timing.get("start_date"), self.zone_name, bool(timing.get("start_date_time"))
            ),
            "clickUpStartAt": epoch_ms_to_datetime(timing.get("start_date")) if timing.get("start_date_time") else None,
            "clickUpDueOn": epoch_ms_to_date(
                timing.get("due_date"), self.zone_name, bool(timing.get("due_date_time"))
            ),
            "clickUpCreatedAt": epoch_ms_to_datetime(timing.get("date_created")),
            "clickUpUpdatedAt": epoch_ms_to_datetime(timing.get("date_updated")),
            "clickUpClosedAt": epoch_ms_to_datetime(timing.get("date_closed")),
            "clickUpTimeEstimateMs": number_or_none(timing.get("time_estimate")),
            "clickUpTimeSpentMs": number_or_none(timing.get("time_spent")),
            "clickUpIsPrivate": bool(privacy.get("private", False)),
            "clickUpIsArchived": bool(states.get("is_archived", False)),
            "clickUpIsDeleted": bool(states.get("is_deleted", False)),
            "clickUpCustomFieldsSummary": "\n".join(field_lines) or None,
            "clickUpDependenciesSummary": summarize_collection(dependencies),
            "clickUpCommentsSummary": summarize_collection(comments),
            "clickUpAssigneesSummary": summarize_collection(assignees),
            "clickUpAttachmentsSummary": summarize_collection(attachments),
            "clickUpSubtaskIds": raw_or_none(as_list(task.get("subtask_ids"))),
            "clickUpCustomFieldsRaw": raw_or_none(fields),
            "clickUpDependenciesRaw": raw_or_none(dependencies),
            "clickUpRelatedTasksRaw": raw_or_none(as_list(task.get("related_tasks"))),
            "clickUpAttachmentsRaw": raw_or_none(as_list(attachments)),
            "clickUpCommentsRaw": raw_or_none(as_list(comments)),
            "clickUpAssigneesRaw": raw_or_none(as_list(assignees)),
            "clickUpSourceSnapshot": task,
            "status": self.normalized_native_status(status_value.get("status") or task.get("status")),
            "clickUpListId": list_records.get(list_id, {}).get("id"),
            "bodyV2": body,
        }
        # A ClickUp date-only value remains a DATE field.  Native dueAt is only
        # populated when the source explicitly carries a time.
        payload["dueAt"] = (
            epoch_ms_to_datetime(timing.get("due_date"))
            if timing.get("due_date_time")
            else None
        )
        for dynamic in self.dynamic_fields.values():
            if dynamic["type"] == "CURRENCY":
                payload[dynamic["name"]] = {
                    "amountMicros": None,
                    "currencyCode": None,
                }
            elif dynamic["type"] == "LINKS":
                payload[dynamic["name"]] = {
                    "primaryLinkUrl": None,
                    "primaryLinkLabel": None,
                    "secondaryLinks": [],
                }
            else:
                payload[dynamic["name"]] = None
        for item in fields:
            external_id = str(item.get("field_id") or "")
            dynamic = self.dynamic_fields.get(external_id)
            if dynamic is None:
                continue
            converted = self.convert_dynamic_value(item, dynamic)
            payload[dynamic["name"]] = converted
        return payload

    def convert_dynamic_value(
        self, item: dict[str, Any], dynamic: dict[str, Any]
    ) -> Any:
        value = item.get("value")
        field_type = dynamic["type"]
        if value in (None, ""):
            return None
        if field_type == "SELECT":
            return dynamic["option_value_by_id"].get(str(value))
        if field_type == "MULTI_SELECT":
            return [
                dynamic["option_value_by_id"][str(option)]
                for option in as_list(value)
                if str(option) in dynamic["option_value_by_id"]
            ]
        if field_type == "CURRENCY":
            return currency_or_none(value, dynamic["currency"])
        if field_type == "DATE":
            return epoch_ms_to_date(value, self.zone_name)
        if field_type == "DATE_TIME":
            return epoch_ms_to_datetime(value)
        if field_type == "LINKS":
            return {
                # Twenty canonicalizes a terminal slash away on LINKS values.
                "primaryLinkUrl": str(value).rstrip("/"),
                "primaryLinkLabel": "",
                "secondaryLinks": [],
            }
        if field_type == "NUMBER":
            return number_or_none(value)
        if field_type == "BOOLEAN":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "sim"}
            return bool(value)
        return summarize_value(value)

    def import_tasks(
        self,
        list_records: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        # Resolve native task links before the ordinary convergence pass when
        # records already exist.  On the first run, a focused post-pass below
        # rewrites only bodies that actually contain newly resolvable links.
        existing_task_records: dict[str, dict[str, Any]] = {}
        for row in self.list_object_records("tasks"):
            external_id = row.get("clickUpId")
            if external_id in (None, ""):
                continue
            external_id = str(external_id)
            if row.get("clickUpImporterKey") != deterministic_uuid(
                "record", "tasks", external_id
            ):
                continue
            existing_task_records[external_id] = row
        initial_link_resolver = build_native_link_resolver(
            self.plan, existing_task_records
        )
        payloads = [
            self.task_payload(task, list_records, initial_link_resolver)
            for task in self.plan.tasks
        ]
        owned = set().union(*(payload.keys() for payload in payloads)) if payloads else set()
        task_records = self.upsert_owned_records(
            "tasks", "clickUpId", payloads, owned, marker_field="clickUpImporterKey"
        )
        parent_by_child = build_parent_links(self.plan.tasks)
        for child_id in (str(task["id"]) for task in self.plan.tasks):
            child = task_records[child_id]
            parent_id = parent_by_child.get(child_id)
            parent = task_records.get(parent_id) if parent_id else None
            if parent_id and parent is None:
                self.runtime_warnings.append(
                    f"unresolved parent link {child_id} -> {parent_id}; external ids were preserved"
                )
                continue
            desired_parent_id = parent["id"] if parent else None
            if child.get("clickUpParentTaskId") != desired_parent_id:
                updated = unwrap_one(
                    self.client.request(
                        f"/rest/tasks/{child['id']}",
                        {"clickUpParentTaskId": desired_parent_id},
                        "PATCH",
                    )
                )
                task_records[child_id] = {**child, **updated}
                self.changes["task_parent_links_updated"] += 1
        complete_link_resolver = build_native_link_resolver(self.plan, task_records)
        for task in self.plan.tasks:
            task_id = str(task["id"])
            current = task_records[task_id]
            desired_body = quill_body_payload(
                task_id,
                task.get("rich_content"),
                task.get("text"),
                complete_link_resolver,
            )
            if canonical(current.get("bodyV2")) == canonical(desired_body):
                continue
            updated = unwrap_one(
                self.client.request(
                    f"/rest/tasks/{current['id']}",
                    {"bodyV2": desired_body},
                    "PATCH",
                )
            )
            task_records[task_id] = {**current, **updated}
            self.changes["task_internal_links_updated"] += 1
        return task_records

    def import_field_values(self, task_records: dict[str, dict[str, Any]]) -> None:
        payloads: list[dict[str, Any]] = []
        for task in self.plan.tasks:
            task_id = str(task["id"])
            for item in as_list(task.get("fields")):
                if not isinstance(item, dict) or item.get("field_id") in (None, ""):
                    continue
                field_id = str(item["field_id"])
                definition = self.plan.field_definitions.get(field_id, {})
                label = str(definition.get("name") or f"Campo ClickUp {field_id}")
                payload = {
                    "name": f"{label} — {task['name']}",
                    "externalId": f"{task_id}:{field_id}",
                    "importerKey": deterministic_uuid(
                        "record", "clickUpFieldValues", f"{task_id}:{field_id}"
                    ),
                    "isSourceActive": True,
                    "taskExternalId": task_id,
                    "fieldExternalId": field_id,
                    "fieldName": label,
                    "fieldTypeCode": number_or_none(item.get("type")),
                    "valueText": summarize_value(
                        item.get("value"), definition.get("type_config", {}).get("options")
                    ),
                    "rawValue": item,
                    "taskId": None,
                }
                if task_id in task_records:
                    payload["taskId"] = task_records[task_id]["id"]
                payloads.append(payload)
        self.upsert_owned_records(
            "clickUpFieldValues",
            "externalId",
            payloads,
            set().union(*(payload.keys() for payload in payloads)) if payloads else set(),
            marker_field="importerKey",
            deactivate_missing=self.plan.has_complete_task_coverage(),
        )

    def import_dependencies(self, task_records: dict[str, dict[str, Any]]) -> None:
        payloads = []
        for edge in build_dependency_edges(self.plan.tasks):
            payload: dict[str, Any] = {
                "name": f"{edge['from']} → {edge['to']}",
                "externalId": edge["externalId"],
                "importerKey": deterministic_uuid(
                    "record", "clickUpDependencies", edge["externalId"]
                ),
                "isSourceActive": True,
                "relationType": edge["type"],
                "fromTaskExternalId": edge["from"],
                "toTaskExternalId": edge["to"],
                "sourceSnapshot": edge,
                "fromTaskId": None,
                "toTaskId": None,
            }
            if edge["from"] in task_records:
                payload["fromTaskId"] = task_records[edge["from"]]["id"]
            if edge["to"] in task_records:
                payload["toTaskId"] = task_records[edge["to"]]["id"]
            if payload["fromTaskId"] is None or payload["toTaskId"] is None:
                self.runtime_warnings.append(
                    f"dependency {edge['externalId']} has an unresolved endpoint; external ids were preserved"
                )
            payloads.append(payload)
        self.upsert_owned_records(
            "clickUpDependencies",
            "externalId",
            payloads,
            set().union(*(payload.keys() for payload in payloads)) if payloads else set(),
            marker_field="importerKey",
            deactivate_missing=self.plan.has_complete_task_coverage(),
        )

    def ensure_view(
        self,
        *,
        stable_key: str,
        name: str,
        view_type: str,
        icon: str,
        field_by_name: dict[str, dict[str, Any]],
        filter_specs: list[dict[str, Any]],
        visible_field_names: list[str],
        main_group_field_name: str | None = None,
        calendar_field_name: str | None = None,
        allowed_group_values: list[str] | None = None,
        position: int = 0,
        strict_visible_fields: bool = False,
        sort_specs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task = self.objects_by_name["task"]
        view_id = deterministic_uuid("view", stable_key)
        views = self.client.list_rest(
            f"/rest/metadata/views?objectMetadataId={task['id']}", limit=500
        )
        current = next((item for item in views if item.get("id") == view_id), None)
        payload: dict[str, Any] = {
            "id": view_id,
            "name": name,
            "objectMetadataId": task["id"],
            "type": view_type,
            "icon": icon,
            "position": position,
            "visibility": "WORKSPACE",
        }
        if main_group_field_name:
            payload["mainGroupByFieldMetadataId"] = field_by_name[main_group_field_name]["id"]
            payload["shouldHideEmptyGroups"] = False
        if calendar_field_name:
            payload["calendarFieldMetadataId"] = field_by_name[calendar_field_name]["id"]
            payload["calendarLayout"] = "MONTH"
        if current is None:
            current = unwrap_one(self.client.request("/rest/metadata/views", payload, "POST"))
            self.changes["views_created"] += 1
        else:
            update = {
                key: value
                for key, value in payload.items()
                if key not in {"id", "objectMetadataId", "type"}
                and canonical(current.get(key)) != canonical(value)
            }
            if update:
                current = unwrap_one(
                    self.client.request(f"/rest/metadata/views/{view_id}", update, "PATCH")
                )
                self.changes["views_updated"] += 1
            else:
                self.changes["views_preserved"] += 1
        self.ensure_view_filters(view_id, stable_key, field_by_name, filter_specs)
        self.ensure_view_fields(
            view_id,
            stable_key,
            field_by_name,
            visible_field_names,
            strict=strict_visible_fields,
        )
        self.ensure_view_sorts(view_id, stable_key, field_by_name, sort_specs or [])
        if allowed_group_values is not None:
            self.configure_view_groups(view_id, allowed_group_values)
        return current

    def ensure_view_sorts(
        self,
        view_id: str,
        stable_key: str,
        field_by_name: dict[str, dict[str, Any]],
        specs: list[dict[str, Any]],
    ) -> None:
        existing = self.client.list_rest(
            f"/rest/metadata/viewSorts?viewId={view_id}", limit=100
        )
        existing = [item for item in existing if item.get("viewId") == view_id]
        by_id = {item["id"]: item for item in existing}
        for index, spec in enumerate(specs):
            field_name = str(spec.get("fieldName") or "")
            metadata = field_by_name.get(field_name)
            if metadata is None:
                self.runtime_warnings.append(
                    f"view {stable_key} skipped unknown sort field {field_name}"
                )
                continue
            sort_id = deterministic_uuid("view-sort", stable_key, index)
            payload = {
                "id": sort_id,
                "viewId": view_id,
                "fieldMetadataId": metadata["id"],
                "direction": str(spec.get("direction") or "ASC").upper(),
            }
            current = by_id.get(sort_id)
            if current is None:
                self.client.request("/rest/metadata/viewSorts", payload, "POST")
                self.changes["view_sorts_created"] += 1
            elif current.get("direction") != payload["direction"]:
                self.client.request(
                    f"/rest/metadata/viewSorts/{sort_id}",
                    {"direction": payload["direction"]},
                    "PATCH",
                )
                self.changes["view_sorts_updated"] += 1

    def ensure_view_filters(
        self,
        view_id: str,
        stable_key: str,
        field_by_name: dict[str, dict[str, Any]],
        specs: list[dict[str, Any]],
    ) -> None:
        existing = self.client.list_rest(
            f"/rest/metadata/viewFilters?viewId={view_id}", limit=200
        )
        existing = [item for item in existing if item.get("viewId") == view_id]
        by_id = {item["id"]: item for item in existing}
        for index, spec in enumerate(specs):
            field_name = spec["fieldName"]
            if field_name not in field_by_name:
                self.runtime_warnings.append(
                    f"view {stable_key} skipped unknown filter field {field_name}"
                )
                continue
            filter_id = deterministic_uuid("view-filter", stable_key, index)
            metadata = field_by_name[field_name]
            operand = str(spec.get("operand", "IS"))
            value = spec.get("value")
            # Twenty v2.45 rejects IS/IS_NOT for TEXT fields. Source list IDs
            # are opaque unique strings, so CONTAINS is the supported native
            # equivalent. SELECT equality requires an array even for one item.
            if metadata.get("type") == "TEXT":
                if operand == "IS":
                    operand = "CONTAINS"
                elif operand == "IS_NOT":
                    operand = "DOES_NOT_CONTAIN"
            if metadata.get("type") in {"SELECT", "MULTI_SELECT"} and operand in {
                "IS",
                "IS_NOT",
            }:
                value = as_list(value)
            payload = {
                "id": filter_id,
                "viewId": view_id,
                "fieldMetadataId": metadata["id"],
                "operand": operand,
                "value": value,
            }
            current = by_id.get(filter_id)
            if current is None:
                self.client.request("/rest/metadata/viewFilters", payload, "POST")
                self.changes["view_filters_created"] += 1
            else:
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
        field_by_name: dict[str, dict[str, Any]],
        field_names: list[str],
        *,
        strict: bool = False,
    ) -> None:
        existing = self.client.list_rest(
            f"/rest/metadata/viewFields?viewId={view_id}", limit=300
        )
        existing = [item for item in existing if item.get("viewId") == view_id]
        by_field = {item.get("fieldMetadataId"): item for item in existing}
        desired_field_ids = {
            field_by_name[name]["id"] for name in field_names if name in field_by_name
        }
        for position, field_name in enumerate(field_names):
            metadata = field_by_name.get(field_name)
            if metadata is None:
                continue
            desired_size = 360 if field_name == "title" else 180
            current = by_field.get(metadata["id"])
            if current is None:
                self.client.request(
                    "/rest/metadata/viewFields",
                    {
                        "id": deterministic_uuid("view-field", stable_key, field_name),
                        "viewId": view_id,
                        "fieldMetadataId": metadata["id"],
                        "isVisible": True,
                        "position": position,
                        "size": desired_size,
                    },
                    "POST",
                )
                self.changes["view_fields_created"] += 1
            else:
                update = {}
                if current.get("isVisible") is not True:
                    update["isVisible"] = True
                if current.get("position") != position:
                    update["position"] = position
                if current.get("size") != desired_size:
                    update["size"] = desired_size
                if update:
                    self.client.request(
                        f"/rest/metadata/viewFields/{current['id']}", update, "PATCH"
                    )
                    self.changes["view_fields_updated"] += 1
        if strict:
            for current in existing:
                if (
                    current.get("fieldMetadataId") not in desired_field_ids
                    and current.get("isVisible") is not False
                ):
                    self.client.request(
                        f"/rest/metadata/viewFields/{current['id']}",
                        {"isVisible": False},
                        "PATCH",
                    )
                    self.changes["view_fields_hidden"] += 1

    def configure_view_groups(self, view_id: str, allowed_values: list[str]) -> None:
        if not allowed_values:
            return
        allowed_position = {value: position for position, value in enumerate(allowed_values)}
        groups = self.client.list_rest(
            f"/rest/metadata/viewGroups?viewId={view_id}", limit=300
        )
        groups = [item for item in groups if item.get("viewId") == view_id]
        for group in groups:
            value = group.get("fieldValue")
            desired_visible = value in allowed_position
            update = {}
            if group.get("isVisible") != desired_visible:
                update["isVisible"] = desired_visible
            # Preserve the current position of hidden groups. Offsetting it on
            # every run causes unbounded drift and defeats idempotency.
            if desired_visible and group.get("position") != allowed_position[value]:
                update["position"] = allowed_position[value]
            if update:
                self.client.request(
                    f"/rest/metadata/viewGroups/{group['id']}", update, "PATCH"
                )
                self.changes["view_groups_updated"] += 1

    def configure_task_fields_widget(self) -> None:
        """Keep source data complete while making the native Task page usable."""
        task = self.objects_by_name["task"]
        query = """
        query ReadTaskFieldsWidget($objectMetadataId: String!, $viewTypes: [ViewType!]) {
          getPageLayouts(objectMetadataId: $objectMetadataId, pageLayoutType: RECORD_PAGE) {
            id type
            tabs { id position isActive widgets {
              id type objectMetadataId isActive
              configuration { ... on FieldsConfiguration {
                configurationType viewId newFieldDefaultVisibility shouldAllowUserToSeeHiddenFields
              } }
            } }
          }
          getViews(objectMetadataId: $objectMetadataId, viewTypes: $viewTypes) {
            id objectMetadataId type isActive
            viewFields { id fieldMetadataId viewId isVisible position viewFieldGroupId isActive deletedAt }
            viewFieldGroups { id name position isVisible viewId isActive deletedAt
              viewFields { id fieldMetadataId viewId isVisible position viewFieldGroupId isActive deletedAt }
            }
          }
        }
        """
        variables = {"objectMetadataId": task["id"], "viewTypes": ["FIELDS_WIDGET"]}
        data = self.client.graphql(query, variables)
        widgets: list[dict[str, Any]] = []
        for layout in as_list(data.get("getPageLayouts")):
            if not isinstance(layout, dict) or layout.get("type") != "RECORD_PAGE":
                continue
            for tab in as_list(layout.get("tabs")):
                if not isinstance(tab, dict) or tab.get("isActive") is False:
                    continue
                widgets.extend(
                    item
                    for item in as_list(tab.get("widgets"))
                    if isinstance(item, dict)
                    and item.get("isActive") is not False
                    and item.get("type") == "FIELDS"
                    and item.get("objectMetadataId") == task["id"]
                    and isinstance(item.get("configuration"), dict)
                    and item["configuration"].get("configurationType") == "FIELDS"
                    and item["configuration"].get("viewId")
                )
        if len(widgets) != 1:
            raise ApiError(f"expected one active Task FIELDS widget; found {len(widgets)}")
        widget = widgets[0]
        view_id = str(widget["configuration"]["viewId"])
        views = [
            item
            for item in as_list(data.get("getViews"))
            if isinstance(item, dict)
            and item.get("id") == view_id
            and item.get("objectMetadataId") == task["id"]
            and item.get("type") == "FIELDS_WIDGET"
            and item.get("isActive") is not False
        ]
        if len(views) != 1:
            raise ApiError(f"expected one active Task FIELDS_WIDGET view; found {len(views)}")
        view = views[0]
        groups = [
            item
            for item in as_list(view.get("viewFieldGroups"))
            if isinstance(item, dict)
            and item.get("isActive") is not False
            and item.get("deletedAt") is None
        ]
        all_view_fields = [
            item
            for item in as_list(view.get("viewFields"))
            if isinstance(item, dict)
            and item.get("isActive") is not False
            and item.get("deletedAt") is None
        ]
        by_field_metadata_id: dict[str, dict[str, Any]] = {}
        for item in all_view_fields:
            metadata_id = str(item.get("fieldMetadataId") or "")
            if not metadata_id:
                continue
            if metadata_id in by_field_metadata_id:
                raise ApiError(f"duplicate Task FIELDS_WIDGET field metadata id: {metadata_id}")
            by_field_metadata_id[metadata_id] = item

        field_by_name = {item["name"]: item for item in task.get("fields", [])}
        anchor_ids = {
            field_by_name[name]["id"]
            for name in ("status", "dueAt", "assignee")
            if name in field_by_name
        }
        general_candidates = [
            group
            for group in groups
            if any(
                item.get("viewFieldGroupId") == group.get("id")
                and item.get("fieldMetadataId") in anchor_ids
                for item in all_view_fields
            )
        ]
        if len(general_candidates) != 1:
            raise ApiError(
                f"could not identify one Task general field group; found {len(general_candidates)}"
            )
        general_group_id = general_candidates[0]["id"]

        preferred_names = [
            "status",
            "dueAt",
            "assignee",
            "clickUpStatus",
            "clickUpPriority",
            "clickUpStartOn",
            "clickUpStartAt",
            "clickUpDueOn",
            "clickUpList",
            "clickUpParentTask",
            *[item["name"] for item in self.dynamic_fields.values()],
            "clickUpSourceUrl",
        ]
        preferred_names = list(dict.fromkeys(preferred_names))
        preferred_ids = [
            field_by_name[name]["id"] for name in preferred_names if name in field_by_name
        ]
        importer_owned_ids = {
            item["id"]
            for item in task.get("fields", [])
            if str(item.get("description") or "").startswith(IMPORTER_MARKER)
        }
        technical_ids = importer_owned_ids - set(preferred_ids)

        desired_groups: list[dict[str, Any]] = []
        for group in sorted(groups, key=lambda item: (item.get("position", 0), item["id"])):
            group_fields = sorted(
                [
                    item
                    for item in all_view_fields
                    if item.get("viewFieldGroupId") == group["id"]
                ],
                key=lambda item: (item.get("position", 0), item["id"]),
            )
            if group["id"] != general_group_id:
                group_fields = [
                    item
                    for item in group_fields
                    if item.get("fieldMetadataId") not in set(preferred_ids) | technical_ids
                ]
                desired_fields = [
                    {
                        "viewFieldId": item["id"],
                        "isVisible": bool(item.get("isVisible")),
                        "position": item.get("position", index),
                    }
                    for index, item in enumerate(group_fields)
                ]
            else:
                current_by_metadata_id = {
                    item["fieldMetadataId"]: item
                    for item in all_view_fields
                    if item.get("fieldMetadataId")
                }
                preferred_entries: list[tuple[str, dict[str, Any] | None]] = [
                    (metadata_id, current_by_metadata_id.get(metadata_id))
                    for metadata_id in preferred_ids
                ]
                other_entries = [
                    (str(item["fieldMetadataId"]), item)
                    for item in group_fields
                    if item.get("fieldMetadataId") not in set(preferred_ids)
                    and item.get("fieldMetadataId") not in technical_ids
                ]
                technical_entries = [
                    (metadata_id, current_by_metadata_id.get(metadata_id))
                    for metadata_id in sorted(technical_ids)
                ]
                desired_fields = []
                seen_metadata_ids: set[str] = set()
                for position, (metadata_id, current) in enumerate(
                    [*preferred_entries, *other_entries, *technical_entries]
                ):
                    if metadata_id in seen_metadata_ids:
                        continue
                    seen_metadata_ids.add(metadata_id)
                    is_visible = metadata_id not in technical_ids
                    if current is not None and metadata_id not in set(preferred_ids) | technical_ids:
                        is_visible = bool(current.get("isVisible"))
                    entry = {
                        ("viewFieldId" if current is not None else "fieldMetadataId"): (
                            current["id"] if current is not None else metadata_id
                        ),
                        "isVisible": is_visible,
                        "position": len(desired_fields),
                    }
                    desired_fields.append(entry)
            desired_groups.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "position": group.get("position", 0),
                    "isVisible": True if group["id"] == general_group_id else bool(group.get("isVisible")),
                    "fields": desired_fields,
                }
            )

        current_groups = []
        for group in sorted(groups, key=lambda item: (item.get("position", 0), item["id"])):
            current_groups.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "position": group.get("position", 0),
                    "isVisible": bool(group.get("isVisible")),
                    "fields": [
                        {
                            "viewFieldId": item["id"],
                            "isVisible": bool(item.get("isVisible")),
                            "position": item.get("position", index),
                        }
                        for index, item in enumerate(
                            sorted(
                                [
                                    field
                                    for field in all_view_fields
                                    if field.get("viewFieldGroupId") == group["id"]
                                ],
                                key=lambda field: (field.get("position", 0), field["id"]),
                            )
                        )
                    ],
                }
            )
        if canonical(current_groups) == canonical(desired_groups):
            self.changes["task_fields_widget_preserved"] += 1
            return
        mutation = """
        mutation ConfigureTaskFieldsWidget($input: UpsertFieldsWidgetInput!) {
          upsertFieldsWidget(input: $input) {
            id
            viewFields { id fieldMetadataId viewId isVisible position viewFieldGroupId isActive deletedAt }
            viewFieldGroups { id name position isVisible viewId isActive deletedAt
              viewFields { id fieldMetadataId viewId isVisible position viewFieldGroupId isActive deletedAt }
            }
          }
        }
        """
        result = self.client.graphql(
            mutation,
            {"input": {"widgetId": widget["id"], "groups": desired_groups}},
        )
        updated = result.get("upsertFieldsWidget")
        if not isinstance(updated, dict) or updated.get("id") != view_id:
            raise ApiError("Task FIELDS widget mutation returned an unexpected view")
        self.changes["task_fields_widget_updated"] += 1

    def import_views(self) -> dict[str, dict[str, Any]]:
        task = self.objects_by_name["task"]
        field_by_name = {item["name"]: item for item in task.get("fields", [])}
        visible = [
            "title",
            "clickUpStatus",
            "clickUpPriority",
            "assignee",
            "clickUpDueOn",
            "dueAt",
            "clickUpCustomFieldsSummary",
            "clickUpSourceUrl",
        ]
        table_views: dict[str, dict[str, Any]] = {}
        # Materialize the seven source-authored views first so the primary
        # workflow is usable even while the lower-priority list tables finish.
        self.import_global_views(field_by_name, visible)
        self.configure_task_fields_widget()
        for index, list_spec in enumerate(self.plan.hierarchy.lists):
            filters = [
                {
                    "fieldName": "clickUpListExternalId",
                    "operand": "IS",
                    "value": list_spec.external_id,
                }
            ]
            table_views[list_spec.external_id] = self.ensure_view(
                stable_key=f"list:{list_spec.external_id}:table",
                name=list_spec.name,
                view_type="TABLE",
                icon="IconList",
                field_by_name=field_by_name,
                filter_specs=filters,
                visible_field_names=visible,
                position=1000 + index,
                strict_visible_fields=True,
            )
        return table_views

    def import_global_views(
        self,
        field_by_name: dict[str, dict[str, Any]],
        default_visible: list[str],
    ) -> None:
        for index, spec in enumerate(self.plan.global_views):
            filters = []
            for raw_filter in as_list(spec.get("filters")):
                if not isinstance(raw_filter, dict):
                    continue
                converted = self.resolve_private_view_filter(raw_filter)
                if converted:
                    filters.append(converted)
            group_field = self.resolve_private_field_name(spec.get("groupByFieldExternalId"))
            calendar_field = self.resolve_private_field_name(spec.get("calendarFieldExternalId"))
            if spec.get("calendarField"):
                calendar_field = str(spec["calendarField"])
            native_visible = spec.get("nativeVisibleFields")
            visible = (
                [str(item) for item in as_list(native_visible)]
                if native_visible is not None
                else list(default_visible)
            )
            for external_id in as_list(spec.get("visibleFieldExternalIds")):
                name = self.resolve_private_field_name(external_id)
                if name:
                    visible.append(name)
            sorts = []
            for raw_sort in as_list(spec.get("sorts")):
                if not isinstance(raw_sort, dict):
                    continue
                field_name = raw_sort.get("fieldName")
                if field_name is None and raw_sort.get("fieldExternalId"):
                    field_name = self.resolve_private_field_name(raw_sort["fieldExternalId"])
                if field_name:
                    sorts.append(
                        {
                            "fieldName": str(field_name),
                            "direction": str(raw_sort.get("direction") or "ASC"),
                        }
                    )
            view = self.ensure_view(
                stable_key=f"global:{spec.get('key') or stable_hash(str(spec['name']))}",
                name=str(spec["name"]),
                view_type=str(spec.get("type") or "TABLE").upper(),
                icon=str(spec.get("icon") or "IconTable"),
                field_by_name=field_by_name,
                filter_specs=filters,
                visible_field_names=list(dict.fromkeys(visible)),
                main_group_field_name=group_field,
                calendar_field_name=calendar_field,
                position=int(spec.get("position", 2000 + index)),
                strict_visible_fields=bool(spec.get("strictVisibleFields", False)),
                sort_specs=sorts,
            )
            self.global_view_records.append(
                {
                    "view": view,
                    "folderExternalId": spec.get("navigationFolderExternalId"),
                }
            )

    def resolve_private_field_name(self, external_id: Any) -> str | None:
        if external_id in (None, ""):
            return None
        dynamic = self.dynamic_fields.get(str(external_id))
        if dynamic is None:
            self.runtime_warnings.append(f"unknown private field definition {external_id}")
            return None
        return str(dynamic["name"])

    def resolve_private_view_filter(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        field_name = spec.get("fieldName")
        external_id = spec.get("fieldExternalId")
        dynamic = self.dynamic_fields.get(str(external_id)) if external_id else None
        if field_name is None and dynamic is not None:
            field_name = dynamic["name"]
        if not field_name:
            self.runtime_warnings.append("global view filter skipped because its field is unknown")
            return None
        value = spec.get("value")
        if dynamic and dynamic["type"] in {"SELECT", "MULTI_SELECT"}:
            if isinstance(value, list):
                value = [dynamic["option_value_by_id"].get(str(item), str(item)) for item in value]
            else:
                value = dynamic["option_value_by_id"].get(str(value), value)
        return {
            "fieldName": str(field_name),
            "operand": str(spec.get("operand") or "IS"),
            "value": value,
        }

    def import_navigation(self, table_views: dict[str, dict[str, Any]]) -> None:
        query = """
        query ImporterNavigationItems {
          navigationMenuItems { id type name icon color folderId viewId targetObjectMetadataId position }
        }
        """
        existing = self.client.graphql(query).get("navigationMenuItems") or []
        by_id = {item["id"]: item for item in existing}
        desired: list[dict[str, Any]] = []
        folder_uuid_by_external_id: dict[str, str] = {
            folder.external_id: deterministic_uuid("navigation-folder", folder.external_id)
            for folder in self.plan.hierarchy.folders
        }
        integration_folder_id = deterministic_uuid("navigation-folder", "clickup-integration")
        helper_object_ids = {
            self.objects_by_name[name]["id"]
            for name in ("clickUpList", "clickUpDependency", "clickUpFieldValue")
        }
        helper_items = [
            item
            for item in existing
            if item.get("type") == "OBJECT"
            and item.get("targetObjectMetadataId") in helper_object_ids
        ]
        if helper_items:
            desired.append(
                {
                    "id": integration_folder_id,
                    "type": "FOLDER",
                    "name": "Integração ClickUp",
                    "icon": "IconDatabaseImport",
                    "color": "gray",
                    "position": 3000,
                }
            )
            for index, item in enumerate(helper_items):
                desired.append(
                    {
                        "id": item["id"],
                        "type": "OBJECT",
                        "targetObjectMetadataId": item["targetObjectMetadataId"],
                        "folderId": integration_folder_id,
                        "position": index,
                    }
                )
        for index, entry in enumerate(self.global_view_records):
            view = entry["view"]
            payload = {
                "id": deterministic_uuid("navigation-global-view", view["id"]),
                "type": "VIEW",
                "viewId": view["id"],
                "position": -2000 + index,
            }
            folder_external_id = entry.get("folderExternalId")
            if folder_external_id:
                if folder_external_id not in folder_uuid_by_external_id:
                    raise ApiError(f"global view targets unknown folder {folder_external_id}")
                payload["folderId"] = folder_uuid_by_external_id[folder_external_id]
                payload["position"] = -100 + index
            desired.append(payload)
        for folder in self.plan.hierarchy.folders:
            item_id = folder_uuid_by_external_id[folder.external_id]
            desired.append(
                {
                    "id": item_id,
                    "type": "FOLDER",
                    "name": folder.name,
                    "icon": "IconFolder",
                    "color": normalize_color(None, folder.external_id),
                    "position": -1900 + folder.position,
                }
            )
        for list_spec in self.plan.hierarchy.lists:
            view = table_views[list_spec.external_id]
            payload: dict[str, Any] = {
                "id": deterministic_uuid("navigation-list-view", list_spec.external_id),
                "type": "VIEW",
                "viewId": view["id"],
                "position": 100 + list_spec.folder_position,
            }
            if list_spec.folder_external_id:
                payload["folderId"] = folder_uuid_by_external_id[list_spec.folder_external_id]
            else:
                payload["position"] = -1800 + list_spec.root_position
            desired.append(payload)

        create_inputs = [item for item in desired if item["id"] not in by_id]
        if create_inputs:
            mutation = """
            mutation ImporterCreateNavigationItems($inputs: [CreateNavigationMenuItemInput!]!) {
              createManyNavigationMenuItems(inputs: $inputs) { id }
            }
            """
            self.client.graphql(mutation, {"inputs": create_inputs})
            self.changes["navigation_items_created"] += len(create_inputs)
        update_inputs = []
        for desired_item in desired:
            current = by_id.get(desired_item["id"])
            if current is None:
                continue
            update = {
                key: value
                for key, value in desired_item.items()
                if key not in {"id", "type", "viewId", "targetObjectMetadataId"}
                and canonical(current.get(key)) != canonical(value)
            }
            if update:
                update_inputs.append({"id": desired_item["id"], "update": update})
        if update_inputs:
            mutation = """
            mutation ImporterUpdateNavigationItems($inputs: [UpdateOneNavigationMenuItemInput!]!) {
              updateManyNavigationMenuItems(inputs: $inputs) { id }
            }
            """
            self.client.graphql(mutation, {"inputs": update_inputs})
            self.changes["navigation_items_updated"] += len(update_inputs)
        self.changes["navigation_items_preserved"] += len(desired) - len(create_inputs) - len(update_inputs)

    def apply_dark_theme(self) -> None:
        for member in self.list_object_records("workspaceMembers"):
            if member.get("colorScheme") == "Dark":
                self.changes["dark_theme_preserved"] += 1
                continue
            self.client.request(
                f"/rest/workspaceMembers/{member['id']}",
                {"colorScheme": "Dark"},
                "PATCH",
            )
            self.changes["dark_theme_updated"] += 1

    def run(self, *, dark_theme: bool) -> dict[str, Any]:
        self.ensure_schema()
        list_records = self.import_lists()
        task_records = self.import_tasks(list_records)
        self.import_field_values(task_records)
        self.import_dependencies(task_records)
        table_views = self.import_views()
        self.import_navigation(table_views)
        if dark_theme:
            self.apply_dark_theme()
        return {
            "status": "ok",
            "mode": "apply",
            "changes": dict(sorted(self.changes.items())),
            "requests": self.client.request_count,
            "writes": self.client.write_count,
            "warnings": [*self.plan.warnings, *self.runtime_warnings],
            "safety": {
                "deletes": 0,
                "missing_source_records_archived": 0,
                "existing_crm_records_replaced": 0,
            },
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hierarchy", required=True, type=Path)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--status-definitions", type=Path)
    parser.add_argument("--space-statuses", type=Path)
    parser.add_argument("--field-definitions", type=Path)
    parser.add_argument("--global-views", type=Path)
    parser.add_argument("--source-ui-views", type=Path)
    parser.add_argument("--task-links", type=Path)
    parser.add_argument("--rich-content", type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--signup-guard-report", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--time-zone", default="America/Sao_Paulo")
    parser.add_argument("--dark-theme", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-workspace-id")
    parser.add_argument("--confirm-target-workspace-id")
    parser.add_argument("--ack-production-write")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def emit_report(report: dict[str, Any], path: Path | None) -> None:
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        temporary.replace(path)


def assert_import_not_cut_over(credential_file: Path, target_workspace_id: str) -> None:
    # Once Twenty owns ongoing edits, an old source snapshot must not win again.
    marker = credential_file.resolve().parent / "clickup-cutover.json"
    if not marker.exists():
        return
    state = load_json(marker)
    if not isinstance(state, dict) or state.get("target_workspace_id") != target_workspace_id:
        raise ContractError("cutover marker is invalid or belongs to another target; import refused")
    raise ContractError(
        "ClickUp reimport is locked by clickup-cutover.json: Twenty owns ongoing edits. "
        "Reconcile later source changes separately instead of replaying the snapshot."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        hierarchy_document = load_json(args.hierarchy)
        task_document = load_json(args.tasks)
        status_document = load_json(args.status_definitions) if args.status_definitions else None
        space_status_document = load_json(args.space_statuses) if args.space_statuses else None
        field_document = load_json(args.field_definitions) if args.field_definitions else None
        global_view_document = load_json(args.global_views) if args.global_views else None
        source_view_document = (
            load_json(args.source_ui_views) if args.source_ui_views else None
        )
        task_links_document = load_json(args.task_links) if args.task_links else None
        rich_content_document = load_json(args.rich_content) if args.rich_content else None
        plan = build_plan(
            hierarchy_document,
            task_document,
            status_document,
            field_document,
            global_view_document,
            task_links_document,
            space_status_document,
            rich_content_document,
            source_view_document,
        )
        if not args.apply:
            emit_report(plan.summary(dark_theme=args.dark_theme), args.report)
            return 0
        if args.ack_production_write != APPLY_ACK:
            raise ContractError(f"apply requires --ack-production-write {APPLY_ACK}")
        if not args.confirm_workspace_id:
            raise ContractError("apply requires --confirm-workspace-id")
        if args.confirm_workspace_id != plan.hierarchy.workspace_external_id:
            raise ContractError("confirmed workspace id does not match the snapshot")
        if args.credential_file is None:
            raise ContractError("apply requires --credential-file")
        if not args.confirm_target_workspace_id:
            raise ContractError("apply requires --confirm-target-workspace-id")
        assert_import_not_cut_over(args.credential_file, args.confirm_target_workspace_id)
        if args.signup_guard_report is None:
            raise ContractError("apply requires --signup-guard-report")
        signup_guard = load_json(args.signup_guard_report)
        if not isinstance(signup_guard, dict) or signup_guard.get("signup_disabled") is not True:
            raise ContractError("signup guard report does not verify disabled signup")
        if signup_guard.get("user_count_before") != 1 or signup_guard.get("user_count_after") != 1:
            raise ContractError("signup guard report does not prove the single-user invariant")
        if signup_guard.get("target_workspace_id") != args.confirm_target_workspace_id:
            raise ContractError("signup guard report target workspace mismatch")
        if signup_guard.get("public_invite_link_enabled") is not False:
            raise ContractError("signup guard report does not prove public invite is disabled")
        try:
            verified_at = datetime.fromisoformat(
                str(signup_guard["verified_at"]).replace("Z", "+00:00")
            )
            age = datetime.now(timezone.utc) - verified_at.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError) as exc:
            raise ContractError("signup guard report has an invalid verification timestamp") from exc
        if age < timedelta(minutes=-1) or age > timedelta(minutes=15):
            raise ContractError("signup guard report is not fresh (maximum age: 15 minutes)")
        credentials = load_json(args.credential_file)
        if not isinstance(credentials, dict):
            raise ContractError("credential file must be an object")
        if str(credentials.get("workspace_id")) != args.confirm_target_workspace_id:
            raise ContractError("credential target workspace id does not match confirmation")
        token = credentials.get("api_key")
        if not isinstance(token, str) or not token:
            raise ContractError("credential file has no api_key")
        base_url = args.base_url or credentials.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ContractError("apply requires --base-url or credential base_url")
        parsed_base = urllib.parse.urlparse(base_url)
        if parsed_base.scheme != "https" and not (
            parsed_base.scheme == "http"
            and parsed_base.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ContractError("remote Twenty base URL must use HTTPS")
        guard_base_url = str(signup_guard.get("base_url", "")).rstrip("/")
        same_guard_endpoint = guard_base_url == base_url.rstrip("/")
        ssh_loopback_alias = (
            parsed_base.scheme == "http"
            and parsed_base.hostname in {"127.0.0.1", "localhost", "::1"}
            and args.base_url is None
            and str(credentials.get("base_url", "")).rstrip("/") == base_url.rstrip("/")
            and urllib.parse.urlparse(guard_base_url).scheme == "https"
        )
        if not same_guard_endpoint and not ssh_loopback_alias:
            raise ContractError("signup guard report base URL mismatch")
        resolve_timezone(args.time_zone)  # validate before constructing the client
        client = TwentyClient(base_url, token)
        preflight = client.preflight(args.confirm_target_workspace_id)
        importer = TwentyImporter(client, plan, zone_name=args.time_zone)
        report = importer.run(dark_theme=args.dark_theme)
        report["preflight"] = preflight
        report["preflight"]["signup_disabled"] = True
        emit_report(report, args.report)
        return 0
    except (ContractError, ApiError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
