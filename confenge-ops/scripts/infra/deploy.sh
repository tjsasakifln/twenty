#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compose=(docker compose --project-name confenge-gestao --env-file "$root/.env" -f "$root/deploy/docker-compose.yml")
[[ -r "$root/.env" ]] || { printf '%s\n' '.env is missing; run init-environment.sh first.' >&2; exit 1; }
docker version >/dev/null
"${compose[@]}" config --quiet
"${compose[@]}" pull
printf '%s\n' 'Preflight passed. No containers were started.'
if [[ "${1:-}" == "--go" ]]; then
  "${compose[@]}" up -d
  ready=false
  for _ in $(seq 1 120); do
    if curl --fail --silent --max-time 3 http://127.0.0.1:18300/healthz >/dev/null; then ready=true; break; fi
    sleep 3
  done
  [[ "$ready" == true ]] || { printf '%s\n' 'Twenty did not become ready within six minutes; inspect project logs before continuing.' >&2; exit 1; }
  "$root/scripts/infra/health.sh"
else
  printf '%s\n' 'Run this script again with --go to start or update the stack.'
fi
