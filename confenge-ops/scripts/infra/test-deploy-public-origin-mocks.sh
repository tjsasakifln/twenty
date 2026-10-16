#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HELPER="$SCRIPT_DIR/deploy-public-origin.sh"
readonly ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

make_case() {
  local name=$1
  local root="$ROOT/$name"
  mkdir -p "$root/app/deploy/nginx" "$root/nginx/sites-available" "$root/nginx/sites-enabled" "$root/nginx/conf.d" "$root/backup" "$root/bin"
  printf 'SERVER_URL=http://before.example\nUNCHANGED=1\n' > "$root/app/.env"
  : > "$root/app/deploy/docker-compose.yml"
  printf 'new map\n' > "$root/app/deploy/nginx/gestao.confenge.com.br-map.conf"
  printf 'new vhost\n' > "$root/app/deploy/nginx/gestao.confenge.com.br.conf"
  printf 'old map\n' > "$root/nginx/conf.d/gestao.confenge.com.br-map.conf"
  printf 'old vhost\n' > "$root/nginx/sites-available/gestao.confenge.com.br"
  printf 'old linked vhost\n' > "$root/nginx/sites-available/old.conf"
  ln -s "$root/nginx/sites-available/old.conf" "$root/nginx/sites-enabled/gestao.confenge.com.br"
  cat > "$root/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_LOG"
case " $* " in *" worker "*) [ "${MOCK_FAIL:-}" = worker ] && exit 21;; esac
case " $* " in *" worker "*)
  if [ "${MOCK_FAIL:-}" = worker-once ] && [ ! -e "$MOCK_WORKER_FAILED" ]; then : > "$MOCK_WORKER_FAILED"; exit 21; fi
  if [ "${MOCK_FAIL:-}" = worker-persist ]; then exit 21; fi
  : > "$MOCK_WORKER_ACTIVE"
;; esac
case " $* " in *" inspect "*) printf 'healthy\n';; esac
EOF
  cat > "$root/bin/nginx" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "nginx $*" >> "$MOCK_LOG"
if [ "${MOCK_FAIL:-}" = nginx ] && [ ! -e "$MOCK_NGINX_FAILED" ]; then
  : > "$MOCK_NGINX_FAILED"
  exit 22
fi
EOF
  cat > "$root/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "systemctl $*" >> "$MOCK_LOG"
exit 0
EOF
  cat > "$root/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "curl $*" >> "$MOCK_LOG"
exit 0
EOF
  cat > "$root/bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod 755 "$root/bin"/*
}

run_case() {
  local name=$1 failure=$2 root="$ROOT/$1"
  make_case "$name"
  set +e
  APP="$root/app" ENV_FILE="$root/app/.env" COMPOSE_FILE="$root/app/deploy/docker-compose.yml" BACKUP_DIR="$root/backup" STAMP=test \
  NGINX_AVAILABLE="$root/nginx/sites-available/gestao.confenge.com.br" NGINX_ENABLED="$root/nginx/sites-enabled/gestao.confenge.com.br" NGINX_MAP="$root/nginx/conf.d/gestao.confenge.com.br-map.conf" \
  DOCKER_BIN="$root/bin/docker" NGINX_BIN="$root/bin/nginx" SYSTEMCTL_BIN="$root/bin/systemctl" CURL_BIN="$root/bin/curl" SLEEP_BIN="$root/bin/sleep" MOCK_LOG="$root/mock.log" MOCK_NGINX_FAILED="$root/nginx.failed" MOCK_WORKER_FAILED="$root/worker.failed" MOCK_WORKER_ACTIVE="$root/worker.active" MOCK_FAIL="$failure" bash "$HELPER" >"$root/run.out" 2>&1
  status=$?
  set -e
  [ "$status" -ne 0 ]
  cmp -s "$root/app/.env" <(printf 'SERVER_URL=http://before.example\nUNCHANGED=1\n')
  cmp -s "$root/nginx/conf.d/gestao.confenge.com.br-map.conf" <(printf 'old map\n')
  cmp -s "$root/nginx/sites-available/gestao.confenge.com.br" <(printf 'old vhost\n')
  [ "$(readlink "$root/nginx/sites-enabled/gestao.confenge.com.br")" = "$root/nginx/sites-available/old.conf" ]
  ! grep -qE '(^| )db( |$)|(^| )redis( |$)' "$root/mock.log"
  grep -q 'server' "$root/mock.log"; grep -q 'worker' "$root/mock.log"
  if [ "$failure" = nginx ]; then grep -q 'systemctl reload nginx' "$root/mock.log"; fi
  if [ "$failure" = worker-once ]; then [ -e "$root/worker.active" ]; fi
  ! grep -q 'ROLLBACK_INCOMPLETE' "$root/run.out"
  printf '%s: rollback verified\n' "$name"
}

run_case worker_failure worker-once
run_case nginx_test_failure nginx
make_case worker_persistent
root="$ROOT/worker_persistent"
set +e
APP="$root/app" ENV_FILE="$root/app/.env" COMPOSE_FILE="$root/app/deploy/docker-compose.yml" BACKUP_DIR="$root/backup" STAMP=test \
NGINX_AVAILABLE="$root/nginx/sites-available/gestao.confenge.com.br" NGINX_ENABLED="$root/nginx/sites-enabled/gestao.confenge.com.br" NGINX_MAP="$root/nginx/conf.d/gestao.confenge.com.br-map.conf" \
DOCKER_BIN="$root/bin/docker" NGINX_BIN="$root/bin/nginx" SYSTEMCTL_BIN="$root/bin/systemctl" CURL_BIN="$root/bin/curl" SLEEP_BIN="$root/bin/sleep" MOCK_LOG="$root/mock.log" MOCK_NGINX_FAILED="$root/nginx.failed" MOCK_WORKER_FAILED="$root/worker.failed" MOCK_WORKER_ACTIVE="$root/worker.active" MOCK_FAIL=worker-persist bash "$HELPER" >"$root/run.out" 2>&1
status=$?
set -e
[ "$status" -ne 0 ]
grep -q 'ROLLBACK_INCOMPLETE: worker restore failed' "$root/run.out"
grep -q 'ROLLBACK_INCOMPLETE: manual recovery required' "$root/run.out"
cmp -s "$root/app/.env" <(printf 'SERVER_URL=http://before.example\nUNCHANGED=1\n')
cmp -s "$root/nginx/conf.d/gestao.confenge.com.br-map.conf" <(printf 'old map\n')
cmp -s "$root/nginx/sites-available/gestao.confenge.com.br" <(printf 'old vhost\n')
[ "$(readlink "$root/nginx/sites-enabled/gestao.confenge.com.br")" = "$root/nginx/sites-available/old.conf" ]
! grep -qE '(^| )db( |$)|(^| )redis( |$)' "$root/mock.log"
printf 'worker_persistent: explicit incomplete rollback verified\n'
printf 'mock rollback tests passed\n'
