#!/usr/bin/env python3
import argparse
import json
import stat
import urllib.request
from pathlib import Path

from deploy_origin_policy import validate_api_origin


EXPECTED_VARIABLE_KEYS = {
    "CONTRACTUAL_MILESTONES_VIEW_ID",
    "OPERATIONAL_CALENDAR_VIEW_ID",
    "CUTOVER_ENABLED",
    "CUTOVER_API_BASE_URL",
    "CUTOVER_MEMBER_ID",
    "CUTOVER_DEFAULT_LIST_ID",
    "CUTOVER_STAGE_STATUS_MAP",
    "CUTOVER_STAGE_TODO",
    "CUTOVER_STAGE_IN_PROGRESS",
    "CUTOVER_STAGE_DONE",
}

QUERY = """
query VerifyInstalledApp($applicationUid: UUID!) {
  findOneApplication(universalIdentifier: $applicationUid) {
    id
    universalIdentifier
    name
    applicationVariables { key value }
  }
  getPageLayouts(pageLayoutType: STANDALONE_PAGE) {
    id
    name
    universalIdentifier
    applicationId
  }
}
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-id", required=True)
    parser.add_argument("--application-uid", required=True)
    parser.add_argument("--page-layout-uid", action="append", required=True)
    args = parser.parse_args()

    if stat.S_IMODE(args.credential_file.stat().st_mode) & 0o077:
        raise SystemExit("Credential file permissions are too broad")
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    if credential.get("workspace_id") != args.expected_workspace_id:
        raise SystemExit("Credential workspace mismatch")
    base_url = validate_api_origin(str(credential.get("base_url", "")))
    api_key = str(credential.get("api_key", ""))
    if not api_key:
        raise SystemExit("Missing API key")

    request = urllib.request.Request(
        base_url + "/metadata",
        data=json.dumps(
            {
                "query": QUERY,
                "variables": {"applicationUid": args.application_uid},
            }
        ).encode(),
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
        raise SystemExit("Metadata GraphQL verification failed")
    data = payload.get("data") or {}
    application = data.get("findOneApplication")
    if not isinstance(application, dict):
        raise SystemExit("Application is not installed")
    if application.get("universalIdentifier") != args.application_uid:
        raise SystemExit("Installed application identity mismatch")

    variables = application.get("applicationVariables") or []
    variables_by_key = {
        item.get("key"): item.get("value")
        for item in variables
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    if set(variables_by_key) != EXPECTED_VARIABLE_KEYS:
        raise SystemExit("Installed application variable schema mismatch")
    if variables_by_key.get("CUTOVER_ENABLED") != "false":
        raise SystemExit("CUTOVER_ENABLED is not false")
    if variables_by_key.get("CUTOVER_API_BASE_URL") != "http://server:3000":
        raise SystemExit("CUTOVER_API_BASE_URL is not the allowlisted internal origin")

    expected_page_uids = set(args.page_layout_uid)
    layouts = [
        layout
        for layout in (data.get("getPageLayouts") or [])
        if isinstance(layout, dict)
        and layout.get("universalIdentifier") in expected_page_uids
    ]
    if {layout.get("universalIdentifier") for layout in layouts} != expected_page_uids:
        raise SystemExit("Installed page layouts are incomplete")
    if any(layout.get("applicationId") != application.get("id") for layout in layouts):
        raise SystemExit("Installed page layout belongs to another application")

    print(
        json.dumps(
            {
                "application_id": application.get("id"),
                "application_name": application.get("name"),
                "cutover_enabled": False,
                "page_layouts": sorted(
                    (
                        {
                            "id": layout.get("id"),
                            "name": layout.get("name"),
                            "universal_identifier": layout.get("universalIdentifier"),
                        }
                        for layout in layouts
                    ),
                    key=lambda item: item["name"],
                ),
                "variable_count": len(variables_by_key),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
