"""Multi-account pool: rotation, enable/disable, cooldown, failover stats.

All accounts are equal — there is no primary/preferred account.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from typing import Any

from auth import (
    AuthError,
    GrokCredentials,
    get_cached_live_credentials,
    list_live_credentials,
    load_credentials_by_id,
    peek_credentials_by_id,
)
from settings_store import (
    get_account_mode,
    get_account_pool_meta,
    get_account_pool_meta_many,
    get_account_pool_state,
    get_cached_account_pool_state,
    patch_account_pool_meta,
    save_account_pool_state,
    touch_account_stats,
)

# Free-usage exhausted payload (manual/auto probe + live traffic).
# Example:
# {"code":"subscription:free-usage-exhausted","error":"You've used all the included
# free usage for model grok-4.5-build-free for now. Usage resets over a rolling
# 24-hour window — tokens (actual/limit): 2368681/2000000. ..."}
_FREE_USAGE_CODE_RE = re.compile(
    r"subscription:free-usage-exhausted|free-usage-exhausted",
    re.IGNORECASE,
)
_FREE_USAGE_TEXT_RE = re.compile(
    r"("
    r"used\s+all\s+the\s+included\s+free\s+usage|"
    r"free\s+usage\s+for\s+model|"
    r"usage\s+resets\s+over\s+a\s+rolling|"
    r"tokens\s*\(\s*actual\s*/\s*limit\s*\)"
    r")",
    re.IGNORECASE,
)
_TOKENS_RE = re.compile(
    r"tokens\s*\(\s*actual\s*/\s*limit\s*\)\s*:\s*(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
_MODEL_IN_ERROR_RE = re.compile(
    r"free\s+usage\s+for\s+model\s+([a-zA-Z0-9._\-]+)",
    re.IGNORECASE,
)

# Modes (all accounts treated equally):
#   round_robin  — cycle all enabled live accounts
#   random       — pick randomly among enabled live accounts
#   least_used   — prefer account with fewest requests
VALID_MODES = ("round_robin", "random", "least_used")

# Default cooldown after 401 / 429 / 5xx (seconds).
# Overridable via settings store / env (admin 系统设置).
DEFAULT_COOLDOWN = 20
AUTH_COOLDOWN = 90  # shorter: recover faster after transient 401/refresh races
RATE_LIMIT_COOLDOWN = 45  # 429 baseline before Retry-After / streak boost
SERVER_ERROR_COOLDOWN = 20
SOFT_MODEL_BLOCK_TTL = 180.0  # free-usage soft block default
# Durable model unavailability: keep account out of this model until probe recovers.
DURABLE_MODEL_BLOCK_TTL = 3600.0
# Keep short for TTFT: long chains mainly help after first failure, but inflate
# pick work / sticky reordering on every request for large pools.
MAX_FAILOVER_ATTEMPTS = 4
COOLDOWN_MAX = 600.0  # hard ceiling for any adaptive cooldown
COOLDOWN_JITTER_RATIO = 0.15  # +/-15% jitter to desync herd recovery
# Probe / request fail streak → temporary kick, then hard disable from pool.
PROBE_FAIL_KICK_STREAK = 2
PROBE_FAIL_DISABLE_STREAK = 4
PROBE_KICK_COOLDOWN_SEC = 600.0

_lock = threading.RLock()
_rr_index = 0


def _now() -> float:
    return time.time()


def parse_free_usage_error(error: str | None, status_code: int | None = None) -> dict[str, Any] | None:
    """Parse free-usage exhausted payload into durable cooldown status fields.

    Returns None when the error is not free-usage-exhausted. On match returns
    a dict suitable for account_pool DB columns / status_stack entry:
      code, reason, model, tokens_actual, tokens_limit, detail
    """
    text = (error or "").strip()
    if not text:
        return None
    code = None
    detail: dict[str, Any] | None = None
    # Prefer structured JSON body.
    if text.startswith("{") or '"code"' in text:
        try:
            detail = json.loads(text)
        except Exception:
            # Sometimes body is nested / truncated — try extract code via regex.
            detail = None
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("error_code") or "") or None
        err_msg = str(detail.get("error") or detail.get("message") or text)
    else:
        err_msg = text
        mcode = re.search(
            r'"code"\s*:\s*"(subscription:free-usage-exhausted|free-usage-exhausted)"',
            text,
            re.I,
        )
        if mcode:
            code = mcode.group(1)
    is_free = bool(
        (code and _FREE_USAGE_CODE_RE.search(code))
        or _FREE_USAGE_CODE_RE.search(err_msg)
        or _FREE_USAGE_TEXT_RE.search(err_msg)
    )
    if not is_free:
        # Bare 429 without free-usage body is temporary rate-limit, not this stack.
        return None
    model = None
    if isinstance(detail, dict):
        model = detail.get("model") or detail.get("model_id")
    if not model:
        mm = _MODEL_IN_ERROR_RE.search(err_msg)
        if mm:
            model = mm.group(1)
    tokens_actual = tokens_limit = None
    tm = _TOKENS_RE.search(err_msg)
    if tm:
        try:
            tokens_actual = int(tm.group(1))
            tokens_limit = int(tm.group(2))
        except (TypeError, ValueError):
            tokens_actual = tokens_limit = None
    return {
        "code": code or "subscription:free-usage-exhausted",
        "reason": (
            f"临时额度耗尽，已冷却，等待下次测活成功"
            + (f" · {model}" if model else "")
            + (
                f" · tokens {tokens_actual}/{tokens_limit}"
                if tokens_actual is not None and tokens_limit is not None
                else ""
            )
        )[:300],
        "model": model,
        "tokens_actual": tokens_actual,
        "tokens_limit": tokens_limit,
        "detail": detail if isinstance(detail, dict) else {"error": err_msg[:500]},
        "status_code": status_code,
        "raw_error": err_msg[:500],
        "at": _now(),
    }


def stack_status_entry(
    meta: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    max_entries: int = 50,
) -> list[dict[str, Any]]:
    """Append a status event to the account-bound status_stack (DB).

    Stacks *status records*, not seconds/counts alone. Each probe/live free-usage
    event becomes one entry. Successful probe clears the stack.
    """
    stack: list[dict[str, Any]] = []
    if isinstance(meta, dict):
        raw = meta.get("status_stack")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    stack.append(dict(item))
    stack.append(dict(entry))
    if len(stack) > max_entries:
        stack = stack[-max_entries:]
    return stack


def apply_free_usage_cooldown(
    account_id: str,
    *,
    error: str = "",
    status_code: int | None = None,
    model: str | None = None,
    source: str = "probe",
) -> dict[str, Any] | None:
    """Stack free-usage cooldown status onto this account in PostgreSQL.

    - Decision reference is the free-usage-exhausted payload.
    - Cooldown calculation uses existing DB status (status_stack / cooldown_count).
    - Each batch/probe failure stacks another status entry (not time).
    - Single successful probe clears stack → normal.
    """
    if not account_id:
        return None
    parsed = parse_free_usage_error(error, status_code)
    if not parsed:
        return None
    if model and not parsed.get("model"):
        parsed["model"] = model
    state = get_account_pool_state()
    meta = _pool_meta(account_id, state)
    # Stack status entry from DB baseline (do not refresh/recompute away).
    entry = {
        "kind": "free_usage_exhausted",
        "code": parsed.get("code"),
        "model": parsed.get("model") or model,
        "tokens_actual": parsed.get("tokens_actual"),
        "tokens_limit": parsed.get("tokens_limit"),
        "source": source,
        "status_code": status_code,
        "at": _now(),
        "reason": parsed.get("reason"),
    }
    stack = stack_status_entry(meta, entry)
    new_count = len(stack)
    # Marker until only for legacy readers; recovery is status_stack clear.
    until = _now() + max(3600.0, float(PROBE_KICK_COOLDOWN_SEC)) * max(1, new_count)
    reason = str(parsed.get("reason") or "临时额度耗尽，已冷却，等待下次测活成功")[:300]
    patch: dict[str, Any] = {
        "pool_status": "cooldown",
        "cooldown_count": new_count,
        "status_stack": stack,
        "cooldown_reason": reason,
        "cooldown_code": parsed.get("code"),
        "cooldown_model": parsed.get("model") or model,
        "cooldown_tokens_actual": parsed.get("tokens_actual"),
        "cooldown_tokens_limit": parsed.get("tokens_limit"),
        "cooldown_detail": parsed.get("detail"),
        "cooldown_until": until,
        "cooldown_sec": float(new_count),
        "last_error": reason,
        "last_status_code": status_code,
        "enabled": True,
        "disabled_reason": None,
        "disabled_source": None,
        "last_probe_status": "cooldown",
        "last_probe_fail_at": _now(),
    }
    # Soft-block the exhausted model on this account.
    mid = parsed.get("model") or model
    if mid:
        try:
            block_model(
                account_id,
                str(mid),
                reason=reason,
                source="temp_usage",
                ttl_sec=float(PROBE_KICK_COOLDOWN_SEC),
            )
        except Exception:
            pass
    saved = patch_account_pool_meta(account_id, patch)
    try:
        from store.pool_redis import set_cooldown

        set_cooldown(account_id, until)
    except Exception:
        pass
    invalidate_pool_summary_cache()
    print(
        f"  [pool] free-usage status stack ×{new_count} "
        f"account={account_id} model={mid} code={parsed.get('code')}"
    )
    return {
        "id": account_id,
        "action": "cooldown",
        "pool_status": "cooldown",
        "cooldown_count": new_count,
        "status_stack_len": new_count,
        "cooldown_code": parsed.get("code"),
        "cooldown_model": mid,
        "cooldown_reason": reason,
        "cooldown_until": until,
        "enabled": True,
        "meta": saved,
    }


# Short process-local cache for pool policy knobs. First _get_setting_value after
# a worker starts can cost hundreds of ms (settings hydrate); request TTFT must
# not pay that on every sticky pick.
_policy_cache: dict[str, tuple[float, float]] = {}
_POLICY_CACHE_TTL = 5.0


def _policy_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    """Read a numeric pool policy from settings store, else env, else default."""
    now = time.time()
    hit = _policy_cache.get(name)
    if hit is not None and now - hit[0] < _POLICY_CACHE_TTL:
        return max(minimum, min(maximum, float(hit[1])))
    val: float | None = None
    try:
        from settings_store import _get_setting_value

        raw = _get_setting_value(name, None)
        if raw is not None and str(raw).strip() != "":
            val = float(raw)
    except Exception:
        val = None
    if val is None:
        import os

        env_key = "GROK2API_" + name.upper()
        raw = os.getenv(env_key)
        if raw is not None and str(raw).strip() != "":
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = None
    if val is None:
        val = float(default)
    clamped = max(minimum, min(maximum, float(val)))
    _policy_cache[name] = (now, clamped)
    return clamped


def _policy_int(name: str, default: int, *, minimum: int = 0, maximum: int = 10_000) -> int:
    return int(_policy_float(name, float(default), minimum=float(minimum), maximum=float(maximum)))


def cooldown_defaults() -> dict[str, float]:
    return {
        "default": _policy_float("cooldown_default_sec", DEFAULT_COOLDOWN, minimum=1, maximum=COOLDOWN_MAX),
        "auth": _policy_float("cooldown_auth_sec", AUTH_COOLDOWN, minimum=5, maximum=COOLDOWN_MAX),
        "rate_limit": _policy_float(
            "cooldown_rate_limit_sec", RATE_LIMIT_COOLDOWN, minimum=5, maximum=COOLDOWN_MAX
        ),
        "server_error": _policy_float(
            "cooldown_server_error_sec", SERVER_ERROR_COOLDOWN, minimum=1, maximum=COOLDOWN_MAX
        ),
        "soft_block_ttl": _policy_float(
            "soft_model_block_ttl_sec", SOFT_MODEL_BLOCK_TTL, minimum=30, maximum=3600
        ),
        "durable_block_ttl": _policy_float(
            "durable_model_block_ttl_sec", DURABLE_MODEL_BLOCK_TTL, minimum=60, maximum=86400
        ),
        "max": _policy_float("cooldown_max_sec", COOLDOWN_MAX, minimum=30, maximum=3600),
        "probe_fail_kick_streak": _policy_float(
            "probe_fail_kick_streak", PROBE_FAIL_KICK_STREAK, minimum=1, maximum=20
        ),
        "probe_fail_disable_streak": _policy_float(
            "probe_fail_disable_streak", PROBE_FAIL_DISABLE_STREAK, minimum=2, maximum=50
        ),
        "probe_kick_cooldown_sec": _policy_float(
            "probe_kick_cooldown_sec", PROBE_KICK_COOLDOWN_SEC, minimum=30, maximum=7200
        ),
    }


def max_failover_attempts() -> int:
    return _policy_int("max_failover_attempts", MAX_FAILOVER_ATTEMPTS, minimum=1, maximum=64)


def _jitter(seconds: float) -> float:
    """Small symmetric jitter so many accounts don't all recover in the same second."""
    try:
        ratio = float(COOLDOWN_JITTER_RATIO)
    except Exception:
        ratio = 0.15
    if seconds <= 0 or ratio <= 0:
        return max(0.0, float(seconds))
    span = float(seconds) * ratio
    return max(0.0, float(seconds) + random.uniform(-span, span))


