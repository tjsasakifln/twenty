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


FIND = """
query FindRegistration($universalIdentifier: String!) {
  findApplicationRegistrationByUniversalIdentifier(
    universalIdentifier: $universalIdentifier
  ) {
    id
    universalIdentifier
    name
    ownerWorkspaceId
  }
}
"""

CREATE = """
mutation CreateRegistration($input: CreateApplicationRegistrationInput!) {
  createApplicationRegistration(input: $input) {
    applicationRegistration {
      id
      universalIdentifier
      name
      ownerWorkspaceId
    }
  }
}
"""


def find_registration(base_url: str, api_key: str, universal_identifier: str):
    data = graphql(
        base_url,
        api_key,
        FIND,
        {"universalIdentifier": universal_identifier},
    )
    return data.get("findApplicationRegistrationByUniversalIdentifier")


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-id", required=True)
    parser.add_argument("--universal-identifier", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    mode = stat.S_IMODE(args.credential_file.stat().st_mode)
    if mode & 0o077:
        raise SystemExit("Credential file permissions are too broad")
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    if credential.get("workspace_id") != args.expected_workspace_id:
        raise SystemExit("Credential workspace mismatch")
    base_url = validate_api_origin(str(credential.get("base_url", "")))
    api_key = str(credential.get("api_key", ""))
    if not api_key:
        raise SystemExit("Missing API key")

    registration = find_registration(base_url, api_key, args.universal_identifier)
    result = "existing"

    if registration is None:
        result = "created"
        try:
            data = graphql(
                base_url,
                api_key,
                CREATE,
                {
                    "input": {
                        "name": args.name,
                        "universalIdentifier": args.universal_identifier,
                    }
                },
            )
            registration = (data.get("createApplicationRegistration") or {}).get(
                "applicationRegistration"
            )
        except Exception:
            # Recover an unknown create outcome by reading the canonical record.
            registration = find_registration(
                base_url, api_key, args.universal_identifier
            )

    if not isinstance(registration, dict):
        raise SystemExit("Application registration was not created or recovered")
    if registration.get("universalIdentifier") != args.universal_identifier:
        raise SystemExit("Recovered registration has unexpected identity")
    if registration.get("ownerWorkspaceId") != args.expected_workspace_id:
        raise SystemExit("Application registration belongs to another workspace")

    atomic_private_json(
        args.evidence,
        {
            "application_registration_id": registration.get("id"),
            "application_universal_identifier": args.universal_identifier,
            "owner_workspace_id": args.expected_workspace_id,
            "result": result,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"Application registration {result}; private evidence written.")


if __name__ == "__main__":
    main()
