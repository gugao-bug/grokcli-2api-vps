#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=/opt/grokcli-2api
COMPOSE=(docker compose -f docker-compose.vps.yml)
BACKUP_ROOT=/opt/grokcli-2api-auto-backups
GIT=(git -c safe.directory="$ROOT")
LOCAL_PATCH="$ROOT/deploy/grok-client-headers.patch"

fetch_origin() {
  local proxy="${GROK2API_GIT_PROXY:-}"
  if [[ -z "$proxy" ]] && docker inspect grokcli-2api-privoxy-1 >/dev/null 2>&1; then
    local proxy_ip
    proxy_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' grokcli-2api-privoxy-1)
    [[ -n "$proxy_ip" ]] && proxy="http://${proxy_ip}:8118"
  fi

  if [[ -n "$proxy" ]]; then
    echo "fetching origin/main through deployment proxy"
    "${GIT[@]}" -c http.proxy="$proxy" fetch --quiet origin main
  else
    "${GIT[@]}" fetch --quiet origin main
  fi
}

apply_local_patch() {
  [[ -s "$LOCAL_PATCH" ]] || return 0
  if "${GIT[@]}" apply --reverse --check "$LOCAL_PATCH" >/dev/null 2>&1; then
    echo "local header compatibility changes already present"
    return 0
  fi
  "${GIT[@]}" apply --3way "$LOCAL_PATCH"
}

exec 9>/var/lock/grokcli-2api-update.lock
flock -n 9 || exit 0

cd "$ROOT"
fetch_origin
old_commit=$("${GIT[@]}" rev-parse HEAD)
new_commit=$("${GIT[@]}" rev-parse origin/main)

if [[ "$old_commit" == "$new_commit" ]]; then
  echo "grokcli-2api already current: ${old_commit:0:12}"
  exit 0
fi

stamp=$(date +%Y%m%d_%H%M%S)
backup_dir="$BACKUP_ROOT/$stamp"
mkdir -p "$backup_dir"
tar -czf "$backup_dir/data.tar.gz" data .env docker-compose.vps.yml Dockerfile.vps
if docker ps --format '{{.Names}}' | grep -qx grokcli-2api-postgres; then
  docker exec grokcli-2api-postgres pg_dump -U grok2api grok2api | gzip >"$backup_dir/postgres.sql.gz"
fi
printf '%s\n' "$old_commit" >"$backup_dir/previous_commit"

rollback() {
  echo "update failed; rolling back to $old_commit" >&2
  "${GIT[@]}" reset --hard "$old_commit"
  apply_local_patch
  "${COMPOSE[@]}" build --pull=false grokcli-2api
  "${COMPOSE[@]}" up -d
}
trap rollback ERR

"${GIT[@]}" reset --hard "$new_commit"
apply_local_patch
"${COMPOSE[@]}" build --pull=false grokcli-2api
"${COMPOSE[@]}" up -d

healthy=0
for _ in $(seq 1 36); do
  if curl -fsS http://127.0.0.1:3000/health >/dev/null; then
    healthy=1
    break
  fi
  sleep 5
done
[[ "$healthy" == 1 ]]

trap - ERR
echo "updated grokcli-2api ${old_commit:0:12} -> ${new_commit:0:12}"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf -- {} +
