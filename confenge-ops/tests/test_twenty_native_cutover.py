from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "twenty_native_cutover",
    ROOT / "integrations" / "clickup" / "twenty_native_cutover.py",
)
assert SPEC and SPEC.loader
cutover = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cutover
SPEC.loader.exec_module(cutover)


def source_fixture() -> dict:
    return {
        "tasks": [
            {
                "data": {
                    "id": "source-assigned",
                    "users": [
                        {
                            "userid": "source-user-1",
                            "type": "assigned",
                            "username": "never used for identity matching",
                        }
                    ],
                }
            },
            {
                "data": {
                    "id": "source-unassigned",
                    "users": [{"userid": "source-user-1", "type": "watcher"}],
                }
            },
        ]
    }


def metadata_field(
    name: str,
    *,
    label: str | None = None,
    description: str | None = None,
    options: list[dict] | None = None,
) -> dict:
    return {
        "id": f"field-{name}",
        "name": name,
        "label": label or name,
        "description": description,
        "options": options or [],
    }


class FakeClient:
    def __init__(self):
        owned = cutover.IMPORTER_MARKER
        self.fields = [
            metadata_field("clickUpId", description=owned),
            metadata_field("clickUpListExternalId", description=owned),
            metadata_field("clickUpList", description=owned),
            metadata_field(
                "clickUpStatus",
                label="Status ClickUp",
                description=owned,
                options=[
                    {"value": "organize", "label": "a organizar"},
                    {"value": "planned", "label": "planejada"},
                    {"value": "executing", "label": "em execução"},
                    {"value": "reviewing", "label": "em revisão"},
                    {"value": "concluded", "label": "concluída"},
                ],
            ),
            metadata_field("clickUpSourceUrl", description=owned),
            metadata_field("assignee", description="Task assignee"),
            metadata_field("status", description="Task status"),
            metadata_field("dueAt", description="Task due date"),
        ]
        self.calls: list[tuple[str, object, str]] = []

    def workspace(self):
        return {
            "id": "workspace-target",
            "activationStatus": "ACTIVE",
            "isPublicInviteLinkEnabled": False,
        }

    def list_rest(self, path: str, *, limit: int = 200):
        if path == "/rest/workspaceMembers":
            return [{"id": "member-target"}]
        if path == "/rest/metadata/objects":
            return [{"id": "task-object", "nameSingular": "task", "fields": self.fields}]
        if path == "/rest/tasks":
            return [
                {
                    "id": "task-assigned",
                    "clickUpId": "source-assigned",
                    "assigneeId": None,
                    "updatedAt": "2026-10-25T18:00:00.000Z",
                },
                {
                    "id": "task-unassigned",
                    "clickUpId": "source-unassigned",
                    "assigneeId": None,
                    "updatedAt": "2026-10-25T18:00:00.000Z",
                },
            ]
        if path == "/rest/clickUpLists":
            return [{"id": "list-record", "externalId": "source-list"}]
        if path == "/rest/metadata/viewFilters":
            return [
                {
                    "id": "legacy-filter",
                    "fieldMetadataId": "field-clickUpListExternalId",
                    "operand": "CONTAINS",
                    "value": "source-list",
                }
            ]
        if path.startswith("/rest/metadata/views?"):
            return [
                {"id": "fields-widget", "type": "FIELDS_WIDGET", "isActive": True},
                {
                    "id": cutover.CONTRACTUAL_MILESTONES_VIEW_ID,
                    "name": "Marcos contratuais",
                    "type": "TABLE",
                    "isActive": True,
                },
                {
                    "id": cutover.OPERATIONAL_CALENDAR_VIEW_ID,
                    "name": "Prazos",
                    "type": "CALENDAR",
                    "isActive": True,
                },
            ]
        if path.startswith("/rest/metadata/viewFields?"):
            return [
                {
                    "id": f"view-{item['name']}",
                    "fieldMetadataId": item["id"],
                    "isVisible": item["name"] != "clickUpSourceUrl",
                }
                for item in self.fields
            ]
        raise AssertionError(f"unexpected path {path}")

    def request(self, path: str, payload=None, method: str = "GET"):
        self.calls.append((path, payload, method))
        if path == "/graphql":
            task_id = payload["variables"]["filter"]["id"]["eq"]
            return {"data": {"updateTasks": [{"id": task_id}]}}
        if method == "GET" and path == "/rest/metadata/viewFilters/legacy-filter":
            return {
                "id": "legacy-filter",
                "fieldMetadataId": "field-clickUpListExternalId",
                "operand": "CONTAINS",
                "value": "source-list",
            }
        if method == "GET" and path.startswith("/rest/metadata/fields/"):
            field_id = path.rsplit("/", 1)[-1]
            return next(item for item in self.fields if item["id"] == field_id)
        if method == "GET" and path.startswith("/rest/metadata/viewFields/"):
            name = path.rsplit("/", 1)[-1].removeprefix("view-")
            return {
                "id": f"view-{name}",
                "fieldMetadataId": f"field-{name}",
                "isVisible": name != "clickUpSourceUrl",
            }
        if method == "GET" and path == (
            "/rest/metadata/views/" + cutover.OPERATIONAL_CALENDAR_VIEW_ID
        ):
            return {
                "id": cutover.OPERATIONAL_CALENDAR_VIEW_ID,
                "name": "Prazos",
                "type": "CALENDAR",
                "isActive": True,
            }
        return {"data": {"id": path.rsplit("/", 1)[-1]}}


