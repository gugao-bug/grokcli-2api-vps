"""Pure OIDC device-code + refresh for xAI (no Grok CLI binary required).

Works on headless Linux servers: show user_code, poll token endpoint,
persist access_token + refresh_token into auth.json with per-user keys
so multiple accounts can coexist.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
import httpx

from auth_store import mutate_auth_map, read_auth_map, upsert_auth_entry, write_auth_map
from config import GROK_CLI_CLIENT_ID, OIDC_DEVICE_URL, OIDC_SCOPES, OIDC_TOKEN_URL

# In-memory device sessions (server-side poll). When Redis is on, also mirrored
# so other workers can poll status for multi-worker admin UX.
_lock = threading.RLock()
_device_sessions: dict[str, dict[str, Any]] = {}
# Serialize refresh for same account (avoid parallel refresh_token races)
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _device_redis() -> bool:
    try:
        from store.redis_client import redis_enabled

        return redis_enabled()
    except Exception:
        return False


def _device_mirror(session_id: str, sess: dict[str, Any] | None) -> None:
    if not _device_redis() or not session_id:
        return
    try:
        from store import sessions_redis

        if sess is None:
            sessions_redis.device_delete(session_id)
        else:
            sessions_redis.device_put(session_id, sess)
    except Exception:
        pass


def _device_load(session_id: str) -> dict[str, Any] | None:
    with _lock:
        local = _device_sessions.get(session_id)
        if local is not None:
            return local
    if not _device_redis():
        return None
    try:
        from store import sessions_redis

        remote = sessions_redis.device_get(session_id)
        if remote:
            with _lock:
                # Cache remotely-created sessions locally for the poll worker.
                _device_sessions.setdefault(session_id, remote)
            return remote
    except Exception:
        pass
    return None


def _b64url_json(segment: str) -> dict[str, Any]:
    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    return _b64url_json(parts[1])


def parse_expires_at(value: Any, token: str | None = None) -> float | None:
    """Accept unix float/int, ISO-8601 string, or JWT exp fallback."""
    if value is None:
        pass
    elif isinstance(value, (int, float)):
        return float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            pass
        else:
            try:
                return float(s)
            except ValueError:
                pass
            try:
                # handle nanoseconds / trailing Z
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                # trim >6 fractional digits for fromisoformat
                if "." in s:
                    head, rest = s.split(".", 1)
                    digits = ""
                    tz = ""
                    for i, ch in enumerate(rest):
                        if ch.isdigit():
                            digits += ch
                        else:
                            tz = rest[i:]
                            break
                    digits = (digits + "000000")[:6]
                    s = f"{head}.{digits}{tz}"
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                pass
    if token:
        exp = decode_jwt_claims(token).get("exp")
        try:
            return float(exp) if exp is not None else None
        except (TypeError, ValueError):
            return None
    return None


def account_storage_id(
    *,
    user_id: str | None = None,
    client_id: str | None = None,
    fallback: str | None = None,
) -> str:
    """
    Stable multi-account key. Prefer user_id so multiple humans sharing the
    same OAuth client_id do not overwrite each other (CLI default key is
    issuer::client_id which is single-slot).
    """
    if user_id:
        return f"https://auth.x.ai::{user_id}"
    if client_id:
        return f"https://auth.x.ai::{client_id}"
    return fallback or f"https://auth.x.ai::imported-{uuid.uuid4().hex[:12]}"


def entry_from_token_response(
    token_data: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    access = token_data.get("access_token") or token_data.get("key")
    if not access or not isinstance(access, str):
        raise ValueError("token response missing access_token")

    claims = decode_jwt_claims(access)
    prev = previous or {}
    user_id = (
        prev.get("user_id")
        or claims.get("principal_id")
        or claims.get("sub")
        or prev.get("principal_id")
    )
    client_id = (
        prev.get("oidc_client_id")
        or claims.get("client_id")
        or claims.get("aud")
        or GROK_CLI_CLIENT_ID
    )
    if isinstance(client_id, list):
        client_id = client_id[0] if client_id else GROK_CLI_CLIENT_ID

    expires_in = token_data.get("expires_in")
    exp = parse_expires_at(None, access)
    if exp is None and expires_in is not None:
        try:
            exp = time.time() + float(expires_in)
        except (TypeError, ValueError):
            exp = None

    entry: dict[str, Any] = {
        "key": access,
        "auth_mode": prev.get("auth_mode") or "oidc",
        "create_time": prev.get("create_time")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "oidc_issuer": prev.get("oidc_issuer") or "https://auth.x.ai",
        "oidc_client_id": str(client_id),
    }
    if exp is not None:
        entry["expires_at"] = float(exp)

    refresh = token_data.get("refresh_token") or prev.get("refresh_token")
    if refresh:
        entry["refresh_token"] = refresh

    email = prev.get("email") or claims.get("email")
    if email:
        entry["email"] = email
    if user_id:
        entry["user_id"] = str(user_id)
        entry["principal_id"] = str(user_id)
    for field in ("first_name", "last_name", "principal_type", "team_id"):
        if prev.get(field) is not None:
            entry[field] = prev[field]
        elif claims.get(field) is not None:
            entry[field] = claims[field]
    if claims.get("team_id") and "team_id" not in entry:
        entry["team_id"] = claims["team_id"]
    if claims.get("principal_type") and "principal_type" not in entry:
        entry["principal_type"] = claims["principal_type"]
    # given_name / family_name from userinfo-like claims
    if claims.get("given_name") and "first_name" not in entry:
        entry["first_name"] = claims["given_name"]
    if claims.get("family_name") and "last_name" not in entry:
        entry["last_name"] = claims["family_name"]

    aid = account_storage_id(user_id=str(user_id) if user_id else None, client_id=str(client_id))
    return aid, entry


def upsert_entry(account_id: str, entry: dict[str, Any], *, merge_same_user: bool = True) -> str:
    """
    Save one account. If another key holds the same user_id, replace/remove it
    so we never keep duplicate tokens for the same person.
    Multi-account safe: keys are per-user (issuer::user_id), not client_id slot.

    On PostgreSQL this is a row-level UPSERT (not a full-table rewrite).
    """
    return upsert_auth_entry(
        account_id, entry, merge_same_user=merge_same_user
    )


def normalize_auth_file_keys() -> dict[str, Any]:
    """
    Re-key entries that only use client_id slot into per-user keys so multiple
    accounts can coexist. Safe no-op when already unique.
    Call on startup and after import/login — legacy keys used
    https://auth.x.ai::<client_id> which breaks multi-account.
    """
    data = read_auth_map()
    if not data:
        return {"ok": True, "changed": 0, "total": 0}

    changed = 0
    new_map: dict[str, Any] = {}
    for old_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key") or entry.get("access_token") or entry.get("token")
        if not token:
            new_map[old_key] = entry
            continue
        entry = dict(entry)
        if entry.get("expires_at") is not None:
            exp = parse_expires_at(
                entry.get("expires_at"), token if isinstance(token, str) else None
            )
            if exp is not None:
                entry["expires_at"] = exp
                entry["key"] = token
        elif isinstance(token, str):
            exp = parse_expires_at(None, token)
            if exp is not None:
                entry["expires_at"] = exp
                entry["key"] = token
        uid = entry.get("user_id") or entry.get("principal_id")
        if not uid and isinstance(token, str):
            claims = decode_jwt_claims(token)
            uid = claims.get("principal_id") or claims.get("sub")
            if uid:
                entry["user_id"] = str(uid)
                entry.setdefault("principal_id", str(uid))
                if claims.get("email") and not entry.get("email"):
                    entry["email"] = claims["email"]
                if claims.get("team_id") and not entry.get("team_id"):
                    entry["team_id"] = claims["team_id"]
                if entry.get("expires_at") is None:
                    exp = parse_expires_at(None, token)
                    if exp is not None:
                        entry["expires_at"] = exp
                if not entry.get("refresh_token") and claims.get("jti"):
                    pass  # refresh only from token response
        new_key = account_storage_id(
            user_id=str(uid) if uid else None,
            fallback=old_key,
        )
        if new_key != old_key:
            changed += 1
        # Prefer entry that has refresh_token when colliding on same user
        if new_key in new_map:
            prev = new_map[new_key]
            if isinstance(prev, dict) and prev.get("refresh_token") and not entry.get(
                "refresh_token"
            ):
                continue
        new_map[new_key] = entry

    if changed or new_map != data:
        write_auth_map(new_map)
    return {"ok": True, "changed": changed, "total": len(new_map)}


class RefreshRevokedError(ValueError):
    """Refresh token permanently rejected by the IdP (invalid_grant / revoked)."""


def _hard_delete_invalid_refresh_enabled() -> bool:
    """Whether permanent RT failures may hard-delete accounts.

    Default OFF: soft-disable (keep credentials, remove from rotation) so a
    misclassified transient error cannot wipe usable accounts.
    Opt-in hard delete: GROK2API_DELETE_INVALID_REFRESH=1
    """
    raw = (
        os.environ.get("GROK2API_DELETE_INVALID_REFRESH")
        or os.environ.get("DELETE_INVALID_REFRESH")
        or "0"
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_permanent_refresh_failure(status_code: int, body: str) -> bool:
    """Return True only for clearly permanent refresh-token rejections.

    Intentionally narrow: bare ``revoked`` / ``invalid_token`` / ``token is
    invalid`` used to match transient proxy / upstream noise and caused usable
    accounts to be purged. Prefer soft-disable even when True.
    """
    text = (body or "").lower()
    if status_code not in (400, 401):
        return False
    # Exact-ish OIDC permanent grant failures only.
    markers = (
        "invalid_grant",
        "refresh token has been revoked",
        "refresh_token has been revoked",
        "refresh token is invalid",
        "refresh_token is invalid",
        "refresh token revoked",
        "refresh_token revoked",
        "refresh token expired",
        "refresh_token expired",
        "token has been revoked",
    )
    return any(marker in text for marker in markers)


def mark_refresh_invalid(
    account_id: str,
    *,
    reason: str = "refresh_token permanently invalid",
    hard_delete: bool | None = None,
) -> dict[str, Any]:
    """Mark one account's refresh token invalid and take it out of rotation.

    Default: soft action only
      - set ``refresh_invalid`` / reason on the durable account entry
      - disable pool rotation (enabled=False)
      - do NOT delete credentials

    Hard delete only when ``hard_delete=True`` or env
    ``GROK2API_DELETE_INVALID_REFRESH=1``.
    """
    aid = str(account_id or "").strip()
    if not aid:
        return {"ok": False, "deleted": False, "disabled": False, "error": "missing account id"}
    reason_s = str(reason or "refresh_token permanently invalid")[:300]
    do_hard = _hard_delete_invalid_refresh_enabled() if hard_delete is None else bool(hard_delete)

    if do_hard:
        try:
            from accounts import remove_account

            removed = bool(remove_account(aid))
        except Exception as e:  # noqa: BLE001
            try:
                def _apply(m: dict[str, Any]) -> None:
                    if aid in m:
                        m.pop(aid, None)
                        return
                    for k, v in list(m.items()):
                        if k == aid or k.endswith(f"::{aid}"):
                            m.pop(k, None)
                            continue
                        if isinstance(v, dict) and (
                            v.get("user_id") == aid or v.get("principal_id") == aid
                        ):
                            m.pop(k, None)

                mutate_auth_map(_apply)
                removed = True
            except Exception as e2:  # noqa: BLE001
                return {
                    "ok": False,
                    "deleted": False,
                    "disabled": False,
                    "id": aid,
                    "error": f"delete failed: {e}; fallback: {e2}"[:300],
                }
        try:
            from settings_store import get_account_pool_state, save_account_pool_state

            state = get_account_pool_state()
            if aid in state:
                state.pop(aid, None)
                save_account_pool_state(state)
        except Exception:
            pass
        try:
            from store.pool_redis import clear_cooldown

            clear_cooldown(aid)
        except Exception:
            pass
        if removed:
            print(
                f"  [token-refresh] HARD-deleted account={aid[:64]} reason={reason_s[:120]}",
                flush=True,
            )
        return {
            "ok": True,
            "deleted": bool(removed),
            "disabled": False,
            "id": aid,
            "reason": reason_s,
            "action": "deleted",
        }

    # Soft path: keep credentials; mark invalid + remove from rotation.
    marked = False
    resolved_id = aid
    try:
        def _mark(m: dict[str, Any]) -> None:
            nonlocal marked, resolved_id
            target_key = aid if isinstance(m.get(aid), dict) else None
            if target_key is None:
                for k, v in list(m.items()):
                    if not isinstance(v, dict):
                        continue
                    if k == aid or k.endswith(f"::{aid}"):
                        target_key = k
                        break
                    if v.get("user_id") == aid or v.get("principal_id") == aid:
                        target_key = k
                        break
            if not target_key or not isinstance(m.get(target_key), dict):
                return
            ent = dict(m[target_key])
            ent["refresh_invalid"] = True
            ent["refresh_invalid_at"] = time.time()
            ent["refresh_invalid_reason"] = reason_s
            m[target_key] = ent
            resolved_id = target_key
            marked = True

        mutate_auth_map(_mark)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "deleted": False,
            "disabled": False,
            "id": aid,
            "error": f"mark failed: {e}"[:300],
        }

    disabled = False
    try:
        from account_pool import kick_from_pool

        kick_from_pool(
            resolved_id,
            reason=f"refresh_invalid: {reason_s}"[:300],
            cooldown_sec=None,
        )
        disabled = True
    except Exception:
        try:
            from settings_store import patch_account_pool_meta

            patch_account_pool_meta(
                resolved_id,
                {
                    "enabled": False,
                    "disabled_reason": f"refresh_invalid: {reason_s}"[:300],
                    "pool_status": "disabled",
                    "last_error": reason_s[:300],
                },
            )
            disabled = True
        except Exception:
            pass

    print(
        f"  [token-refresh] soft-disabled account={resolved_id[:64]} reason={reason_s[:120]}",
        flush=True,
    )
    return {
        "ok": True,
        "deleted": False,
        "disabled": bool(disabled or marked),
        "marked": marked,
        "id": resolved_id,
        "reason": reason_s,
        "action": "disabled",
    }


def delete_account_for_refresh_failure(
    account_id: str,
    *,
    reason: str = "refresh_token permanently invalid",
) -> dict[str, Any]:
    """Back-compat wrapper: soft-disable by default (see mark_refresh_invalid)."""
    return mark_refresh_invalid(account_id, reason=reason)


def refresh_access_token(
    entry: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """
    Exchange refresh_token for a new access_token (and rotated refresh_token).
    Raises ValueError / httpx.HTTPError on failure.

    Pass a shared `client` when refreshing many accounts to avoid opening
    hundreds of TLS sessions at once (WSL/low-RAM friendly).
    """
    rt = entry.get("refresh_token")
    if not rt:
        raise ValueError("no refresh_token on account")
    # Permanently bad refresh tokens are marked by a previous cycle so we do
    # not burn OIDC quota every few minutes on the same dead accounts.
    if entry.get("refresh_invalid"):
        raise RefreshRevokedError(
            str(entry.get("refresh_invalid_reason") or "refresh_token marked invalid")
        )
    client_id = (
        entry.get("oidc_client_id")
        or GROK_CLI_CLIENT_ID
    )
    form = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": str(client_id),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client is not None:
        resp = client.post(OIDC_TOKEN_URL, data=form, headers=headers)
    else:
        with httpx.Client(timeout=30.0) as c:
            resp = c.post(OIDC_TOKEN_URL, data=form, headers=headers)
    if resp.status_code >= 400:
        body = resp.text[:400]
        if _is_permanent_refresh_failure(resp.status_code, body):
            raise RefreshRevokedError(f"refresh failed {resp.status_code}: {body}")
        raise ValueError(f"refresh failed {resp.status_code}: {body}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ValueError("invalid refresh response")
    return data


def _account_refresh_lock(account_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        lock = _refresh_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[account_id] = lock
        return lock


def refresh_and_persist(
    account_id: str,
    entry: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Refresh one account under a per-account lock (multi-account safe).

    When `persist=False`, only performs the OIDC exchange and returns the new
    entry — caller is responsible for a single batched write (startup bulk
    refresh). This avoids rewriting a multi-MB auth.json once per account.
    """
    lock = _account_refresh_lock(account_id)
    with lock:
        # re-read latest entry — another thread may have just refreshed
        latest_map = read_auth_map()
        latest = latest_map.get(account_id)
        if not isinstance(latest, dict):
            # try by user_id
            uid = entry.get("user_id") or entry.get("principal_id")
            if uid:
                for k, v in latest_map.items():
                    if isinstance(v, dict) and (
                        v.get("user_id") == uid or v.get("principal_id") == uid
                    ):
                        latest = v
                        account_id = k
                        break
            if not isinstance(latest, dict):
                latest = entry
        token_data = refresh_access_token(latest, client=client)
        new_id, new_entry = entry_from_token_response(token_data, previous=latest)
        uid = new_entry.get("user_id")
        if uid:
            new_id = account_storage_id(user_id=str(uid))
        else:
            new_id = account_id
        if persist:
            upsert_entry(new_id, new_entry)
        return {"account_id": new_id, "entry": new_entry}


