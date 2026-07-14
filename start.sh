#!/usr/bin/env bash
# Start grokcli-2api in high-concurrency mode (multi-worker + Redis + PostgreSQL)
set -euo pipefail
cd "$(dirname "$0")"

# Ensure local env from template (never commit real .env)
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit secrets (admin password, mail keys) as needed."
  else
    echo "WARN: .env.example missing; continuing with process environment only." >&2
  fi
fi
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

if ! $PY -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
  echo "Installing core dependencies..."
  $PY -m pip install -r requirements.txt
fi

if ! $PY -c "import curl_cffi, requests" 2>/dev/null; then
  echo "Installing remaining core dependencies..."
  $PY -m pip install -r requirements.txt
fi

# High-concurrency store deps (redis + psycopg + cryptography) are required.
if ! $PY -c "import redis, psycopg, cryptography" 2>/dev/null; then
  echo "Installing high-concurrency store deps..."
  $PY -m pip install -r requirements-store.txt
fi

# Vendored grok-build-auth package path
export PYTHONPATH="$(pwd)/grok-build-auth${PYTHONPATH:+:$PYTHONPATH}"

export GROK2API_OPEN_BROWSER="${GROK2API_OPEN_BROWSER:-0}"
export GROK2API_HOST="${GROK2API_HOST:-0.0.0.0}"
export GROK2API_PORT="${GROK2API_PORT:-3000}"
export GROK2API_ACCOUNT_MODE="${GROK2API_ACCOUNT_MODE:-round_robin}"
export GROK2API_TOKEN_MAINTAIN="${GROK2API_TOKEN_MAINTAIN:-1}"
export GROK2API_REASONING_COMPAT="${GROK2API_REASONING_COMPAT:-off}"
# Default multi-worker; auto-scale if unset is handled in config.py
export GROK2API_WORKERS="${GROK2API_WORKERS:-}"
# Production start: hot-reload OFF unless explicitly set (use ./dev.sh for reload).
export GROK2API_RELOAD="${GROK2API_RELOAD:-0}"
export GROK2API_STORE_BACKEND="${GROK2API_STORE_BACKEND:-hybrid}"
export REDIS_URL="${REDIS_URL:-${GROK2API_REDIS_URL:-redis://127.0.0.1:6379/0}}"
export DATABASE_URL="${DATABASE_URL:-${GROK2API_DATABASE_URL:-postgresql://grok2api:grok2api@127.0.0.1:5432/grok2api}}"
export GROK2API_REDIS_URL="${GROK2API_REDIS_URL:-$REDIS_URL}"
export GROK2API_DATABASE_URL="${GROK2API_DATABASE_URL:-$DATABASE_URL}"

# Inline captcha (only meaningful when running with entrypoint / docker image)
export GROK2API_CAPTCHA_PROVIDER="${GROK2API_CAPTCHA_PROVIDER:-local}"
export GROK2API_INLINE_SOLVER="${GROK2API_INLINE_SOLVER:-1}"
export GROK2API_REG_CONCURRENCY="${GROK2API_REG_CONCURRENCY:-3}"
export TURNSTILE_THREAD="${TURNSTILE_THREAD:-${GROK2API_REG_CONCURRENCY:-3}}"
export TURNSTILE_BROWSER_TYPE="${TURNSTILE_BROWSER_TYPE:-camoufox}"
export TURNSTILE_PORT="${TURNSTILE_PORT:-5072}"

PORT="$GROK2API_PORT"
echo "Starting grokcli-2api (high-concurrency)..."
echo "  Admin:     http://127.0.0.1:${PORT}/admin"
echo "  Health:    http://127.0.0.1:${PORT}/health"
echo "  Metrics:   http://127.0.0.1:${PORT}/metrics"
echo "  OpenAI:    http://127.0.0.1:${PORT}/v1"
echo "  Workers:   \${GROK2API_WORKERS:-auto(cpu)}"
echo "  Redis:     ${REDIS_URL}"
echo "  Postgres:  ${DATABASE_URL%%@*}@…"
echo "  Mode:      hybrid multi-worker (file backend disabled)"
echo "  Captcha:   provider=${GROK2API_CAPTCHA_PROVIDER} inline=${GROK2API_INLINE_SOLVER} threads=${TURNSTILE_THREAD}"
echo ""

exec $PY app.py
