from __future__ import annotations

import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "codex-routines"
sys.path.insert(0, str(INTEGRATION))

from catalog import reconcile_catalog  # noqa: E402
from cli import remote_deliver  # noqa: E402
from contract import (  # noqa: E402
    ContractError,
    build_event,
    load_catalog,
    validate_catalog_document,
    validate_event,
)
from outbox import Outbox  # noqa: E402
from twenty import (  # noqa: E402
    ApiError,
    MARKER,
    ROUTINE_FIELDS,
    RUN_FIELDS,
    TASK_FIELDS,
    RoutineGateway,
    TwentyClient,
    schedule_description,
    validate_base_url,
    _field_payload,
)


CATALOG_PATH = INTEGRATION / "catalog.example.json"
NOW = "2026-10-25T18:00:00-03:00"


def catalog():
    return load_catalog(CATALOG_PATH)


def start_event(**updates):
    payload = {
        "automation_id": "example-knowledge-routine",
        "event_type": "START",
        "scheduled_for": "2026-10-25T21:00:00-03:00",
        "observed_at": NOW,
        "trigger": "SCHEDULED",
        "status": "RUNNING",
        "catalog": catalog(),
        "startedAt": NOW,
    }
    payload.update(updates)
    return build_event(**payload)


def outcome_event(**updates):
    payload = {
        "automation_id": "example-knowledge-routine",
        "event_type": "OUTCOME",
        "scheduled_for": "2026-10-25T21:00:00-03:00",
        "observed_at": "2026-10-25T21:07:00-03:00",
        "trigger": "SCHEDULED",
        "status": "SUCCEEDED",
        "catalog": catalog(),
        "startedAt": NOW,
        "finishedAt": "2026-10-25T21:07:00-03:00",
        "coverageStatus": "COMPLETE",
        "summary": "2 notas incorporadas e verificadas.",
        "aggregateMetrics": {"notesCaptured": 2, "conflicts": 0},
        "domainRefs": [],
        "operationalItems": [],
    }
    payload.update(updates)
    return build_event(**payload)


