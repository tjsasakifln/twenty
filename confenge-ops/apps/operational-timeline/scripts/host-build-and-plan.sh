#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

readonly APP_DIR="${APP_DIR:-/opt/confenge-gestao/apps/operational-timeline}"
readonly CREDENTIAL_FILE="${CREDENTIAL_FILE:-/etc/confenge-gestao/admin.json}"
readonly REMOTE_NAME="${REMOTE_NAME:-confenge-production}"
readonly PLAN_OUTPUT="${PLAN_OUTPUT:?PLAN_OUTPUT is required}"
readonly EXPECTED_WORKSPACE_ID="${CONFENGE_EXPECTED_WORKSPACE_ID:?CONFENGE_EXPECTED_WORKSPACE_ID is required}"
readonly PUBLIC_URL="${TWENTY_PUBLIC_URL:?TWENTY_PUBLIC_URL is required}"

test "$(id -u)" -eq 0 || { printf 'Run as root.\n' >&2; exit 1; }
test -d "$APP_DIR" || { printf 'App directory not found.\n' >&2; exit 1; }
test -r "$CREDENTIAL_FILE" || { printf 'Credential file is not readable.\n' >&2; exit 1; }
test "$PUBLIC_URL" = 'https://gestao.confenge.com.br' || {
  printf 'Refusing unexpected public origin.\n' >&2
  exit 1
}
resolved_app_dir="$(realpath -e "$APP_DIR")"
case "$resolved_app_dir" in
  /opt/confenge-gestao/apps/operational-timeline|/root/confenge-timeline-deploy/operational-timeline) ;;
  *) printf 'Refusing unexpected app directory.\n' >&2; exit 1 ;;
esac
python3 "$resolved_app_dir/scripts/test_deploy_origin_policy.py" >/dev/null

private_env="$(mktemp)"
cleanup() {
  rm -f "$private_env"
  unset TWENTY_DEPLOY_API_KEY CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS
  rm -rf -- "$resolved_app_dir/node_modules" "$resolved_app_dir/.yarn" "$resolved_app_dir/.twenty"
}
trap cleanup EXIT

python3 - "$CREDENTIAL_FILE" "$EXPECTED_WORKSPACE_ID" "$PUBLIC_URL" "$resolved_app_dir/scripts" >"$private_env" <<'PY'
import json
import shlex
import sys
import urllib.request

credential_path, expected_workspace_id, public_url, scripts_dir = sys.argv[1:]
sys.path.insert(0, scripts_dir)
from deploy_origin_policy import validate_api_origin, validate_public_origin
with open(credential_path, encoding="utf-8") as handle:
    credential = json.load(handle)

if credential.get("workspace_id") != expected_workspace_id:
    raise SystemExit("Credential workspace mismatch")

base_url = validate_api_origin(str(credential.get("base_url", "")))
api_key = str(credential.get("api_key", ""))
if not api_key:
    raise SystemExit("Invalid production credential shape")
public_url = validate_public_origin(public_url)

request = urllib.request.Request(
    base_url + "/rest/metadata/objects?limit=500",
    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
)
with urllib.request.urlopen(request, timeout=60) as response:
    payload = json.load(response)

if isinstance(payload, list):
    rows = payload
elif isinstance(payload, dict):
    rows = payload.get("data") or payload.get("items") or payload.get("records") or []
else:
    rows = []

by_name = {row.get("nameSingular"): row for row in rows if isinstance(row, dict)}
names = ("clickUpDependency", "clickUpList")
universal_ids = []
for name in names:
    value = by_name.get(name, {}).get("universalIdentifier")
    if not isinstance(value, str) or not value:
        raise SystemExit(f"Missing object metadata: {name}")
    universal_ids.append(value)

print("TWENTY_REMOTE_URL=" + shlex.quote(public_url.rstrip("/")))
print("TWENTY_DEPLOY_API_KEY=" + shlex.quote(api_key))
print(
    "CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS="
    + shlex.quote(",".join(universal_ids))
)
PY

# The file is root-only and removed by the EXIT trap. Do not enable shell tracing.
# shellcheck disable=SC1090
. "$private_env"
export TWENTY_DEPLOY_API_KEY CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS

install -d -m 0700 "$(dirname "$PLAN_OUTPUT")"
install -d -m 0700 /root/.twenty

# The host is intentionally kept lean; use the already-running Twenty 2.45
# image, which carries the compatible Node 24 runtime and official Yarn.
sdk_image="$(docker inspect -f '{{.Config.Image}}' confenge-gestao-server-1)"
run_sdk() {
  docker run --rm --network host --user 0:0 \
    -e HOME=/root \
    -e TWENTY_DEPLOY_API_KEY \
    -e TWENTY_REMOTE_URL \
    -e CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS \
    -v "$resolved_app_dir:/work" \
    -v /root/.twenty:/root/.twenty \
    -w /work \
    --entrypoint sh \
    "$sdk_image" -ec "$1"
}

run_sdk 'corepack enable && yarn install --immutable && yarn test && yarn typecheck && yarn build && yarn deploy:check'

run_sdk 'corepack enable && yarn twenty remote:add --as '"$REMOTE_NAME"' --url "$TWENTY_REMOTE_URL" --api-key "$TWENTY_DEPLOY_API_KEY"'

run_sdk 'corepack enable && yarn twenty --remote '"$REMOTE_NAME"' plan . --no-delete' | tee "$PLAN_OUTPUT"
chmod 0600 "$PLAN_OUTPUT"

printf 'Build and read-only plan completed. apply was not executed.\n'