class AssignmentEvidenceTests(unittest.TestCase):
    def test_credentials_reject_remote_http_before_client_can_send_token(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials_path = Path(directory) / "admin.json"
            credentials_path.write_text(
                json.dumps(
                    {
                        "base_url": "http://attacker.invalid",
                        "api_key": "must-not-leave-process",
                        "workspace_id": "workspace-target",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(cutover.CutoverError, "must use HTTPS"):
                cutover.credentials(credentials_path)

    def test_credentials_reject_wrong_https_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials_path = Path(directory) / "admin.json"
            credentials_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://attacker.invalid",
                        "api_key": "must-not-leave-process",
                        "workspace_id": "workspace-target",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(cutover.CutoverError, "hostname"):
                cutover.credentials(credentials_path)

    def test_uses_only_explicit_assigned_type_and_ignores_names(self):
        assigned, user_id = cutover.assignment_evidence(source_fixture())

        self.assertEqual(assigned, {"source-assigned": "source-user-1"})
        self.assertEqual(user_id, "source-user-1")

    def test_rejects_more_than_one_source_assignee_identity(self):
        document = source_fixture()
        document["tasks"].append(
            {
                "data": {
                    "id": "other",
                    "users": [{"userid": "source-user-2", "type": "assigned"}],
                }
            }
        )

        with self.assertRaisesRegex(cutover.CutoverError, "exactly one distinct"):
            cutover.assignment_evidence(document)


class ConfigurationPlanTests(unittest.TestCase):
    def build_plan(self, client: FakeClient | None = None):
        return cutover.build_configuration_plan(
            client or FakeClient(),
            source_fixture(),
            expected_workspace_id="workspace-target",
            default_list_external_id="source-list",
            explicit_stage_map={
                "a organizar": "TODO",
                "planejada": "TODO",
                "em execução": "IN_PROGRESS",
                "em revisão": "IN_PROGRESS",
                "concluída": "DONE",
            },
        )

    def test_assigns_only_source_assigned_task(self):
        plan = self.build_plan()

        self.assertEqual(plan.assignment_evidence_count, 1)
        self.assertEqual(
            plan.assignments,
            [
                (
                    "task-assigned",
                    "2026-10-25T18:00:00.000Z",
                    {"assigneeId": "member-target"},
                )
            ],
        )

    def test_converts_text_list_filter_to_direct_relation(self):
        plan = self.build_plan()
        self.assertEqual(len(plan.filters), 1)
        _, _expected, patch = plan.filters[0]
        self.assertEqual(patch["fieldMetadataId"], "field-clickUpList")
        self.assertEqual(patch["operand"], "IS")
        self.assertEqual(
            json.loads(patch["value"]),
            {"isCurrentRecordSelected": False, "selectedRecordIds": ["list-record"]},
        )

    def test_emits_complete_private_application_variable_document(self):
        plan = self.build_plan()

        self.assertEqual(
            set(plan.application_variables),
            {
                "CUTOVER_MEMBER_ID",
                "CUTOVER_DEFAULT_LIST_ID",
                "CUTOVER_STAGE_STATUS_MAP",
                "CUTOVER_STAGE_TODO",
                "CUTOVER_STAGE_IN_PROGRESS",
                "CUTOVER_STAGE_DONE",
                "CONTRACTUAL_MILESTONES_VIEW_ID",
                "OPERATIONAL_CALENDAR_VIEW_ID",
            },
        )
        self.assertEqual(
            plan.application_variables["CONTRACTUAL_MILESTONES_VIEW_ID"],
            cutover.CONTRACTUAL_MILESTONES_VIEW_ID,
        )
        self.assertEqual(
            plan.application_variables["OPERATIONAL_CALENDAR_VIEW_ID"],
            cutover.OPERATIONAL_CALENDAR_VIEW_ID,
        )
        self.assertEqual(
            json.loads(plan.application_variables["CUTOVER_STAGE_STATUS_MAP"])["reviewing"],
            "IN_PROGRESS",
        )

    def test_renames_only_the_deterministic_legacy_calendar_view(self):
        plan = self.build_plan()

        self.assertEqual(
            plan.metadata_views,
            [
                (
                    cutover.OPERATIONAL_CALENDAR_VIEW_ID,
                    {"name": "Prazos"},
                    {"name": "Datas sem horário (legado)"},
                )
            ],
        )

    def test_apply_executes_exactly_the_planned_patches(self):
        client = FakeClient()
        plan = self.build_plan(client)
        cutover.apply_configuration(client, plan)

        writes = [call for call in client.calls if call[2] != "GET"]
        self.assertEqual(len(writes), plan.summary()["writes_planned"])
        paths = [path for path, _payload, method in client.calls if method == "PATCH"]
        self.assertIn("/graphql", [path for path, _payload, _method in client.calls])
        self.assertIn("/rest/metadata/viewFilters/legacy-filter", paths)

    def test_assignment_cas_zero_rows_aborts_without_blind_patch(self):
        class ConflictedClient(FakeClient):
            def request(self, path: str, payload=None, method: str = "GET"):
                if path == "/graphql":
                    self.calls.append((path, payload, method))
                    return {"data": {"updateTasks": []}}
                return super().request(path, payload, method)

        client = ConflictedClient()
        plan = self.build_plan(client)

        with self.assertRaisesRegex(cutover.CutoverError, "concurrent task edit"):
            cutover.apply_configuration(client, plan)
        self.assertFalse(any(call[2] == "PATCH" for call in client.calls))

    def test_metadata_revalidation_refuses_concurrent_label_edit(self):
        class ConcurrentMetadataClient(FakeClient):
            def request(self, path: str, payload=None, method: str = "GET"):
                result = super().request(path, payload, method)
                if method == "GET" and path == "/rest/metadata/fields/field-assignee":
                    return {**result, "label": "Edited concurrently"}
                return result

        client = ConcurrentMetadataClient()
        plan = self.build_plan(client)

        with self.assertRaisesRegex(cutover.CutoverError, "concurrent edit"):
            cutover.apply_configuration(client, plan)

    def test_failure_journal_records_completed_prefix_for_safe_resume(self):
        class FailingClient(FakeClient):
            def request(self, path: str, payload=None, method: str = "GET"):
                if method == "PATCH" and path == "/rest/metadata/fields/field-assignee":
                    raise cutover.CutoverError("injected failure")
                return super().request(path, payload, method)

        client = FailingClient()
        plan = self.build_plan(client)
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "journal.json"
            with self.assertRaisesRegex(cutover.CutoverError, "injected failure"):
                cutover.apply_configuration(client, plan, journal_path=journal_path)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))

        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["error_type"], "CutoverError")
        self.assertEqual(
            [item["kind"] for item in journal["completed"]],
            ["assignment", "relation_filter"],
        )

    def test_resume_excludes_completed_assignment_and_preserves_later_unassign(self):
        plan = self.build_plan()
        resumed = cutover.exclude_completed_operations(
            plan,
            {
                "completed": [
                    {"kind": "assignment", "id": "task-assigned"},
                ]
            },
        )

        self.assertEqual(resumed.assignments, [])

    def test_marker_accepts_only_preparing_before_first_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            marker_path = Path(directory) / "marker.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "target_workspace_id": "workspace-target",
                        "phase": "native-configured",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(cutover.CutoverError, "exactly preparing"):
                cutover.verify_marker(
                    marker_path,
                    "workspace-target",
                    required_phase="preparing",
                )

    def test_resume_recovers_crash_after_complete_journal_before_marker_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker_path = root / "marker.json"
            journal_path = root / "journal.json"
            source_path = root / "source.json"
            stage_map_path = root / "stage-map.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "target_workspace_id": "workspace-target",
                        "phase": "preparing",
                    }
                ),
                encoding="utf-8",
            )
            journal_path.write_text(
                json.dumps({"status": "complete", "completed": []}),
                encoding="utf-8",
            )
            source_path.write_text("{}", encoding="utf-8")
            stage_map_path.write_text(
                json.dumps(
                    {
                        "a organizar": "TODO",
                        "em execução": "IN_PROGRESS",
                        "concluída": "DONE",
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                credentials=root / "admin.json",
                source_tasks=source_path,
                stage_map=stage_map_path,
                todo_stage="a organizar",
                in_progress_stage="em execução",
                done_stage="concluída",
                default_list_external_id="source-list",
                apply=True,
                ack=cutover.ACKNOWLEDGEMENT,
                cutover_marker=marker_path,
                resume=True,
                journal=journal_path,
                application_variables=root / "vars.json",
                contractual_milestones_view_id="milestones-view",
                operational_calendar_view_id="calendar-view",
            )
            converged = cutover.ConfigurationPlan()
            with mock.patch.object(
                cutover,
                "credentials",
                return_value=(FakeClient(), "workspace-target"),
            ), mock.patch.object(
                cutover,
                "build_configuration_plan",
                return_value=converged,
            ):
                self.assertEqual(cutover.configure_command(args), 0)

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["phase"], "native-configured")
            self.assertEqual(
                marker["native_configuration_journal"], str(journal_path)
            )


if __name__ == "__main__":
    unittest.main()
