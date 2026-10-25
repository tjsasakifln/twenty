from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "clickup_twenty_importer", ROOT / "integrations" / "clickup" / "clickup_twenty_importer.py"
)
assert SPEC and SPEC.loader
importer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = importer
SPEC.loader.exec_module(importer)


def hierarchy_fixture():
    return {
        "hierarchy": {
            "root": {
                "id": "workspace-1",
                "name": "Example workspace",
                "children": [
                    {
                        "id": "space-1",
                        "name": "Example space",
                        "type": "space",
                        "children": [
                            {
                                "id": "folder-1",
                                "name": "Folder A",
                                "type": "folder",
                                "children": [
                                    {
                                        "id": "list-1",
                                        "name": "List A",
                                        "type": "list",
                                        "children": [],
                                    }
                                ],
                            },
                            {
                                "id": "list-2",
                                "name": "Direct list",
                                "type": "list",
                                "children": [],
                            },
                        ],
                    }
                ],
            }
        }
    }


def tasks_fixture():
    return {
        "workspace_id": "workspace-1",
        "found_count": 2,
        "requested_ids": ["task-1", "task-2"],
        "missing": [],
        "tasks": [
            {
                "status": "found",
                "object_id": "task-1",
                "data": {
                    "id": "task-1",
                    "name": "Synthetic task",
                    "text_content": "First paragraph\nSecond paragraph",
                    "status": "Doing",
                    "status_id": "status-shared",
                    "priority": "2",
                    "lists": [{"list_id": "list-1", "type": "home"}],
                    "fields": [
                        {
                            "field_id": "field-select",
                            "type": 1,
                            "value": "option-1",
                            "value_options": None,
                        }
                    ],
                    "related_tasks": ["task-2"],
                    "subtask_ids": ["task-2"],
                    "attachments": [],
                    "users": [],
                    "time_mgmt": {
                        "date_created": "1792893686550",
                        "date_updated": "1792895654018",
                        "due_date": "1792825200000",
                        "due_date_time": False,
                    },
                    "privacy": {"private": False},
                    "states": {"is_archived": False, "is_deleted": False},
                },
            },
            {
                "status": "found",
                "object_id": "task-2",
                "data": {
                    "id": "task-2",
                    "name": "Synthetic subtask",
                    "text_content": "",
                    "status": "Doing",
                    "status_id": "status-shared",
                    "lists": [{"list_id": "list-2", "type": "home"}],
                    "fields": [],
                    "related_tasks": [],
                    "subtask_ids": [],
                    "attachments": [],
                    "users": [],
                    "time_mgmt": {},
                    "privacy": {"private": True},
                    "states": {"is_archived": False, "is_deleted": False},
                },
            },
        ],
    }


def statuses_fixture():
    return {
        "statuses": [
            {
                "list_id": "list-1",
                "status_id": "status-shared",
                "status": "Doing",
                "count": 1,
            },
            {
                "list_id": "list-2",
                "status_id": "status-shared",
                "status": "Doing",
                "count": 1,
            },
        ]
    }


def fields_fixture():
    return {
        "fields": [
            {
                "id": "field-select",
                "name": "Synthetic dropdown",
                "type": "drop_down",
                "type_config": {
                    "options": [
                        {
                            "id": "option-1",
                            "name": "Option one",
                            "orderindex": 0,
                            "color": "blue",
                        }
                    ]
                },
            }
        ]
    }


