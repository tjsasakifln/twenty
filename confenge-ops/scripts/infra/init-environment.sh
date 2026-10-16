#!/usr/bin/env bash
set -euo pipefail
umask 077
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
env_file="$root/.env"
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Run as root.' >&2; exit 1; }
command -v openssl >/dev/null || { printf '%s\n' 'openssl is required.' >&2; exit 1; }
if [[ ! -e "$env_file" ]]; then
  password=$(openssl rand -hex 20)
  key=$(openssl rand -base64 32 | tr -d '\n')
  tmp=$(mktemp "${env_file}.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  cat >"$tmp" <<EOF
PG_DATABASE_USER=postgres
PG_DATABASE_PASSWORD=$password
PG_DATABASE_NAME=confenge
SERVER_URL=http://127.0.0.1:18300
ENCRYPTION_KEY=$key
FALLBACK_ENCRYPTION_KEY=
STORAGE_TYPE=local
EOF
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
  trap - EXIT
fi
printf '%s\n' 'Environment is present; secrets were not printed.'
