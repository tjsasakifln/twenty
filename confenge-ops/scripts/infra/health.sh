#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compose=(docker compose --project-name confenge-gestao --env-file "$root/.env" -f "$root/deploy/docker-compose.yml")
[[ -r "$root/.env" ]] || { printf '%s\n' '.env is missing.' >&2; exit 1; }
"${compose[@]}" ps --format 'table {{.Name}}\t{{.State}}\t{{.Status}}'
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:18300/healthz >/dev/null
printf '%s\n' 'HTTP health check passed.'