class SnapshotContractTests(unittest.TestCase):
    def test_parses_folder_and_direct_list(self):
        parsed = importer.parse_hierarchy(hierarchy_fixture())
        self.assertEqual(len(parsed.folders), 1)
        self.assertEqual(len(parsed.lists), 2)
        direct = next(item for item in parsed.lists if item.external_id == "list-2")
        self.assertIsNone(direct.folder_external_id)

    def test_rejects_duplicate_hierarchy_id(self):
        document = hierarchy_fixture()
        document["hierarchy"]["root"]["children"][0]["children"][1]["id"] = "list-1"
        with self.assertRaisesRegex(importer.ContractError, "duplicate hierarchy id"):
            importer.parse_hierarchy(document)

    def test_projected_task_format_is_normalized(self):
        hierarchy = importer.parse_hierarchy(hierarchy_fixture())
        tasks, warnings = importer.parse_tasks(tasks_fixture(), hierarchy)
        self.assertEqual(warnings, [])
        self.assertEqual(tasks[0]["text"], "First paragraph\nSecond paragraph")
        self.assertEqual(tasks[0]["timing"]["due_date"], "1792825200000")
        self.assertEqual(tasks[0]["task_status"]["id"], "status-shared")
        self.assertEqual(importer.get_home_list_id(tasks[1]), "list-2")

    def test_unknown_list_warns_without_dropping_task(self):
        hierarchy = importer.parse_hierarchy(hierarchy_fixture())
        document = tasks_fixture()
        document["tasks"][0]["data"]["lists"][0]["list_id"] = "missing-list"
        tasks, warnings = importer.parse_tasks(document, hierarchy)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(any("unknown list" in warning for warning in warnings))

    def test_duplicate_task_id_is_rejected(self):
        hierarchy = importer.parse_hierarchy(hierarchy_fixture())
        document = tasks_fixture()
        document["tasks"][1]["data"]["id"] = "task-1"
        with self.assertRaisesRegex(importer.ContractError, "duplicate task id"):
            importer.parse_tasks(document, hierarchy)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture()
        )

    def test_shared_source_status_id_has_one_editable_option(self):
        self.assertEqual(len(self.plan.status_specs), 2)
        self.assertEqual({item.label for item in self.plan.status_specs}, {"Doing"})
        self.assertEqual(len({item.value for item in self.plan.status_specs}), 1)
        self.assertEqual(len({item.option_id for item in self.plan.status_specs}), 1)

    def test_conflicting_shared_status_identity_is_rejected(self):
        statuses = statuses_fixture()
        statuses["statuses"][1]["status"] = "Different label"
        with self.assertRaisesRegex(importer.ContractError, "conflicting labels or colors"):
            importer.build_plan(
                hierarchy_fixture(), tasks_fixture(), statuses, fields_fixture()
            )

    def test_parent_and_related_edge_are_stable(self):
        self.assertEqual(importer.build_parent_links(self.plan.tasks)["task-2"], "task-1")
        edges = importer.build_dependency_edges(self.plan.tasks)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["type"], "RELATED")

    def test_date_only_stays_date(self):
        date_value = importer.epoch_ms_to_date("1792886400000", "America/Sao_Paulo")
        self.assertEqual(date_value, "2026-10-25")
        self.assertEqual(
            importer.epoch_ms_to_date(
                "1792886400000", "America/Sao_Paulo", has_time=True
            ),
            "2026-10-24",
        )
        self.assertIsNone(importer.epoch_ms_to_datetime("not-a-date"))

    def test_source_css_colors_map_to_nearest_native_palette(self):
        self.assertEqual(importer.normalize_color("#2563eb", "status-blue"), "blue")
        self.assertEqual(importer.normalize_color("rgb(92, 71, 205)", "status-purple"), "purple")
        self.assertEqual(importer.normalize_color("rgba(239,68,68,0.8)", "status-red"), "red")

    def test_rich_text_ids_are_deterministic(self):
        first = importer.rich_text_payload("task-1", "A\nB")
        second = importer.rich_text_payload("task-1", "A\nB")
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first["blocknote"])[0]["content"][0]["text"], "A")

    def test_owned_option_merge_preserves_existing_id(self):
        merged = importer.TwentyImporter.merge_owned_options(
            [{"id": "existing", "value": "A", "label": "Old", "position": 4}],
            [{"id": "desired", "value": "A", "label": "New", "position": 0}],
        )
        self.assertEqual(merged[0]["id"], "existing")
        self.assertEqual(merged[0]["label"], "New")
        self.assertEqual(merged[0]["position"], 0)
        self.assertEqual(len(merged), 1)

    def test_quill_links_and_lists_are_preserved(self):
        content = json.dumps({"ops": [
            {"insert": "Open", "attributes": {"link": "https://example.com"}},
            {"insert": "\n", "attributes": {"list": {"list": "bullet"}}},
        ]})
        body = importer.quill_body_payload("task-1", content, "fallback")
        block = json.loads(body["blocknote"])[0]
        self.assertEqual(block["type"], "bulletListItem")
        self.assertEqual(block["content"][0]["type"], "link")
        self.assertEqual(body["markdown"], "- [Open](https://example.com)")

    def test_known_internal_links_resolve_to_native_routes_only(self):
        global_views = {"views": [{"key": "focus", "name": "Focus", "type": "TABLE"}]}
        source_views = {
            "views": [{
                "name": "Focus",
                "url": "https://app.clickup.com/workspace-1/v/l/source-view",
            }]
        }
        plan = importer.build_plan(
            hierarchy_fixture(),
            tasks_fixture(),
            statuses_fixture(),
            fields_fixture(),
            global_views,
            source_view_document=source_views,
        )
        resolver = importer.build_native_link_resolver(
            plan,
            {"task-1": {"id": "native-task-uuid"}},
        )
        self.assertEqual(
            resolver("https://app.clickup.com/t/workspace-1/task-1"),
            "/object/task/native-task-uuid",
        )
        self.assertEqual(
            resolver("https://app.clickup.com/workspace-1/v/l/li/list-1"),
            "/objects/tasks?viewId="
            + importer.deterministic_uuid("view", "list:list-1:table"),
        )
        self.assertEqual(
            resolver("https://app.clickup.com/workspace-1/v/li/list-1/folder-1?pr=1"),
            "/objects/tasks?viewId="
            + importer.deterministic_uuid("view", "list:list-1:table"),
        )
        self.assertEqual(
            resolver("https://app.clickup.com/workspace-1/v/l/source-view"),
            "/objects/tasks?viewId="
            + importer.deterministic_uuid("view", "global:focus"),
        )
        external = "https://drive.google.com/file/d/keep-me"
        unknown_clickup = "https://app.clickup.com/t/workspace-1/not-imported"
        self.assertEqual(resolver(external), external)
        self.assertEqual(resolver(unknown_clickup), unknown_clickup)

        content = json.dumps({"ops": [
            {
                "insert": "Task",
                "attributes": {"link": "https://app.clickup.com/t/task-1"},
            },
            {"insert": " and Drive", "attributes": {"link": external}},
            {"insert": "\n"},
        ]})
        body = importer.quill_body_payload("guide", content, "fallback", resolver)
        self.assertIn("/object/task/native-task-uuid", body["markdown"])
        self.assertIn(external, body["markdown"])

    def test_task_link_coverage_is_exact(self):
        links = [
            {"url": "https://app.clickup.com/t/workspace-1/task-1"},
            {"url": "https://app.clickup.com/t/workspace-1/task-2"},
        ]
        plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture(),
            task_links_document=links,
        )
        self.assertTrue(all(task.get("source_url") for task in plan.tasks))
        with self.assertRaisesRegex(importer.ContractError, "coverage mismatch"):
            importer.build_plan(
                hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture(),
                task_links_document=links[:1],
            )

    def test_task_payload_clears_removed_owned_values(self):
        instance = importer.TwentyImporter(object(), self.plan, zone_name="America/Sao_Paulo")
        instance.dynamic_fields = {"field-select": {
            "name": "clickUpCfSynthetic", "type": "SELECT",
            "option_value_by_id": {"option-1": "OPTION_ONE"}, "currency": "BRL",
        }}
        payload = instance.task_payload(dict(self.plan.tasks[1]), {})
        self.assertIsNone(payload["bodyV2"])
        self.assertIsNone(payload["clickUpListId"])
        self.assertIsNone(payload["clickUpCfSynthetic"])