def ensure_fresh_entry(
    account_id: str,
    entry: dict[str, Any],
    *,
    skew_seconds: float = 120.0,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Refresh if expired / near expiry and refresh_token exists.

    By default swallows transient errors so callers can fall back. Pass
    ``raise_on_error=True`` when the access token is already expired and the
    caller cannot proceed with a stale token.

    Permanent RT failures (invalid_grant / revoked) delete the account immediately.
    """
    token = entry.get("key")
    exp = parse_expires_at(entry.get("expires_at"), token if isinstance(token, str) else None)
    now = time.time()
    already_expired = exp is not None and exp <= now
    if exp is not None and exp > now + skew_seconds:
        return entry
    if not entry.get("refresh_token"):
        return entry
    try:
        result = refresh_and_persist(account_id, entry)
        return result["entry"]
    except RefreshRevokedError as e:
        # Soft-disable by default; never hard-delete on request path unless opted in.
        mark_refresh_invalid(account_id, reason=str(e))
        raise
    except Exception:
        if raise_on_error or already_expired:
            raise
        return entry


# ── Device authorization flow ───────────────────────────────────────────────


def start_device_authorization(
    *,
    client_id: str | None = None,
    scopes: str | None = None,
) -> dict[str, Any]:
    """Start OIDC device flow; returns session for UI polling."""
    cid = client_id or GROK_CLI_CLIENT_ID
    scope = scopes or OIDC_SCOPES
    form = {"client_id": cid, "scope": scope}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            OIDC_DEVICE_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"device code request failed {resp.status_code}: {resp.text[:400]}",
            }
        data = resp.json()

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    if not device_code or not user_code:
        return {"ok": False, "error": f"unexpected device response: {data}"}

    session_id = uuid.uuid4().hex[:12]
    verification_url = (
        data.get("verification_uri_complete")
        or data.get("verification_uri")
        or "https://accounts.x.ai/oauth2/device"
    )
    interval = int(data.get("interval") or 5)
    expires_in = int(data.get("expires_in") or 1800)
    started = time.time()

    sess = {
        "id": session_id,
        "mode": "device_oidc",
        "status": "waiting_user",
        "device_code": device_code,
        "user_code": str(user_code).upper(),
        "verification_url": verification_url,
        "client_id": cid,
        "interval": max(3, interval),
        "expires_at": started + expires_in,
        "started_at": started,
        "finished_at": None,
        "message": (
            f"请在浏览器打开 {verification_url} ，输入设备码 {str(user_code).upper()}"
        ),
        "error": None,
        "output": json.dumps(data, ensure_ascii=False),
        "account_id": None,
        "email": None,
    }
    with _lock:
        _device_sessions[session_id] = sess
    _device_mirror(session_id, sess)

    # background poller (must run on the worker that created the session —
    # other workers only read mirrored status from Redis)
    t = threading.Thread(target=_device_poll_worker, args=(session_id,), daemon=True)
    t.start()

    return {
        "ok": True,
        "session_id": session_id,
        "user_code": sess["user_code"],
        "verification_url": verification_url,
        "status": "waiting_user",
        "message": sess["message"],
        "interval": sess["interval"],
        "expires_in": expires_in,
        "capture": True,
        "native_oidc": True,
        "command": f"OIDC device @ {OIDC_DEVICE_URL}",
    }


def _device_update(session_id: str, **fields: Any) -> dict[str, Any] | None:
    with _lock:
        sess = _device_sessions.get(session_id)
        if not sess:
            return None
        sess.update(fields)
        snap = dict(sess)
    _device_mirror(session_id, snap)
    return snap


def _device_poll_worker(session_id: str) -> None:
    while True:
        with _lock:
            sess = _device_sessions.get(session_id)
            if not sess or sess.get("status") in ("success", "error", "expired"):
                return
            if time.time() > float(sess.get("expires_at") or 0):
                sess["status"] = "expired"
                sess["error"] = "device code expired"
                sess["message"] = "设备码已过期，请重新发起登录"
                sess["finished_at"] = time.time()
                snap = dict(sess)
                _device_mirror(session_id, snap)
                return
            device_code = sess["device_code"]
            client_id = sess["client_id"]
            interval = int(sess.get("interval") or 5)

        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    OIDC_TOKEN_URL,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                body_text = resp.text
                try:
                    body = resp.json()
                except Exception:
                    body = {}
        except Exception as e:  # noqa: BLE001
            _device_update(session_id, message=f"轮询网络异常，重试中: {e}")
            time.sleep(interval)
            continue

        err = body.get("error") if isinstance(body, dict) else None
        if resp.status_code == 200 and body.get("access_token"):
            try:
                account_id, entry = entry_from_token_response(body)
                # enrich email via userinfo if missing
                if not entry.get("email"):
                    try:
                        with httpx.Client(timeout=15.0) as client:
                            ui = client.get(
                                "https://auth.x.ai/oauth2/userinfo",
                                headers={"Authorization": f"Bearer {entry['key']}"},
                            )
                            if ui.status_code == 200:
                                u = ui.json()
                                if isinstance(u, dict):
                                    if u.get("email"):
                                        entry["email"] = u["email"]
                                    if u.get("given_name"):
                                        entry["first_name"] = u["given_name"]
                                    if u.get("family_name"):
                                        entry["last_name"] = u["family_name"]
                    except Exception:
                        pass
                upsert_entry(account_id, entry)
                with _lock:
                    sess = _device_sessions.get(session_id)
                    if sess:
                        sess["status"] = "success"
                        sess["message"] = f"登录成功: {entry.get('email') or account_id}"
                        sess["account_id"] = account_id
                        sess["email"] = entry.get("email")
                        sess["finished_at"] = time.time()
                        sess["output"] = (sess.get("output") or "") + "\n" + body_text[:500]
                        _device_mirror(session_id, dict(sess))
            except Exception as e:  # noqa: BLE001
                _device_update(
                    session_id,
                    status="error",
                    error=str(e),
                    message=f"保存凭证失败: {e}",
                    finished_at=time.time(),
                )
            return

        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval = min(interval + 5, 30)
                _device_update(session_id, interval=interval)
            time.sleep(interval)
            continue

        if err == "expired_token":
            _device_update(
                session_id,
                status="expired",
                error=err,
                message="设备码已过期，请重新发起登录",
                finished_at=time.time(),
            )
            return

        if err in ("access_denied", "access_denied"):
            _device_update(
                session_id,
                status="error",
                error=err,
                message="用户拒绝授权",
                finished_at=time.time(),
            )
            return

        # other errors
        with _lock:
            sess = _device_sessions.get(session_id)
            if sess:
                # keep waiting on transient unknown if still 4xx authorization_pending style
                if resp.status_code in (400, 401) and err:
                    sess["status"] = "error"
                    sess["error"] = f"{err}: {body.get('error_description') or body_text[:200]}"
                    sess["message"] = sess["error"]
                    sess["finished_at"] = time.time()
                    _device_mirror(session_id, dict(sess))
                    return
                sess["message"] = f"等待授权… ({resp.status_code})"
                _device_mirror(session_id, dict(sess))
        time.sleep(interval)


def get_device_session(session_id: str) -> dict[str, Any] | None:
    sess = _device_load(session_id)
    if not sess:
        return None
    return {
        "session_id": sess.get("id") or session_id,
        "mode": sess.get("mode"),
        "status": sess.get("status"),
        "user_code": sess.get("user_code"),
        "verification_url": sess.get("verification_url"),
        "message": sess.get("message"),
        "error": sess.get("error"),
        "output_tail": (sess.get("output") or "")[-2000:],
        "started_at": sess.get("started_at"),
        "finished_at": sess.get("finished_at"),
        "account_id": sess.get("account_id"),
        "email": sess.get("email"),
        "ok": sess.get("status") in ("running", "waiting_user", "success"),
        "native_oidc": True,
    }


def list_device_sessions() -> list[dict[str, Any]]:
    ids: list[str] = []
    with _lock:
        now = time.time()
        dead = [
            k
            for k, v in _device_sessions.items()
            if v.get("finished_at") and now - float(v["finished_at"]) > 3600
        ]
        for k in dead:
            _device_sessions.pop(k, None)
            _device_mirror(k, None)
        ids = list(_device_sessions.keys())
    if _device_redis():
        try:
            from store import sessions_redis

            for sid, _ in sessions_redis.device_list():
                if sid not in ids:
                    ids.append(sid)
        except Exception:
            pass
    out: list[dict[str, Any]] = []
    for k in ids:
        item = get_device_session(k)
        if item:
            out.append(item)
    return out


# Strict non-repeat sweep for background token refresh (shared via Redis).
_REFRESH_SWEEP_META = ("token_refresh", "sweep", "meta")
_REFRESH_SWEEP_COVERED = ("token_refresh", "sweep", "covered")
_REFRESH_SWEEP_TTL = 6 * 3600
_local_refresh_sweep: dict[str, Any] = {
    "generation": 0,
    "started_at": 0.0,
    "covered": set(),
}
_refresh_sweep_lock = threading.RLock()


def _refresh_sweep_ttl() -> int:
    try:
        interval = float(os.getenv("GROK2API_TOKEN_MAINTAIN_INTERVAL", "180") or 180)
    except Exception:
        interval = 180.0
    return max(int(_REFRESH_SWEEP_TTL), int(max(60.0, interval) * 40))


def _refresh_sweep_load() -> tuple[int, set[str], float]:
    try:
        from store.redis_client import get_str, key, redis_enabled, smembers

        if redis_enabled():
            meta_raw = get_str(key(*_REFRESH_SWEEP_META)) or ""
            gen = 0
            started = 0.0
            if meta_raw:
                parts = str(meta_raw).split("|", 1)
                try:
                    gen = int(parts[0] or 0)
                except (TypeError, ValueError):
                    gen = 0
                if len(parts) > 1:
                    try:
                        started = float(parts[1] or 0)
                    except (TypeError, ValueError):
                        started = 0.0
            return gen, smembers(key(*_REFRESH_SWEEP_COVERED)), started
    except Exception:
        pass
    with _refresh_sweep_lock:
        return (
            int(_local_refresh_sweep.get("generation") or 0),
            set(_local_refresh_sweep.get("covered") or set()),
            float(_local_refresh_sweep.get("started_at") or 0.0),
        )


def _refresh_sweep_start_new() -> tuple[int, set[str], float]:
    now = time.time()
    gen = int(now)
    try:
        from store.redis_client import delete, key, redis_enabled, set_ex

        if redis_enabled():
            delete(key(*_REFRESH_SWEEP_COVERED))
            set_ex(key(*_REFRESH_SWEEP_META), f"{gen}|{now}", _refresh_sweep_ttl())
            with _refresh_sweep_lock:
                _local_refresh_sweep["generation"] = gen
                _local_refresh_sweep["started_at"] = now
                _local_refresh_sweep["covered"] = set()
            return gen, set(), now
    except Exception:
        pass
    with _refresh_sweep_lock:
        _local_refresh_sweep["generation"] = gen
        _local_refresh_sweep["started_at"] = now
        _local_refresh_sweep["covered"] = set()
        return gen, set(), now


def _refresh_sweep_mark(ids: list[str]) -> int:
    ids = [str(x) for x in ids if x]
    if not ids:
        try:
            from store.redis_client import key, redis_enabled, scard

            if redis_enabled():
                return scard(key(*_REFRESH_SWEEP_COVERED))
        except Exception:
            pass
        with _refresh_sweep_lock:
            return len(_local_refresh_sweep.get("covered") or set())
    try:
        from store.redis_client import (
            expire,
            get_str,
            key,
            redis_enabled,
            sadd,
            scard,
            set_ex,
        )

        if redis_enabled():
            meta = get_str(key(*_REFRESH_SWEEP_META))
            if not meta:
                _refresh_sweep_start_new()
            else:
                set_ex(key(*_REFRESH_SWEEP_META), meta, _refresh_sweep_ttl())
            sadd(key(*_REFRESH_SWEEP_COVERED), *ids, ttl_sec=_refresh_sweep_ttl())
            expire(key(*_REFRESH_SWEEP_COVERED), _refresh_sweep_ttl())
            with _refresh_sweep_lock:
                cov = _local_refresh_sweep.setdefault("covered", set())
                if not isinstance(cov, set):
                    cov = set()
                    _local_refresh_sweep["covered"] = cov
                cov.update(ids)
            return scard(key(*_REFRESH_SWEEP_COVERED))
    except Exception:
        pass
    with _refresh_sweep_lock:
        cov = _local_refresh_sweep.setdefault("covered", set())
        if not isinstance(cov, set):
            cov = set()
            _local_refresh_sweep["covered"] = cov
        cov.update(ids)
        return len(cov)


def refresh_all_accounts(
    *,
    only_near_expiry: bool = True,
    skew_seconds: float = 300.0,
    max_workers: int | None = None,
    max_accounts: int | None = None,
    account_ids: list[str] | None = None,
    strict_sweep: bool | None = None,
) -> dict[str, Any]:
    """
    Refresh accounts that have refresh_token (optionally only near expiry).

    Designed for large pools (hundreds of accounts):
      - bounded thread pool (default TOKEN_REFRESH_WORKERS)
      - shared httpx client per worker (no 1-client-per-request storm)
      - single batched auth.json write at the end (not one rewrite per account)
      - optional max_accounts cap so a cycle never tries all 700 at once
      - optional account_ids to refresh only selected accounts
      - strict_sweep (default on for background batch): each needing-refresh
        account is attempted at most once per sweep generation, so permanent
        failures cannot starve the rest of the pool forever
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        from config import TOKEN_REFRESH_BATCH, TOKEN_REFRESH_WORKERS
    except Exception:
        TOKEN_REFRESH_WORKERS = 4
        TOKEN_REFRESH_BATCH = 40

    if max_workers is None:
        max_workers = TOKEN_REFRESH_WORKERS
    if max_accounts is None:
        # Selected-account renew should not be silently truncated by the
        # background batch cap used for full-pool maintenance.
        max_accounts = None if account_ids else TOKEN_REFRESH_BATCH
    # Strict sweep only for background pool maintenance (batch-capped, not selected).
    if strict_sweep is None:
        strict_sweep = bool(account_ids is None and max_accounts)

    data = read_auth_map()
    results: list[dict[str, Any]] = []
    candidates: list[tuple[str, dict[str, Any]]] = []
    now = time.time()
    wanted: set[str] | None = None
    if account_ids is not None:
        wanted = {str(x).strip() for x in account_ids if str(x).strip()}
        if not wanted:
            return {
                "ok": True,
                "results": [],
                "refreshed": 0,
                "deferred": 0,
                "attempted": 0,
                "workers": 0,
                "selected": 0,
            }

    for aid, entry in list(data.items()):
        if not isinstance(entry, dict):
            continue
        if wanted is not None and aid not in wanted:
            continue
        if not entry.get("refresh_token"):
            results.append({"id": aid, "ok": False, "error": "no refresh_token"})
            continue
        if entry.get("refresh_invalid"):
            results.append(
                {
                    "id": aid,
                    "ok": False,
                    "skipped": True,
                    "reason": "refresh_invalid",
                    "error": str(entry.get("refresh_invalid_reason") or "refresh_token marked invalid")[:200],
                }
            )
            continue
        token = entry.get("key")
        exp = parse_expires_at(
            entry.get("expires_at"), token if isinstance(token, str) else None
        )
        if only_near_expiry and exp is not None and exp > now + skew_seconds:
            results.append(
                {"id": aid, "ok": True, "skipped": True, "reason": "still_valid"}
            )
            continue
        candidates.append((aid, entry))

    if wanted is not None:
        existing = set(data.keys())
        for missing in sorted(wanted - existing):
            results.append({"id": missing, "ok": False, "error": "account_not_found"})

    # Prefer already-expired, then soonest-expiring accounts when batch-capped.
    # Missing expires_at sorts last among non-expired so known-dead tokens go first.
    def _exp_key(item: tuple[str, dict[str, Any]]) -> tuple[int, float]:
        _aid, entry = item
        token = entry.get("key")
        exp = parse_expires_at(
            entry.get("expires_at"), token if isinstance(token, str) else None
        )
        if exp is None:
            return (2, float("inf"))
        if float(exp) <= now:
            return (0, float(exp))
        return (1, float(exp))

    candidates.sort(key=_exp_key)
    deferred = 0
    sweep_info: dict[str, Any] | None = None
    if max_accounts and len(candidates) > max_accounts:
        if strict_sweep:
            cand_ids = [aid for aid, _ in candidates]
            cand_set = set(cand_ids)
            gen, covered, started = _refresh_sweep_load()
            if gen <= 0:
                gen, covered, started = _refresh_sweep_start_new()
            covered = {x for x in covered if x in cand_set}
            remaining = [(aid, e) for aid, e in candidates if aid not in covered]
            reset = False
            if not remaining:
                # All current need-refresh accounts already attempted this sweep
                # → new generation so permanent failures get another chance later,
                # after everyone else had a turn.
                gen, covered, started = _refresh_sweep_start_new()
                remaining = list(candidates)
                reset = True
                print(
                    f"  [token-refresh] sweep reset gen={gen} "
                    f"need_refresh={len(candidates)} (previous generation fully covered)"
                )
            # Still prefer soonest-expiring among *uncovered* accounts.
            remaining.sort(key=_exp_key)
            deferred = max(0, len(remaining) - int(max_accounts))
            for aid, _ in remaining[int(max_accounts) :]:
                results.append(
                    {
                        "id": aid,
                        "ok": True,
                        "skipped": True,
                        "reason": "batch_deferred",
                    }
                )
            candidates = remaining[: int(max_accounts)]
            sweep_info = {
                "mode": "strict_sweep",
                "generation": gen,
                "covered": len(covered),
                "need_refresh": len(cand_ids),
                "remaining": deferred,
                "started_at": started or None,
                "reset": reset,
            }
        else:
            deferred = len(candidates) - max_accounts
            for aid, _ in candidates[max_accounts:]:
                results.append(
                    {
                        "id": aid,
                        "ok": True,
                        "skipped": True,
                        "reason": "batch_deferred",
                    }
                )
            candidates = candidates[:max_accounts]

    updates: dict[str, dict[str, Any]] = {}
    invalid_marks: dict[str, str] = {}
    updates_lock = threading.Lock()
    # One shared client per worker thread instead of opening a fresh TCP/TLS
    # session for every account in the batch.
    _tls = threading.local()
    _clients: list[httpx.Client] = []
    _clients_lock = threading.Lock()

    def _thread_client() -> httpx.Client:
        client = getattr(_tls, "client", None)
        if client is None or client.is_closed:
            client = httpx.Client(timeout=30.0)
            _tls.client = client
            with _clients_lock:
                _clients.append(client)
        return client

    def _refresh_one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        aid, entry = item
        try:
            r = refresh_and_persist(
                aid, entry, client=_thread_client(), persist=False
            )
            # Successful refresh clears any previous invalid mark.
            new_entry = dict(r["entry"])
            new_entry.pop("refresh_invalid", None)
            new_entry.pop("refresh_invalid_at", None)
            new_entry.pop("refresh_invalid_reason", None)
            with updates_lock:
                updates[r["account_id"]] = new_entry
                # Drop old key if remounted to a different storage id
                if r["account_id"] != aid:
                    updates.setdefault("__delete__", {})  # type: ignore[arg-type]
            return {
                "id": r["account_id"],
                "ok": True,
                "email": new_entry.get("email"),
                "expires_at": new_entry.get("expires_at"),
            }
        except RefreshRevokedError as e:
            reason = str(e)[:300]
            with updates_lock:
                invalid_marks[aid] = reason
            # Soft-disable immediately so dead RTs leave rotation; hard delete
            # only when GROK2API_DELETE_INVALID_REFRESH=1.
            action = "disabled"
            try:
                res = mark_refresh_invalid(aid, reason=reason)
                action = str((res or {}).get("action") or "disabled")
            except Exception:
                pass
            return {
                "id": aid,
                "ok": False,
                "error": reason,
                "permanent": True,
                "reason": "refresh_invalid",
                "deleted": action == "deleted",
                "disabled": action != "deleted",
                "action": action,
            }
        except Exception as e:  # noqa: BLE001
            return {"id": aid, "ok": False, "error": str(e)[:300]}

    workers = max(1, min(int(max_workers or 1), max(1, len(candidates))))
    if candidates:
        try:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="tok-refresh-"
            ) as ex:
                futs = [ex.submit(_refresh_one, c) for c in candidates]
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception as e:  # noqa: BLE001
                        results.append({"id": "?", "ok": False, "error": str(e)[:300]})
        finally:
            with _clients_lock:
                for client in _clients:
                    try:
                        if not client.is_closed:
                            client.close()
                    except Exception:
                        pass
                _clients.clear()

    # Permanent refresh failures: default soft-disable (mark invalid + leave pool).
    # Hard-delete only when GROK2API_DELETE_INVALID_REFRESH=1 (handled inside
    # mark_refresh_invalid already). Temporary failures keep the account.
    disabled_ids: list[str] = []
    deleted_ids: list[str] = []
    deleted_reasons: dict[str, str] = {}
    if invalid_marks:
        hard = _hard_delete_invalid_refresh_enabled()
        for aid, reason in invalid_marks.items():
            deleted_reasons[aid] = reason
            if hard:
                deleted_ids.append(aid)
            else:
                disabled_ids.append(aid)
            for r in results:
                if r.get("id") == aid and (
                    r.get("permanent") or r.get("reason") == "refresh_invalid"
                ):
                    if hard:
                        r["deleted"] = True
                        r["disabled"] = False
                        r["reason"] = "refresh_invalid_deleted"
                        r["action"] = "deleted"
                    else:
                        r["deleted"] = False
                        r["disabled"] = True
                        r["reason"] = "refresh_invalid_disabled"
                        r["action"] = "disabled"
                    r["error"] = (
                        reason or r.get("error") or "refresh_token permanently invalid"
                    )[:300]

    # Single batched write for successful refreshes (+ optional hard deletes).
    if updates or deleted_ids:
        delete_set = set(deleted_ids)

        def _apply(m: dict[str, Any]) -> None:
            for aid, entry in updates.items():
                if aid == "__delete__" or not isinstance(entry, dict):
                    continue
                if aid in delete_set:
                    continue
                # Dedupe only exact same storage user_id (never by access token —
                # colliding JWTs across accounts would wipe good ones).
                uid = entry.get("user_id") or entry.get("principal_id")
                for k in list(m.keys()):
                    if k == aid:
                        continue
                    v = m.get(k)
                    if not isinstance(v, dict):
                        continue
                    same_user = bool(
                        uid
                        and (v.get("user_id") == uid or v.get("principal_id") == uid)
                    )
                    if same_user:
                        del m[k]
                m[aid] = entry
            # Hard-delete path only (opt-in). Soft path already mutated above.
            for aid in delete_set:
                if aid in m:
                    del m[aid]

        try:
            mutate_auth_map(_apply)
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"batch write failed: {e}"[:400],
                "results": results,
                "refreshed": 0,
                "deferred": deferred,
                "attempted": len(candidates),
                "invalidated": len(invalid_marks),
                "deleted": 0,
                "disabled": 0,
            }

        if deleted_ids:
            try:
                from settings_store import get_account_pool_state, save_account_pool_state

                state = get_account_pool_state()
                changed = False
                for aid in deleted_ids:
                    if aid in state:
                        state.pop(aid, None)
                        changed = True
                if changed:
                    save_account_pool_state(state)
            except Exception:
                pass
            for aid in deleted_ids:
                try:
                    from store.pool_redis import clear_cooldown

                    clear_cooldown(aid)
                except Exception:
                    pass
            print(
                f"  [token-refresh] HARD-deleted {len(deleted_ids)} account(s) "
                f"with permanently invalid refresh_token "
                f"(GROK2API_DELETE_INVALID_REFRESH=1)",
                flush=True,
            )
        if disabled_ids:
            print(
                f"  [token-refresh] soft-disabled {len(disabled_ids)} account(s) "
                f"with permanently invalid refresh_token",
                flush=True,
            )

    # Mark attempted accounts as covered for this sweep generation (success or fail).
    # Permanent invalids are also covered so they don't monopolize every cycle;
    # a new generation starts only after the rest of the need-refresh set had a turn.
    if strict_sweep and candidates:
        tried = [aid for aid, _ in candidates]
        covered_total = _refresh_sweep_mark(tried)
        if sweep_info is not None:
            sweep_info["covered"] = covered_total
            need_n = int(sweep_info.get("need_refresh") or 0)
            if need_n:
                sweep_info["remaining"] = max(0, need_n - covered_total)
                deferred = int(sweep_info["remaining"])

    out = {
        "ok": True,
        "results": results,
        "refreshed": sum(1 for r in results if r.get("ok") and not r.get("skipped")),
        "deferred": deferred,
        "attempted": len(candidates),
        "workers": workers,
        "invalidated": len(invalid_marks),
        "deleted": len(deleted_ids),
        "disabled": len(disabled_ids),
        "deleted_ids": deleted_ids[:50],
        "disabled_ids": disabled_ids[:50],
    }
    if deleted_reasons:
        sample_ids = (deleted_ids or disabled_ids)[:5]
        out["invalid_sample"] = [
            {"id": aid, "reason": (deleted_reasons.get(aid) or "")[:160]}
            for aid in sample_ids
        ]
        # Keep deleted_sample for older admin UIs when hard-delete is on.
        if deleted_ids:
            out["deleted_sample"] = [
                {"id": aid, "reason": (deleted_reasons.get(aid) or "")[:160]}
                for aid in deleted_ids[:5]
            ]
    if sweep_info is not None:
        out["sweep"] = sweep_info
    if wanted is not None:
        out["selected"] = len(wanted)
    return out


