#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly SOURCE_DIR=${1:?source directory is required}
readonly CATALOG_PATH=${2:?private catalog path is required}
readonly DESTINATION=/opt/confenge-gestao/integrations/codex-routines
readonly OUTBOX_DIR=/var/lib/confenge-gestao/codex-routines
readonly STAGING=$(mktemp -d /opt/confenge-gestao/integrations/codex-routines.staging.XXXXXX)
trap 'rm -rf "$STAGING"' EXIT

install -m 0644 "$SOURCE_DIR"/*.py "$STAGING"/
install -m 0644 "$SOURCE_DIR"/README.md "$SOURCE_DIR"/catalog.example.json "$STAGING"/
install -m 0600 "$CATALOG_PATH" "$STAGING/catalog.json"
/opt/confenge-gestao/.venv/bin/python "$STAGING/cli.py" catalog-sync >/dev/null
mkdir -p "$OUTBOX_DIR"
chmod 0700 "$OUTBOX_DIR"
if [[ -d "$DESTINATION" ]]; then
  mv "$DESTINATION" "${DESTINATION}.previous.$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$STAGING" "$DESTINATION"
trap - EXIT
printf '%s\n' "$DESTINATION"
