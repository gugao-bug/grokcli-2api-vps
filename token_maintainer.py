"""Background maintenance for multi-account auth on long-running servers.

- Normalize auth.json keys (CLI client_id → per-user multi-account)
- Proactively refresh access tokens via refresh_token before expiry
- Adaptive interval: refresh sooner when any token is near expiry
- Batched / concurrency-capped cycles so large pools (700+) don't freeze WSL
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from maintenance_gate import maintenance_slot

_stop = threading.Event()
_thread: threading.Thread | None = None
_last_run: dict[str, Any] = {}
_wakeup = threading.Event()  # force an early cycle from admin UI
_force_next = False
_force_lock = threading.Lock()
_min_remaining_cache: dict[str, Any] = {"at": 0.0, "value": None}
_MIN_REMAINING_CACHE_TTL = 15.0


def _interval() -> float:
    try:
        # Allow 30s+ so operators can run faster recovery batches (e.g. 90s).
        return max(30.0, float(os.getenv("GROK2API_TOKEN_MAINTAIN_INTERVAL", "90")))
    except ValueError:
        return 90.0


def _skew() -> float:
    try:
        return float(os.getenv("GROK2API_TOKEN_REFRESH_SKEW", "120"))
    except ValueError:
        return 120.0


def _startup_delay() -> float:
    try:
        from config import TOKEN_MAINTAIN_STARTUP_DELAY

        return max(5.0, float(TOKEN_MAINTAIN_STARTUP_DELAY))
    except Exception:
        return 45.0


def _min_remaining_seconds(*, force: bool = False) -> float | None:
    """Smallest access-token remaining lifetime across live accounts."""
    now = time.time()
    if (
        not force
        and _min_remaining_cache.get("at")
        and now - float(_min_remaining_cache["at"]) < _MIN_REMAINING_CACHE_TTL
    ):
        return _min_remaining_cache.get("value")  # type: ignore[return-value]
    try:
        from auth import list_live_credentials

        remains: list[float] = []
        for c in list_live_credentials(include_expired=True, auto_refresh=False):
            if c.expires_at is None:
                continue
            remains.append(float(c.expires_at) - now)
        value = min(remains) if remains else None
    except Exception:
        value = None
    _min_remaining_cache["at"] = now
    _min_remaining_cache["value"] = value
    return value


def _next_wait_seconds() -> float:
    """
    Adaptive sleep: if any token expires soon, poll more frequently so
    expires_at gets refreshed automatically without manual clicks.
    """
    base = _interval()
    rem = _min_remaining_seconds()
    if rem is None:
        return base
    # Already expired (or past skew) → retry aggressively.
    if rem <= 0:
        return min(base, 30.0)
    # Within 15 minutes of expiry → check every 45s
    if rem <= 15 * 60:
        return min(base, 45.0)
    # Within 1 hour → check every 2 minutes
    if rem <= 3600:
        return min(base, 120.0)
    return base


def run_once(*, force: bool = False) -> dict[str, Any]:
    """
    Normalize keys + refresh tokens.
    force=True refreshes every account that has refresh_token (updates expires_at),
    still batch-capped so a single cycle never fans out to all 700 accounts.
    """
    result: dict[str, Any] = {
        "ok": True,
        "normalized": None,
        "refresh": None,
        "force": force,
        "accounts": [],
        "deferred_busy": False,
    }
    # Prefer waiting for model probes to finish (tokens are more important),
    # but never hang forever if a probe cycle is stuck on network.
    with maintenance_slot("token_maintainer", blocking=True, timeout=180.0) as got:
        if not got:
            result["ok"] = True
            result["deferred_busy"] = True
            result["error"] = "maintenance slot busy — deferred"
            _last_run.clear()
            _last_run.update(result)
            _last_run["at"] = time.time()
            print("  [token-maintainer] deferred: maintenance slot busy")
            return result
        try:
            from accounts import list_accounts
            from oidc_auth import normalize_auth_file_keys, refresh_all_accounts

            result["normalized"] = normalize_auth_file_keys()
            # Opportunistic cleanup of permanently unusable accounts:
            # refresh_invalid marks, no-RT+no-access, no-RT+access-expired.
            # Default soft-disable (keep credentials); hard delete only with env.
            try:
                from oidc_auth import purge_refresh_invalid_accounts

                purged = purge_refresh_invalid_accounts(dry_run=False)
                result["purged_dead"] = {
                    "deleted": int((purged or {}).get("deleted") or 0),
                    "disabled": int((purged or {}).get("disabled") or 0),
                    "action": (purged or {}).get("action") or "disabled",
                    "by_reason": (purged or {}).get("by_reason") or {},
                }
            except Exception as e:  # noqa: BLE001
                result["purged_dead"] = {
                    "deleted": 0,
                    "disabled": 0,
                    "error": str(e)[:200],
                }
            # force: still only-near-expiry=False, but max_accounts batch applies
            # Background cycles use a generous skew so already-expired tokens are
            # always candidates; near-expiry (~15m) is also included.
            skew = max(900.0, _skew() * 4)
            # force: refresh even far-from-expiry, but still batch-capped so one
            # admin click never rewrites 700 accounts at once on WSL.
            try:
                from config import TOKEN_REFRESH_BATCH
            except Exception:
                TOKEN_REFRESH_BATCH = 40
            # Prefer larger batches when many tokens are already expired so the
            # pool recovers faster instead of only refreshing 20-40/cycle.
            rem = _min_remaining_seconds(force=True)
            if force:
                force_batch = min(max(TOKEN_REFRESH_BATCH * 2, 40), 120)
            elif rem is not None and rem <= 0:
                force_batch = min(max(TOKEN_REFRESH_BATCH * 2, 40), 100)
            else:
                force_batch = TOKEN_REFRESH_BATCH
            refresh = refresh_all_accounts(
                only_near_expiry=not force,
                skew_seconds=skew if not force else 365 * 86400.0,
                max_accounts=force_batch,
                # Background / force batch: strict non-repeat sweep so permanent
                # refresh failures cannot monopolize every cycle.
                strict_sweep=True,
            )
            # Keep full result for the direct admin/API caller, but never retain
            # hundreds of per-account rows in the background status cache —
            # that alone made /health ~100KB on a 400+ pool.
            rows = refresh.get("results") if isinstance(refresh, dict) else None
            slim_refresh = {
                k: v
                for k, v in (refresh or {}).items()
                if k != "results"
            }
            if isinstance(rows, list):
                failed = [r for r in rows if not r.get("ok") and not r.get("skipped")]
                slim_refresh["failed_sample"] = failed[:5]
                slim_refresh["failed"] = len(failed)
                slim_refresh["skipped"] = sum(1 for r in rows if r.get("skipped"))
                slim_refresh["invalidated"] = sum(
                    1
                    for r in rows
                    if r.get("permanent")
                    or r.get("deleted")
                    or r.get("reason")
                    in ("refresh_invalid", "refresh_invalid_deleted")
                )
                slim_refresh["deleted"] = int(
                    (refresh or {}).get("deleted")
                    or sum(1 for r in rows if r.get("deleted"))
                    or 0
                )
            result["refresh"] = slim_refresh
            accounts = list_accounts()
            result["accounts"] = []  # never embed full account list in status cache
            result["accounts_total"] = len(accounts)
            result["min_remaining_sec"] = _min_remaining_seconds(force=True)
            # Attach full refresh only on the returned object for admin force-run.
            result_full = dict(result)
            result_full["refresh"] = refresh
            result = result_full
            # Operator-visible cycle log (kept short).
            try:
                sw = (refresh or {}).get("sweep") or {}
                print(
                    "  [token-maintainer] cycle: "
                    f"refreshed={slim_refresh.get('refreshed')} "
                    f"attempted={slim_refresh.get('attempted')} "
                    f"failed={slim_refresh.get('failed')} "
                    f"deferred={slim_refresh.get('deferred')} "
                    f"deleted={slim_refresh.get('deleted') or slim_refresh.get('invalidated') or 0} "
                    f"force={force}"
                    + (
                        f" sweep=gen:{sw.get('generation')} "
                        f"covered={sw.get('covered')}/{sw.get('need_refresh')} "
                        f"left={sw.get('remaining')}"
                        if sw
                        else ""
                    )
                )
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            result["ok"] = False
            result["error"] = str(e)[:400]
    # Persist a slim snapshot for status()/health, not the full per-account dump.
    slim_last = {
        k: v
        for k, v in result.items()
        if k not in ("accounts",)
    }
    if isinstance(slim_last.get("refresh"), dict) and "results" in slim_last["refresh"]:
        rows = slim_last["refresh"].get("results") or []
        sweep = slim_last["refresh"].get("sweep")
        slim_last["refresh"] = {
            k: v for k, v in slim_last["refresh"].items() if k != "results"
        }
        slim_last["refresh"]["failed"] = sum(
            1 for r in rows if not r.get("ok") and not r.get("skipped")
        )
        slim_last["refresh"]["skipped"] = sum(1 for r in rows if r.get("skipped"))
        slim_last["refresh"]["failed_sample"] = [
            r for r in rows if not r.get("ok") and not r.get("skipped")
        ][:5]
        if sweep:
            slim_last["refresh"]["sweep"] = sweep
    # Stamp completion time AFTER slim copy so Redis/UI always get `at`.
    finished_at = time.time()
    slim_last["at"] = finished_at
    # Prefer the just-computed remaining; fall back if cycle errored early.
    if slim_last.get("min_remaining_sec") is None:
        try:
            slim_last["min_remaining_sec"] = _min_remaining_seconds(force=True)
        except Exception:
            pass
    try:
        slim_last["next_wait_sec"] = _next_wait_seconds()
    except Exception:
        slim_last["next_wait_sec"] = _interval()
    # Durable task log for admin「任务日志」.
    # Skip pure deferred no-ops and empty background sweeps to avoid flood.
    if not slim_last.get("deferred_busy"):
        try:
            import task_log

            rf = slim_last.get("refresh") if isinstance(slim_last.get("refresh"), dict) else {}
            refreshed = int(rf.get("refreshed") or 0)
            attempted = int(rf.get("attempted") or refreshed or 0)
            failed = int(rf.get("failed") or 0)
            deleted = int(rf.get("deleted") or 0)
            disabled = int(rf.get("disabled") or rf.get("invalidated") or 0)
            purged = 0
            purged_disabled = 0
            try:
                pd = (slim_last.get("purged_dead") or {}) or {}
                purged = int(pd.get("deleted") or 0)
                purged_disabled = int(pd.get("disabled") or 0)
            except Exception:
                purged = 0
                purged_disabled = 0
            meaningful = bool(
                force
                or slim_last.get("ok") is False
                or attempted
                or refreshed
                or failed
                or deleted
                or disabled
                or purged
                or purged_disabled
                or slim_last.get("error")
            )
            if meaningful:
                cleanup_n = deleted or purged
                disable_n = disabled or purged_disabled
                extra = ""
                if cleanup_n:
                    extra += f" · 硬删除 {cleanup_n}"
                elif disable_n:
                    extra += f" · 移出轮询 {disable_n}"
                summary = (
                    f"Token 续期{'（强制）' if force else ''}："
                    f"刷新 {refreshed}/{attempted or refreshed}"
                    f" · 失败 {failed}"
                    + extra
                )
                st = "done"
                if failed and refreshed:
                    st = "partial"
                elif failed and not refreshed and attempted:
                    st = "error"
                if slim_last.get("ok") is False:
                    st = "error"
                task_log.record(
                    "token_refresh",
                    summary=summary,
                    status=st,
                    ok=bool(slim_last.get("ok", True))
                    and not (failed and not refreshed and attempted),
                    progress_done=refreshed,
                    progress_total=attempted or refreshed,
                    detail={
                        "force": force,
                        "refresh": rf,
                        "purged_dead": slim_last.get("purged_dead"),
                        "error": slim_last.get("error"),
                    },
                )
        except Exception:
            pass
    _last_run.clear()
    _last_run.update(slim_last)
    # Also mirror last run into Redis so non-leader workers can show real status.
    try:
        from store.redis_client import key, redis_enabled, set_ex
        import json as _json

        if redis_enabled() and slim_last:
            set_ex(
                key("token_maintainer", "last_run"),
                _json.dumps(slim_last, ensure_ascii=False, default=str),
                3600,
            )
    except Exception:
        pass
    return result


def request_run_soon(*, force: bool = True) -> None:
    """Wake the background worker for an early cycle."""
    global _force_next
    with _force_lock:
        _force_next = bool(force)
    _wakeup.set()


def _worker() -> None:
    # Stagger startup so normalize + first HTTP requests aren't simultaneous
    # with model-health probe fan-out (large pools freeze WSL otherwise).
    if _stop.wait(_startup_delay()):
        return
    while not _stop.is_set():
        if not is_enabled():
            # paused via admin toggle — idle until re-enabled / stop
            _wakeup.clear()
            _wakeup.wait(timeout=5.0)
            continue
        run_once(force=False)
        wait = _next_wait_seconds()
        # Wait either for interval or an admin-triggered wakeup
        _wakeup.clear()
        triggered = _wakeup.wait(timeout=wait)
        if _stop.is_set():
            break
        if triggered:
            with _force_lock:
                global _force_next
                do_force = _force_next
                _force_next = False
            # admin asked for refresh — do a force pass (still batch-capped)
            run_once(force=do_force)


def is_enabled() -> bool:
    try:
        from settings_store import get_token_maintain_enabled
        return bool(get_token_maintain_enabled())
    except Exception:
        return os.getenv("GROK2API_TOKEN_MAINTAIN", "1").lower() not in ("0", "false", "no")


def start_background() -> None:
    global _thread
    if not is_enabled():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_worker, name="g2a-token-maintainer", daemon=True)
    _thread.start()


def stop_background() -> None:
    global _thread
    _stop.set()
    _wakeup.set()
    th = _thread
    if th and th.is_alive():
        th.join(timeout=2.0)
    _thread = None


def status(*, light: bool = False) -> dict[str, Any]:
    local_running = bool(_thread and _thread.is_alive())
    try:
        from config import TOKEN_REFRESH_BATCH, TOKEN_REFRESH_WORKERS
    except Exception:
        TOKEN_REFRESH_BATCH = 20
        TOKEN_REFRESH_WORKERS = 2
    # Cluster-aware: only leader process has the thread. Non-leaders would
    # otherwise always report running=false and confuse the admin UI.
    cluster_running = local_running
    leader_id = None
    is_leader = False
    try:
        from store.leader import is_leader as _is_leader, status as _leader_status
        is_leader = bool(_is_leader())
        ls = _leader_status()
        leader_id = ls.get("leader_id")
        if local_running:
            cluster_running = True
        elif is_enabled():
            # Only claim cluster_running when a live Redis leader lock exists.
            # A stale last_run snapshot alone must NOT report running=true.
            try:
                from store.redis_client import get_str, key, redis_enabled
                if redis_enabled():
                    lid = get_str(key("lock", "maintainer_leader"))
                    if lid:
                        leader_id = lid
                        # Presence of lock key (with TTL) means some worker owns leadership.
                        cluster_running = True
            except Exception:
                pass
    except Exception:
        pass
    out = {
        "running": bool(cluster_running),
        "local_running": local_running,
        "cluster_running": bool(cluster_running),
        "leader_running": bool(cluster_running and is_enabled()),
        "is_leader": is_leader,
        "leader_id": leader_id,
        "enabled": is_enabled(),
        "interval_sec": _interval(),
        "refresh_skew_sec": _skew(),
        "startup_delay_sec": _startup_delay(),
        "refresh_workers": TOKEN_REFRESH_WORKERS,
        "refresh_batch": TOKEN_REFRESH_BATCH,
    }
    last = dict(_last_run) if _last_run else None
    # Non-leader workers: read mirrored last_run from Redis.
    if last is None:
        try:
            from store.redis_client import get_str, key, redis_enabled
            import json as _json

            if redis_enabled():
                raw = get_str(key("token_maintainer", "last_run"))
                if raw:
                    last = _json.loads(raw)
        except Exception:
            last = None

    # Always surface fields the admin UI needs — even in light mode.
    # Prefer live local compute on the leader; fall back to last-run snapshot.
    rem = None
    next_wait = None
    if local_running:
        try:
            rem = _min_remaining_seconds(force=not light)
        except Exception:
            rem = None
        try:
            next_wait = _next_wait_seconds()
        except Exception:
            next_wait = _interval()
    if rem is None and isinstance(last, dict):
        try:
            rem = float(last.get("min_remaining_sec")) if last.get("min_remaining_sec") is not None else None
        except (TypeError, ValueError):
            rem = None
    if next_wait is None and isinstance(last, dict) and last.get("next_wait_sec") is not None:
        try:
            next_wait = float(last.get("next_wait_sec"))
        except (TypeError, ValueError):
            next_wait = None
    if next_wait is None:
        next_wait = _interval()

    out["min_remaining_sec"] = rem
    out["next_wait_sec"] = next_wait

    if light:
        # Keep /health tiny: only last outcome summary, no per-account rows.
        if last:
            refresh = last.get("refresh")
            if isinstance(refresh, dict):
                refresh = {
                    k: v
                    for k, v in refresh.items()
                    if k
                    in (
                        "ok",
                        "refreshed",
                        "deferred",
                        "attempted",
                        "workers",
                        "failed",
                        "skipped",
                        "invalidated",
                        "deleted",
                        "sweep",
                    )
                }
            out["last"] = {
                "ok": last.get("ok"),
                "at": last.get("at"),
                "force": last.get("force"),
                "deferred_busy": last.get("deferred_busy"),
                "accounts_total": last.get("accounts_total"),
                "min_remaining_sec": last.get("min_remaining_sec")
                if last.get("min_remaining_sec") is not None
                else rem,
                "next_wait_sec": last.get("next_wait_sec")
                if last.get("next_wait_sec") is not None
                else next_wait,
                "refresh": refresh,
            }
        else:
            out["last"] = None
    else:
        out["last"] = last
    return out