def _parse_retry_after(error: str = "", headers: dict[str, Any] | None = None) -> float | None:
    """Best-effort Retry-After seconds from headers or error body text."""
    if headers:
        raw = None
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                raw = v
                break
        if raw is not None:
            try:
                return max(1.0, float(raw))
            except (TypeError, ValueError):
                pass
            # HTTP-date form is rare for this upstream; ignore.
    text = (error or "").lower()
    # "retry after 30s" / "retry in 1 minute"
    import re

    m = re.search(r"retry[- ]?(?:after|in)\s*(\d+(?:\.\d+)?)\s*(s|sec|seconds|m|min|minutes)?", text)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "s").lower()
        if unit.startswith("m"):
            val *= 60.0
        return max(1.0, val)
    m = re.search(r'"retry_after"\s*:\s*(\d+(?:\.\d+)?)', text)
    if m:
        return max(1.0, float(m.group(1)))
    return None


def compute_cooldown_seconds(
    *,
    status_code: int | None = None,
    error: str = "",
    consecutive_fails: int = 0,
    headers: dict[str, Any] | None = None,
    model_soft_blocked: bool = False,
) -> float:
    """Adaptive cooldown: status baseline × fail streak, capped, with jitter.

    When a temporary model soft-block is also applied (free-usage 429), keep the
    account-level cooldown short so *other models* on the same account can still
    be scheduled; the model block already covers this model.
    """
    pol = cooldown_defaults()
    retry_after = _parse_retry_after(error, headers)

    if status_code == 401:
        base = pol["auth"]
    elif status_code == 429:
        base = pol["rate_limit"]
        if model_soft_blocked:
            # Model is already soft-blocked; don't also sideline the whole account long.
            base = min(base, max(10.0, pol["default"] * 0.75))
    elif status_code in (502, 503, 504, 500):
        base = pol["server_error"]
    elif status_code in (403, 404):
        base = max(8.0, pol["default"] * 0.5)
    else:
        base = max(5.0, pol["default"] * 0.35)

    if retry_after is not None:
        # Honor upstream hint but keep within policy ceiling.
        base = max(base, min(retry_after, pol["max"]))

    # Exponential-ish growth on consecutive failures: 1, 1.5, 2.25, ... capped ×4
    streak = max(0, int(consecutive_fails or 0))
    mult = 1.0
    if streak >= 2:
        mult = min(4.0, 1.5 ** min(streak - 1, 6))
    seconds = min(pol["max"], base * mult)
    return _jitter(seconds)


def prune_expired_model_blocks(account_id: str | None = None) -> int:
    """Drop soft model blocks whose `until` has passed. Returns removed count."""
    state = get_account_pool_state()
    now = _now()
    removed = 0
    targets = [account_id] if account_id else list(state.keys())
    for aid in targets:
        if not aid:
            continue
        meta = state.get(aid) or {}
        if not isinstance(meta, dict):
            continue
        blocked = meta.get("blocked_models")
        if not isinstance(blocked, dict) or not blocked:
            continue
        new_blocked = dict(blocked)
        changed = False
        for model, entry in list(new_blocked.items()):
            if not isinstance(entry, dict):
                continue
            until = entry.get("until")
            if until is None:
                continue
            try:
                if now >= float(until):
                    new_blocked.pop(model, None)
                    changed = True
                    removed += 1
            except (TypeError, ValueError):
                continue
        if changed:
            patch_account_pool_meta(
                aid,
                {"blocked_models": new_blocked if new_blocked else None},
            )
    return removed


def _pool_meta(
    account_id: str,
    state: dict[str, Any],
    *,
    redis_overlay: bool = True,
) -> dict[str, Any]:
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    blocked = meta.get("blocked_models") or {}
    if not isinstance(blocked, dict):
        blocked = {}
    # Lazy-expire soft model blocks so UI/counts don't treat them as permanent.
    if isinstance(blocked, dict) and blocked:
        now = _now()
        cleaned: dict[str, Any] = {}
        for mid, entry in blocked.items():
            if isinstance(entry, dict) and entry.get("until") is not None:
                try:
                    if now >= float(entry["until"]):
                        continue
                except (TypeError, ValueError):
                    pass
            cleaned[mid] = entry
        blocked = cleaned
    out = {
        "enabled": bool(meta.get("enabled", True)),
        "weight": max(1, int(meta.get("weight") or 1)),
        "request_count": int(meta.get("request_count") or 0),
        "success_count": int(meta.get("success_count") or 0),
        "fail_count": int(meta.get("fail_count") or 0),
        "consecutive_fails": int(meta.get("consecutive_fails") or 0),
        "last_used_at": meta.get("last_used_at"),
        "last_error": meta.get("last_error"),
        "last_status_code": meta.get("last_status_code"),
        "cooldown_until": meta.get("cooldown_until"),
        "cooldown_sec": meta.get("cooldown_sec"),
        # Stacked cooldown count bound to this account (not wall-clock).
        "cooldown_count": int(meta.get("cooldown_count") or 0),
        "cooldown_reason": meta.get("cooldown_reason"),
        "cooldown_code": meta.get("cooldown_code"),
        "cooldown_model": meta.get("cooldown_model"),
        "cooldown_tokens_actual": meta.get("cooldown_tokens_actual"),
        "cooldown_tokens_limit": meta.get("cooldown_tokens_limit"),
        "status_stack": list(meta.get("status_stack") or [])
        if isinstance(meta.get("status_stack"), list)
        else [],
        "disabled_for_quota": bool(meta.get("disabled_for_quota")),
        "disabled_reason": meta.get("disabled_reason"),
        "disabled_source": meta.get("disabled_source"),
        "quota_disabled_at": meta.get("quota_disabled_at"),
        "quota_source": meta.get("quota_source"),
        "last_quota": meta.get("last_quota"),
        "last_probe": meta.get("last_probe"),
        "last_probe_status": meta.get("last_probe_status"),
        "blocked_models": blocked,
        "blocked_model_ids": list(blocked.keys()),
        # Probe escalation (stored in account_pool.extra JSONB)
        "probe_fail_streak": int(meta.get("probe_fail_streak") or 0),
        "last_probe_ok_at": meta.get("last_probe_ok_at"),
        "last_probe_fail_at": meta.get("last_probe_fail_at"),
        # Durable derived status from DB (normal / cooldown / disabled / …)
        "pool_status": meta.get("pool_status"),
    }
    # Overlay Redis hot counters / cooldowns when multi-worker store is on.
    # Durable account-bound status remains authoritative.
    # IMPORTANT: request-path account picking must pass redis_overlay=False.
    # merge_pool_meta does per-account Redis HGETALL/GET and costs multi-seconds
    # on 1k+ pools (dominant TTFT pick latency).
    if account_id and redis_overlay:
        try:
            from store.pool_redis import merge_pool_meta

            out = merge_pool_meta(account_id, out)
            blocked = out.get("blocked_models") or blocked
            if isinstance(blocked, dict):
                out["blocked_model_ids"] = list(blocked.keys())
        except Exception:
            pass
    try:
        out["cooldown_count"] = int(out.get("cooldown_count") or meta.get("cooldown_count") or 0)
    except (TypeError, ValueError):
        out["cooldown_count"] = 0
    # Count-based cooling OR legacy until-based cooling.
    cooling = is_in_cooldown(out)
    out["in_cooldown"] = cooling
    if not out.get("pool_status"):
        if out.get("disabled_for_quota"):
            out["pool_status"] = "quota_disabled"
        elif out.get("enabled") is False:
            out["pool_status"] = "disabled"
        elif cooling:
            out["pool_status"] = "cooldown"
        elif out.get("blocked_model_ids"):
            out["pool_status"] = "model_blocked"
        else:
            out["pool_status"] = "normal"
    elif cooling and out.get("pool_status") == "normal":
        # Durable count says cooling — don't trust stale normal label.
        out["pool_status"] = "cooldown"
    return out


