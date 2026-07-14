#!/usr/bin/env bash
# Main container entrypoint:
# 1) optionally start in-process Turnstile Solver on 127.0.0.1:5072
# 2) start grokcli-2api (app.py)
set -euo pipefail
cd /app

APP_CMD=("python" "app.py")
if [[ "$#" -gt 0 ]]; then
  APP_CMD=("$@")
fi

provider="$(echo "${GROK2API_CAPTCHA_PROVIDER:-${CAPTCHA_PROVIDER:-local}}" | tr '[:upper:]' '[:lower:]')"
enable_solver="${GROK2API_INLINE_SOLVER:-1}"
solver_port="${TURNSTILE_PORT:-5072}"
# Keep captcha browser pool size aligned with registration concurrency.
reg_concurrency="${GROK2API_REG_CONCURRENCY:-3}"
solver_thread="${TURNSTILE_THREAD:-${reg_concurrency}}"
solver_browser="${TURNSTILE_BROWSER_TYPE:-camoufox}"
solver_host="${TURNSTILE_HOST:-127.0.0.1}"
solver_pid=""

start_inline_solver() {
  if [[ ! -f /app/turnstile-solver/api_solver.py ]]; then
    echo "[entrypoint] turnstile-solver missing; skip inline solver"
    return 0
  fi
  mkdir -p /app/turnstile-solver/logs /app/turnstile-solver/keys
  # Lazy browsers (default): pool warms on first captcha, reclaims after idle.
  # TURNSTILE_LAZY=0 restores eager warm-up. TURNSTILE_IDLE_SEC=0 disables reclaim.
  export TURNSTILE_LAZY="${TURNSTILE_LAZY:-1}"
  export TURNSTILE_IDLE_SEC="${TURNSTILE_IDLE_SEC:-180}"
  echo "[entrypoint] starting inline turnstile-solver on ${solver_host}:${solver_port} (thread=${solver_thread}, browser=${solver_browser}, lazy=${TURNSTILE_LAZY}, idle=${TURNSTILE_IDLE_SEC}s)"
  (
    cd /app/turnstile-solver
    exec python api_solver.py \
      --browser_type "${solver_browser}" \
      --thread "${solver_thread}" \
      --host "${solver_host}" \
      --port "${solver_port}" \
      --debug
  ) > /app/turnstile-solver/logs/turnstile_solver.log 2>&1 &
  solver_pid=$!
  echo "${solver_pid}" > /app/turnstile-solver/logs/turnstile_solver.pid
  echo "[entrypoint] inline solver pid=${solver_pid}"

  # Wait until solver HTTP is ready (best-effort, but fail loud if still down)
  for i in $(seq 1 90); do
    if curl -fsS -m 1 "http://127.0.0.1:${solver_port}/health" >/dev/null 2>&1 \
      || curl -fsS -m 1 "http://127.0.0.1:${solver_port}/" >/dev/null 2>&1; then
      echo "[entrypoint] inline solver ready"
      return 0
    fi
    if ! kill -0 "${solver_pid}" 2>/dev/null; then
      echo "[entrypoint] WARN: inline solver exited early; see turnstile-solver/logs/turnstile_solver.log" >&2
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] WARN: inline solver not ready after 90s; registration will wait/block until it is" >&2
}

cleanup() {
  if [[ -n "${solver_pid}" ]] && kill -0 "${solver_pid}" 2>/dev/null; then
    echo "[entrypoint] stopping inline solver pid=${solver_pid}"
    kill "${solver_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${solver_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Force local solver URL to loopback when using inline mode.
if [[ "${provider}" == "local" && "${enable_solver}" != "0" ]]; then
  export GROK2API_CAPTCHA_PROVIDER=local
  export CAPTCHA_PROVIDER=local
  export GROK2API_LOCAL_SOLVER_URL="http://127.0.0.1:${solver_port}"
  export LOCAL_SOLVER_URL="http://127.0.0.1:${solver_port}"
  start_inline_solver
fi

echo "[entrypoint] starting app: ${APP_CMD[*]}"
exec "${APP_CMD[@]}"