def purge_refresh_invalid_accounts(
    *,
    dry_run: bool = False,
    hard_delete: bool | None = None,
) -> dict[str, Any]:
    """Handle permanently unusable accounts.

    Default (safe): soft-disable only
      - mark ``refresh_invalid``
      - remove from rotation (enabled=False)
      - keep credentials so a false positive can be re-enabled / re-imported

    Hard delete only when ``hard_delete=True`` or
    ``GROK2API_DELETE_INVALID_REFRESH=1``.

    Targets:
      1. accounts already marked ``refresh_invalid``
      2. accounts with neither refresh_token nor access token
      3. accounts with no refresh_token whose access token is already expired
    """
    data = read_auth_map()
    doomed: list[tuple[str, str]] = []
    now = time.time()
    for aid, entry in list(data.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("refresh_invalid"):
            doomed.append(
                (
                    aid,
                    str(entry.get("refresh_invalid_reason") or "refresh_invalid")[:300],
                )
            )
            continue

        has_rt = bool(entry.get("refresh_token"))
        token = entry.get("key") if isinstance(entry.get("key"), str) else None
        has_access = bool(token)
        if not has_rt and not has_access:
            doomed.append((aid, "no_refresh_token_and_no_access_token"))
            continue
        if not has_rt:
            # No RT: if access is already expired (or cannot be parsed as live),
            # this account can never be renewed.
            exp = parse_expires_at(entry.get("expires_at"), token)
            if exp is None:
                if not has_access:
                    doomed.append((aid, "no_refresh_token_and_no_expiry"))
                continue
            if float(exp) <= now:
                doomed.append((aid, "no_refresh_token_and_access_expired"))

    do_hard = _hard_delete_invalid_refresh_enabled() if hard_delete is None else bool(hard_delete)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_delete": len(doomed) if do_hard else 0,
            "would_disable": 0 if do_hard else len(doomed),
            "hard_delete": do_hard,
            "ids": [a for a, _ in doomed[:100]],
            "sample": [{"id": a, "reason": r[:160]} for a, r in doomed[:5]],
        }
    if not doomed:
        return {
            "ok": True,
            "deleted": 0,
            "disabled": 0,
            "ids": [],
            "sample": [],
            "hard_delete": do_hard,
        }

    ids = [a for a, _ in doomed]
    by_reason: dict[str, int] = {}
    for _aid, reason in doomed:
        key = str(reason or "unknown").split(":")[0][:64]
        by_reason[key] = by_reason.get(key, 0) + 1

    if do_hard:
        try:
            from accounts import remove_accounts

            result = remove_accounts(ids)
            removed = list(result.get("removed") or ids)
        except Exception:
            def _apply(m: dict[str, Any]) -> None:
                for aid in ids:
                    m.pop(aid, None)

            mutate_auth_map(_apply)
            removed = ids

        try:
            from settings_store import get_account_pool_state, save_account_pool_state

            state = get_account_pool_state()
            changed = False
            for aid in removed:
                if aid in state:
                    state.pop(aid, None)
                    changed = True
            if changed:
                save_account_pool_state(state)
        except Exception:
            pass
        for aid in removed:
            try:
                from store.pool_redis import clear_cooldown

                clear_cooldown(aid)
            except Exception:
                pass
        print(
            f"  [token-refresh] HARD-purged {len(removed)} permanently invalid account(s)"
            + (f" reasons={by_reason}" if by_reason else ""),
            flush=True,
        )
        return {
            "ok": True,
            "deleted": len(removed),
            "disabled": 0,
            "ids": removed[:100],
            "sample": [{"id": a, "reason": r[:160]} for a, r in doomed[:5]],
            "by_reason": by_reason,
            "hard_delete": True,
            "action": "deleted",
        }

    # Soft path: mark + disable, keep credentials.
    disabled = 0
    for aid, reason in doomed:
        try:
            res = mark_refresh_invalid(aid, reason=reason, hard_delete=False)
            if res.get("ok"):
                disabled += 1
        except Exception:
            continue
    print(
        f"  [token-refresh] soft-disabled {disabled} permanently invalid account(s)"
        + (f" reasons={by_reason}" if by_reason else ""),
        flush=True,
    )
    return {
        "ok": True,
        "deleted": 0,
        "disabled": disabled,
        "ids": ids[:100],
        "sample": [{"id": a, "reason": r[:160]} for a, r in doomed[:5]],
        "by_reason": by_reason,
        "hard_delete": False,
        "action": "disabled",
    }