def is_model_blocked(
    account_id: str,
    model: str | None,
    state: dict[str, Any] | None = None,
    *,
    durable_only: bool = False,
    meta: dict[str, Any] | None = None,
) -> bool:
    """True if this account must not be scheduled for `model`.

    Soft blocks (temp free-usage) expire via `until` / `ttl_sec` and are treated
    as unblocked once past that timestamp so agent loops resume automatically.

    durable_only=True: only permanent blocks count (no `until` / non-temp source).
    Used by soft-recovery so temporary free-usage never empties the whole pool.
    """
    if not account_id or not model:
        return False
    if meta is None:
        if state is None:
            state = get_account_pool_state()
        # Scheduling decisions use durable PG/file meta only (no Redis fan-out).
        meta = _pool_meta(account_id, state, redis_overlay=False)
    blocked = meta.get("blocked_models") or {}
    if not isinstance(blocked, dict) or model not in blocked:
        return False
    entry = blocked.get(model)
    if not isinstance(entry, dict):
        return True
    until = entry.get("until")
    source = str(entry.get("source") or "")
    if durable_only:
        # Temporary free-usage / soft TTL blocks are ignored in last-resort recovery.
        if until is not None or source in ("temp_usage", "soft", "temporary"):
            return False
        return True
    if until is None:
        return True
    try:
        return _now() < float(until)
    except (TypeError, ValueError):
        return True


