from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "operational_overview",
    ROOT
    / "integrations"
    / "operational-overview"
    / "operational_overview.py",
)
assert SPEC and SPEC.loader
overview = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = overview
SPEC.loader.exec_module(overview)


def field(
    identifier: str,
    name: str,
    label: str,
    field_type: str,
    *,
    options: list[dict] | None = None,
    description: str | None = None,
) -> dict:
    return {
        "id": identifier,
        "name": name,
        "label": label,
        "type": field_type,
        "options": options or [],
        "description": description,
    }


class FakeClient:
    def __init__(self, *, duplicate_dashboard: bool = False, with_routines: bool = False):
        self.read_count = 0
        self.write_count = 0
        self.calls: list[tuple[str, object, str]] = []
        self._next = 1
        self.workspace_value = {
            "id": "workspace-target",
            "activationStatus": "ACTIVE",
            "isPublicInviteLinkEnabled": False,
        }
        self.task = {
            "id": "task-object",
            "nameSingular": "task",
            "namePlural": "tasks",
            "labelSingular": "Task",
            "fields": [
                field("field-title", "title", "Title", "TEXT"),
                field(
                    "field-status",
                    "status",
                    "Situação geral",
                    "SELECT",
                    options=[
                        {"id": "todo", "value": "TODO", "label": "To do"},
                        {
                            "id": "progress",
                            "value": "IN_PROGRESS",
                            "label": "In progress",
                        },
                        {"id": "done", "value": "DONE", "label": "Done"},
                    ],
                ),
                field("field-due-at", "dueAt", "Prazo com horário", "DATE_TIME"),
                field("field-due-on", "clickUpDueOn", "Prazo (data)", "DATE"),
                field("field-stage", "clickUpStatus", "Etapa", "SELECT"),
                field("field-assignee", "assignee", "Responsável", "RELATION"),
                field(
                    "field-front",
                    "clickUpCfFront",
                    "Frente",
                    "SELECT",
                    options=[
                        {"id": "front-1", "value": "F_1", "label": "Front 1"},
                        {"id": "front-2", "value": "F_2", "label": "Front 2"},
                    ],
                ),
                field(
                    "field-focus",
                    "clickUpCfFocus",
                    "Foco atual",
                    "SELECT",
                    options=[
                        {"id": "focus-1", "value": "P_1", "label": "1 | Prioridade"},
                        {"id": "focus-2", "value": "P_2", "label": "2 | Prioridade"},
                        {"id": "focus-3", "value": "P_3", "label": "3 | Prioridade"},
                    ],
                ),
            ],
        }
        self.objects = [self.task]
        if with_routines:
            self.objects.append(
                {
                    "id": "routine-object",
                    "nameSingular": "codexRoutine",
                    "namePlural": "codexRoutines",
                    "fields": [
                        field(f"routine-{name}", name, name, "TEXT")
                        for name in (
                            "name",
                            "scheduleDescription",
                            "schedulerStatus",
                            "lastRunStatus",
                            "lastCoverageStatus",
                            "lastFinishedAt",
                            "lastSummary",
                        )
                    ],
                }
            )
        self.dashboards = [
            {
                "id": "dashboard-target",
                "title": overview.DEFAULT_DASHBOARD_TITLE,
                "pageLayoutId": "layout-target",
            }
        ]
        if duplicate_dashboard:
            self.dashboards.append(
                {
                    "id": "dashboard-duplicate",
                    "title": overview.DEFAULT_DASHBOARD_TITLE,
                    "pageLayoutId": "layout-duplicate",
                }
            )
        self.tabs: list[dict] = [
            {
                "id": "tab-today",
                "title": "Hoje",
                "pageLayoutId": "layout-target",
                "layoutMode": "GRID",
            }
        ]
        self.widgets: list[dict] = []
        self.views: list[dict] = []
        self.view_fields: list[dict] = []
        self.view_filters: list[dict] = []
        self.view_filter_groups: list[dict] = []
        self.view_sorts: list[dict] = []
        self.relation_graphs: dict[str, dict] = {}
        self.ai_models: list[dict] = []
        self.task_records = [
            {
                "id": "task-1",
                "status": "TODO",
                "dueAt": None,
                "clickUpDueOn": "2026-10-25",
                "clickUpCfFront": "F_1",
                "clickUpCfFocus": "P_1",
            },
            {
                "id": "task-2",
                "status": "DONE",
                "dueAt": None,
                "clickUpDueOn": None,
                "clickUpCfFront": "F_2",
                "clickUpCfFocus": None,
            },
        ]

    def _id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next}"
        self._next += 1
        return value

    def workspace(self):
        self.read_count += 1
        return dict(self.workspace_value)

    def client_config(self):
        self.read_count += 1
        return {
            "aiModels": self.ai_models,
            "aiEvaluationModels": [
                {"modelId": "evaluation-only", "isAvailable": False}
            ],
            "aiModelTiers": [],
        }

    def workspace_query(self, query: str, variables: dict):
        del query, variables
        self.read_count += 1
        return {
            "tasks": {
                "edges": [{"node": item} for item in self.task_records],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }

    def relation_field(self, field_id: str):
        self.read_count += 1
        return json.loads(json.dumps(self.relation_graphs[field_id]))

    def list_rest(self, path: str, *, limit: int = 200):
        del limit
        self.read_count += 1
        route, _, query_string = path.partition("?")
        query = urllib.parse.parse_qs(query_string)
        if route == "/rest/metadata/objects":
            return self.objects
        if route == "/rest/dashboards":
            return self.dashboards
        if route == "/rest/metadata/pageLayoutTabs":
            layout_id = (query.get("pageLayoutId") or [None])[0]
            return [item for item in self.tabs if item["pageLayoutId"] == layout_id]
        if route == "/rest/metadata/pageLayoutWidgets":
            tab_id = (query.get("pageLayoutTabId") or [None])[0]
            return [item for item in self.widgets if item["pageLayoutTabId"] == tab_id]
        if route == "/rest/metadata/views":
            object_id = (query.get("objectMetadataId") or [None])[0]
            return [item for item in self.views if item["objectMetadataId"] == object_id]
        mapping = {
            "/rest/metadata/viewFields": self.view_fields,
            "/rest/metadata/viewFilters": self.view_filters,
            "/rest/metadata/viewFilterGroups": self.view_filter_groups,
            "/rest/metadata/viewSorts": self.view_sorts,
        }
        if route in mapping:
            view_id = (query.get("viewId") or [None])[0]
            return [item for item in mapping[route] if item["viewId"] == view_id]
        raise AssertionError(f"unexpected list path {path}")

    def request(self, path: str, payload=None, method: str = "GET"):
        method = method.upper()
        self.calls.append((path, payload, method))
        if method == "GET":
            self.read_count += 1
            raise AssertionError(f"unexpected GET {path}")
        self.write_count += 1
        if method == "POST" and path == "/rest/metadata/objects":
            created = {
                **payload,
                "id": "goal-object",
                "fields": [field("goal-name", "name", "Name", "TEXT")],
            }
            self.objects.append(created)
            return created
        if method == "PATCH" and path.startswith("/rest/metadata/objects/"):
            identifier = path.rsplit("/", 1)[-1]
            current = next(item for item in self.objects if item["id"] == identifier)
            current.update(payload)
            return current
        if method == "POST" and path == "/rest/metadata/fields":
            source = next(
                item for item in self.objects if item["id"] == payload["objectMetadataId"]
            )
            created = {**payload, "id": self._id("field")}
            if payload["type"] == "RELATION":
                relation_payload = payload["relationCreationPayload"]
                created["settings"] = {
                    "relationType": relation_payload["type"]
                }
                target = next(
                    item
                    for item in self.objects
                    if item["id"]
                    == relation_payload["targetObjectMetadataId"]
                )
                inverse = field(
                    self._id("inverse"),
                    overview.GOAL_TASKS_FIELD_NAME,
                    relation_payload["targetFieldLabel"],
                    "RELATION",
                )
                inverse["settings"] = {"relationType": "ONE_TO_MANY"}
                created["relation"] = {
                    "type": "MANY_TO_ONE",
                    "sourceObjectMetadata": {"id": source["id"]},
                    "targetObjectMetadata": {"id": target["id"]},
                    "sourceFieldMetadata": {
                        "id": created["id"],
                        "name": created["name"],
                    },
                    "targetFieldMetadata": {
                        "id": inverse["id"],
                        "name": inverse["name"],
                    },
                }
                inverse["relation"] = {
                    "type": "ONE_TO_MANY",
                    "sourceObjectMetadata": {"id": target["id"]},
                    "targetObjectMetadata": {"id": source["id"]},
                    "sourceFieldMetadata": {
                        "id": inverse["id"],
                        "name": inverse["name"],
                    },
                    "targetFieldMetadata": {
                        "id": created["id"],
                        "name": created["name"],
                    },
                }
                self.relation_graphs[created["id"]] = json.loads(json.dumps(created))
                self.relation_graphs[inverse["id"]] = json.loads(json.dumps(inverse))
                target["fields"].append(inverse)
            source["fields"].append(created)
            return created
        if method == "PATCH" and path.startswith("/rest/metadata/fields/"):
            identifier = path.rsplit("/", 1)[-1]
            current = next(
                item
                for obj in self.objects
                for item in obj.get("fields", [])
                if item["id"] == identifier
            )
            current.update(payload)
            return current
        if method == "POST" and path == "/rest/metadata/views":
            created = dict(payload)
            self.views.append(created)
            return created
        if method == "PATCH" and path.startswith("/rest/metadata/views/"):
            identifier = path.rsplit("/", 1)[-1]
            current = next(item for item in self.views if item["id"] == identifier)
            current.update(payload)
            return current
        collections = {
            "/rest/metadata/viewFields": self.view_fields,
            "/rest/metadata/viewFilters": self.view_filters,
            "/rest/metadata/viewFilterGroups": self.view_filter_groups,
            "/rest/metadata/viewSorts": self.view_sorts,
        }
        if method == "POST" and path in collections:
            created = dict(payload)
            collections[path].append(created)
            return created
        for route, collection in collections.items():
            if method == "PATCH" and path.startswith(route + "/"):
                identifier = path.rsplit("/", 1)[-1]
                current = next(item for item in collection if item["id"] == identifier)
                current.update(payload)
                return current
        if method == "POST" and path == "/rest/metadata/pageLayoutTabs":
            created = {**payload, "id": self._id("tab")}
            self.tabs.append(created)
            return created
        if method == "POST" and path == "/rest/metadata/pageLayoutWidgets":
            created = {**payload, "id": self._id("widget")}
            self.widgets.append(created)
            return created
        raise AssertionError(f"unexpected mutation {method} {path}")


class SourceEvidenceTests(unittest.TestCase):
    def test_summarizes_source_metadata_without_copying_task_content(self):
        tasks = {
            "captured_at": "2026-10-25",
            "tasks": [
                {
                    "data": {
                        "id": "one",
                        "name": "sensitive title",
                        "time_mgmt": {"due_date": "1"},
                        "fields": [
                            {"field_id": "front", "value": "front-a"},
                            {"field_id": "focus", "value": "priority-1"},
                        ],
                    }
                },
                {"data": {"id": "two", "name": "another title", "fields": []}},
            ],
        }
        fields = {
            "fields": [
                {
                    "id": "front",
                    "name": "Frente",
                    "type_config": {
                        "options": [{"id": "front-a", "name": "Front A"}]
                    },
                },
                {
                    "id": "focus",
                    "name": "Foco atual",
                    "type_config": {
                        "options": [
                            {"id": "priority-1", "name": "1 | Prioridade"}
                        ]
                    },
                },
            ]
        }

        evidence = overview.source_snapshot_evidence(tasks, fields)

        self.assertEqual(evidence["task_count"], 2)
        self.assertEqual(evidence["tasks_with_due_date"], 1)
        self.assertEqual(evidence["front_counts"], {"<empty>": 1, "Front A": 1})
        self.assertNotIn("sensitive title", overview.canonical(evidence))


class ReadOnlyPlanTests(unittest.TestCase):
    def test_reuses_existing_dashboard_and_distinguishes_ai_visibility(self):
        client = FakeClient()
        settings = overview.OverviewSettings()

        inspection = overview.inspect_live(client, settings)
        report = overview.plan_report(inspection, None)

        self.assertEqual(report["dashboard"]["id"], "dashboard-target")
        self.assertFalse(report["dashboard"]["operation_tab_exists"])
        self.assertTrue(report["ai"]["feature_present"])
        self.assertFalse(report["ai"]["configured"])
        self.assertFalse(report["ai"]["business_data_sent"])
        self.assertEqual(report["task_metadata"]["record_counts"]["total"], 2)
        self.assertEqual(client.write_count, 0)

    def test_rejects_ambiguous_dashboard_title(self):
        with self.assertRaisesRegex(overview.OverviewError, "exactly one existing"):
            overview.inspect_live(FakeClient(duplicate_dashboard=True), overview.OverviewSettings())


class ApplyTests(unittest.TestCase):
    def apply_once(self, client: FakeClient):
        inspection = overview.inspect_live(client, overview.OverviewSettings())
        return overview.Configurator(
            client, inspection, overview.OverviewSettings()
        ).apply()

    def test_creates_only_schema_views_tab_and_widgets(self):
        client = FakeClient()

        result = self.apply_once(client)

        goal = next(item for item in client.objects if item["nameSingular"] == "operationalGoal")
        self.assertEqual(goal["description"], overview.MARKER)
        self.assertEqual(
            next(item for item in goal["fields"] if item["name"] == "name")["label"],
            "Nome",
        )
        self.assertEqual(result["goal_records_created"], 0)
        self.assertEqual(result["task_records_updated"], 0)
        self.assertEqual(len(client.dashboards), 1)
        self.assertEqual(
            {item["title"] for item in client.widgets},
            {
                "Operação | Foco atual",
                "Operação | Pendências vencidas",
                "Operação | Progresso por frente",
                "Operação | Metas",
            },
        )
        self.assertFalse(any(path.startswith("/rest/operationalGoals") for path, _, _ in client.calls))

    def test_second_run_is_idempotent(self):
        client = FakeClient()
        self.apply_once(client)
        first_write_count = client.write_count

        result = self.apply_once(client)

        self.assertEqual(client.write_count, first_write_count)
        self.assertEqual(result["writes"], first_write_count)
        self.assertEqual(result["changes"], {})

    def test_overdue_view_uses_or_group_for_both_deadline_kinds(self):
        client = FakeClient()
        self.apply_once(client)

        root_id = overview.deterministic_uuid(
            "view-filter-group", "task-overdue", "root"
        )
        due_id = overview.deterministic_uuid(
            "view-filter-group", "task-overdue", "due"
        )
        root = next(item for item in client.view_filter_groups if item["id"] == root_id)
        due = next(item for item in client.view_filter_groups if item["id"] == due_id)
        due_filters = [
            item
            for item in client.view_filters
            if item.get("viewFilterGroupId") == due_id
        ]

        self.assertEqual(root["logicalOperator"], "AND")
        self.assertEqual(due["logicalOperator"], "OR")
        self.assertEqual(due["parentViewFilterGroupId"], root_id)
        self.assertEqual(
            {item["fieldMetadataId"] for item in due_filters},
            {"field-due-at", "field-due-on"},
        )
        self.assertTrue(all(item["operand"] == "IS_IN_PAST" for item in due_filters))

    def test_adds_routine_widget_only_when_canonical_model_is_complete(self):
        client = FakeClient(with_routines=True)

        self.apply_once(client)

        self.assertIn(
            "Operação | Rotinas Codex", {item["title"] for item in client.widgets}
        )
        self.assertEqual(
            next(
                item
                for item in client.widgets
                if item["title"] == "Operação | Rotinas Codex"
            )["objectMetadataId"],
            "routine-object",
        )
        routine_view_fields = {
            item["fieldMetadataId"]
            for item in client.view_fields
            if item["viewId"] == overview.ROUTINES_VIEW_ID and item["isVisible"]
        }
        self.assertEqual(
            routine_view_fields,
            {
                f"routine-{name}"
                for name in (
                    "name",
                    "scheduleDescription",
                    "schedulerStatus",
                    "lastRunStatus",
                    "lastCoverageStatus",
                    "lastFinishedAt",
                    "lastSummary",
                )
            },
        )

    def test_refuses_to_overwrite_widget_title_collision(self):
        client = FakeClient()
        self.apply_once(client)
        focus_widget = next(
            item for item in client.widgets if item["title"] == "Operação | Foco atual"
        )
        focus_widget["configuration"] = {"configurationType": "RECORD_TABLE"}

        with self.assertRaisesRegex(overview.OverviewError, "refusing to overwrite"):
            self.apply_once(client)

    def test_owned_views_fail_closed_on_unexpected_filter_group_or_sort(self):
        cases = {
            "filter": "view_filters",
            "filter_group": "view_filter_groups",
            "sort": "view_sorts",
        }
        for child_kind, attribute in cases.items():
            with self.subTest(child_kind=child_kind):
                client = FakeClient()
                self.apply_once(client)
                writes_before = client.write_count
                getattr(client, attribute).append(
                    {
                        "id": f"manual-{child_kind}",
                        "viewId": overview.FOCUS_VIEW_ID,
                    }
                )

                with self.assertRaisesRegex(
                    overview.OverviewError, f"unexpected .* children"
                ):
                    self.apply_once(client)

                self.assertEqual(client.write_count, writes_before)

    def test_owned_view_reconciles_stale_sort_subfield(self):
        client = FakeClient()
        self.apply_once(client)
        sort_id = overview.deterministic_uuid("view-sort", "task-focus", 0)
        focus_sort = next(item for item in client.view_sorts if item["id"] == sort_id)
        focus_sort["subFieldName"] = "stale"
        writes_before = client.write_count

        result = self.apply_once(client)

        self.assertIsNone(focus_sort["subFieldName"])
        self.assertEqual(client.write_count, writes_before + 1)
        self.assertEqual(result["changes"], {"view_sorts_updated": 1})

    def test_goal_relation_rejects_wrong_source_target_with_marker_present(self):
        client = FakeClient()
        self.apply_once(client)
        writes_before = client.write_count
        source = next(
            item for item in client.task["fields"] if item["name"] == "operationalGoal"
        )
        source["relation"]["targetObjectMetadata"]["id"] = "wrong-object"

        with self.assertRaisesRegex(overview.OverviewError, "relation graph"):
            self.apply_once(client)

        self.assertEqual(client.write_count, writes_before)

    def test_goal_relation_rejects_wrong_inverse_direction_with_names_present(self):
        client = FakeClient()
        self.apply_once(client)
        writes_before = client.write_count
        goal = next(
            item for item in client.objects if item["nameSingular"] == "operationalGoal"
        )
        inverse = next(
            item
            for item in goal["fields"]
            if item["name"] == overview.GOAL_TASKS_FIELD_NAME
        )
        inverse["settings"]["relationType"] = "MANY_TO_ONE"
        inverse["relation"]["type"] = "MANY_TO_ONE"

        with self.assertRaisesRegex(overview.OverviewError, "relation graph"):
            self.apply_once(client)

        self.assertEqual(client.write_count, writes_before)

    def test_goal_relation_hydrates_graph_when_rest_omits_it(self):
        client = FakeClient()
        self.apply_once(client)
        writes_before = client.write_count
        goal = next(
            item for item in client.objects if item["nameSingular"] == "operationalGoal"
        )
        source = next(
            item for item in client.task["fields"] if item["name"] == "operationalGoal"
        )
        inverse = next(
            item
            for item in goal["fields"]
            if item["name"] == overview.GOAL_TASKS_FIELD_NAME
        )
        source.pop("relation")
        inverse.pop("relation")

        result = self.apply_once(client)

        self.assertEqual(client.write_count, writes_before)
        self.assertEqual(result["changes"], {})


class SafetyTests(unittest.TestCase):
    def test_cli_cannot_override_canonical_https_hostname(self):
        with self.assertRaises(SystemExit):
            overview.build_parser().parse_args(
                [
                    "--credential-file",
                    "admin.json",
                    "--expected-hostname",
                    "attacker.invalid",
                ]
            )

    def test_credentials_reject_noncanonical_https_before_token_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            path.write_text(
                json.dumps(
                    {
                        "base_url": "https://attacker.invalid",
                        "api_key": "must-not-leave-process",
                        "workspace_id": "workspace-target",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(overview.OverviewError, "canonical HTTPS"):
                overview.credentials(path)

    def test_credentials_allow_host_local_http(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            path.write_text(
                json.dumps(
                    {
                        "base_url": "http://127.0.0.1:3000",
                        "api_key": "host-local-token",
                        "workspace_id": "workspace-target",
                    }
                ),
                encoding="utf-8",
            )

            client, workspace_id = overview.credentials(path)

        self.assertEqual(client.base_url, "http://127.0.0.1:3000")
        self.assertEqual(workspace_id, "workspace-target")

    def test_apply_requires_exact_workspace_chain_and_disabled_public_invite(self):
        client = FakeClient()
        inspection = overview.inspect_live(client, overview.OverviewSettings())

        with self.assertRaisesRegex(overview.OverviewError, "workspace confirmation"):
            overview.verify_apply_preflight(
                inspection,
                credential_workspace_id="workspace-target",
                confirmed_workspace_id="wrong",
            )

        inspection.workspace["isPublicInviteLinkEnabled"] = True
        with self.assertRaisesRegex(overview.OverviewError, "public workspace invite"):
            overview.verify_apply_preflight(
                inspection,
                credential_workspace_id="workspace-target",
                confirmed_workspace_id="workspace-target",
            )


if __name__ == "__main__":
    unittest.main()
