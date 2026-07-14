#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=/opt/grokcli-2api
COMPOSE=(docker compose -f docker-compose.vps.yml)
BACKUP_ROOT=/opt/grokcli-2api-auto-backups
GIT=(git -c safe.directory="$ROOT")
LOCAL_PATCH="$ROOT/deploy/grok-client-headers.patch"

apply_local_patch() {
  [[ -s "$LOCAL_PATCH" ]] || return 0
  "${GIT[@]}" apply --check "$LOCAL_PATCH"
  "${GIT[@]}" apply "$LOCAL_PATCH"
}

exec 9>/var/lock/grokcli-2api-update.lock
flock -n 9 || exit 0

cd "$ROOT"
"${GIT[@]}" fetch --quiet origin main
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
