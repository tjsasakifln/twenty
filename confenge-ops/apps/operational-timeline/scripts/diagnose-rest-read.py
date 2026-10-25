#!/usr/bin/env python3
import argparse
import json
import stat
import urllib.error
import urllib.request
from pathlib import Path

from deploy_origin_policy import validate_api_origin


def safe_shape(value: object, depth: int = 0) -> object:
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: safe_shape(child, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "item": safe_shape(value[0], depth + 1) if value else None,
        }
    return type(value).__name__


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-id", required=True)
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

    for object_plural in ("tasks", "clickUpDependencies"):
        for depth in (1, 0):
            url = f"{base_url}/rest/{object_plural}?limit=1&depth={depth}"
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                    print(
                        f"object={object_plural} depth={depth}: status={response.status} shape="
                        + json.dumps(safe_shape(payload), sort_keys=True)
                    )
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                    safe = {
                        key: payload.get(key)
                        for key in ("statusCode", "code", "error", "message")
                        if key in payload
                    }
                except json.JSONDecodeError:
                    safe = {"error": "non-json error body"}
                print(
                    f"object={object_plural} depth={depth}: status={error.code} error="
                    + json.dumps(safe, ensure_ascii=False, sort_keys=True)
                )


if __name__ == "__main__":
    main()
