#!/usr/bin/env python3
import argparse
import json
import os
import stat
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from deploy_origin_policy import validate_api_origin


EXPECTED_KEYS = {
    "CONTRACTUAL_MILESTONES_VIEW_ID",
    "OPERATIONAL_CALENDAR_VIEW_ID",
    "CUTOVER_API_BASE_URL",
    "CUTOVER_MEMBER_ID",
    "CUTOVER_DEFAULT_LIST_ID",
    "CUTOVER_STAGE_STATUS_MAP",
    "CUTOVER_STAGE_TODO",
    "CUTOVER_STAGE_IN_PROGRESS",
    "CUTOVER_STAGE_DONE",
}
SECRET_KEYS = {
    "CUTOVER_MEMBER_ID",
    "CUTOVER_DEFAULT_LIST_ID",
    "CUTOVER_STAGE_STATUS_MAP",
    "CUTOVER_STAGE_TODO",
    "CUTOVER_STAGE_IN_PROGRESS",
    "CUTOVER_STAGE_DONE",
}

FIND_APPLICATION = """
query FindApplicationForVariableLoad($applicationUid: UUID!) {
  findOneApplication(universalIdentifier: $applicationUid) {
    id
    universalIdentifier
    applicationVariables { key value }
  }
}
"""

UPDATE_VARIABLE = """
mutation UpdatePrivateApplicationVariable(
  $key: String!
  $value: String!
  $applicationId: UUID!
) {
  updateOneApplicationVariable(
    key: $key
    value: $value
    applicationId: $applicationId
  )
}
"""


def normalize_values(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != EXPECTED_KEYS:
        raise ValueError("Variable file must contain exactly the approved nine keys")
    normalized: dict[str, str] = {}
    for key in sorted(EXPECTED_KEYS):
        value = payload[key]
        if key == "CUTOVER_STAGE_STATUS_MAP":
            if isinstance(value, dict):
                value = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
            elif isinstance(value, str):
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError("CUTOVER_STAGE_STATUS_MAP must encode an object")
                value = json.dumps(
                    parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
            else:
                raise ValueError("CUTOVER_STAGE_STATUS_MAP must be an object or JSON string")
        if key == "CUTOVER_API_BASE_URL":
            if value not in ("http://server:3000", "http://server:3000/"):
                raise ValueError(
                    "CUTOVER_API_BASE_URL must be the allowlisted internal origin"
                )
            value = "http://server:3000"
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        normalized[key] = value
    return normalized


def graphql(base_url: str, api_key: str, query: str, variables: dict) -> dict:
    request = urllib.request.Request(
        base_url + "/metadata",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError("Metadata GraphQL request failed")
    return payload.get("data") or {}


def read_application(base_url: str, api_key: str, application_uid: str) -> dict:
    application = graphql(
        base_url,
        api_key,
        FIND_APPLICATION,
        {"applicationUid": application_uid},
    ).get("findOneApplication")
    if not isinstance(application, dict):
        raise RuntimeError("Application is not installed")
    if application.get("universalIdentifier") != application_uid:
        raise RuntimeError("Installed application identity mismatch")
    return application


def values_by_key(application: dict) -> dict[str, str]:
    return {
        item.get("key"): item.get("value")
        for item in (application.get("applicationVariables") or [])
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and isinstance(item.get("value"), str)
    }


def atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_private_file(path: Path, label: str) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"{label} permissions are too broad")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--variable-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-id", required=True)
    parser.add_argument("--application-uid", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    require_private_file(args.credential_file, "Credential file")
    require_private_file(args.variable_file, "Variable file")
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    if credential.get("workspace_id") != args.expected_workspace_id:
        raise SystemExit("Credential workspace mismatch")
    base_url = validate_api_origin(str(credential.get("base_url", "")))
    api_key = str(credential.get("api_key", ""))
    if not api_key:
        raise SystemExit("Missing API key")
    values = normalize_values(
        json.loads(args.variable_file.read_text(encoding="utf-8"))
    )

    application = read_application(base_url, api_key, args.application_uid)
    current = values_by_key(application)
    if current.get("CUTOVER_ENABLED") != "false":
        raise SystemExit("Refusing variable load while CUTOVER_ENABLED is not false")
    if not EXPECTED_KEYS.issubset(current):
        raise SystemExit("Installed application variable schema is incomplete")

    changed_keys = []
    for key in sorted(EXPECTED_KEYS):
        # Twenty deliberately masks secret values on metadata reads. Re-submit
        # them idempotently and use the mutation acknowledgement as evidence.
        if key not in SECRET_KEYS and current.get(key) == values[key]:
            continue
        updated = graphql(
            base_url,
            api_key,
            UPDATE_VARIABLE,
            {
                "key": key,
                "value": values[key],
                "applicationId": application.get("id"),
            },
        ).get("updateOneApplicationVariable")
        if updated is not True:
            raise RuntimeError(f"Application variable update failed for {key}")
        changed_keys.append(key)

    verified_application = read_application(base_url, api_key, args.application_uid)
    verified = values_by_key(verified_application)
    if verified.get("CUTOVER_ENABLED") != "false":
        raise SystemExit("CUTOVER_ENABLED changed during variable load")
    mismatched_keys = sorted(
        key
        for key, value in values.items()
        if key not in SECRET_KEYS and verified.get(key) != value
    )
    if mismatched_keys:
        raise SystemExit(
            "Application variable verification failed for keys: "
            + ", ".join(mismatched_keys)
        )

    atomic_private_json(
        args.evidence,
        {
            "application_id": application.get("id"),
            "application_universal_identifier": args.application_uid,
            "changed_keys": changed_keys,
            "cutover_enabled": False,
            "loaded_key_count": len(values),
            "secret_keys_acknowledged": sorted(SECRET_KEYS),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(
        f"Loaded {len(values)} application variables; verified public values, "
        "secret mutation acknowledgements, schema and gate; "
        "CUTOVER_ENABLED remains false."
    )


if __name__ == "__main__":
    main()