class CliSafetyTests(unittest.TestCase):
    def test_default_dry_run_performs_no_network_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hierarchy_path = root / "hierarchy.json"
            task_path = root / "tasks.json"
            status_path = root / "statuses.json"
            field_path = root / "fields.json"
            for path, value in (
                (hierarchy_path, hierarchy_fixture()),
                (task_path, tasks_fixture()),
                (status_path, statuses_fixture()),
                (field_path, fields_fixture()),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                with redirect_stdout(output):
                    exit_code = importer.main(
                        [
                            "--hierarchy",
                            str(hierarchy_path),
                            "--tasks",
                            str(task_path),
                            "--status-definitions",
                            str(status_path),
                            "--field-definitions",
                            str(field_path),
                        ]
                    )
            self.assertEqual(exit_code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["network_requests"], 0)
            self.assertEqual(report["records"]["tasks_available"], 2)
            self.assertEqual(report["views"]["table"], 2)

    def test_apply_requires_explicit_ack_before_credentials_are_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hierarchy_path = root / "hierarchy.json"
            task_path = root / "tasks.json"
            hierarchy_path.write_text(json.dumps(hierarchy_fixture()), encoding="utf-8")
            task_path.write_text(json.dumps(tasks_fixture()), encoding="utf-8")
            exit_code = importer.main(
                [
                    "--hierarchy",
                    str(hierarchy_path),
                    "--tasks",
                    str(task_path),
                    "--apply",
                ]
            )
            self.assertEqual(exit_code, 2)


class FakeRecordClient:
    def __init__(self):
        self.rows = []
        self.writes = 0

    def list_rest(self, path, limit=200):
        return [dict(row) for row in self.rows]

    def request(self, path, payload=None, method="GET", **kwargs):
        if method == "POST":
            row = {"id": f"row-{len(self.rows) + 1}", **payload}
            self.rows.append(row)
            self.writes += 1
            return row
        if method == "PATCH":
            row_id = path.rsplit("/", 1)[-1]
            row = next(item for item in self.rows if item["id"] == row_id)
            row.update(payload)
            self.writes += 1
            return dict(row)
        raise AssertionError(method)


class ReconciliationTests(unittest.TestCase):
    def test_apply_twice_has_zero_second_writes_and_deactivates_removed(self):
        plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture()
        )
        client = FakeRecordClient()
        instance = importer.TwentyImporter(client, plan, zone_name="America/Sao_Paulo")
        key = "source-1"
        payload = {
            "externalId": key,
            "importerKey": importer.deterministic_uuid("record", "widgets", key),
            "isSourceActive": True,
            "name": "One",
        }
        owned = set(payload)
        instance.upsert_owned_records(
            "widgets", "externalId", [payload], owned,
            marker_field="importerKey", deactivate_missing=True,
        )
        first_writes = client.writes
        instance.upsert_owned_records(
            "widgets", "externalId", [payload], owned,
            marker_field="importerKey", deactivate_missing=True,
        )
        self.assertEqual(client.writes, first_writes)
        instance.upsert_owned_records(
            "widgets", "externalId", [], set(),
            marker_field="importerKey", deactivate_missing=True,
        )
        self.assertFalse(client.rows[0]["isSourceActive"])

    def test_mutating_request_is_not_retried_after_ambiguous_failure(self):
        client = importer.TwentyClient("https://example.com", "secret", write_delay=0)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("lost")) as call:
            with self.assertRaises(importer.ApiError):
                client.request("/rest/widgets", {"name": "x"}, "POST")
        self.assertEqual(call.call_count, 1)

    def test_helper_record_batch_upsert_is_bounded_and_idempotent(self):
        class BatchClient(FakeRecordClient):
            def __init__(self):
                super().__init__()
                self.batch_sizes = []

            def request(self, path, payload=None, method="GET", **kwargs):
                if method == "POST" and path.startswith("/rest/batch/clickUpFieldValues"):
                    self.batch_sizes.append(len(payload))
                    created = []
                    by_key = {row["externalId"]: row for row in self.rows}
                    for item in payload:
                        row = by_key.get(item["externalId"])
                        if row is None:
                            row = {"id": f"row-{len(self.rows) + 1}", **item}
                            self.rows.append(row)
                            by_key[item["externalId"]] = row
                        created.append(dict(row))
                    self.writes += 1
                    return {"data": {"createClickUpFieldValues": created}}
                return super().request(path, payload, method, **kwargs)

        plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture()
        )
        client = BatchClient()
        instance = importer.TwentyImporter(client, plan, zone_name="America/Sao_Paulo")
        payloads = [
            {
                "externalId": f"task:field:{index}",
                "importerKey": importer.deterministic_uuid(
                    "record", "clickUpFieldValues", f"task:field:{index}"
                ),
                "isSourceActive": True,
            }
            for index in range(201)
        ]
        owned = set(payloads[0])
        instance.upsert_owned_records(
            "clickUpFieldValues",
            "externalId",
            payloads,
            owned,
            marker_field="importerKey",
        )
        self.assertEqual(client.batch_sizes, [200, 1])
        first_writes = client.writes
        instance.upsert_owned_records(
            "clickUpFieldValues",
            "externalId",
            payloads,
            owned,
            marker_field="importerKey",
        )
        self.assertEqual(client.writes, first_writes)
        with self.assertRaisesRegex(importer.ApiError, "duplicate desired"):
            instance.upsert_owned_records(
                "clickUpFieldValues",
                "externalId",
                [payloads[0], dict(payloads[0])],
                owned,
                marker_field="importerKey",
            )

    def test_hidden_view_group_position_is_stable_across_two_runs(self):
        class GroupClient:
            def __init__(self):
                self.groups = [
                    {"id": "visible", "viewId": "view-1", "fieldValue": "A", "position": 8, "isVisible": False},
                    {"id": "hidden", "viewId": "view-1", "fieldValue": "B", "position": 47, "isVisible": True},
                    {"id": "other", "viewId": "other-view", "fieldValue": "A", "position": 99, "isVisible": True},
                ]
                self.patches = 0

            def list_rest(self, path, limit=300):
                return [dict(item) for item in self.groups]

            def request(self, path, payload=None, method="GET", **kwargs):
                self.patches += 1
                row_id = path.rsplit("/", 1)[-1]
                row = next(item for item in self.groups if item["id"] == row_id)
                row.update(payload)
                return dict(row)

        plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture()
        )
        client = GroupClient()
        instance = importer.TwentyImporter(client, plan, zone_name="America/Sao_Paulo")
        instance.configure_view_groups("view-1", ["A"])
        first_patch_count = client.patches
        hidden = next(item for item in client.groups if item["id"] == "hidden")
        self.assertEqual(hidden["position"], 47)
        self.assertFalse(hidden["isVisible"])
        instance.configure_view_groups("view-1", ["A"])
        self.assertEqual(client.patches, first_patch_count)
        other = next(item for item in client.groups if item["id"] == "other")
        self.assertEqual(other["position"], 99)

    def test_view_filter_payload_matches_field_operand_contract(self):
        class FilterClient:
            def __init__(self):
                self.payloads = []

            def list_rest(self, path, limit=200):
                return []

            def request(self, path, payload=None, method="GET", **kwargs):
                self.payloads.append(payload)
                return payload

        plan = importer.build_plan(
            hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture()
        )
        client = FilterClient()
        instance = importer.TwentyImporter(client, plan, zone_name="America/Sao_Paulo")
        fields = {
            "listKey": {"id": "field-text", "type": "TEXT"},
            "stage": {"id": "field-select", "type": "SELECT"},
        }
        instance.ensure_view_filters(
            "view-1",
            "test-view",
            fields,
            [
                {"fieldName": "listKey", "operand": "IS", "value": "list-1"},
                {"fieldName": "stage", "operand": "IS", "value": "OPTION_A"},
            ],
        )
        self.assertEqual(client.payloads[0]["operand"], "CONTAINS")
        self.assertEqual(client.payloads[0]["value"], "list-1")
        self.assertEqual(client.payloads[1]["operand"], "IS")
        self.assertEqual(client.payloads[1]["value"], ["OPTION_A"])

    def test_task_fields_widget_preserves_groups_and_converges(self):
        class FieldsWidgetClient:
            def __init__(self):
                self.mutations = []
                self.groups = [
                    {"id": "group-general", "name": "General", "position": 0, "isVisible": True},
                    {"id": "group-other", "name": "Other", "position": 1, "isVisible": True},
                ]
                self.fields = [
                    {"id": "vf-status", "fieldMetadataId": "fm-status", "viewId": "fields-view", "isVisible": True, "position": 0, "viewFieldGroupId": "group-general", "isActive": True, "deletedAt": None},
                    {"id": "vf-note", "fieldMetadataId": "fm-note", "viewId": "fields-view", "isVisible": True, "position": 0, "viewFieldGroupId": "group-other", "isActive": True, "deletedAt": None},
                    {"id": "vf-clickup-status", "fieldMetadataId": "fm-clickup-status", "viewId": "fields-view", "isVisible": False, "position": 1, "viewFieldGroupId": "group-other", "isActive": True, "deletedAt": None},
                    {"id": "vf-dynamic", "fieldMetadataId": "fm-dynamic", "viewId": "fields-view", "isVisible": False, "position": 2, "viewFieldGroupId": "group-other", "isActive": True, "deletedAt": None},
                    {"id": "vf-technical", "fieldMetadataId": "fm-technical", "viewId": "fields-view", "isVisible": True, "position": 3, "viewFieldGroupId": "group-other", "isActive": True, "deletedAt": None},
                    {"id": "vf-ungrouped", "fieldMetadataId": "fm-ungrouped", "viewId": "fields-view", "isVisible": True, "position": 9, "viewFieldGroupId": None, "isActive": True, "deletedAt": None},
                ]

            def _read(self):
                groups = []
                for group in self.groups:
                    groups.append({
                        **group,
                        "viewId": "fields-view", "isActive": True, "deletedAt": None,
                        "viewFields": [dict(item) for item in self.fields if item["viewFieldGroupId"] == group["id"]],
                    })
                return {
                    "getPageLayouts": [{
                        "id": "layout", "type": "RECORD_PAGE",
                        "tabs": [{"id": "tab", "position": 0, "isActive": True, "widgets": [{
                            "id": "widget", "type": "FIELDS", "objectMetadataId": "task-meta", "isActive": True,
                            "configuration": {"configurationType": "FIELDS", "viewId": "fields-view"},
                        }]}],
                    }],
                    "getViews": [{
                        "id": "fields-view", "objectMetadataId": "task-meta", "type": "FIELDS_WIDGET", "isActive": True,
                        "viewFields": [dict(item) for item in self.fields], "viewFieldGroups": groups,
                    }],
                }

            def graphql(self, query, variables=None):
                if query.lstrip().startswith("query"):
                    return self._read()
                payload = variables["input"]
                self.mutations.append(json.loads(json.dumps(payload)))
                for group_input in payload["groups"]:
                    group = next(item for item in self.groups if item["id"] == group_input["id"])
                    group.update({key: group_input[key] for key in ("name", "position", "isVisible")})
                    for field_input in group_input["fields"]:
                        if "viewFieldId" in field_input:
                            row = next(item for item in self.fields if item["id"] == field_input["viewFieldId"])
                        else:
                            row = {"id": "vf-created-source-url", "fieldMetadataId": field_input["fieldMetadataId"], "viewId": "fields-view", "isActive": True, "deletedAt": None}
                            self.fields.append(row)
                        row.update({"viewFieldGroupId": group_input["id"], "isVisible": field_input["isVisible"], "position": field_input["position"]})
                return {"upsertFieldsWidget": {"id": "fields-view", "viewFields": [], "viewFieldGroups": []}}

        plan = importer.build_plan(hierarchy_fixture(), tasks_fixture(), statuses_fixture(), fields_fixture())
        client = FieldsWidgetClient()
        instance = importer.TwentyImporter(client, plan, zone_name="America/Sao_Paulo")
        instance.objects_by_name["task"] = {
            "id": "task-meta",
            "fields": [
                {"id": "fm-status", "name": "status", "description": "native"},
                {"id": "fm-note", "name": "note", "description": "native"},
                {"id": "fm-ungrouped", "name": "unrelated", "description": "native"},
                {"id": "fm-clickup-status", "name": "clickUpStatus", "description": importer.IMPORTER_MARKER},
                {"id": "fm-dynamic", "name": "clickUpCfDynamic", "description": importer.IMPORTER_MARKER},
                {"id": "fm-technical", "name": "clickUpId", "description": importer.IMPORTER_MARKER},
                {"id": "fm-source-url", "name": "clickUpSourceUrl", "description": importer.IMPORTER_MARKER},
            ],
        }
        instance.dynamic_fields = {"source-dynamic": {"name": "clickUpCfDynamic"}}
        instance.configure_task_fields_widget()
        self.assertEqual(len(client.mutations), 1)
        payload = client.mutations[0]
        self.assertEqual({item["id"] for item in payload["groups"]}, {"group-general", "group-other"})
        general = next(item for item in payload["groups"] if item["id"] == "group-general")
        other = next(item for item in payload["groups"] if item["id"] == "group-other")
        self.assertEqual([item.get("viewFieldId") for item in other["fields"]], ["vf-note"])
        flattened_ids = [item.get("viewFieldId") for group in payload["groups"] for item in group["fields"] if item.get("viewFieldId")]
        self.assertEqual(flattened_ids.count("vf-note"), 1)
        self.assertNotIn("vf-ungrouped", flattened_ids)
        self.assertFalse(next(item for item in general["fields"] if item.get("viewFieldId") == "vf-technical")["isVisible"])
        self.assertTrue(next(item for item in general["fields"] if item.get("viewFieldId") == "vf-clickup-status")["isVisible"])
        self.assertTrue(next(item for item in general["fields"] if item.get("viewFieldId") == "vf-dynamic")["isVisible"])
        self.assertIn({"fieldMetadataId": "fm-source-url", "isVisible": True, "position": 3}, general["fields"])
        instance.configure_task_fields_widget()
        self.assertEqual(len(client.mutations), 1)
        self.assertEqual(next(item for item in client.fields if item["id"] == "vf-ungrouped")["position"], 9)


