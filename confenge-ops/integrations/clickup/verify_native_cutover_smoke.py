#!/usr/bin/env python3
"""Create disposable QA records to verify the native Twenty cutover.

The script never talks to ClickUp.  It creates tagged Twenty records, waits for
the application events to converge, validates list/assignee/stage/date/parent
and dependency behaviour, then soft-deletes only the records it created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from twenty_native_cutover import (
    CutoverError,
    atomic_write_json,
    credentials,
    normalized,
    preflight,
    unwrap_one,
)


ACK = "RUN_DISPOSABLE_NATIVE_CUTOVER_SMOKE"


def task_version_snapshot(tasks: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item["id"]): str(item.get("updatedAt") or "")
        for item in tasks
        if item.get("id")
    }


def task_version_fingerprint(snapshot: dict[str, str]) -> dict[str, Any]:
    canonical = json.dumps(
        sorted(snapshot.items()), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        "count": len(snapshot),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def wait_for(
    read: Callable[[], dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    description: str,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.5)
    raise CutoverError(f"smoke timeout waiting for {description}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--application-variables", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--ack", required=True)
    args = parser.parse_args()
    if args.ack != ACK:
        raise CutoverError(f"--ack must be exactly {ACK}")

    client, workspace_id = credentials(args.credentials)
    preflight(client, workspace_id)
    variables = json.loads(args.application_variables.read_text(encoding="utf-8"))
    member_id = str(variables["CUTOVER_MEMBER_ID"])
    default_list_id = str(variables["CUTOVER_DEFAULT_LIST_ID"])
    done_stage = str(variables["CUTOVER_STAGE_DONE"])

    task_meta = next(
        item
        for item in client.list_rest("/rest/metadata/objects", limit=500)
        if item.get("nameSingular") == "task"
    )
    stage_field = next(
        item for item in task_meta.get("fields") or [] if item.get("name") == "clickUpStatus"
    )
    reviewing_stage = next(
        str(option["value"])
        for option in stage_field.get("options") or []
        if normalized(option.get("label")) == normalized("em revisão")
    )

    run_id = uuid.uuid4()
    tag = "[QA CUTOVER " + run_id.hex[:10] + "]"
    task_ids = [str(uuid.uuid5(run_id, name)) for name in ("parent", "child", "peer")]
    dependency_ids = [str(uuid.uuid5(run_id, "dependency"))]
    historical_tasks_before = client.list_rest("/rest/tasks", limit=200)
    historical_dependencies_before = client.list_rest(
        "/rest/clickUpDependencies", limit=200
    )
    historical_before = task_version_snapshot(historical_tasks_before)
    journal: dict[str, Any] = {
        "version": 1,
        "status": "prepared",
        "tag": tag,
        "task_ids": task_ids,
        "dependency_ids": dependency_ids,
        "workspace_preflight": {
            "matched": True,
            "active": True,
            "public_invite_disabled": True,
        },
        "historical_fingerprint_before": task_version_fingerprint(
            historical_before
        ),
        "created_task_ids": [],
        "created_dependency_ids": [],
        "cleanup_attempted_task_ids": [],
        "cleanup_attempted_dependency_ids": [],
        "cleanup_errors": [],
    }
    atomic_write_json(args.journal, journal)

    def save_journal() -> None:
        atomic_write_json(args.journal, journal)

    def create_task(record_id: str, suffix: str, **extra: Any) -> dict[str, Any]:
        task = unwrap_one(
            client.request(
                "/rest/tasks",
                {"id": record_id, "title": f"{tag} {suffix}", **extra},
                "POST",
            )
        )
        if str(task.get("id")) != record_id:
            raise CutoverError("Twenty did not preserve the pre-journaled QA task id")
        journal["created_task_ids"].append(record_id)
        save_journal()
        return task

    def read_task(task_id: str) -> dict[str, Any]:
        return unwrap_one(client.request(f"/rest/tasks/{task_id}"))

    smoke_error: BaseException | None = None
    result: dict[str, Any] = {}
    journal["status"] = "running"
    save_journal()
    try:
        parent = create_task(task_ids[0], "parent", clickUpListId=default_list_id)
        child = create_task(task_ids[1], "child")
        peer = create_task(
            task_ids[2], "dependency-target", clickUpListId=default_list_id
        )

        for task in (parent, child, peer):
            wait_for(
                lambda task_id=str(task["id"]): read_task(task_id),
                lambda value: (
                    value.get("assigneeId") == member_id
                    and value.get("clickUpListId") == default_list_id
                    and bool(value.get("clickUpStatus"))
                    and value.get("status") == "TODO"
                ),
                "created-task defaults",
            )

        child_id = str(child["id"])
        parent_id = str(parent["id"])
        peer_id = str(peer["id"])
        client.request(
            f"/rest/tasks/{child_id}", {"clickUpParentTaskId": parent_id}, "PATCH"
        )
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("clickUpParentTaskId") == parent_id,
            "editable parent relation",
        )

        dependency = unwrap_one(
            client.request(
                "/rest/clickUpDependencies",
                {
                    "name": f"{tag} dependency",
                    "id": dependency_ids[0],
                    "fromTaskId": child_id,
                    "toTaskId": peer_id,
                    "relationType": "DEPENDS_ON",
                },
                "POST",
            )
        )
        if str(dependency.get("id")) != dependency_ids[0]:
            raise CutoverError("Twenty did not preserve the pre-journaled dependency id")
        journal["created_dependency_ids"].append(dependency_ids[0])
        save_journal()
        all_dependencies = client.list_rest("/rest/clickUpDependencies", limit=200)
        if not any(
            item.get("id") == dependency["id"]
            and item.get("fromTaskId") == child_id
            and item.get("toTaskId") == peer_id
            for item in all_dependencies
        ):
            raise CutoverError("dependency relation is not readable by the extension contract")

        client.request(
            f"/rest/tasks/{child_id}", {"clickUpStatus": reviewing_stage}, "PATCH"
        )
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("clickUpStatus") == reviewing_stage
            and value.get("status") == "IN_PROGRESS",
            "Etapa to native status",
        )
        stage_round_trip_cycles = 3
        for cycle in range(1, stage_round_trip_cycles + 1):
            client.request(
                f"/rest/tasks/{child_id}", {"clickUpStatus": done_stage}, "PATCH"
            )
            wait_for(
                lambda: read_task(child_id),
                lambda value: value.get("status") == "DONE"
                and value.get("clickUpStatus") == done_stage
                and value.get("cutoverStageBeforeDone") == reviewing_stage,
                f"Etapa completion memory cycle {cycle}",
            )
            client.request(f"/rest/tasks/{child_id}", {"status": "TODO"}, "PATCH")
            wait_for(
                lambda: read_task(child_id),
                lambda value: value.get("status") == "IN_PROGRESS"
                and value.get("clickUpStatus") == reviewing_stage
                and value.get("cutoverStageBeforeDone") in (None, ""),
                f"Etapa exact reopen cycle {cycle}",
            )

        client.request(f"/rest/tasks/{child_id}", {"status": "DONE"}, "PATCH")
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("status") == "DONE"
            and value.get("clickUpStatus") == done_stage
            and value.get("cutoverStageBeforeDone") == reviewing_stage,
            "native completion",
        )
        client.request(f"/rest/tasks/{child_id}", {"status": "TODO"}, "PATCH")
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("status") == "IN_PROGRESS"
            and value.get("clickUpStatus") == reviewing_stage,
            "native reopen",
        )

        client.request(
            f"/rest/tasks/{child_id}", {"clickUpDueOn": "2026-11-03"}, "PATCH"
        )
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("clickUpDueOn") == "2026-11-03"
            and value.get("dueAt") is None,
            "date-only deadline",
        )
        client.request(
            f"/rest/tasks/{child_id}",
            {"dueAt": "2026-11-03T14:30:00.000Z"},
            "PATCH",
        )
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("dueAt") == "2026-11-03T14:30:00.000Z"
            and value.get("clickUpDueOn") is None,
            "timed deadline exclusivity",
        )
        client.request(
            f"/rest/tasks/{child_id}", {"clickUpDueOn": "2026-11-04"}, "PATCH"
        )
        wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("clickUpDueOn") == "2026-11-04"
            and value.get("dueAt") is None,
            "date-only replacement",
        )

        client.request(f"/rest/tasks/{child_id}", {"assigneeId": None}, "PATCH")
        stable = wait_for(
            lambda: read_task(child_id),
            lambda value: value.get("assigneeId") is None,
            "explicit unassignment",
        )
        stable_updated_at = stable.get("updatedAt")
        time.sleep(2)
        stable = read_task(child_id)
        if stable.get("assigneeId") is not None or stable.get("updatedAt") != stable_updated_at:
            raise CutoverError("explicit unassignment was overwritten or event loop remained active")

        result = {
            "created_tasks": len(task_ids),
            "created_dependencies": len(dependency_ids),
            "defaults": True,
            "list_relation": True,
            "stage_round_trip": True,
            "stage_round_trip_cycles": stage_round_trip_cycles,
            "native_status_round_trip": True,
            "date_semantics": True,
            "explicit_unassign_persisted": True,
            "subtask_relation": True,
            "dependency_relation": True,
            "idempotent": True,
        }
    except BaseException as exc:
        smoke_error = exc
        journal["smoke_error_type"] = type(exc).__name__
    finally:
        for dependency_id in reversed(dependency_ids):
            journal["cleanup_attempted_dependency_ids"].append(dependency_id)
            try:
                client.request(
                    f"/rest/clickUpDependencies/{dependency_id}", None, "DELETE"
                )
            except CutoverError as exc:
                if "HTTP 404" not in str(exc):
                    journal["cleanup_errors"].append(
                        {"kind": "dependency", "error_type": type(exc).__name__}
                    )
        for task_id in reversed(task_ids):
            journal["cleanup_attempted_task_ids"].append(task_id)
            try:
                client.request(f"/rest/tasks/{task_id}", None, "DELETE")
            except CutoverError as exc:
                if "HTTP 404" not in str(exc):
                    journal["cleanup_errors"].append(
                        {"kind": "task", "error_type": type(exc).__name__}
                    )
        journal["status"] = "cleanup-attempted"
        save_journal()

    remaining_tasks = client.list_rest("/rest/tasks", limit=200)
    remaining_dependencies = client.list_rest("/rest/clickUpDependencies", limit=200)
    cleanup_incomplete = any(item.get("id") in task_ids for item in remaining_tasks) or any(
        item.get("id") in dependency_ids for item in remaining_dependencies
    )
    if cleanup_incomplete:
        journal["cleanup_errors"].append(
            {"kind": "verification", "error_type": "RecordsRemain"}
        )
    historical_after = task_version_snapshot(remaining_tasks)
    qa_ids = set(task_ids)
    before_ids = set(historical_before)
    after_ids = set(historical_after)
    historical_activity = {
        "changed_ids": sorted(
            record_id
            for record_id in before_ids & after_ids
            if record_id not in qa_ids
            and historical_before[record_id] != historical_after[record_id]
        ),
        "added_ids": sorted((after_ids - before_ids) - qa_ids),
        "removed_ids": sorted((before_ids - after_ids) - qa_ids),
    }
    journal["historical_fingerprint_after"] = task_version_fingerprint(
        historical_after
    )
    journal["historical_activity"] = historical_activity
    if smoke_error is not None or journal["cleanup_errors"]:
        journal["status"] = "failed"
        save_journal()
        if smoke_error is not None:
            raise smoke_error
        raise CutoverError("QA cleanup encountered one or more errors")

    imported_tasks = [item for item in remaining_tasks if item.get("clickUpId")]
    final_counts = {
        "imported_tasks": len(imported_tasks),
        "total_tasks": len(remaining_tasks),
        "subtasks": sum(bool(item.get("clickUpParentTaskId")) for item in remaining_tasks),
        "dependencies": len(remaining_dependencies),
    }
    expected_counts = {
        "imported_tasks": sum(
            bool(item.get("clickUpId")) for item in historical_tasks_before
        ),
        "total_tasks": len(historical_before)
        + len(historical_activity["added_ids"])
        - len(historical_activity["removed_ids"]),
        "subtasks": sum(
            bool(item.get("clickUpParentTaskId")) for item in historical_tasks_before
        ),
        "dependencies": len(historical_dependencies_before),
    }
    if final_counts != expected_counts:
        journal["status"] = "failed-count-verification"
        journal["final_counts"] = final_counts
        save_journal()
        raise CutoverError("post-cleanup historical counts changed")
    journal["status"] = "complete"
    journal["final_counts"] = final_counts
    save_journal()
    print(
        json.dumps(
            {
                **result,
                "qa_cleanup": True,
                "non_qa_activity_detected": any(historical_activity.values()),
                **final_counts,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
