#!/usr/bin/env bash
# Run as root on the VPS. No credentials are stored in this file.
set -Eeuo pipefail

APP=${APP:-/opt/confenge-gestao}
ENV_FILE=${ENV_FILE:-"$APP/.env"}; COMPOSE_FILE=${COMPOSE_FILE:-"$APP/deploy/docker-compose.yml"}
DOMAIN=${DOMAIN:-gestao.confenge.com.br}
NGINX_AVAILABLE=${NGINX_AVAILABLE:-"/etc/nginx/sites-available/$DOMAIN"}; NGINX_ENABLED=${NGINX_ENABLED:-"/etc/nginx/sites-enabled/$DOMAIN"}; NGINX_MAP=${NGINX_MAP:-"/etc/nginx/conf.d/$DOMAIN-map.conf"}
BACKUP_DIR=${BACKUP_DIR:-/etc/confenge-gestao/rollback}; STAMP=${STAMP:-"$(date -u +%Y%m%dT%H%M%SZ)"}
DOCKER_BIN=${DOCKER_BIN:-docker}; NGINX_BIN=${NGINX_BIN:-nginx}; SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-systemctl}; CURL_BIN=${CURL_BIN:-curl}; SLEEP_BIN=${SLEEP_BIN:-sleep}
health_was_active=false; extra_was_active=false; transaction_started=false; app_changed=false; rollback_running=false; rollback_incomplete=false

systemctl_cmd() { "$SYSTEMCTL_BIN" "$@"; }
nginx_cmd() { "$NGINX_BIN" "$@"; }
compose_cmd() { "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
snapshot_file() { local source=$1 label=$2; if [ -e "$source" ] || [ -L "$source" ]; then cp -pL "$source" "$BACKUP_DIR/$label.$STAMP"; printf 'present\n' > "$BACKUP_DIR/$label.$STAMP.state"; else printf 'absent\n' > "$BACKUP_DIR/$label.$STAMP.state"; fi; }
restore_file() { local target=$1 label=$2; if [ "$(cat "$BACKUP_DIR/$label.$STAMP.state")" = present ]; then install -D -m 0644 "$BACKUP_DIR/$label.$STAMP" "$target"; else rm -f -- "$target"; fi; }
snapshot_link() { if [ -L "$NGINX_ENABLED" ]; then readlink "$NGINX_ENABLED" > "$BACKUP_DIR/nginx-link.$STAMP"; printf 'symlink\n' > "$BACKUP_DIR/nginx-link.$STAMP.state"; elif [ -e "$NGINX_ENABLED" ]; then printf 'refusing to replace non-symlink %s\n' "$NGINX_ENABLED" >&2; return 1; else printf 'absent\n' > "$BACKUP_DIR/nginx-link.$STAMP.state"; fi; }
restore_link() { if [ "$(cat "$BACKUP_DIR/nginx-link.$STAMP.state")" = symlink ]; then ln -sfn "$(cat "$BACKUP_DIR/nginx-link.$STAMP")" "$NGINX_ENABLED"; else rm -f -- "$NGINX_ENABLED"; fi; }
restore_timers() { "$health_was_active" && systemctl_cmd start confenge-gestao-health.timer || true; "$extra_was_active" && systemctl_cmd start confenge-gestao-extra-sync.timer || true; }
restore_app() {
  [ "$app_changed" = true ] || return 0
  local failed=false
  cp -p "$BACKUP_DIR/env.$STAMP" "$ENV_FILE" || failed=true
  compose_cmd up -d --no-deps --force-recreate server || { printf 'ROLLBACK_INCOMPLETE: server restore failed\n' >&2; failed=true; }
  compose_cmd up -d --no-deps --force-recreate worker || { printf 'ROLLBACK_INCOMPLETE: worker restore failed\n' >&2; failed=true; }
  if "$failed"; then return 1; fi
  return 0
}
rollback() {
  [ "$transaction_started" = true ] || return 0
  [ "$rollback_running" = false ] || return 0
  rollback_running=true
  restore_app || rollback_incomplete=true
  restore_file "$NGINX_MAP" nginx-map || rollback_incomplete=true
  restore_file "$NGINX_AVAILABLE" nginx-vhost || rollback_incomplete=true
  restore_link || rollback_incomplete=true
  if nginx_cmd -t; then systemctl_cmd reload nginx || rollback_incomplete=true; else rollback_incomplete=true; fi
  if "$rollback_incomplete"; then printf 'ROLLBACK_INCOMPLETE: manual recovery required\n' >&2; return 1; fi
}
on_exit() { local status=$?; if [ "$status" -ne 0 ]; then rollback || true; fi; restore_timers; exit "$status"; }

main() {
  mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
  snapshot_file "$ENV_FILE" env; snapshot_file "$NGINX_MAP" nginx-map; snapshot_file "$NGINX_AVAILABLE" nginx-vhost; snapshot_link; transaction_started=true
  systemctl_cmd is-active --quiet confenge-gestao-health.timer && health_was_active=true || true
  systemctl_cmd is-active --quiet confenge-gestao-extra-sync.timer && extra_was_active=true || true
  "$health_was_active" && systemctl_cmd stop confenge-gestao-health.timer; "$extra_was_active" && systemctl_cmd stop confenge-gestao-extra-sync.timer
  if grep -q '^SERVER_URL=' "$ENV_FILE"; then sed -i 's#^SERVER_URL=.*#SERVER_URL=https://gestao.confenge.com.br#' "$ENV_FILE"; else printf '\nSERVER_URL=https://gestao.confenge.com.br\n' >> "$ENV_FILE"; fi
  app_changed=true; compose_cmd up -d --no-deps --force-recreate server
  local ready=false
  for _ in $(seq 1 120); do
    if "$CURL_BIN" --fail --silent --max-time 3 http://127.0.0.1:18300/healthz >/dev/null && [ "$("$DOCKER_BIN" inspect -f '{{.State.Health.Status}}' confenge-gestao-server-1 2>/dev/null || true)" = healthy ]; then ready=true; break; fi
    "$SLEEP_BIN" 3
  done
  [ "$ready" = true ]
  compose_cmd up -d --no-deps --force-recreate worker
  install -m 0644 "$APP/deploy/nginx/gestao.confenge.com.br-map.conf" "$NGINX_MAP"; install -m 0644 "$APP/deploy/nginx/gestao.confenge.com.br.conf" "$NGINX_AVAILABLE"; ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"
  nginx_cmd -t; systemctl_cmd reload nginx
  transaction_started=false
}

trap on_exit EXIT
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
