#!/usr/bin/env bash
# Rebuild/restart grokcli-2api with minimal API downtime (public script).
# - Build new image FIRST (old container keeps serving)
# - Recreate only the app container (keep redis/postgres up)
# - Wait until /health is ok before exiting
#
# Private host glue (sub2api, custom networks) lives in docker-rebuild.local.sh
# (gitignored). Run that after this script if needed.
set -euo pipefail
cd "$(dirname "$0")"

APP_SERVICE="${GROKCLI_APP_SERVICE:-grokcli-2api}"
# Default health port matches docker-compose.yml (override via env / compose override)
HEALTH_PORT="${GROKCLI_HEALTH_PORT:-3000}"
HEALTH_URL="${GROKCLI_HEALTH_URL:-http://127.0.0.1:${HEALTH_PORT}/health}"

echo "== git =="
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git status -sb || true
  echo "HEAD=$(git rev-parse --short HEAD)"
fi

echo "== local fingerprint =="
python3 -c 'from pathlib import Path; import re
adapter = Path("grok_build_adapter.py").read_text(encoding="utf-8")
app = Path("app.py").read_text(encoding="utf-8")
m1 = re.search(r"ADAPTER_BUILD\s*=\s*\"([^\"]+)\"", adapter)
m2 = re.search(r"APP_VERSION\s*=\s*\"([^\"]+)\"", app)
print("ADAPTER_BUILD=", m1.group(1) if m1 else None)
print("APP_VERSION=", m2.group(1) if m2 else None)
print("adapter_present=", Path("grok_build_adapter.py").exists())
print("engine_dir_present=", Path("grok-build-auth/xconsole_client").exists())
'

echo "== env =="
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit secrets before production use."
  else
    echo "ERROR: missing .env and .env.example" >&2
    exit 1
  fi
else
  echo "using existing .env"
fi

NO_CACHE="${NO_CACHE:-0}"
echo "== admin assets =="
python3 scripts/build_admin_assets.py || true

echo "== build (old container still serving) =="
if [[ "$NO_CACHE" == "1" ]]; then
  DOCKER_BUILDKIT=1 docker compose build --no-cache --pull "$APP_SERVICE"
else
  DOCKER_BUILDKIT=1 docker compose build "$APP_SERVICE"
fi

echo "== ensure deps up =="
docker compose up -d redis postgres


echo "== recreate app only =="
# --no-deps keeps redis/postgres untouched; -d detaches; --force-recreate swaps container
docker compose up -d --no-deps --force-recreate "$APP_SERVICE"

echo "== wait health =="
ok=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/tmp/g2a-health.json 2>/dev/null; then
    ver=$(python3 -c 'import json;print(json.load(open("/tmp/g2a-health.json")).get("version",""))' 2>/dev/null || true)
    echo "health ok (try $i) version=${ver}"
    ok=1
    break
  fi
  sleep 1
done
if [[ "$ok" != "1" ]]; then
  echo "ERROR: health not ready after recreate" >&2
  docker compose logs --tail=80 "$APP_SERVICE" || true
  exit 1
fi

echo "== logs =="
docker compose logs --tail=40 "$APP_SERVICE" || true
echo
echo "Done. host health=${HEALTH_URL}"
echo "Tip: private cross-stack glue → ./docker-rebuild.local.sh (not in git)"
