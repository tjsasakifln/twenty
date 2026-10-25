from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "clickup"
    / "set_native_cutover_gate.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("set_native_cutover_gate", MODULE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def function(
    uid: str,
    name: str,
    application_id: str,
    database_trigger: dict | None = None,
) -> dict:
    item = {
        "id": f"id-{uid}",
        "universalIdentifier": uid,
        "applicationId": application_id,
        "workspaceId": "workspace-id",
        "name": name,
        "updatedAt": "2026-10-25T20:00:00.000Z",
        "checksum": f"checksum-{uid}",
        "deletedAt": None,
        "isBuildUpToDate": True,
        "executionMode": "LIVE",
        "runtime": "nodejs22.x",
    }
    for key in gate.TRIGGER_FIELDS:
        item[key] = None
    item["databaseEventTriggerSettings"] = database_trigger
    return item


def inventory() -> list[dict]:
    app = "cutover-app-id"
    return [
        function(
            gate.CREATED_FUNCTION_UID,
            "task-cutover-created",
            app,
            {"eventName": "task.created"},
        ),
        function(
            gate.UPDATED_FUNCTION_UID,
            "task-cutover-updated",
            app,
            {"eventName": "task.updated", "updatedFields": gate.UPDATED_FIELDS},
        ),
        function("custom-1", "Extract domain from email", "custom-app"),
        function("custom-2", "Find matching company by domain", "custom-app"),
        function("custom-3", "Is this a personal email?", "custom-app"),
    ]


def allowlist(items: list[dict]) -> dict:
    return {"version": 1, "logic_functions": copy.deepcopy(items)}


class InventoryGuardTests(unittest.TestCase):
    def assert_guard_rejected_without_mutation(self, guard_callable) -> None:
        application = {
            "id": "cutover-app-id",
            "applicationVariables": [{"key": "CUTOVER_ENABLED", "value": "false"}],
        }
        with mock.patch.object(gate, "graphql") as graphql:
            with self.assertRaises(gate.CutoverError):
                gate.set_gate_value(
                    object(), application, "true", guard_callable
                )
            graphql.assert_not_called()

    def assert_rejected_without_mutation(self, items: list[dict]) -> None:
        expected = allowlist(inventory())
        def reject() -> dict:
            return gate.validate_logic_function_inventory(
                items, expected, "cutover-app-id"
            )
        self.assert_guard_rejected_without_mutation(reject)

    def test_accepts_exact_five_function_allowlist(self) -> None:
        items = inventory()
        self.assertEqual(
            gate.validate_logic_function_inventory(
                items, allowlist(items), "cutover-app-id"
            ),
            {"logic_function_count": 5, "cutover_trigger_count": 2},
        )

    def test_rejects_extra_uid_before_mutation(self) -> None:
        items = inventory() + [function("extra", "extra", "custom-app")]
        self.assert_rejected_without_mutation(items)

    def test_rejects_trigger_on_custom_function_before_mutation(self) -> None:
        items = inventory()
        items[2]["databaseEventTriggerSettings"] = {"eventName": "task.*"}
        self.assert_rejected_without_mutation(items)

    def test_rejects_changed_uid_or_event_before_mutation(self) -> None:
        items = inventory()
        items[0]["databaseEventTriggerSettings"] = {"eventName": "task.updated"}
        self.assert_rejected_without_mutation(items)

    def test_rejects_stale_build_before_mutation(self) -> None:
        items = inventory()
        items[0]["isBuildUpToDate"] = False
        expected = allowlist(items)
        expected["logic_functions"][0]["isBuildUpToDate"] = False
        self.assert_guard_rejected_without_mutation(
            lambda: gate.validate_logic_function_inventory(
                items, expected, "cutover-app-id"
            )
        )

    def test_rejects_missing_checksum_before_mutation(self) -> None:
        items = inventory()
        items[1]["checksum"] = None
        expected = allowlist(items)
        self.assert_guard_rejected_without_mutation(
            lambda: gate.validate_logic_function_inventory(
                items, expected, "cutover-app-id"
            )
        )

    def test_driver_contract_rejects_any_mismatch(self) -> None:
        valid_server = {
            "LOGIC_FUNCTION_TYPE": "DISABLED",
            "CODE_INTERPRETER_TYPE": "DISABLED",
        }
        valid_worker = {
            "LOGIC_FUNCTION_TYPE": "LOCAL",
            "CODE_INTERPRETER_TYPE": "DISABLED",
        }
        gate.validate_drivers(valid_server, valid_worker)
        for server, worker in [
            ({**valid_server, "LOGIC_FUNCTION_TYPE": "LOCAL"}, valid_worker),
            (valid_server, {**valid_worker, "LOGIC_FUNCTION_TYPE": "DISABLED"}),
            (valid_server, {**valid_worker, "CODE_INTERPRETER_TYPE": "LOCAL"}),
        ]:
            self.assert_guard_rejected_without_mutation(
                lambda server=server, worker=worker: gate.validate_drivers(
                    server, worker
                )
            )

    def test_rejects_queue_job_for_unknown_function(self) -> None:
        self.assert_guard_rejected_without_mutation(
            lambda: gate.validate_logic_function_jobs({"unknown"}, inventory())
        )

        gate.validate_logic_function_jobs(
            {inventory()[0]["id"], inventory()[1]["id"]}, inventory()
        )

    def test_rejects_queued_custom_function(self) -> None:
        self.assert_guard_rejected_without_mutation(
            lambda: gate.validate_logic_function_jobs(
                {inventory()[2]["id"]}, inventory()
            )
        )

    def test_queue_reader_inspects_non_numeric_hash_job_ids(self) -> None:
        responses = [
            mock.Mock(stdout="bull:logic-function-queue:workspace:job\n"),
            mock.Mock(stdout="hash\n"),
            mock.Mock(stdout='{"logicFunctionId":"owned-function"}\n'),
        ]
        with mock.patch.object(
            gate.subprocess, "run", side_effect=responses
        ) as run:
            self.assertEqual(
                gate.read_logic_function_job_ids("redis"), {"owned-function"}
            )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["TYPE", "bull:logic-function-queue:workspace:job"])

    def test_database_inventory_reader_parses_private_fields(self) -> None:
        row = inventory()[0]
        with mock.patch.object(
            gate.subprocess,
            "run",
            return_value=mock.Mock(stdout=gate.json.dumps(row) + "\n"),
        ) as run:
            self.assertEqual(
                gate.read_logic_functions_from_database("db", "name", "user"),
                [row],
            )
        self.assertIn("checksum", run.call_args.kwargs["input"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_disable_bypasses_broken_runtime_guard(self) -> None:
        before = {
            "id": "cutover-app-id",
            "applicationVariables": [{"key": "CUTOVER_ENABLED", "value": "true"}],
        }
        after = {
            "id": "cutover-app-id",
            "applicationVariables": [{"key": "CUTOVER_ENABLED", "value": "false"}],
        }
        guard = mock.Mock(side_effect=gate.CutoverError("broken inventory"))
        with mock.patch.object(
            gate, "graphql", return_value={"updateOneApplicationVariable": True}
        ) as graphql, mock.patch.object(gate, "read_application", return_value=after):
            self.assertEqual(
                gate.set_gate_value(object(), before, "false", guard),
                ("true", "false", None),
            )
            guard.assert_not_called()
            graphql.assert_called_once()


if __name__ == "__main__":
    unittest.main()