class ContractTests(unittest.TestCase):
    def test_catalog_has_exact_expected_routines(self):
        loaded = catalog()
        self.assertEqual(len(loaded["routines"]), 2)
        self.assertEqual(
            set(loaded["byId"]),
            {
                "example-knowledge-routine",
                "example-status-routine",
            },
        )

    def test_catalog_rejects_prompt_and_unknown_fields(self):
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        raw["routines"][0]["prompt"] = "must never be copied"
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            validate_catalog_document(raw)
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        raw["routines"][0]["allowExtraSync"] = "false"
        with self.assertRaisesRegex(ContractError, "must be a boolean"):
            validate_catalog_document(raw)

    def test_catalog_refresh_tracks_live_metadata_without_prompt_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automations = root / "automations"
            existing = automations / "example-status-routine"
            empty_policy = automations / "example-knowledge-routine"
            new = automations / "new-routine"
            existing.mkdir(parents=True)
            empty_policy.mkdir(parents=True)
            new.mkdir(parents=True)
            (existing / "automation.toml").write_text(
                '\n'.join([
                    'id = "example-status-routine"',
                    'name = "Status updated"',
                    'kind = "heartbeat"',
                    'rrule = "RRULE:FREQ=DAILY;BYHOUR=9"',
                    'status = "PAUSED"',
                    'prompt = "private prompt must not leave this file"',
                ]),
                encoding="utf-8",
            )
            (empty_policy / "automation.toml").write_text(
                '\n'.join([
                    'id = "example-knowledge-routine"',
                    'name = "Knowledge routine"',
                    'kind = "heartbeat"',
                    'rrule = "RRULE:FREQ=DAILY;BYHOUR=21"',
                    'status = "ACTIVE"',
                    'prompt = "private"',
                ]),
                encoding="utf-8",
            )
            (new / "automation.toml").write_text(
                '\n'.join([
                    'id = "new-routine"',
                    'name = "New routine"',
                    'kind = "cron"',
                    'rrule = "RRULE:FREQ=WEEKLY;BYDAY=MO"',
                    'status = "ACTIVE"',
                    'model = "example-model"',
                    'reasoning_effort = "medium"',
                    'prompt = "another private prompt"',
                ]),
                encoding="utf-8",
            )
            private_catalog = root / "catalog.json"
            private_document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            private_document["routines"][0]["allowedDomainObjects"] = []
            private_catalog.write_text(
                json.dumps(private_document), encoding="utf-8"
            )
            refreshed = reconcile_catalog(automations, private_catalog)
            self.assertEqual(
                set(refreshed["byId"]),
                {"example-status-routine", "example-knowledge-routine", "new-routine"},
            )
            status = refreshed["byId"]["example-status-routine"]
            self.assertEqual(status["schedulerStatus"], "PAUSED")
            self.assertFalse(status["allowExtraSync"])
            self.assertEqual(status["allowedDomainObjects"], ["task"])
            added = refreshed["byId"]["new-routine"]
            self.assertEqual(added["allowedDomainObjects"], ["task"])
            self.assertFalse(added["allowExtraSync"])
            self.assertEqual(added["model"], "example-model")
            self.assertEqual(
                refreshed["byId"]["example-knowledge-routine"]["allowedDomainObjects"],
                [],
            )
            saved = private_catalog.read_text(encoding="utf-8")
            self.assertNotIn("private prompt", saved)
            self.assertNotIn('"prompt"', saved)

    def test_stable_run_key_is_same_for_start_and_outcome(self):
        self.assertEqual(start_event()["runKey"], outcome_event()["runKey"])
        self.assertNotEqual(start_event()["eventKey"], outcome_event()["eventKey"])

    def test_sensitive_fields_and_secret_like_text_are_rejected(self):
        raw = outcome_event()
        raw.pop("payloadHash")
        raw["token"] = "not-allowed"
        with self.assertRaisesRegex(ContractError, "unsupported fields"):
            validate_event(raw, catalog())
        raw.pop("token")
        raw["summary"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        with self.assertRaisesRegex(ContractError, "possible credential"):
            validate_event(raw, catalog())
        raw["summary"] = "Enviar para pessoa@example.com"
        with self.assertRaisesRegex(ContractError, "possible personal email"):
            validate_event(raw, catalog())

    def test_domain_refs_are_allowlisted_by_routine(self):
        with self.assertRaisesRegex(ContractError, "domain object is not allowed"):
            outcome_event(domainRefs=[{"object": "contrato", "externalKey": "x"}])
        accepted = outcome_event(domainRefs=[{"object": "task", "externalKey": "continuity-gap"}])
        self.assertEqual(accepted["domainRefs"][0]["object"], "task")

    def test_extra_sync_requires_private_opt_in(self):
        with self.assertRaisesRegex(ContractError, "Extra sync is not allowed"):
            outcome_event(requestExtraSync=True)
        private_catalog = catalog()
        private_catalog["byId"]["example-status-routine"]["allowExtraSync"] = True
        for routine in private_catalog["routines"]:
            if routine["automationId"] == "example-status-routine":
                routine["allowExtraSync"] = True
        accepted = build_event(
            automation_id="example-status-routine",
            event_type="OUTCOME",
            scheduled_for="2026-10-26T08:00:00-03:00",
            observed_at="2026-10-26T09:00:00-03:00",
            trigger="SCHEDULED",
            status="SUCCEEDED",
            catalog=private_catalog,
            coverageStatus="COMPLETE",
            requestExtraSync=True,
        )
        self.assertTrue(accepted["requestExtraSync"])

    def test_task_contract_is_bounded_and_has_honest_state(self):
        event = outcome_event(
            status="PARTIAL",
            coverageStatus="PARTIAL",
            operationalItems=[
                {
                    "externalKey": "source-gap-2026-10-25",
                    "title": "Resolver lacuna de origem",
                    "kind": "PENDING",
                    "status": "WAITING",
                    "summary": "A fonte ficou indisponível; reconsultar na próxima ocorrência.",
                    "refs": [{"url": "https://example.test/evidence"}],
                }
            ],
        )
        self.assertEqual(event["operationalItems"][0]["status"], "WAITING")

    def test_contract_rejects_unknown_fields_and_requires_title(self):
        raw = outcome_event()
        raw["unexpected"] = True
        with self.assertRaisesRegex(ContractError, "event contains unsupported fields"):
            validate_event(raw, catalog())
        with self.assertRaisesRegex(ContractError, "title is required"):
            outcome_event(
                operationalItems=[
                    {"externalKey": "x", "kind": "task", "status": "todo"}
                ]
            )
        with self.assertRaisesRegex(ContractError, "unsupported fields"):
            outcome_event(
                operationalItems=[
                    {
                        "externalKey": "x",
                        "title": "X",
                        "kind": "task",
                        "status": "todo",
                        "privatePayload": {},
                    }
                ]
            )

    def test_nested_values_are_normalized(self):
        event = outcome_event(
            operationalItems=[
                {
                    "externalKey": " x ",
                    "title": " Revisar ",
                    "kind": "pending",
                    "status": "waiting",
                    "summary": " Resumo ",
                    "refs": [{"url": " https://example.test/ref "}],
                }
            ]
        )
        item = event["operationalItems"][0]
        self.assertEqual(item["externalKey"], "x")
        self.assertEqual(item["title"], "Revisar")
        self.assertEqual(item["kind"], "PENDING")
        self.assertEqual(item["status"], "WAITING")
        self.assertEqual(item["refs"][0]["url"], "https://example.test/ref")


class OriginTests(unittest.TestCase):
    def test_schedule_description_is_strict_and_human_readable(self):
        timezone_name = "America/Sao_Paulo"
        self.assertEqual(
            schedule_description(
                "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=0;BYSECOND=0",
                timezone_name,
            ),
            "Segundas, quartas e sextas às 08:00 (America/Sao_Paulo)",
        )
        self.assertEqual(
            schedule_description(
                "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=WE;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                timezone_name,
            ),
            "Quartas às 09:00 (America/Sao_Paulo), 5 ocorrências",
        )
        self.assertEqual(
            schedule_description("FREQ=MINUTELY;INTERVAL=30", timezone_name),
            "A cada 30 minutos",
        )
        self.assertEqual(
            schedule_description("FREQ=MONTHLY;BYMONTHDAY=1", timezone_name),
            "Agendamento personalizado no Codex",
        )
        self.assertEqual(
            schedule_description(
                "FREQ=DAILY;BYHOUR=8;BYMINUTE=0;BYSECOND=30", timezone_name
            ),
            "Agendamento personalizado no Codex",
        )

    def test_allows_only_canonical_https_or_explicit_loopback(self):
        self.assertEqual(
            validate_base_url("https://gestao.confenge.com.br/"),
            "https://gestao.confenge.com.br",
        )
        self.assertEqual(validate_base_url("http://127.0.0.1:18300"), "http://127.0.0.1:18300")
        self.assertEqual(validate_base_url("http://localhost:18300"), "http://localhost:18300")
        with self.assertRaisesRegex(ApiError, "canonical HTTPS"):
            validate_base_url("http://gestao.confenge.com.br")
        with self.assertRaisesRegex(ApiError, "canonical HTTPS"):
            validate_base_url("https://attacker.example")
        with self.assertRaisesRegex(ApiError, "must not contain a path"):
            validate_base_url("https://gestao.confenge.com.br/redirect")

    def test_unique_identifiers_are_not_ui_editable(self):
        unique = _field_payload("object", ("runKey", "Run", "TEXT", True))
        ordinary = _field_payload("object", ("summary", "Summary", "TEXT", False))
        self.assertFalse(unique["isUIEditable"])
        self.assertNotIn("isUIEditable", ordinary)

    def test_workspace_mismatch_fails_before_any_record_write(self):
        class WorkspaceClient(TwentyClient):
            def __init__(self):
                super().__init__("http://127.0.0.1:18300", "fake", request_interval=0)
                self.calls = []

            def request(self, path, payload=None, method="GET"):
                self.calls.append((method, path, payload))
                return {"data": {"currentWorkspace": {"id": "other-workspace"}}}

        client = WorkspaceClient()
        with self.assertRaisesRegex(ApiError, "does not match"):
            client.preflight_workspace("expected-workspace")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0:2], ("POST", "/metadata"))
        self.assertFalse(any(path.startswith("/rest/") for _, path, _ in client.calls))

    def test_ssh_envelope_is_explicit_utf8_bytes(self):
        event = outcome_event(summary="Cobertura técnica concluída.")
        args = SimpleNamespace(
            wsl_distribution="Ubuntu",
            ssh_host="example-host",
            remote_python="/opt/python",
            remote_cli="/opt/cli.py",
            timeout=10,
        )

        def fake_run(command, **kwargs):
            self.assertIsInstance(kwargs["input"], bytes)
            envelope = json.loads(kwargs["input"].decode("utf-8"))
            self.assertEqual(envelope["event"]["summary"], "Cobertura técnica concluída.")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"confirmed": True, "payloadHash": event["payloadHash"]}
                ).encode("utf-8"),
                stderr=b"",
            )

        with patch("cli.subprocess.run", side_effect=fake_run):
            receipt = remote_deliver(event, args, catalog())
        self.assertTrue(receipt["confirmed"])

    def test_ssh_delivery_fails_closed_without_host(self):
        args = SimpleNamespace(
            wsl_distribution="Ubuntu",
            ssh_host=None,
            remote_python="/opt/python",
            remote_cli="/opt/cli.py",
            timeout=10,
        )
        with self.assertRaisesRegex(RuntimeError, "SSH delivery requires"):
            remote_deliver(outcome_event(), args, catalog())


