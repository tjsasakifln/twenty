#!/usr/bin/env python3
"""Enable or disable native cutover events with target-bound verification."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

from twenty_native_cutover import (
    CutoverError,
    atomic_write_json,
    credentials,
    preflight,
    verify_marker,
)


ACK = "SET_NATIVE_CUTOVER_GATE"
APPLICATION_UID = "5ed4ced4-1f75-4030-958e-ff5011b8b63e"
CREATED_FUNCTION_UID = "cc1231f3-d464-4344-beff-56d9495f572c"
UPDATED_FUNCTION_UID = "0dca05ee-7131-4540-85b8-61ffff9944f5"
UPDATED_FIELDS = [
    "status",
    "clickUpStatus",
    "clickUpStartOn",
    "clickUpStartAt",
    "clickUpDueOn",
    "dueAt",
]
TRIGGER_FIELDS = (
    "cronTriggerSettings",
    "databaseEventTriggerSettings",
    "httpRouteTriggerSettings",
    "toolTriggerSettings",
    "workflowActionTriggerSettings",
    "serverRouteTriggerSettings",
)

FIND = """
query CutoverGateApplication($uid: UUID!) {
  findOneApplication(universalIdentifier: $uid) {
    id universalIdentifier applicationVariables { key value }
  }
}
"""

UPDATE = """
mutation CutoverGate($key: String!, $value: String!, $applicationId: UUID!) {
  updateOneApplicationVariable(key: $key, value: $value, applicationId: $applicationId)
}
"""

LOGIC_FUNCTION_INVENTORY_SQL = r'''copy (
  select json_build_object(
    'id', l.id,
    'universalIdentifier', l."universalIdentifier",
    'applicationId', l."applicationId",
    'workspaceId', l."workspaceId",
    'name', l.name,
    'updatedAt', l."updatedAt",
    'checksum', l.checksum,
    'deletedAt', l."deletedAt",
    'isBuildUpToDate', l."isBuildUpToDate",
    'executionMode', l."executionMode",
    'runtime', l.runtime,
    'cronTriggerSettings', l."cronTriggerSettings",
    'databaseEventTriggerSettings', l."databaseEventTriggerSettings",
    'httpRouteTriggerSettings', l."httpRouteTriggerSettings",
    'toolTriggerSettings', l."toolTriggerSettings",
    'workflowActionTriggerSettings', l."workflowActionTriggerSettings",
    'serverRouteTriggerSettings', l."serverRouteTriggerSettings"
  )::text
  from core."logicFunction" l
  where l."deletedAt" is null
  order by l.id
) to stdout;'''


def graphql(client, query: str, variables: dict) -> dict:
    response = client.request(
        "/metadata", {"query": query, "variables": variables}, "POST"
    )
    if response.get("errors"):
        raise CutoverError("cutover gate metadata request failed")
    data = response.get("data")
    if not isinstance(data, dict):
        raise CutoverError("cutover gate metadata request returned no data")
    return data


def read_application(client) -> dict:
    application = graphql(client, FIND, {"uid": APPLICATION_UID}).get(
        "findOneApplication"
    )
    if (
        not isinstance(application, dict)
        or application.get("universalIdentifier") != APPLICATION_UID
    ):
        raise CutoverError("native cutover application identity mismatch")
    return application


def gate_value(application: dict) -> str | None:
    values = {
        item.get("key"): item.get("value")
        for item in application.get("applicationVariables") or []
        if isinstance(item, dict)
    }
    return values.get("CUTOVER_ENABLED")


def validate_private_file(path: Path) -> None:
    info = path.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise CutoverError(f"private file permissions are unsafe: {path}")
    parent = path.parent.stat()
    if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) & 0o077:
        raise CutoverError(f"private directory permissions are unsafe: {path.parent}")


def read_logic_functions_from_database(
    container: str, database: str, user: str
) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "docker", "exec", "-i", container, "psql", "-X", "-q",
            "-U", user, "-d", database,
        ],
        input=LOGIC_FUNCTION_INVENTORY_SQL,
        check=True,
        capture_output=True,
        text=True,
    )
    items = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if not all(isinstance(item, dict) for item in items):
        raise CutoverError("logic function inventory query returned invalid data")
    return items


def validate_logic_function_inventory(
    actual: list[dict[str, Any]],
    allowlist: dict[str, Any],
    application_id: str,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    expected = allowlist.get("logic_functions")
    if allowlist.get("version") != 1 or not isinstance(expected, list):
        raise CutoverError("logic function allowlist format is invalid")
    if len(actual) != 5 or len(expected) != 5:
        raise CutoverError("logic function inventory must contain exactly five entries")
    actual_by_uid = {str(item.get("universalIdentifier")): item for item in actual}
    expected_by_uid = {
        str(item.get("universalIdentifier")): item for item in expected
        if isinstance(item, dict)
    }
    if len(actual_by_uid) != 5 or set(actual_by_uid) != set(expected_by_uid):
        raise CutoverError("logic function UID allowlist mismatch")

    owned = {
        CREATED_FUNCTION_UID: {
            "name": "task-cutover-created",
            "trigger": {"eventName": "task.created"},
        },
        UPDATED_FUNCTION_UID: {
            "name": "task-cutover-updated",
            "trigger": {
                "eventName": "task.updated",
                "updatedFields": UPDATED_FIELDS,
            },
        },
    }
    trigger_count = 0
    for uid, item in actual_by_uid.items():
        expected_item = expected_by_uid[uid]
        for key in (
            "id",
            "universalIdentifier",
            "applicationId",
            "workspaceId",
            "name",
            "updatedAt",
            "checksum",
            "deletedAt",
            "isBuildUpToDate",
            "executionMode",
            "runtime",
            *TRIGGER_FIELDS,
        ):
            if item.get(key) != expected_item.get(key):
                raise CutoverError(f"logic function allowlist mismatch for {key}")

        configured_triggers = [key for key in TRIGGER_FIELDS if item.get(key) is not None]
        if item.get("deletedAt") is not None:
            raise CutoverError("deleted logic function cannot be allowlisted")
        if workspace_id is not None and item.get("workspaceId") != workspace_id:
            raise CutoverError("logic function belongs to the wrong workspace")
        if uid in owned:
            contract = owned[uid]
            if item.get("applicationId") != application_id:
                raise CutoverError("cutover function belongs to the wrong application")
            if item.get("name") != contract["name"]:
                raise CutoverError("cutover function name mismatch")
            if configured_triggers != ["databaseEventTriggerSettings"]:
                raise CutoverError("cutover function has an unexpected trigger")
            if item.get("databaseEventTriggerSettings") != contract["trigger"]:
                raise CutoverError("cutover database event trigger mismatch")
            if item.get("isBuildUpToDate") is not True or not item.get("checksum"):
                raise CutoverError("cutover function build is stale or missing checksum")
            if item.get("executionMode") != "LIVE":
                raise CutoverError("cutover function execution mode mismatch")
            trigger_count += 1
        elif configured_triggers:
            raise CutoverError("non-cutover logic function must not have a trigger")

    if trigger_count != 2:
        raise CutoverError("exactly two cutover triggers are required")
    return {
        "logic_function_count": len(actual),
        "cutover_trigger_count": trigger_count,
    }


def read_container_runtime(container: str) -> dict[str, str]:
    completed = subprocess.run(
        ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(completed.stdout)
    if not isinstance(values, list):
        raise CutoverError("container environment inspection returned invalid data")
    return {
        key: value
        for entry in values
        if isinstance(entry, str) and "=" in entry
        for key, value in [entry.split("=", 1)]
        if key in {"LOGIC_FUNCTION_TYPE", "CODE_INTERPRETER_TYPE"}
    }


def validate_drivers(server: dict[str, str], worker: dict[str, str]) -> None:
    if server.get("LOGIC_FUNCTION_TYPE") != "DISABLED":
        raise CutoverError("server logic function driver must remain DISABLED")
    if worker.get("LOGIC_FUNCTION_TYPE") != "LOCAL":
        raise CutoverError("worker logic function driver must be LOCAL")
    if server.get("CODE_INTERPRETER_TYPE") != "DISABLED" or worker.get(
        "CODE_INTERPRETER_TYPE"
    ) != "DISABLED":
        raise CutoverError("code interpreter must remain DISABLED")


def read_logic_function_job_ids(redis_container: str) -> set[str]:
    scanned = subprocess.run(
        [
            "docker",
            "exec",
            redis_container,
            "redis-cli",
            "--scan",
            "--pattern",
            "bull:logic-function-queue:*",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    job_ids: set[str] = set()
    for key in scanned.stdout.splitlines():
        key_type = subprocess.run(
            ["docker", "exec", redis_container, "redis-cli", "TYPE", key],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if key_type != "hash":
            continue
        data = subprocess.run(
            ["docker", "exec", redis_container, "redis-cli", "HGET", key, "data"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not data:
            continue
        payload = json.loads(data)
        logic_function_id = payload.get("logicFunctionId")
        if not isinstance(logic_function_id, str) or not logic_function_id:
            raise CutoverError("logic function queue job has no function id")
        job_ids.add(logic_function_id)
    return job_ids


def validate_logic_function_jobs(
    queued_function_ids: set[str], actual: list[dict[str, Any]]
) -> None:
    allowed_ids = {
        str(item.get("id"))
        for item in actual
        if item.get("universalIdentifier")
        in {CREATED_FUNCTION_UID, UPDATED_FUNCTION_UID}
    }
    unknown = queued_function_ids - allowed_ids
    if unknown:
        raise CutoverError("logic function queue references an unknown function")


def set_gate_value(
    client,
    application: dict[str, Any],
    enabled: str,
    enable_guard: Callable[[], dict[str, Any]],
) -> tuple[str, str, dict[str, Any] | None]:
    before = gate_value(application)
    if before not in {"true", "false"}:
        raise CutoverError("CUTOVER_ENABLED is missing or invalid")
    guard_evidence = enable_guard() if enabled == "true" else None
    if before != enabled:
        updated = graphql(
            client,
            UPDATE,
            {
                "key": "CUTOVER_ENABLED",
                "value": enabled,
                "applicationId": application["id"],
            },
        ).get("updateOneApplicationVariable")
        if updated is not True:
            raise CutoverError("CUTOVER_ENABLED mutation was not acknowledged")
    after = gate_value(read_application(client))
    if after != enabled:
        raise CutoverError("CUTOVER_ENABLED read-back verification failed")
    return before, after, guard_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--cutover-marker", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--enabled", choices=("true", "false"), required=True)
    parser.add_argument("--runtime-allowlist", type=Path)
    parser.add_argument("--server-container", default="confenge-gestao-server-1")
    parser.add_argument("--worker-container", default="confenge-gestao-worker-1")
    parser.add_argument("--redis-container", default="confenge-gestao-redis-1")
    parser.add_argument("--db-container", default="confenge-gestao-db-1")
    parser.add_argument("--db-name", default="confenge")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--ack", required=True)
    args = parser.parse_args()
    if args.ack != ACK:
        raise CutoverError(f"--ack must be exactly {ACK}")

    validate_private_file(args.credentials)

    client, workspace_id = credentials(args.credentials)
    preflight(client, workspace_id)
    verify_marker(args.cutover_marker, workspace_id, required_phase="native-configured")
    application = read_application(client)

    def enable_guard() -> dict[str, Any]:
        if args.runtime_allowlist is None:
            raise CutoverError("--runtime-allowlist is required when enabling")
        validate_private_file(args.runtime_allowlist)
        allowlist = json.loads(args.runtime_allowlist.read_text(encoding="utf-8"))
        functions = read_logic_functions_from_database(
            args.db_container, args.db_name, args.db_user
        )
        inventory = validate_logic_function_inventory(
            functions, allowlist, str(application["id"]), workspace_id
        )
        queued_function_ids = read_logic_function_job_ids(args.redis_container)
        validate_logic_function_jobs(queued_function_ids, functions)
        server_runtime = read_container_runtime(args.server_container)
        worker_runtime = read_container_runtime(args.worker_container)
        validate_drivers(server_runtime, worker_runtime)
        return {
            **inventory,
            "queued_function_reference_count": len(queued_function_ids),
            "server_logic_function_type": "DISABLED",
            "worker_logic_function_type": "LOCAL",
            "code_interpreter_type": "DISABLED",
        }

    before, after, guard_evidence = set_gate_value(
        client, application, args.enabled, enable_guard
    )
    atomic_write_json(
        args.evidence,
        {
            "application_universal_identifier": APPLICATION_UID,
            "workspace_preflight": True,
            "before": before,
            "after": after,
            "mutation_acknowledged": before != after,
            "enable_guard": guard_evidence,
        },
    )
    print(json.dumps({"cutover_enabled": after == "true"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