class CutoverLockTests(unittest.TestCase):
    def setUp(self):
        self.canonical_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.canonical_directory.cleanup)
        self.canonical_marker = Path(self.canonical_directory.name) / "clickup-cutover.json"
        self.marker_patch = patch.object(importer, "CANONICAL_CUTOVER_MARKER", self.canonical_marker)
        self.marker_patch.start()
        self.addCleanup(self.marker_patch.stop)

    def test_import_before_cutover_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            importer.assert_import_not_cut_over(Path(directory) / "admin.json", "target-test")

    def test_cutover_blocks_reimport_before_client_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hierarchy.json").write_text(json.dumps(hierarchy_fixture()), encoding="utf-8")
            (root / "tasks.json").write_text(json.dumps(tasks_fixture()), encoding="utf-8")
            (root / "clickup-cutover.json").write_text(
                json.dumps({"target_workspace_id": "target-test", "system_of_record": "twenty"}),
                encoding="utf-8",
            )
            with patch.object(importer, "TwentyClient") as client, patch("sys.stderr", new_callable=io.StringIO) as errors:
                result = importer.main([
                    "--hierarchy", str(root / "hierarchy.json"),
                    "--tasks", str(root / "tasks.json"), "--apply",
                    "--ack-production-write", importer.APPLY_ACK,
                    "--confirm-workspace-id", "workspace-1",
                    "--confirm-target-workspace-id", "target-test",
                    "--credential-file", str(root / "admin.json"),
                ])
            self.assertEqual(result, 2)
            self.assertIn("reimport is locked", errors.getvalue())
            client.assert_not_called()

    def test_malformed_or_wrong_target_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for state in ({}, {"target_workspace_id": "other-target"}, []):
                with self.subTest(state=state):
                    (root / "clickup-cutover.json").write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(importer.ContractError):
                        importer.assert_import_not_cut_over(root / "admin.json", "target-test")

    def test_moving_credentials_cannot_bypass_the_production_marker(self):
        self.canonical_marker.write_text(
            json.dumps({"target_workspace_id": "target-test"}), encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as other_directory:
            with self.assertRaisesRegex(importer.ContractError, "reimport is locked"):
                importer.assert_import_not_cut_over(Path(other_directory) / "admin-copy.json", "target-test")

    def test_invalid_or_unreadable_production_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as other_directory:
            credential = Path(other_directory) / "admin-copy.json"
            for content in ("{", "[]", '{"target_workspace_id":"other"}'):
                with self.subTest(content=content):
                    self.canonical_marker.write_text(content, encoding="utf-8")
                    with self.assertRaises((ValueError, importer.ContractError)):
                        importer.assert_import_not_cut_over(credential, "target-test")
            with patch.object(importer, "load_json", side_effect=PermissionError("unreadable")):
                with self.assertRaises(PermissionError):
                    importer.assert_import_not_cut_over(credential, "target-test")


if __name__ == "__main__":
    unittest.main()