class OutboxTests(unittest.TestCase):
    def test_enqueue_is_durable_and_identical_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            box = Outbox(path)
            event = outcome_event()
            self.assertEqual(box.enqueue(event, now=100), "queued")
            self.assertEqual(box.enqueue(event, now=101), "duplicate")
            self.assertEqual(box.status()["pending"], 1)
            box.close()
            reopened = Outbox(path)
            self.assertEqual(reopened.status()["pending"], 1)
            reopened.close()

    def test_conflicting_same_phase_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Outbox(Path(directory) / "outbox.sqlite3")
            box.enqueue(outcome_event(summary="primeiro"), now=100)
            with self.assertRaisesRegex(ContractError, "conflicting replay"):
                box.enqueue(outcome_event(summary="segundo"), now=101)
            box.close()

    def test_flush_requires_matching_confirmation_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Outbox(Path(directory) / "outbox.sqlite3")
            event = outcome_event()
            box.enqueue(event, now=100)
            first = box.flush(lambda _: {"confirmed": False}, now=100, jitter=lambda: 0)
            self.assertEqual(first, {"sent": 0, "retried": 1, "dead": 0, "busy": 0})
            second = box.flush(
                lambda _: {"confirmed": True, "payloadHash": event["payloadHash"]},
                now=103,
                jitter=lambda: 0,
            )
            self.assertEqual(second, {"sent": 1, "retried": 0, "dead": 0, "busy": 0})
            self.assertEqual(box.status()["sent"], 1)
            self.assertEqual(box.event_status(event["eventKey"]), "SENT")
            box.close()

    def test_two_connections_serialize_delivery_and_preserve_newest_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            first = Outbox(path)
            second = Outbox(path)
            older = outcome_event(
                scheduled_for="2026-10-26T21:00:00-03:00",
                observed_at="2026-10-26T21:05:00-03:00",
                finishedAt="2026-10-26T21:05:00-03:00",
                status="FAILED",
                coverageStatus="UNAVAILABLE",
                errorCode="OLD",
                summary="older",
            )
            newer = outcome_event(
                scheduled_for="2026-10-27T21:00:00-03:00",
                observed_at="2026-10-27T21:05:00-03:00",
                finishedAt="2026-10-27T21:05:00-03:00",
                summary="newer",
            )
            first.enqueue(older, now=100)
            first.enqueue(newer, now=101)
            client = FakeClient()
            gateway = RoutineGateway(client, catalog())
            gateway.sync_catalog()
            nested = []

            def deliver(event):
                if not nested:
                    nested.append(second.flush(lambda _: self.fail("concurrent delivery ran"), now=102))
                return gateway.apply_event(event)

            try:
                summary = first.flush(deliver, now=102)
                self.assertEqual(summary, {"sent": 2, "retried": 0, "dead": 0, "busy": 0})
                self.assertEqual(nested, [{"sent": 0, "retried": 0, "dead": 0, "busy": 1}])
                routine = next(
                    item for item in client.records["codexRoutines"]
                    if item["automationId"] == newer["automationId"]
                )
                self.assertEqual(routine["lastRunKey"], newer["runKey"])
                self.assertEqual(routine["lastSummary"], "newer")
            finally:
                second.close()
                first.close()

    def test_expired_delivery_lease_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            box = Outbox(path)
            event = outcome_event()
            box.enqueue(event, now=100)
            box.connection.execute(
                "UPDATE delivery_lock SET owner='crashed-worker',lease_until=99 WHERE lock_id=1"
            )
            box.connection.commit()
            try:
                result = box.flush(
                    lambda _: {"confirmed": True, "payloadHash": event["payloadHash"]},
                    now=100,
                )
                self.assertEqual(result, {"sent": 1, "retried": 0, "dead": 0, "busy": 0})
            finally:
                box.close()

    def test_dead_event_requires_explicit_audited_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Outbox(Path(directory) / "outbox.sqlite3")
            event = outcome_event()
            box.enqueue(event, now=100)
            result = box.flush(
                lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
                now=100,
                max_attempts=1,
            )
            self.assertEqual(result["dead"], 1)
            self.assertEqual(box.event_status(event["eventKey"]), "DEAD")
            self.assertEqual(box.requeue_dead(event_key=event["eventKey"], now=200), 1)
            self.assertEqual(box.event_status(event["eventKey"]), "PENDING")
            row = box.connection.execute(
                "SELECT attempts,requeue_count,last_dead_error FROM events WHERE event_key=?",
                (event["eventKey"],),
            ).fetchone()
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(row["requeue_count"], 1)
            self.assertEqual(row["last_dead_error"], "offline")
            self.assertEqual(box.requeue_dead(event_key=event["eventKey"], now=201), 0)
            box.close()