def is_in_cooldown(meta: dict[str, Any]) -> bool:
    """True while this account has durable stacked cooldown status in DB.

    Source of truth (no refresh/recompute from Redis):
      1) status_stack length > 0
      2) cooldown_count > 0
      3) pool_status == cooldown
      4) legacy cooldown_until still in future
    Only successful 测活 / manual clear clears these fields.
    """
    if not isinstance(meta, dict):
        return False
    stack = meta.get("status_stack")
    if isinstance(stack, list) and len(stack) > 0:
        return True
    try:
        if int(meta.get("cooldown_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if str(meta.get("pool_status") or "") == "cooldown":
        return True
    until = meta.get("cooldown_until")
    if until is None:
        return False
    try:
        return _now() < float(until)
    except (TypeError, ValueError):
        return False


def stack_cooldown_count(
    meta: dict[str, Any] | None,
    *,
    add: int = 1,
) -> int:
    """Stack cooldown **count** on this account (not wall-clock seconds).

    Bound to the account row in DB via ``cooldown_count`` / ``pool_status``.
    Each failed probe/live free-usage event increments by 1 (or ``add``).
    A single successful probe clears the count and status → normal.
    """
    cur = 0
    if isinstance(meta, dict):
        try:
            cur = int(meta.get("cooldown_count") or 0)
        except (TypeError, ValueError):
            cur = 0
        # Backward-compat: if legacy time-based cooldown is still active but
        # count is missing, treat as at least 1 so status stays cooling.
        if cur <= 0 and is_in_cooldown(meta):
            cur = 1
    return max(0, cur + max(0, int(add or 0)))


def stack_cooldown_until(
    meta: dict[str, Any] | None,
    add_sec: float,
    *,
    now: float | None = None,
) -> tuple[float, float]:
    """Legacy helper kept for call sites that still pass seconds.

    Prefer count-based stacking via ``stack_cooldown_count``. This now only
    stamps a far-future ``cooldown_until`` marker so old UI paths that only
    check until still see "cooling", while the real stack is ``cooldown_count``.
    """
    t0 = float(now if now is not None else _now())
    count = stack_cooldown_count(meta, add=1 if float(add_sec or 0) > 0 else 0)
    # Marker until: long enough that prune_expired won't clear while count>0.
    # Real recovery is count→0 on successful probe, not clock expiry.
    until = t0 + max(3600.0, float(add_sec or 0.0)) * max(1, count)
    return until, float(count)


def prune_expired_cooldowns(account_id: str | None = None) -> int:
    """Clear durable cooldown_until that already elapsed. Returns cleared count."""
    state = get_account_pool_state()
    now = _now()
    cleared = 0
    targets = [account_id] if account_id else list(state.keys())
    for aid in targets:
        if not aid:
            continue
        meta = state.get(aid) or {}
        if not isinstance(meta, dict):
            continue
        until = meta.get("cooldown_until")
        if until is None:
            continue
        try:
            if now < float(until):
                continue
        except (TypeError, ValueError):
            continue
        patch_account_pool_meta(
            aid,
            {
                "cooldown_until": None,
                "cooldown_sec": None,
                "pool_status": "normal",
            },
        )
        try:
            from store.pool_redis import clear_cooldown

            clear_cooldown(aid)
        except Exception:
            pass
        cleared += 1
    if cleared:
        invalidate_pool_summary_cache()
    return cleared


def list_pool_accounts() -> list[dict[str, Any]]:
    """Live credentials merged with pool metadata (for admin UI).

    Read-only status routes must not synchronously refresh OIDC tokens: a
    stalled upstream refresh otherwise blocks this single Uvicorn worker and
    makes every endpoint appear offline.
    """
    state = get_account_pool_state()
    out: list[dict[str, Any]] = []
    for creds in list_live_credentials(include_expired=True, auto_refresh=False):
        meta = _pool_meta(creds.auth_key or "", state)
        out.append(
            {
                "id": creds.auth_key,
                "email": creds.email,
                "user_id": creds.user_id,
                "team_id": creds.team_id,
                "expires_at": creds.expires_at,
                "expired": creds.expired,
                "has_refresh_token": bool(creds.refresh_token),
                "token_hint": _mask(creds.token),
                **meta,
                "in_cooldown": is_in_cooldown(meta),
                "cooldown_remaining_sec": max(
                    0.0,
                    float(meta.get("cooldown_until") or 0) - _now(),
                )
                if meta.get("cooldown_until")
                else 0.0,
            }
        )
    return out


def _mask(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "****"
    return token[:6] + "..." + token[-4:]


def _eligible(
    creds: GrokCredentials,
    state: dict[str, Any],
    *,
    model: str | None = None,
    allow_refreshable_expired: bool = False,
    redis_overlay: bool = False,
) -> bool:
    # Expired access tokens are normally ineligible. When the caller will
    # immediately refresh (acquire path), keep accounts that still have a
    # refresh_token so auto-renew can revive them instead of reporting
    # "all expired".
    if creds.expired and not (allow_refreshable_expired and creds.refresh_token):
        return False
    aid = creds.auth_key or ""
    # Request-path scheduling uses durable meta only (no per-account Redis).
    meta = _pool_meta(aid, state, redis_overlay=redis_overlay)
    if not meta["enabled"]:
        return False
    if is_in_cooldown(meta):
        return False
    if model and is_model_blocked(aid, model, state, meta=meta):
        return False
    return True


def _pick_round_robin(eligible: list[GrokCredentials]) -> GrokCredentials:
    global _rr_index
    if not eligible:
        raise AuthError("No eligible accounts for round-robin")
    # Prefer Redis global cursor so multi-worker RR stays balanced.
    try:
        from store.pool_redis import rr_next

        n = rr_next()
        if n is not None:
            return eligible[int(n) % len(eligible)]
    except Exception:
        pass
    with _lock:
        idx = _rr_index % len(eligible)
        _rr_index = (idx + 1) % len(eligible)
        return eligible[idx]


def _health_penalty(meta: dict[str, Any]) -> float:
    """Higher = less healthy (used as sort key / inverse weight)."""
    pen = float(meta.get("consecutive_fails") or 0) * 3.0
    fails = float(meta.get("fail_count") or 0)
    ok = float(meta.get("success_count") or 0)
    total = max(1.0, fails + ok)
    pen += (fails / total) * 5.0
    if is_in_cooldown(meta):
        try:
            pen += max(0.0, float(meta.get("cooldown_until") or 0) - _now()) / 30.0
        except Exception:
            pen += 2.0
    return pen


def _pick_random(eligible: list[GrokCredentials], state: dict[str, Any]) -> GrokCredentials:
    weights = []
    for c in eligible:
        meta = _pool_meta(c.auth_key or "", state)
        # Down-weight unhealthy accounts instead of pure equal weight.
        w = max(0.05, float(meta["weight"]) / (1.0 + _health_penalty(meta)))
        weights.append(w)
    return random.choices(eligible, weights=weights, k=1)[0]


def _pick_least_used(eligible: list[GrokCredentials], state: dict[str, Any]) -> GrokCredentials:
    def score(c: GrokCredentials) -> tuple[float, int, float]:
        meta = _pool_meta(c.auth_key or "", state)
        # Prefer healthy + least used + least recently used
        return (
            _health_penalty(meta),
            meta["request_count"],
            float(meta["last_used_at"] or 0),
        )

    return min(eligible, key=score)


_last_normalize_at = 0.0
_NORMALIZE_MIN_INTERVAL = 30.0  # avoid re-scanning auth.json every request


def _ensure_multi_account_layout() -> None:
    """Re-key CLI client_id single-slot into per-user keys (throttled)."""
    global _last_normalize_at
    now = time.time()
    if now - _last_normalize_at < _NORMALIZE_MIN_INTERVAL:
        return
    try:
        from oidc_auth import normalize_auth_file_keys

        normalize_auth_file_keys()
        _last_normalize_at = now
    except Exception:
        pass


def acquire(
    exclude: set[str] | None = None,
    *,
    model: str | None = None,
    auto_refresh: bool = True,
) -> GrokCredentials:
    """
    Select next account according to configured mode.
    `exclude` skips already-tried accounts in a failover pass.
    `model` skips accounts that blocked this model as unavailable.
    Auto-refreshes near-expiry tokens via refresh_token when available.
    """
    exclude = exclude or set()
    mode = get_account_mode()
    if mode not in VALID_MODES:
        mode = "round_robin"

    _ensure_multi_account_layout()

    # Never network-refresh the whole pool here. Selection is pure local filtering;
    # only the single picked account is refreshed (if already expired).
    all_live = list_live_credentials(
        include_expired=bool(auto_refresh),
        auto_refresh=False,
    )
    if not all_live:
        raise AuthError(
            "No live accounts in auth store. "
            "Use device-code login, import token/auth.json, "
            "or add more accounts to the pool."
        )

    state = get_account_pool_state()
    # Opportunistic cleanup so soft free-usage blocks don't accumulate forever.
    try:
        if random.random() < 0.02:
            prune_expired_model_blocks()
    except Exception:
        pass
    candidates = [c for c in all_live if (c.auth_key or "") not in exclude]

    eligible = [
        c
        for c in candidates
        if _eligible(
            c,
            state,
            model=model,
            allow_refreshable_expired=bool(auto_refresh),
            redis_overlay=False,
        )
    ]
    # Soft recovery ladder — never hard-fail light API calls while any enabled
    # live account still exists. Agent frontends treat empty-pool as "stop".
    def _usable(c: GrokCredentials) -> bool:
        return (not c.expired) or (bool(auto_refresh) and bool(c.refresh_token))

    def _meta_local(c: GrokCredentials) -> dict[str, Any]:
        return _pool_meta(c.auth_key or "", state, redis_overlay=False)

    if not eligible:
        # 1) ignore cooldown, still honor model soft/hard blocks
        eligible = []
        for c in candidates:
            if not _usable(c):
                continue
            meta = _meta_local(c)
            if not meta.get("enabled", True):
                continue
            if model and is_model_blocked(c.auth_key or "", model, state, meta=meta):
                continue
            eligible.append(c)
    if not eligible:
        # 2) also ignore temporary model soft-blocks (keep durable permanent blocks)
        eligible = []
        for c in candidates:
            if not _usable(c):
                continue
            meta = _meta_local(c)
            if not meta.get("enabled", True):
                continue
            if model and is_model_blocked(
                c.auth_key or "", model, state, durable_only=True, meta=meta
            ):
                continue
            eligible.append(c)
    if not eligible:
        # 3) last resort: any enabled live account (even permanent model block)
        # Prefer trying over returning AuthError — upstream may have recovered.
        eligible = [
            c
            for c in candidates
            if _usable(c) and _meta_local(c).get("enabled", True)
        ]
    if not eligible and candidates:
        # 4) absolute last resort: any live candidate (disabled only if quota-disabled still skipped)
        eligible = [
            c
            for c in candidates
            if _usable(c) and not _meta_local(c).get("disabled_for_quota")
        ]
    if eligible and not any(
        _eligible(c, state, model=model, redis_overlay=False) for c in candidates
    ):
        # We are in soft-recovery mode: prefer soonest-ready / healthiest.
        try:
            def _cd_key(c: GrokCredentials) -> tuple[int, float, float]:
                meta = _meta_local(c)
                blocked = 1 if (
                    model and is_model_blocked(c.auth_key or "", model, state, meta=meta)
                ) else 0
                try:
                    until = float(meta.get("cooldown_until") or 0)
                except Exception:
                    until = 0.0
                return (blocked, until, _health_penalty(meta))
            eligible = sorted(eligible, key=_cd_key)
        except Exception:
            pass
        if mode in ("random", "least_used") and len(eligible) > 3:
            window = eligible[: max(3, min(12, len(eligible) // 4 or 3))]
            if mode == "random":
                return _ensure_fresh_creds(
                    _pick_random(window, state), auto_refresh=auto_refresh
                )
            return _ensure_fresh_creds(
                _pick_least_used(window, state), auto_refresh=auto_refresh
            )
        if eligible:
            return _ensure_fresh_creds(eligible[0], auto_refresh=auto_refresh)
    if not eligible:
        msg = "No eligible accounts (all disabled, expired, excluded"
        if model:
            msg += f", or blocked for model `{model}`"
        msg += "). Enable accounts, clear model blocks, or re-login."
        raise AuthError(msg)

    if mode == "round_robin":
        picked = _pick_round_robin(eligible)
    elif mode == "random":
        picked = _pick_random(eligible, state)
    elif mode == "least_used":
        picked = _pick_least_used(eligible, state)
    else:
        picked = eligible[0]
    return _ensure_fresh_creds(picked, auto_refresh=auto_refresh)


def report_success(account_id: str | None, *, model: str | None = None) -> None:
    """Record a successful live request.

    Policy: free-usage / temp cooldown stays until the next successful **probe**
    (测活). Ordinary chat/API success must NOT clear, rewrite, or re-derive
    cooldown status — only counters / last_used.
    """
    if not account_id:
        return
    meta = _pool_meta(account_id, get_account_pool_state())
    still_cooling = is_in_cooldown(meta)
    # Live traffic never clears cooldown (clear_cooldown always False).
    touch_account_stats(
        account_id,
        success=True,
        clear_cooldown=False,
        consecutive_fails=0,
        # Preserve durable cooldown fields; do not let success path stamp
        # pool_status=normal while still cooling.
        preserve_cooldown=True,
    )
    # While cooling: do not touch status-bearing meta at all (no streak patch,
    # no soft-unblock) so UI/DB cooldown state is not "refreshed" to normal.
    if still_cooling:
        return
    # Not cooling: safe to clear probe fail streak for healthy live traffic.
    try:
        patch_account_pool_meta(account_id, {"probe_fail_streak": 0})
    except Exception:
        pass
    # Soft model blocks may clear on live success only when account is NOT in
    # account-level cooldown (cooldown recovery is probe-only).
    if model:
        try:
            state = get_account_pool_state()
            meta2 = state.get(account_id) or {}
            blocked = meta2.get("blocked_models") if isinstance(meta2, dict) else None
            if isinstance(blocked, dict) and model in blocked:
                entry = blocked.get(model) or {}
                src = str((entry or {}).get("source") or "") if isinstance(entry, dict) else ""
                until = (entry or {}).get("until") if isinstance(entry, dict) else None
                if until is not None or src in ("temp_usage", "soft", "temporary"):
                    unblock_model(account_id, model)
        except Exception:
            pass


def report_failure(
    account_id: str | None,
    *,
    error: str = "",
    status_code: int | None = None,
    cooldown: float | None = None,
    model: str | None = None,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record a live/request failure and put the account into cooldown.

    Any upstream error during rotation (401/429/5xx/network/proxy) stacks a
    durable cooldown status so subsequent acquire()/try_acquire_sequence() skip
    this account. free-usage-exhausted uses the dedicated status stack.
    Returns a small summary for logs/debug (never raises).
    """
    if not account_id:
        return None

    # Read streak before writing so adaptive cooldown can scale.
    state = get_account_pool_state()
    meta = _pool_meta(account_id, state)
    prev_streak = int(meta.get("consecutive_fails") or 0)
    streak = prev_streak + 1

    # Hard quota/credit errors → remove from rotation immediately (before cooldown)
    kicked = False
    try:
        from quota import handle_upstream_error_for_quota

        kicked = bool(
            handle_upstream_error_for_quota(
                account_id, error=error, status_code=status_code
            )
        )
    except Exception:
        kicked = False

    # Model / free-usage errors → soft or hard block for THIS model only.
    model_action = None
    try:
        from model_health import handle_upstream_error_for_model, is_temporary_usage_error

        if model:
            # Align soft-block TTL with admin policy when temporary.
            if is_temporary_usage_error(error, status_code):
                # model_health uses its own default; still call it for soft block.
                model_action = handle_upstream_error_for_model(
                    account_id, model=model, error=error, status_code=status_code
                )
            else:
                model_action = handle_upstream_error_for_model(
                    account_id, model=model, error=error, status_code=status_code
                )
    except Exception:
        model_action = None

    soft_blocked = bool(
        model_action
        and (
            (isinstance(model_action, dict) and model_action.get("model"))
            or True
        )
    )
    # free-usage-exhausted → stack durable status on this account (DB), not time.
    free = apply_free_usage_cooldown(
        account_id,
        error=error,
        status_code=status_code,
        model=model,
        source="live",
    )
    if free:
        # Still bump fail counters without wiping the free-usage status stack.
        until = free.get("cooldown_until")
        try:
            until_f = float(until) if until is not None else (_now() + 600.0)
        except (TypeError, ValueError):
            until_f = _now() + 600.0
        touch_account_stats(
            account_id,
            success=False,
            error=str(free.get("cooldown_reason") or error)[:300],
            consecutive_fails=streak,
            last_status_code=status_code,
            cooldown_until=until_f,
            cooldown_sec=float(free.get("cooldown_count") or 1),
            preserve_cooldown=True,
        )
        # Multi-worker: always mirror cooldown key so other workers skip immediately.
        try:
            from store.pool_redis import set_cooldown

            set_cooldown(account_id, until_f)
        except Exception:
            pass
        print(
            f"  [pool] live fail → cooldown account={account_id[:48]} "
            f"code={free.get('cooldown_code')} count={free.get('cooldown_count')} "
            f"model={model or free.get('cooldown_model') or '-'}",
            flush=True,
        )
        return {
            "action": "cooldown",
            "kind": "free_usage",
            "account_id": account_id,
            "cooldown_code": free.get("cooldown_code"),
            "cooldown_count": free.get("cooldown_count"),
            "cooldown_until": until_f,
            "kicked": kicked,
            "soft_blocked": soft_blocked,
        }

    if cooldown is None:
        if kicked:
            cooldown = 5.0
        else:
            cooldown = compute_cooldown_seconds(
                status_code=status_code,
                error=error,
                consecutive_fails=streak,
                headers=headers,
                model_soft_blocked=bool(soft_blocked),
            )
    # Non free-usage failures: stack a generic status entry bound to account.
    entry = {
        "kind": "request_fail",
        "code": f"http_{status_code}" if status_code else "request_fail",
        "model": model,
        "source": "live",
        "status_code": status_code,
        "at": _now(),
        "reason": (error or "")[:200],
    }
    stack = stack_status_entry(meta, entry)
    new_count = len(stack)
    until = _now() + max(float(cooldown or 60.0), 60.0) * max(1, new_count)
    err_store = (error or "")[:300]
    if err_store.startswith("{") and len(err_store) > 160:
        err_store = err_store[:160] + "…"
    try:
        patch_account_pool_meta(
            account_id,
            {
                "status_stack": stack,
                "cooldown_count": new_count,
                "pool_status": "cooldown",
                "cooldown_until": until,
                "cooldown_sec": float(new_count),
                "cooldown_reason": err_store,
                "cooldown_code": entry["code"],
                "cooldown_model": model,
            },
        )
    except Exception:
        pass
    # Hot mirror so multi-worker rotation skips this account without waiting for PG lag.
    try:
        from store.pool_redis import set_cooldown

        set_cooldown(account_id, until)
    except Exception:
        pass
    touch_account_stats(
        account_id,
        success=False,
        error=err_store,
        cooldown_until=until,
        consecutive_fails=streak,
        last_status_code=status_code,
        cooldown_sec=float(new_count),
    )
    print(
        f"  [pool] live fail → cooldown account={account_id[:48]} "
        f"code={entry['code']} count={new_count} status={status_code} "
        f"model={model or '-'} until={int(until)}",
        flush=True,
    )
    return {
        "action": "cooldown",
        "kind": "request_fail",
        "account_id": account_id,
        "cooldown_code": entry["code"],
        "cooldown_count": new_count,
        "cooldown_until": until,
        "status_code": status_code,
        "kicked": kicked,
        "soft_blocked": soft_blocked,
    }


def set_account_enabled(account_id: str, enabled: bool) -> dict[str, Any] | None:
    state = get_account_pool_state()
    # ensure key exists even if new
    meta = dict(state.get(account_id) or {})
    meta["enabled"] = bool(enabled)
    patch: dict[str, Any] = {"enabled": bool(enabled)}
    if enabled:
        # Manual re-enable clears auto quota-disable + model blocks + cooldown
        for k in (
            "disabled_for_quota",
            "disabled_reason",
            "disabled_source",
            "quota_disabled_at",
            "quota_source",
            "blocked_models",
            "cooldown_until",
            "cooldown_sec",
            "consecutive_fails",
            "last_error",
            "last_status_code",
        ):
            meta.pop(k, None)
            patch[k] = None
        patch["pool_status"] = "normal"
        try:
            from store.pool_redis import clear_cooldown

            clear_cooldown(account_id)
        except Exception:
            pass
    else:
        patch["enabled"] = False
        patch["pool_status"] = "disabled"
    # Immediate durable write (PG commit / file flush).
    patch_account_pool_meta(account_id, patch)
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "enabled": enabled}


def block_model(
    account_id: str,
    model: str,
    *,
    reason: str = "模型不可用",
    source: str = "probe",
    ttl_sec: float | None = None,
) -> dict[str, Any] | None:
    """Stop scheduling this account for a specific model.

    Pass `ttl_sec` for temporary free-usage / soft blocks. Without TTL the
    block is durable until manually cleared or a successful probe unblocks it.
    """
    if not account_id or not model:
        return None
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    blocked = meta.get("blocked_models")
    if not isinstance(blocked, dict):
        blocked = {}
    else:
        blocked = dict(blocked)
    already = model in blocked
    now = _now()
    entry: dict[str, Any] = {
        "reason": (reason or "模型不可用")[:300],
        "blocked_at": now,
        "source": source,
    }
    if ttl_sec is not None and float(ttl_sec) > 0:
        # Stack soft-block TTL with any remaining until for this account+model.
        remaining = 0.0
        prev = blocked.get(model) if isinstance(blocked.get(model), dict) else None
        if prev and prev.get("until") is not None:
            try:
                remaining = max(0.0, float(prev["until"]) - now)
            except (TypeError, ValueError):
                remaining = 0.0
        total_ttl = remaining + float(ttl_sec)
        entry["ttl_sec"] = total_ttl
        entry["until"] = now + total_ttl
        entry["stacked_add_sec"] = float(ttl_sec)
    blocked[model] = entry
    last_error = f"[{model}] {blocked[model]['reason']}"
    patch_account_pool_meta(
        account_id,
        {"blocked_models": blocked, "last_error": last_error},
    )
    if not already:
        ttl_note = f" ttl={int(ttl_sec)}s" if ttl_sec else ""
        print(
            f"  [model] blocked{ttl_note} {model} for account "
            f"{account_id}: {blocked[model]['reason']}"
        )
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {
        "id": account_id,
        "blocked_models": blocked,
        "model": model,
        "reason": blocked[model]["reason"],
    }


def unblock_model(account_id: str, model: str | None = None) -> dict[str, Any] | None:
    """Clear one model block, or all model blocks if model is None."""
    if not account_id:
        return None
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        return None
    blocked = meta.get("blocked_models")
    if not isinstance(blocked, dict):
        blocked = {}
    patch: dict[str, Any] = {}
    if model is None:
        patch["blocked_models"] = None
        blocked = {}
    elif model in blocked:
        blocked = dict(blocked)
        blocked.pop(model, None)
        patch["blocked_models"] = blocked if blocked else None
    else:
        return {"id": account_id, "blocked_models": blocked}
    patch_account_pool_meta(account_id, patch)
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "blocked_models": blocked if model is not None else {}}


def disable_for_quota(
    account_id: str,
    *,
    reason: str = "额度已耗尽",
    source: str = "billing",
) -> dict[str, Any] | None:
    """Disable account permanently from rotation due to quota exhaustion."""
    state = get_account_pool_state()
    meta = state.get(account_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    already = meta.get("enabled") is False and meta.get("disabled_for_quota")
    reason_s = (reason or "额度已耗尽")[:300]
    now = _now()
    patch_account_pool_meta(
        account_id,
        {
            "enabled": False,
            "disabled_for_quota": True,
            "disabled_reason": reason_s,
            "disabled_source": source,
            "quota_disabled_at": now,
            "quota_source": source,
            "last_error": reason_s,
            "last_quota": {
                "ok": True,
                "fetched_at": now,
                "account_id": account_id,
                "exhausted": True,
                "auto_disabled": True,
                "summary": f"额度耗尽 · 已移出轮询（{reason_s}）",
                "display": {"summary": f"额度耗尽 · 已移出轮询（{reason_s}）"},
                "source": source or "billing",
            },
        },
    )
    if not already:
        print(
            f"  [quota] account disabled from pool: "
            f"{account_id} — {reason_s}"
        )
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {
        "id": account_id,
        "enabled": False,
        "disabled_for_quota": True,
        "disabled_reason": reason_s,
    }


def save_quota_snapshot(account_id: str, quota_result: dict[str, Any]) -> None:
    """Persist last quota status on pool meta (DB/settings), no secrets.

    Stores both healthy and exhausted/error summaries so admin UI can render
    cached quota without re-querying upstream every time.
    """
    if not account_id or not isinstance(quota_result, dict):
        return
    display = quota_result.get("display") if isinstance(quota_result.get("display"), dict) else {}
    snap = {
        "ok": bool(quota_result.get("ok", True)),
        "fetched_at": quota_result.get("fetched_at") or _now(),
        "account_id": account_id,
        "email": quota_result.get("email"),
        "user_id": quota_result.get("user_id"),
        "monthly_limit": quota_result.get("monthly_limit"),
        "used": quota_result.get("used"),
        "remaining": quota_result.get("remaining"),
        "usage_percent": quota_result.get("usage_percent"),
        "unlimited_or_free": quota_result.get("unlimited_or_free"),
        "exhausted": bool(quota_result.get("exhausted")),
        "exhaust_reason": quota_result.get("exhaust_reason"),
        "auto_disabled": bool(quota_result.get("auto_disabled")),
        "summary": display.get("summary") or quota_result.get("summary"),
        "billing_period_end": quota_result.get("billing_period_end"),
        "error": quota_result.get("error"),
        "status_code": quota_result.get("status_code"),
        "display": {
            "summary": display.get("summary") or quota_result.get("summary"),
        } if (display or quota_result.get("summary")) else None,
        "source": "cached",
    }
    # drop Nones for compact JSON
    snap = {k: v for k, v in snap.items() if v is not None}
    patch: dict[str, Any] = {"last_quota": snap}
    # keep disable flags coherent when exhausted
    if snap.get("exhausted") or snap.get("auto_disabled"):
        patch.update({
            "disabled_for_quota": True,
            "enabled": False,
            "disabled_reason": (snap.get("exhaust_reason") or snap.get("summary") or "额度已耗尽")[:300],
            "quota_disabled_at": snap.get("fetched_at") or _now(),
            "quota_source": "billing",
        })
    patch_account_pool_meta(account_id, patch)



_pool_summary_light_cache: dict[str, Any] | None = None
_pool_summary_light_at = 0.0
_POOL_SUMMARY_LIGHT_TTL = 2.0  # seconds; status polls are frequent


def invalidate_pool_summary_cache() -> None:
    global _pool_summary_light_cache, _pool_summary_light_at
    _pool_summary_light_cache = None
    _pool_summary_light_at = 0.0


def pool_summary(*, include_accounts: bool = True) -> dict[str, Any]:
    """Summarize pool health.

    `include_accounts=False` keeps the payload small for /health and status
    routes on large multi-account pools (hundreds of entries) and avoids
    building the full admin account dict list.

    Cooldown counts always come from durable DB/meta (not Redis TTL alone).
    Expired cooldowns are pruned lazily; active ones are never zeroed by
    status polling.
    """
    if include_accounts:
        try:
            # Only clear cooldowns whose until timestamp already elapsed.
            if random.random() < 0.25:
                prune_expired_cooldowns()
        except Exception:
            pass
        accounts = list_pool_accounts()
        live = [a for a in accounts if not a.get("expired")]
        enabled = [a for a in live if a.get("enabled")]
        cooling = [a for a in enabled if a.get("in_cooldown")]
        quota_disabled = [a for a in accounts if a.get("disabled_for_quota")]
        model_blocked = [
            a for a in accounts if (a.get("blocked_model_ids") or a.get("blocked_models"))
        ]
        return {
            "mode": get_account_mode(),
            "total": len(accounts),
            "live": len(live),
            "enabled": len(enabled),
            "in_cooldown": len(cooling),
            "quota_disabled": len(quota_disabled),
            "model_blocked": len(model_blocked),
            "accounts": accounts,
            "source": "durable",
        }

    # Lightweight counts-only path for /health and frequent status polls.
    global _pool_summary_light_cache, _pool_summary_light_at
    now = time.time()
    if (
        _pool_summary_light_cache is not None
        and now - _pool_summary_light_at < _POOL_SUMMARY_LIGHT_TTL
    ):
        return dict(_pool_summary_light_cache)
    try:
        # Only purge elapsed durable cooldowns (never active ones).
        if random.random() < 0.15:
            prune_expired_cooldowns()
    except Exception:
        pass
    # Prefer SQL aggregates when PostgreSQL owns the pool (O(1) vs O(n) live scan).
    try:
        from store.settings_pg import enabled as pg_on, pool_counts
        from store.accounts_pg import count_accounts

        if pg_on():
            # Live SQL over accounts ⟕ account_pool (no snapshot).
            # Throttle maintenance so overview polling stays fast.
            maintain = False
            try:
                if now - float(_pool_summary_light_at or 0) > 30:
                    maintain = True
            except Exception:
                maintain = True
            try:
                counts = pool_counts(maintain=maintain)
            except TypeError:
                counts = pool_counts()
            except Exception:
                # Do not invent in_cooldown=0 on SQL errors — fall through to
                # meta scan so a transient DB blip doesn't flash zero.
                raise
            out = {
                "mode": get_account_mode(),
                "total": int(counts.get("total") or count_accounts() or 0),
                "live": int(counts.get("live") or counts.get("total") or 0),
                "enabled": int(counts.get("enabled") or counts.get("live") or 0),
                "in_cooldown": int(counts.get("in_cooldown") or 0),
                "quota_disabled": int(counts.get("quota_disabled") or 0),
                "model_blocked": int(counts.get("model_blocked") or 0),
                "source": "postgres",
            }
            _pool_summary_light_cache = dict(out)
            _pool_summary_light_at = now
            return out
    except Exception:
        pass
    state = get_account_pool_state()
    total = live = enabled = cooling = quota_disabled = model_blocked = 0
    for creds in list_live_credentials(include_expired=True, auto_refresh=False):
        total += 1
        meta = _pool_meta(creds.auth_key or "", state)
        if meta.get("disabled_for_quota"):
            quota_disabled += 1
        if meta.get("blocked_model_ids") or meta.get("blocked_models"):
            model_blocked += 1
        if creds.expired:
            continue
        live += 1
        if not meta["enabled"]:
            continue
        enabled += 1
        if is_in_cooldown(meta):
            cooling += 1
    out = {
        "mode": get_account_mode(),
        "total": total,
        "live": live,
        "enabled": enabled,
        "in_cooldown": cooling,
        "quota_disabled": quota_disabled,
        "model_blocked": model_blocked,
    }
    _pool_summary_light_cache = dict(out)
    _pool_summary_light_at = now
    return out


def _ensure_fresh_creds(
    creds: GrokCredentials,
    *,
    auto_refresh: bool = True,
) -> GrokCredentials:
    """Refresh only the selected account when access token is expired.

    Near-expiry is left to background token_maintainer so request TTFT is not
    blocked by an OIDC round-trip on every call. Hard-expired tokens still
    refresh on demand (otherwise the request cannot succeed).
    """
    if not auto_refresh or not creds or not creds.auth_key:
        return creds
    # Only pay the OIDC cost when the access token is already unusable.
    if not creds.expired:
        return creds
    if not creds.refresh_token:
        return creds
    try:
        return load_credentials_by_id(creds.auth_key)
    except Exception:
        # Keep original; caller/upstream will fail over if still expired.
        return creds


def try_acquire_sequence(
    max_attempts: int | None = None,
    *,
    model: str | None = None,
    prefer_account_id: str | None = None,
) -> list[GrokCredentials]:
    """
    Build an ordered list of accounts to try for one request (failover chain).
    Covers enabled live accounts; skips model-blocked accounts.

    `prefer_account_id`: conversation affinity — put this account first so
    multi-turn chats stay on the same account (memory continuity), unless it is
    cooling / model-blocked (then it stays in the chain but not forced first).

    Sticky multi-turn fast path: when prefer_account is ready/fresh, skip the
    full-pool scan and return a short chain (sticky first). Compatibility is
    unchanged — failover still works via the short backup list built only if
    sticky is unusable.
    """
    _ensure_multi_account_layout()

    # ── Sticky affinity fast path (dominant multi-turn TTFT win) ──────────
    # Load only the preferred account + durable meta. Avoid list_live over
    # 1k accounts and full account_pool SELECT when the conversation is pinned.
    # Intentionally avoid max_failover_attempts()/settings hydrate here — first
    # policy read after a worker starts can cost hundreds of ms.
    if prefer_account_id:
        sticky = peek_credentials_by_id(prefer_account_id)
        if sticky is not None and sticky.auth_key:
            # Single-row meta — never full-pool state on the sticky hot path.
            sm_raw = get_account_pool_meta(sticky.auth_key or "")
            sticky_state = {sticky.auth_key or "": sm_raw}
            sm = _pool_meta(sticky.auth_key or "", sticky_state, redis_overlay=False)
            sticky_blocked = bool(
                model
                and is_model_blocked(
                    sticky.auth_key or "", model, sticky_state, meta=sm
                )
            )
            # CRITICAL for TTFT: never OIDC-refresh on the sticky hot path.
            # Expired sticky accounts fall through to the full picker, which
            # prefers already-fresh tokens and only refreshes the first
            # candidate when necessary. Request-path RT exchange was the
            # main reason live sticky picks still showed 150–300ms.
            sticky_ready = (
                bool(sm.get("enabled", True))
                and not sm.get("disabled_for_quota")
                and not is_in_cooldown(sm)
                and not sticky_blocked
                and int(sm.get("consecutive_fails") or 0) < 2
                and not sticky.expired
                and bool(sticky.token)
            )
            if sticky_ready:
                first = sticky
                # Optional backups only from warm live-creds cache. Never
                # rebuild full pool / full pool-state just for backups.
                backups: list[GrokCredentials] = []
                try:
                    if max_attempts is not None:
                        limit = max(1, int(max_attempts))
                    else:
                        # Compile-time default only — no settings IO on hot path.
                        limit = max(1, int(MAX_FAILOVER_ATTEMPTS))
                    if limit > 1:
                        cached = get_cached_live_credentials(
                            include_expired=True
                        ) or []
                        sticky_id = first.auth_key or ""
                        sticky_uid = first.user_id or ""
                        # Prefer warm full pool-state cache when present;
                        # otherwise treat missing backup meta as enabled.
                        warm_state = get_cached_account_pool_state() or {}
                        for c in cached:
                            if not c or not c.auth_key:
                                continue
                            if c.auth_key == sticky_id:
                                continue
                            if sticky_uid and (
                                c.user_id == sticky_uid
                                or (c.auth_key or "").endswith(
                                    f"::{sticky_uid}"
                                )
                            ):
                                continue
                            # Backups must already be fresh — no OIDC here.
                            if c.expired:
                                continue
                            if warm_state:
                                meta = _pool_meta(
                                    c.auth_key or "",
                                    warm_state,
                                    redis_overlay=False,
                                )
                                if not meta.get("enabled", True):
                                    continue
                                if is_in_cooldown(meta):
                                    continue
                                if model and is_model_blocked(
                                    c.auth_key or "",
                                    model,
                                    warm_state,
                                    meta=meta,
                                ):
                                    continue
                            backups.append(c)
                            if len(backups) >= max(0, limit - 1):
                                break
                except Exception:
                    backups = []
                return [first] + backups

    mode = get_account_mode()
    # Prefer warm process-local pool-state. Full SELECT of 1k+ rows is the main
    # cold-path pick cost (~200–350ms) and must not run on every request.
    state = get_cached_account_pool_state()
    state_is_partial = False
    if state is None:
        state = {}
        state_is_partial = True
    # Prefer non-expired first (no network). Include expired-but-refreshable so
    # the chain can still revive them if every live account is cooling/blocked.
    all_live = list_live_credentials(include_expired=True, auto_refresh=False)

    def _usable(c: GrokCredentials) -> bool:
        return (not c.expired) or bool(c.refresh_token)

    # Prefer already-fresh accounts for TTFT; expired ones stay as fallback.
    # Keep this cheap: avoid multiple full-list passes + repeated meta lookups.
    fresh: list[GrokCredentials] = []
    refreshable: list[GrokCredentials] = []
    for c in all_live:
        if not c.expired:
            fresh.append(c)
        elif c.refresh_token:
            refreshable.append(c)
    pool_order = fresh + refreshable

    # Candidate window — we only need a short failover chain, not a ranked full
    # pool. On cold meta, hydrate just this window via WHERE id = ANY(...).
    limit_target = (
        max(1, int(max_attempts))
        if max_attempts is not None
        else max(1, int(MAX_FAILOVER_ATTEMPTS))
    )
    # Over-fetch a bit so cooldowns/blocks inside the window still leave a chain.
    window_n = min(len(pool_order), max(24, limit_target * 12))
    if prefer_account_id and pool_order:
        # Ensure sticky candidate is considered even if outside the RR window.
        pref = prefer_account_id
        head: list[GrokCredentials] = []
        rest: list[GrokCredentials] = []
        for c in pool_order:
            aid = c.auth_key or ""
            if aid == pref or c.user_id == pref or aid.endswith(f"::{pref}"):
                head.append(c)
            else:
                rest.append(c)
        pool_order = head + rest
    # Round-robin / random only need a rotated window, not whole-pool sort.
    if mode == "random" and len(pool_order) > window_n:
        sample = list(pool_order)
        random.shuffle(sample)
        pool_window = sample[:window_n]
    elif mode != "least_used" and len(pool_order) > window_n:
        start = 0
        try:
            from store.pool_redis import rr_next

            n = rr_next()
            if n is not None and pool_order:
                start = int(n) % len(pool_order)
            else:
                raise RuntimeError("redis rr unavailable")
        except Exception:
            global _rr_index
            with _lock:
                start = _rr_index % max(len(pool_order), 1)
                _rr_index = (start + 1) % max(len(pool_order), 1)
        pool_window = pool_order[start:] + pool_order[:start]
        pool_window = pool_window[:window_n]
        # Keep sticky (if any) at front of window for affinity reordering later.
        if prefer_account_id:
            pref = prefer_account_id
            sticky_w = [
                c
                for c in pool_window
                if (c.auth_key or "") == pref
                or c.user_id == pref
                or (c.auth_key or "").endswith(f"::{pref}")
            ]
            if sticky_w:
                pool_window = sticky_w + [
                    c for c in pool_window if c not in sticky_w
                ]
    else:
        # least_used benefits from a larger sample but still not the full 1k+.
        pool_window = pool_order[: min(len(pool_order), max(window_n, 64))]

    if state_is_partial and pool_window:
        try:
            ids = [c.auth_key for c in pool_window if c.auth_key]
            batch = get_account_pool_meta_many(ids)
            if isinstance(batch, dict) and batch:
                state.update(batch)
        except Exception:
            pass

    # Precompute durable meta once per account for this pick.
    # No Redis overlay here — that was the multi-second TTFT bottleneck.
    meta_by_id: dict[str, dict[str, Any]] = {}

    def _meta(c: GrokCredentials) -> dict[str, Any]:
        aid = c.auth_key or ""
        m = meta_by_id.get(aid)
        if m is None:
            m = _pool_meta(aid, state, redis_overlay=False)
            meta_by_id[aid] = m
        return m

    enabled: list[GrokCredentials] = []
    for c in pool_window:
        if not _usable(c):
            continue
        meta = _meta(c)
        if not meta.get("enabled", True):
            continue
        if model and is_model_blocked(c.auth_key or "", model, state, meta=meta):
            continue
        enabled.append(c)
    if not enabled:
        # ignore temporary model soft-blocks (free-usage) so light calls still work
        for c in pool_window:
            if not _usable(c):
                continue
            meta = _meta(c)
            if not meta.get("enabled", True):
                continue
            if model and is_model_blocked(
                c.auth_key or "", model, state, durable_only=True, meta=meta
            ):
                continue
            enabled.append(c)
    if not enabled:
        # any enabled live account in window
        for c in pool_window:
            if _usable(c) and _meta(c).get("enabled", True):
                enabled.append(c)
    if not enabled:
        # absolute last resort: window minus quota-disabled, else whole window
        for c in pool_window:
            if _usable(c) and not _meta(c).get("disabled_for_quota"):
                enabled.append(c)
        if not enabled:
            enabled = [c for c in pool_window if _usable(c)]
    if not enabled and pool_order:
        # Window was unlucky (all cooling/disabled). One bounded expand only —
        # still avoid full-table state read by batching the next slice.
        extra = [c for c in pool_order if c not in pool_window][: max(32, window_n)]
        if state_is_partial and extra:
            try:
                ids = [c.auth_key for c in extra if c.auth_key]
                batch = get_account_pool_meta_many(ids)
                if isinstance(batch, dict) and batch:
                    state.update(batch)
            except Exception:
                pass
        for c in extra:
            if not _usable(c):
                continue
            meta = _meta(c)
            if not meta.get("enabled", True):
                continue
            if model and is_model_blocked(c.auth_key or "", model, state, meta=meta):
                continue
            enabled.append(c)
            if len(enabled) >= max(8, limit_target * 2):
                break

    # De-dupe by user_id (legacy dual keys)
    seen_users: set[str] = set()
    deduped: list[GrokCredentials] = []
    for c in enabled:
        uid = c.user_id or c.auth_key or ""
        if uid in seen_users:
            continue
        seen_users.add(uid)
        deduped.append(c)
    enabled = deduped

    def cool_key(c: GrokCredentials) -> tuple[int, float, int, float]:
        meta = _meta(c)
        cooling = 1 if is_in_cooldown(meta) else 0
        # sooner-ready cooling accounts rank ahead of long-cooling ones
        until = 0.0
        if cooling:
            try:
                until = float(meta.get("cooldown_until") or 0)
            except Exception:
                until = 0.0
        used = int(meta.get("request_count") or 0)
        last = float(meta.get("last_used_at") or 0)
        health = _health_penalty(meta)
        if mode == "least_used":
            return (cooling, health, used, last)
        return (cooling, until if cooling else health, used if mode == "least_used" else 0, last)

    if mode == "random":
        ordered = list(enabled)
        random.shuffle(ordered)
        ordered.sort(
            key=lambda c: (
                1 if is_in_cooldown(_meta(c)) else 0,
                _health_penalty(_meta(c)),
            )
        )
    elif mode == "least_used":
        ordered = sorted(enabled, key=cool_key)
    else:  # round_robin — window already rotated; just prefer ready accounts
        if not enabled:
            return []
        # O(1) position map — preserve RR order from the window construction.
        pos = {id(c): i for i, c in enumerate(enabled)}
        not_cooling = [c for c in enabled if not is_in_cooldown(_meta(c))]
        cooling = [c for c in enabled if is_in_cooldown(_meta(c))]
        cooling.sort(key=lambda c: float(_meta(c).get("cooldown_until") or 0))
        not_cooling_sorted = sorted(
            not_cooling,
            key=lambda c: (
                int(_health_penalty(_meta(c)) // 3),
                pos.get(id(c), 0),
            ),
        )
        ordered = not_cooling_sorted + cooling

    # Conversation affinity: pin multi-turn chat to same account first only when ready.
    if prefer_account_id and ordered:
        sticky: list[GrokCredentials] = []
        rest: list[GrokCredentials] = []
        pref = prefer_account_id
        for c in ordered:
            aid = c.auth_key or ""
            if aid == pref or c.user_id == pref or aid.endswith(f"::{pref}"):
                sticky.append(c)
            else:
                rest.append(c)
        if sticky:
            sm = _meta(sticky[0])
            sticky_blocked = bool(
                model
                and is_model_blocked(
                    sticky[0].auth_key or "", model, state, meta=sm
                )
            )
            # Prefer ready peers first when sticky is cooling / model-blocked /
            # already failing. Keep sticky in chain for later try (affinity).
            if (
                is_in_cooldown(sm)
                or sticky_blocked
                or int(sm.get("consecutive_fails") or 0) >= 2
                or sticky[0].expired
            ):
                ordered = rest + sticky
            else:
                ordered = sticky + rest

    limit = max_attempts if max_attempts is not None else max_failover_attempts()
    # Soft-block waves: allow a modest longer chain, but keep it TTFT-friendly.
    if max_attempts is None:
        ready = sum(
            1
            for c in ordered
            if not is_in_cooldown(_meta(c))
            and not (
                model
                and is_model_blocked(
                    c.auth_key or "", model, state, meta=_meta(c)
                )
            )
        )
        if ready < 2:
            limit = max(int(limit or 1), min(8, max(4, len(ordered) // 8 or 4)))
    if limit is not None:
        ordered = ordered[: max(1, int(limit))]
    # Only refresh the first candidate if it is already expired. Refreshing the
    # whole chain here serializes OIDC RTs before any upstream byte is sent.
    if ordered:
        first = _ensure_fresh_creds(ordered[0], auto_refresh=True)
        if first.expired and not first.refresh_token:
            # First account unusable and no refresh path — drop it and try next
            # without paying OIDC for the rest of the chain yet.
            rest = list(ordered[1:])
            return [c for c in rest if (not c.expired) or c.refresh_token]
        ordered = [first] + list(ordered[1:])
    return [c for c in ordered if (not c.expired) or c.refresh_token]


def clear_account_cooldown(account_id: str) -> dict[str, Any] | None:
    """Manually clear cooldown so the account re-enters rotation immediately.

    Clears both Redis hot cooldown and durable PG/file pool meta.
    """
    if not account_id:
        return None
    try:
        from store.pool_redis import clear_cooldown

        clear_cooldown(account_id)
    except Exception:
        pass
    try:
        from store.redis_client import delete, key, redis_enabled

        if redis_enabled():
            delete(key("cooldown", account_id))
    except Exception:
        pass
    meta = patch_account_pool_meta(
        account_id,
        {
            "cooldown_count": 0,
            "status_stack": [],
            "cooldown_until": None,
            "cooldown_sec": None,
            "cooldown_reason": None,
            "cooldown_code": None,
            "cooldown_model": None,
            "cooldown_tokens_actual": None,
            "cooldown_tokens_limit": None,
            "cooldown_detail": None,
            "consecutive_fails": 0,
            "probe_fail_streak": 0,
            "last_error": None,
            "last_probe_status": "normal",
            "pool_status": "normal",
        },
    )
    invalidate_pool_summary_cache()
    for a in list_pool_accounts():
        if a["id"] == account_id:
            a["in_cooldown"] = False
            a["cooldown_remaining_sec"] = 0.0
            a["cooldown_count"] = 0
            a["pool_status"] = (meta or {}).get("pool_status") or "normal"
            return a
    return {
        "id": account_id,
        "in_cooldown": False,
        "cooldown_remaining_sec": 0.0,
        "cooldown_count": 0,
        "pool_status": (meta or {}).get("pool_status") or "normal",
        "consecutive_fails": 0,
    }


def kick_from_pool(
    account_id: str,
    *,
    reason: str = "手动移出轮询",
    cooldown_sec: float | None = None,
) -> dict[str, Any] | None:
    """Temporarily or permanently remove an account from rotation.

    - cooldown_sec > 0: soft kick (cooldown only)
    - cooldown_sec is None/0: disable account (enabled=False) without quota flag
    """
    if not account_id:
        return None
    reason_s = (reason or "手动移出轮询")[:300]
    if cooldown_sec and float(cooldown_sec) > 0:
        meta0 = _pool_meta(account_id, get_account_pool_state())
        new_count = stack_cooldown_count(meta0, add=1)
        until = _now() + max(float(cooldown_sec), 60.0) * max(1, new_count)
        # Durable PG meta bound to this account_id: stack count, not replace time.
        try:
            patch_account_pool_meta(
                account_id,
                {
                    "cooldown_count": new_count,
                    "pool_status": "cooldown",
                    "cooldown_until": until,
                    "cooldown_sec": float(new_count),
                    "last_error": reason_s,
                },
            )
        except Exception:
            pass
        touch_account_stats(
            account_id,
            success=False,
            error=reason_s,
            cooldown_until=until,
            consecutive_fails=max(
                1, int(meta0.get("consecutive_fails") or 0)
            ),
            cooldown_sec=float(new_count),
        )
        try:
            from store.pool_redis import set_cooldown

            set_cooldown(account_id, until)
        except Exception:
            pass
        invalidate_pool_summary_cache()
        for a in list_pool_accounts():
            if a["id"] == account_id:
                a["in_cooldown"] = True
                a["cooldown_until"] = until
                a["cooldown_remaining_sec"] = max(0.0, until - _now())
                a["cooldown_sec"] = float(new_count)
                a["cooldown_count"] = new_count
                a["pool_status"] = "cooldown"
                a["last_error"] = reason_s
                return a
        return {
            "id": account_id,
            "in_cooldown": True,
            "cooldown_until": until,
            "cooldown_remaining_sec": max(0.0, until - _now()),
            "cooldown_sec": float(new_count),
            "cooldown_count": new_count,
            "pool_status": "cooldown",
            "last_error": reason_s,
        }
    # Hard remove from pool (manual disable; not quota)
    patch_account_pool_meta(
        account_id,
        {
            "enabled": False,
            "disabled_reason": reason_s,
            "last_error": reason_s,
            "pool_status": "disabled",
        },
    )
    for a in list_pool_accounts():
        if a["id"] == account_id:
            return a
    return {"id": account_id, "enabled": False, "disabled_reason": reason_s}


def record_model_probe_outcome(
    account_id: str | None,
    *,
    model: str | None = None,
    available: bool,
    error: str = "",
    status_code: int | None = None,
    source: str = "probe",
    auto_kick: bool = True,
) -> dict[str, Any] | None:
    """Track probe success/fail streaks and escalate with cooldown only.

    Never hard-disables accounts for temporary free-usage / 429s.
    Successful probe is the only automatic path that clears durable cooldown
    (aside from manual clear / enable). Ordinary live traffic must not.
    """
    if not account_id:
        return None
    state = get_account_pool_state()
    meta = _pool_meta(account_id, state)
    pol = cooldown_defaults()
    kick_at = int(pol.get("probe_fail_kick_streak") or PROBE_FAIL_KICK_STREAK)
    disable_at = int(pol.get("probe_fail_disable_streak") or PROBE_FAIL_DISABLE_STREAK)
    kick_cd = float(pol.get("probe_kick_cooldown_sec") or PROBE_KICK_COOLDOWN_SEC)
    disable_at = max(kick_at + 1, disable_at)

    if available:
        # 单次测活成功：改变账号状态 normal，清空 DB 中的 status_stack / 冷却字段。
        was_cooling = is_in_cooldown(meta)
        prev_count = 0
        try:
            prev_count = int(meta.get("cooldown_count") or 0)
        except (TypeError, ValueError):
            prev_count = 0
        prev_stack = meta.get("status_stack") if isinstance(meta.get("status_stack"), list) else []
        if prev_stack:
            prev_count = max(prev_count, len(prev_stack))
        patch: dict[str, Any] = {
            "probe_fail_streak": 0,
            "consecutive_fails": 0,
            "last_probe_ok_at": _now(),
            "last_probe_status": "normal",
            # Clear stacked free-usage status (DB columns + extra stack).
            "cooldown_count": 0,
            "status_stack": [],
            "cooldown_until": None,
            "cooldown_sec": None,
            "cooldown_reason": None,
            "cooldown_code": None,
            "cooldown_model": None,
            "cooldown_tokens_actual": None,
            "cooldown_tokens_limit": None,
            "cooldown_detail": None,
            "last_error": None,
            "pool_status": "normal",
        }
        try:
            from store.pool_redis import clear_cooldown

            clear_cooldown(account_id)
        except Exception:
            pass
        # Also drop soft/temp model blocks for the probed model so status is normal.
        if model:
            try:
                blocked = meta.get("blocked_models") if isinstance(meta.get("blocked_models"), dict) else {}
                entry = blocked.get(model) if isinstance(blocked, dict) else None
                if isinstance(entry, dict):
                    src = str(entry.get("source") or "")
                    until = entry.get("until")
                    if until is not None or src in ("temp_usage", "soft", "temporary", "probe", ""):
                        new_blocked = dict(blocked)
                        new_blocked.pop(model, None)
                        patch["blocked_models"] = new_blocked if new_blocked else None
            except Exception:
                pass
        # Auto re-enable only when we previously kicked via model health, not quota.
        if (
            meta.get("enabled") is False
            and not meta.get("disabled_for_quota")
            and str(meta.get("quota_source") or meta.get("disabled_source") or "")
            in ("", "model_health", "probe", "probe_kick", "None")
        ):
            src = str(meta.get("disabled_source") or meta.get("quota_source") or "")
            reason = str(meta.get("disabled_reason") or "")
            if src in ("model_health", "probe", "probe_kick") or reason.startswith(
                ("模型探测失败", "模型不可用", "探测连续失败", "临时额度耗尽")
            ):
                patch["enabled"] = True
                patch["disabled_reason"] = None
                patch["disabled_source"] = None
        # Force status normal after successful probe (unless still quota-disabled).
        if not meta.get("disabled_for_quota") and patch.get("enabled", meta.get("enabled", True)) is not False:
            patch["pool_status"] = "normal"
        patch_account_pool_meta(account_id, patch)
        invalidate_pool_summary_cache()
        return {
            "id": account_id,
            "probe_fail_streak": 0,
            "action": "recovered",
            "enabled": True if patch.get("enabled", meta.get("enabled", True)) is not False else False,
            "cleared_cooldown": True,
            "was_cooling": was_cooling,
            "cleared_cooldown_count": prev_count,
            "cooldown_count": 0,
            "pool_status": patch.get("pool_status") or "normal",
        }

    # Failure path — probe changes account status in DB.
    streak = int(meta.get("probe_fail_streak") or 0) + 1
    result: dict[str, Any] = {
        "id": account_id,
        "probe_fail_streak": streak,
        "action": "recorded",
    }

    # free-usage-exhausted is the reference signal for cooldown (auto + manual probe).
    free = apply_free_usage_cooldown(
        account_id,
        error=error,
        status_code=status_code,
        model=model,
        source=source or "probe",
    )
    if free:
        # Keep probe streak bookkeeping on the same account row.
        try:
            patch_account_pool_meta(
                account_id,
                {
                    "probe_fail_streak": streak,
                    "last_probe_fail_at": _now(),
                    "last_probe_status": "cooldown",
                },
            )
        except Exception:
            pass
        free["probe_fail_streak"] = streak
        return free

    # Non free-usage probe failures: only escalate after streak threshold.
    reason = (
        f"探测连续失败×{streak}"
        + (f" model={model}" if model else "")
        + (f" HTTP {status_code}" if status_code else "")
        + (f": {(error or '')[:120]}" if error else "")
    )[:300]
    patch_fail: dict[str, Any] = {
        "probe_fail_streak": streak,
        "last_error": reason,
        "last_status_code": status_code,
        "last_probe_fail_at": _now(),
        "last_probe_status": "error",
    }
    if auto_kick and streak >= kick_at:
        # Stack a generic probe-fail status entry (still status stack, not time).
        entry = {
            "kind": "probe_fail",
            "code": "probe_fail",
            "model": model,
            "source": source or "probe",
            "status_code": status_code,
            "at": _now(),
            "reason": reason,
        }
        stack = stack_status_entry(meta, entry)
        new_count = len(stack)
        until = _now() + float(kick_cd) * max(1, new_count)
        patch_fail.update(
            {
                "status_stack": stack,
                "cooldown_count": new_count,
                "pool_status": "cooldown",
                "cooldown_until": until,
                "cooldown_sec": float(new_count),
                "cooldown_reason": reason,
                "cooldown_code": "probe_fail",
                "cooldown_model": model,
                "last_probe_status": "cooldown",
            }
        )
        patch_account_pool_meta(account_id, patch_fail)
        try:
            from store.pool_redis import set_cooldown

            set_cooldown(account_id, until)
        except Exception:
            pass
        print(f"  [pool] probe-fail status stack ×{new_count} {account_id}: {reason}")
        result["action"] = "cooldown"
        result["cooldown_count"] = new_count
        result["status_stack_len"] = new_count
        result["cooldown_until"] = until
        result["pool_status"] = "cooldown"
        return result

    # Not yet kick threshold: only streak counters — keep existing cooldown status.
    patch_account_pool_meta(account_id, patch_fail)
    return result




def reenable_probe_kick_accounts() -> dict[str, Any]:
    """Re-enable accounts hard-disabled by old probe_kick free-usage logic.

    Temporary usage should never leave enabled=false. Returns counts.
    """
    state = get_account_pool_state()
    reenabled = 0
    for aid, meta in list(state.items()):
        if not isinstance(meta, dict):
            continue
        if meta.get("enabled") is not False:
            continue
        if meta.get("disabled_for_quota"):
            continue
        src = str(meta.get("disabled_source") or meta.get("quota_source") or "")
        reason = str(meta.get("disabled_reason") or "")
        if src not in ("probe_kick", "model_health", "probe") and not reason.startswith(
            ("探测连续失败", "模型探测失败", "模型不可用")
        ):
            continue
        patch_account_pool_meta(
            aid,
            {
                "enabled": True,
                "disabled_reason": None,
                "disabled_source": None,
                # keep existing cooldown if still active; do not force clear all
            },
        )
        reenabled += 1
    if reenabled:
        invalidate_pool_summary_cache()
        print(f"  [pool] re-enabled {reenabled} probe_kick account(s) (no hard-disable policy)")
    return {"ok": True, "reenabled": reenabled}

def load_for_id(account_id: str) -> GrokCredentials:
    return load_credentials_by_id(account_id)