def metadata_object(name, plural, fields):
    return {
        "id": f"meta-{name}",
        "nameSingular": name,
        "namePlural": plural,
        "description": MARKER if name != "task" else None,
        "fields": [
            {
                "id": f"field-{name}-{spec[0]}",
                "name": spec[0],
                "description": MARKER,
                "type": spec[2],
                "isUnique": spec[3],
            }
            for spec in fields
        ],
    }


class FakeClient:
    def __init__(self):
        self.objects = [
            metadata_object("codexRoutine", "codexRoutines", ROUTINE_FIELDS),
            metadata_object("codexRoutineRun", "codexRoutineRuns", RUN_FIELDS),
            metadata_object("task", "tasks", TASK_FIELDS),
        ]
        self.objects[1]["fields"].append(
            {
                "id": "relation-run-routine",
                "name": "codexRoutine",
                "description": RoutineGateway._relation_description("meta-codexRoutine"),
                "type": "RELATION",
                "settings": {
                    "relationType": "MANY_TO_ONE",
                    "targetObjectMetadataId": "meta-codexRoutine",
                },
            }
        )
        self.objects[0]["fields"].append(
            {
                "id": "relation-routine-runs",
                "name": "codexRoutineRuns",
                "type": "RELATION",
                "settings": {
                    "relationType": "ONE_TO_MANY",
                    "targetObjectMetadataId": "meta-codexRoutineRun",
                },
            }
        )
        self.records = {"codexRoutines": [], "codexRoutineRuns": [], "tasks": []}
        self.calls = []

    def relation_field(self, field_id):
        if field_id == "relation-run-routine":
            return {
                "id": field_id,
                "name": "codexRoutine",
                "type": "RELATION",
                "settings": {"relationType": "MANY_TO_ONE"},
                "relation": {
                    "type": "MANY_TO_ONE",
                    "sourceObjectMetadata": {"id": "meta-codexRoutineRun", "nameSingular": "codexRoutineRun"},
                    "targetObjectMetadata": {"id": "meta-codexRoutine", "nameSingular": "codexRoutine"},
                    "sourceFieldMetadata": {"id": "relation-run-routine", "name": "codexRoutine"},
                    "targetFieldMetadata": {"id": "relation-routine-runs", "name": "tarefas"},
                },
            }
        if field_id == "relation-routine-runs":
            return {
                "id": field_id,
                "name": "tarefas",
                "type": "RELATION",
                "settings": {"relationType": "ONE_TO_MANY"},
                "relation": {
                    "type": "ONE_TO_MANY",
                    "sourceObjectMetadata": {"id": "meta-codexRoutine", "nameSingular": "codexRoutine"},
                    "targetObjectMetadata": {"id": "meta-codexRoutineRun", "nameSingular": "codexRoutineRun"},
                    "sourceFieldMetadata": {"id": "relation-routine-runs", "name": "tarefas"},
                    "targetFieldMetadata": {"id": "relation-run-routine", "name": "codexRoutine"},
                },
            }
        raise AssertionError(field_id)

    def list_rest(self, path, limit=500):
        if path == "/rest/metadata/objects":
            return self.objects
        plural = path.split("/")[2].split("?")[0]
        return [dict(item) for item in self.records[plural]]

    def request(self, path, payload=None, method="GET"):
        self.calls.append((method, path, payload))
        if path.startswith("/rest/metadata/"):
            raise AssertionError("schema is already complete in this fake")
        plural = path.split("/")[2]
        if method == "POST":
            record = {"id": f"{plural}-{len(self.records[plural]) + 1}", **payload}
            self.records[plural].append(record)
            return record
        if method == "PATCH":
            record_id = path.split("/")[3]
            record = next(item for item in self.records[plural] if item["id"] == record_id)
            record.update(payload)
            return dict(record)
        raise AssertionError((method, path))


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.gateway = RoutineGateway(self.client, catalog())
        self.gateway.sync_catalog()

    def test_schema_plan_is_empty_when_owned_schema_exists(self):
        summary = self.gateway.schema_plan().summary()
        self.assertEqual(summary["objectsToCreate"], [])
        self.assertEqual(summary["fieldsToCreate"], [])
        self.assertFalse(summary["relationToCreate"])
        self.assertEqual(
            summary["viewVisibilityPolicy"]["taskTechnicalFields"],
            "hidden_in_all_views_and_widgets",
        )

    def test_catalog_sync_marks_missing_automation_not_found(self):
        self.client.records["codexRoutines"].append(
            {
                "id": "codexRoutines-stale",
                "name": "Removed routine",
                "automationId": "removed-routine",
                "schedulerStatus": "ACTIVE",
            }
        )
        result = self.gateway.sync_catalog()
        stale = next(
            item for item in self.client.records["codexRoutines"]
            if item["automationId"] == "removed-routine"
        )
        self.assertEqual(result["notFound"], 1)
        self.assertEqual(stale["schedulerStatus"], "NOT_FOUND")

    def test_schema_relation_collision_fails_closed(self):
        relation = next(
            item
            for item in self.client.objects[1]["fields"]
            if item["name"] == "codexRoutine"
        )
        original = self.client.relation_field

        def wrong_target(field_id):
            result = original(field_id)
            if field_id == relation["id"]:
                result["relation"]["targetObjectMetadata"]["id"] = "wrong-target"
            return result

        self.client.relation_field = wrong_target
        with self.assertRaisesRegex(ApiError, "relation target object mismatch"):
            self.gateway.schema_plan()

    def test_view_policy_hides_technical_fields_and_shows_daily_fields(self):
        class ViewClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.objects[0]["fields"].extend(
                    [
                        {"id": "field-routine-name", "name": "name", "label": "Name"},
                        {"id": "field-routine-created", "name": "createdAt", "label": "Creation date"},
                    ]
                )
                self.objects[1]["fields"].append(
                    {"id": "field-run-name", "name": "name", "label": "Name"}
                )
                self.views = [
                    {"id": "task-widget", "objectMetadataId": "meta-task", "type": "FIELDS_WIDGET"},
                    {"id": "routine-table", "objectMetadataId": "meta-codexRoutine", "type": "TABLE", "key": "INDEX"},
                    {"id": "run-table", "objectMetadataId": "meta-codexRoutineRun", "type": "TABLE", "key": "INDEX"},
                ]
                self.view_fields = [
                    {"id": "vf-task-key", "viewId": "task-widget", "fieldMetadataId": "field-task-codexRoutineItemKey", "isVisible": True},
                    {"id": "vf-routine-key", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-automationId", "isVisible": True},
                    {"id": "vf-routine-name", "viewId": "routine-table", "fieldMetadataId": "field-routine-name", "isVisible": True, "position": 0},
                    {"id": "vf-routine-created", "viewId": "routine-table", "fieldMetadataId": "field-routine-created", "isVisible": True, "position": 1},
                    {"id": "vf-routine-schedule", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-schedule", "isVisible": True, "position": 7},
                    {"id": "vf-routine-frequency", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-scheduleDescription", "isVisible": False, "position": 28},
                    {"id": "vf-routine-status", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-schedulerStatus", "isVisible": True, "position": 8},
                    {"id": "vf-routine-summary", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-lastSummary", "isVisible": False, "position": 25},
                    {"id": "vf-routine-coverage", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-lastCoverageStatus", "isVisible": True, "position": 18},
                    {"id": "vf-routine-finished", "viewId": "routine-table", "fieldMetadataId": "field-codexRoutine-lastFinishedAt", "isVisible": True, "position": 20},
                    {"id": "vf-run-name", "viewId": "run-table", "fieldMetadataId": "field-run-name", "isVisible": True, "position": 0},
                    {"id": "vf-run-status", "viewId": "run-table", "fieldMetadataId": "field-codexRoutineRun-outcomeStatus", "isVisible": True, "position": 10},
                    {"id": "vf-run-coverage", "viewId": "run-table", "fieldMetadataId": "field-codexRoutineRun-coverageStatus", "isVisible": True, "position": 11},
                    {"id": "vf-run-finished", "viewId": "run-table", "fieldMetadataId": "field-codexRoutineRun-finishedAt", "isVisible": True, "position": 13},
                    {"id": "vf-run-hash", "viewId": "run-table", "fieldMetadataId": "field-codexRoutineRun-payloadHash", "isVisible": True},
                    {"id": "vf-run-summary", "viewId": "run-table", "fieldMetadataId": "field-codexRoutineRun-summary", "isVisible": False, "position": 15},
                ]

            def list_rest(self, path, limit=500):
                if path == "/rest/metadata/views":
                    return self.views
                if path == "/rest/metadata/viewFields":
                    return self.view_fields
                return super().list_rest(path, limit)

            def request(self, path, payload=None, method="GET"):
                if method == "PATCH" and path.startswith("/rest/metadata/viewFields/"):
                    self.calls.append((method, path, payload))
                    view_field = next(
                        item for item in self.view_fields if item["id"] == path.rsplit("/", 1)[1]
                    )
                    view_field.update(payload)
                    return dict(view_field)
                if method == "PATCH" and path.startswith("/rest/metadata/fields/"):
                    self.calls.append((method, path, payload))
                    field_id = path.rsplit("/", 1)[1]
                    field = next(
                        field
                        for obj in self.objects
                        for field in obj["fields"]
                        if field["id"] == field_id
                    )
                    field.update(payload)
                    return dict(field)
                return super().request(path, payload, method)

        extra_view = ViewClient()
        extra_view.views.append(
            {"id": "routine-table-user", "objectMetadataId": "meta-codexRoutine", "type": "TABLE", "key": "INDEX"}
        )
        with self.assertRaisesRegex(ApiError, "exactly one owned INDEX TABLE"):
            RoutineGateway(extra_view, catalog()).apply_view_visibility()
        self.assertEqual(extra_view.calls, [])

        missing_view_field = ViewClient()
        missing_view_field.view_fields = [
            item
            for item in missing_view_field.view_fields
            if item["id"] != "vf-routine-summary"
        ]
        with self.assertRaisesRegex(ApiError, "exactly one TABLE viewField"):
            RoutineGateway(missing_view_field, catalog()).apply_view_visibility()
        self.assertEqual(missing_view_field.calls, [])

        duplicate_view_field = ViewClient()
        duplicate = dict(
            next(
                item
                for item in duplicate_view_field.view_fields
                if item["id"] == "vf-routine-summary"
            )
        )
        duplicate["id"] = "vf-routine-summary-duplicate"
        duplicate_view_field.view_fields.append(duplicate)
        with self.assertRaisesRegex(ApiError, "exactly one TABLE viewField"):
            RoutineGateway(duplicate_view_field, catalog()).apply_view_visibility()
        self.assertEqual(duplicate_view_field.calls, [])

        human_view = ViewClient()
        human_view.views.append(
            {
                "id": "routine-table-human",
                "objectMetadataId": "meta-codexRoutine",
                "type": "TABLE",
                "key": None,
            }
        )
        human_view.view_fields.append(
            {
                "id": "vf-human-schedule",
                "viewId": "routine-table-human",
                "fieldMetadataId": "field-codexRoutine-schedule",
                "isVisible": True,
                "position": 0,
            }
        )
        RoutineGateway(human_view, catalog()).apply_view_visibility()
        human_schedule = next(
            item for item in human_view.view_fields if item["id"] == "vf-human-schedule"
        )
        self.assertTrue(human_schedule["isVisible"])
        self.assertEqual(human_schedule["position"], 0)

        client = ViewClient()
        counts = RoutineGateway(client, catalog()).apply_view_visibility()
        visibility = {item["id"]: item["isVisible"] for item in client.view_fields}
        self.assertFalse(visibility["vf-task-key"])
        self.assertFalse(visibility["vf-routine-key"])
        self.assertFalse(visibility["vf-routine-created"])
        self.assertFalse(visibility["vf-routine-schedule"])
        self.assertTrue(visibility["vf-routine-frequency"])
        self.assertTrue(visibility["vf-routine-summary"])
        self.assertFalse(visibility["vf-run-hash"])
        self.assertTrue(visibility["vf-run-summary"])
        positions = {item["id"]: item.get("position") for item in client.view_fields}
        self.assertEqual(positions["vf-routine-name"], 0)
        self.assertEqual(positions["vf-routine-status"], 1)
        self.assertEqual(positions["vf-routine-frequency"], 2)
        self.assertEqual(positions["vf-routine-summary"], 3)
        self.assertEqual(positions["vf-routine-coverage"], 4)
        self.assertEqual(positions["vf-routine-finished"], 5)
        name_fields = [
            field
            for obj in client.objects[:2]
            for field in obj["fields"]
            if field.get("name") == "name"
        ]
        self.assertEqual([field["label"] for field in name_fields], ["Nome", "Nome"])
        self.assertGreaterEqual(counts["hidden"], 5)
        self.assertGreaterEqual(counts["shown"], 3)
        self.assertGreaterEqual(counts["reordered"], 6)
        self.assertEqual(counts["renamed"], 2)
        writes_after_first = len(client.calls)
        second = RoutineGateway(client, catalog()).apply_view_visibility()
        self.assertEqual(len(client.calls), writes_after_first)
        self.assertEqual(second["hidden"], 0)
        self.assertEqual(second["shown"], 0)
        self.assertEqual(second["reordered"], 0)
        self.assertEqual(second["renamed"], 0)

    def test_start_then_terminal_outcome_updates_one_run(self):
        start = start_event()
        start_receipt = self.gateway.apply_event(start)
        self.assertTrue(start_receipt["created"])
        outcome = outcome_event(
            operationalItems=[
                {
                    "externalKey": "review-gap",
                    "title": "Revisar conflito no cofre",
                    "kind": "PENDING",
                    "status": "WAITING",
                    "summary": "Conflito preservado para revisão humana.",
                    "refs": [],
                }
            ]
        )
        receipt = self.gateway.apply_event(outcome)
        self.assertFalse(receipt["created"])
        self.assertEqual(len(self.client.records["codexRoutineRuns"]), 1)
        self.assertEqual(self.client.records["codexRoutineRuns"][0]["outcomeStatus"], "SUCCEEDED")
        self.assertEqual(len(self.client.records["tasks"]), 1)
        routine = next(
            item
            for item in self.client.records["codexRoutines"]
            if item["automationId"] == outcome["automationId"]
        )
        self.assertEqual(routine["lastRunStatus"], "SUCCEEDED")
        self.assertEqual(routine["lastDomainRecordCount"], 0)

    def test_identical_terminal_replay_is_noop(self):
        event = outcome_event()
        self.gateway.apply_event(event)
        writes_before = len(self.client.calls)
        receipt = self.gateway.apply_event(event)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual(len(self.client.calls), writes_before)

    def test_terminal_replay_repairs_partial_effects_before_ack(self):
        event = outcome_event(
            operationalItems=[
                {
                    "externalKey": "repair-me",
                    "title": "Criar pendência",
                    "kind": "PENDING",
                    "status": "WAITING",
                    "summary": "Persistir mesmo após resposta perdida.",
                    "refs": [],
                }
            ]
        )
        original_request = self.client.request
        failed = {"value": False}

        def fail_after_task_persist(path, payload=None, method="GET"):
            result = original_request(path, payload, method)
            if path == "/rest/tasks" and method == "POST" and not failed["value"]:
                failed["value"] = True
                raise ApiError("lost response after task commit")
            return result

        self.client.request = fail_after_task_persist
        with self.assertRaisesRegex(ApiError, "lost response"):
            self.gateway.apply_event(event)
        self.assertEqual(len(self.client.records["codexRoutineRuns"]), 1)
        self.assertEqual(len(self.client.records["tasks"]), 1)
        self.client.request = original_request
        receipt = self.gateway.apply_event(event)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual(len(self.client.records["codexRoutineRuns"]), 1)
        self.assertEqual(len(self.client.records["tasks"]), 1)
        routine = next(
            item
            for item in self.client.records["codexRoutines"]
            if item["automationId"] == event["automationId"]
        )
        self.assertEqual(routine["lastRunStatus"], "SUCCEEDED")

    def test_lost_response_after_run_commit_converges_on_retry(self):
        event = outcome_event(
            operationalItems=[
                {
                    "externalKey": "after-run-commit",
                    "title": "Criar após replay",
                    "kind": "TASK",
                    "status": "TODO",
                    "refs": [],
                }
            ]
        )
        original_request = self.client.request
        failed = {"value": False}

        def fail_after_run_persist(path, payload=None, method="GET"):
            result = original_request(path, payload, method)
            if path == "/rest/codexRoutineRuns" and method == "POST" and not failed["value"]:
                failed["value"] = True
                raise ApiError("lost response after run commit")
            return result

        self.client.request = fail_after_run_persist
        with self.assertRaisesRegex(ApiError, "lost response after run"):
            self.gateway.apply_event(event)
        self.assertEqual(len(self.client.records["codexRoutineRuns"]), 1)
        self.assertEqual(len(self.client.records["tasks"]), 0)
        self.client.request = original_request
        receipt = self.gateway.apply_event(event)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual(len(self.client.records["tasks"]), 1)

    def test_late_start_after_terminal_is_acknowledged_without_regression(self):
        outcome = outcome_event()
        self.gateway.apply_event(outcome)
        receipt = self.gateway.apply_event(start_event())
        self.assertTrue(receipt["duplicate"])
        self.assertTrue(receipt["supersededStart"])
        self.assertEqual(
            self.client.records["codexRoutineRuns"][0]["outcomeStatus"],
            "SUCCEEDED",
        )

    def test_existing_task_preserves_human_title_body_and_due(self):
        first = outcome_event(
            operationalItems=[
                {
                    "externalKey": "human-owned-content",
                    "title": "Título inicial",
                    "kind": "PENDING",
                    "status": "WAITING",
                    "summary": "Corpo inicial",
                    "dueAt": "2026-10-30T09:00:00-03:00",
                    "refs": [],
                }
            ]
        )
        self.gateway.apply_event(first)
        task = self.client.records["tasks"][0]
        task["title"] = "Título editado por Tiago"
        task["bodyV2"] = {"markdown": "Nota humana", "blocknote": "[]"}
        task["dueAt"] = "2026-11-02T10:00:00-03:00"
        correction = build_event(
            automation_id="example-knowledge-routine",
            event_type="CORRECTION",
            scheduled_for="2026-10-25T21:00:00-03:00",
            observed_at="2026-10-25T21:10:00-03:00",
            trigger="SCHEDULED",
            status="SUCCEEDED",
            catalog=catalog(),
            coverageStatus="COMPLETE",
            correctionReason="A pendência foi reconciliada.",
            operationalItems=[
                {
                    "externalKey": "human-owned-content",
                    "title": "Título do agente não deve sobrescrever",
                    "kind": "PENDING",
                    "status": "DONE",
                    "summary": "Corpo do agente não deve sobrescrever",
                    "dueAt": "2026-11-03T10:00:00-03:00",
                    "refs": [],
                }
            ],
        )
        self.gateway.apply_event(correction)
        self.assertEqual(task["title"], "Título editado por Tiago")
        self.assertEqual(task["bodyV2"]["markdown"], "Nota humana")
        self.assertEqual(task["dueAt"], "2026-11-02T10:00:00-03:00")
        self.assertEqual(task["codexRoutineItemStatus"], "DONE")

    def test_terminal_conflict_requires_explicit_correction(self):
        self.gateway.apply_event(outcome_event(status="FAILED", errorCode="SOURCE_UNAVAILABLE"))
        with self.assertRaisesRegex(ContractError, "requires CORRECTION"):
            self.gateway.apply_event(outcome_event(status="SUCCEEDED"))
        correction = build_event(
            automation_id="example-knowledge-routine",
            event_type="CORRECTION",
            scheduled_for="2026-10-25T21:00:00-03:00",
            observed_at="2026-10-25T21:10:00-03:00",
            trigger="SCHEDULED",
            status="SUCCEEDED",
            catalog=catalog(),
            coverageStatus="COMPLETE",
            correctionReason="A leitura posterior confirmou a escrita e o readback.",
        )
        receipt = self.gateway.apply_event(correction)
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(self.client.records["codexRoutineRuns"][0]["outcomeStatus"], "SUCCEEDED")

    def test_terminal_same_status_but_changed_payload_requires_correction(self):
        self.gateway.apply_event(outcome_event(summary="primeiro"))
        with self.assertRaisesRegex(ContractError, "requires CORRECTION"):
            self.gateway.apply_event(outcome_event(summary="segundo"))

    def test_late_old_outcome_does_not_regress_routine_last_state(self):
        newer = outcome_event(
            scheduled_for="2026-10-27T21:00:00-03:00",
            observed_at="2026-10-27T21:05:00-03:00",
            finishedAt="2026-10-27T21:05:00-03:00",
            summary="nova",
        )
        older = outcome_event(
            scheduled_for="2026-10-26T21:00:00-03:00",
            observed_at="2026-10-26T21:10:00-03:00",
            finishedAt="2026-10-26T21:10:00-03:00",
            status="FAILED",
            coverageStatus="UNAVAILABLE",
            errorCode="OFFLINE",
            summary="antiga atrasada",
        )
        self.gateway.apply_event(newer)
        self.gateway.apply_event(older)
        routine = next(
            item
            for item in self.client.records["codexRoutines"]
            if item["automationId"] == newer["automationId"]
        )
        self.assertEqual(routine["lastRunKey"], newer["runKey"])
        self.assertEqual(routine["lastRunStatus"], "SUCCEEDED")
        self.assertEqual(routine["lastSummary"], "nova")


class PaginationTests(unittest.TestCase):
    def test_list_rest_reads_second_page(self):
        class PagedClient(TwentyClient):
            def __init__(self):
                super().__init__("http://127.0.0.1:18300", "fake", request_interval=0)

            def request(self, path, payload=None, method="GET"):
                if "starting_after=cursor-1" in path:
                    return {
                        "data": [{"id": "target"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                    }
                return {
                    "data": [{"id": "first"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }

        self.assertEqual(
            [item["id"] for item in PagedClient().list_rest("/rest/tasks", limit=1)],
            ["first", "target"],
        )


if __name__ == "__main__":
    unittest.main()
