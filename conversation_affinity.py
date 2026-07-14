"""Conversation → account sticky affinity.

Keeps multi-turn chats on the **same Grok account** so rotating the pool
(round_robin / least_used / random) does not interrupt prior memory mid-chat.

Fingerprint priority:
  1. Explicit conversation id (header or body `conversation_id` / metadata)
  2. OpenAI `user` + conversation root
  3. Stable hash of conversation root (system + first user message)

Bindings are kept in memory and flushed to data/affinity.json so restarts
do not drop sticky sessions within TTL.

When REDIS_URL is set, bindings live in Redis (TTL keys) so multi-worker
processes share sticky sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR

_lock = threading.RLock()
# fingerprint -> {account_id, bound_at, last_seen, hits}
_map: dict[str, dict[str, Any]] = {}
_loaded = False
_dirty = False
_last_flush = 0.0

AFFINITY_FILE = Path(os.getenv("GROK2API_AFFINITY_FILE", DATA_DIR / "affinity.json"))


def _redis_mode() -> bool:
    try:
        from store.redis_client import redis_enabled

        return redis_enabled()
    except Exception:
        return False


def _enabled() -> bool:
    try:
        from settings_store import get_conversation_affinity_enabled

        return bool(get_conversation_affinity_enabled())
    except Exception:
        pass
    return os.getenv("GROK2API_CONVERSATION_AFFINITY", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def _ttl() -> float:
    try:
        return max(60.0, float(os.getenv("GROK2API_AFFINITY_TTL", "7200")))
    except ValueError:
        return 7200.0


def _max_entries() -> int:
    try:
        return max(100, int(os.getenv("GROK2API_AFFINITY_MAX", "5000")))
    except ValueError:
        return 5000


def _flush_interval() -> float:
    try:
        return max(5.0, float(os.getenv("GROK2API_AFFINITY_FLUSH_SEC", "15")))
    except ValueError:
        return 15.0


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if not AFFINITY_FILE.is_file():
            return
        data = json.loads(AFFINITY_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return
        now = time.time()
        ttl = _ttl()
        for k, v in entries.items():
            if not isinstance(v, dict) or not v.get("account_id"):
                continue
            last = float(v.get("last_seen") or v.get("bound_at") or 0)
            if now - last > ttl:
                continue
            _map[str(k)] = {
                "account_id": str(v["account_id"]),
                "bound_at": float(v.get("bound_at") or last),
                "last_seen": last,
                "hits": int(v.get("hits") or 0),
            }
    except Exception:
        pass


def _schedule_flush_locked() -> None:
    global _dirty, _last_flush
    _dirty = True
    now = time.time()
    if now - _last_flush >= _flush_interval():
        _flush_locked()


def _flush_locked() -> None:
    global _dirty, _last_flush
    _dirty = False
    _last_flush = time.time()
    try:
        AFFINITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "ttl_sec": _ttl(),
            "entries": _map,
        }
        tmp = AFFINITY_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(AFFINITY_FILE)
    except OSError:
        _dirty = True


def flush() -> None:
    with _lock:
        _ensure_loaded()
        _flush_locked()


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif p.get("type") == "text" and isinstance(p.get("content"), str):
                    parts.append(p["content"])
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return str(content)[:500]
    return str(content)[:500]


def _msg_role_content(m: Any) -> tuple[str, str]:
    if hasattr(m, "role"):
        role = str(getattr(m, "role", "") or "")
        content = _content_text(getattr(m, "content", None))
        return role, content
    if isinstance(m, dict):
        return str(m.get("role") or ""), _content_text(m.get("content"))
    return "", ""


def conversation_fingerprint(
    messages: list[Any] | None,
    *,
    user: str | None = None,
    conversation_id: str | None = None,
    api_key_id: str | None = None,
    prompt_cache_key: str | None = None,
) -> str | None:
    """
    Stable id for one multi-turn chat. Same root messages → same fingerprint
    across turns; different chats (or new first user message) → new id.

    Priority for sticky identity:
      1. conversation_id
      2. prompt_cache_key (OpenAI / sub2api cache sticky key)
      3. user + conversation root
      4. conversation root alone
    """
    if not _enabled():
        return None

    parts: list[str] = []
    if api_key_id:
        parts.append(f"key:{api_key_id}")

    cid = (conversation_id or "").strip()
    if cid:
        parts.append(f"cid:{cid}")
        return "fp:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    pck = (prompt_cache_key or "").strip()
    if pck:
        parts.append(f"pck:{pck}")
        # Cache key alone is enough for multi-turn stickiness; still fold root
        # when present so different chats reusing a generic key don't collide.
        root = _conversation_root(messages)
        if root:
            parts.append(f"root:{root}")
        return "fp:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    u = (user or "").strip()
    if u and u.lower() not in ("user", "default", "anonymous", "string"):
        parts.append(f"user:{u}")
        root = _conversation_root(messages)
        if root:
            parts.append(f"root:{root}")
        return "fp:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    root = _conversation_root(messages)
    if not root:
        return None
    parts.append(f"root:{root}")
    return "fp:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _conversation_root(messages: list[Any] | None) -> str:
    """
    Root identity of a chat: system prompt(s) + first user message.
    Later assistant/tool turns do not change the root → affinity holds.
    """
    if not messages:
        return ""
    system_parts: list[str] = []
    first_user: str | None = None
    for m in messages:
        role, content = _msg_role_content(m)
        role_l = role.lower()
        if role_l == "system" and content:
            system_parts.append(content[:2000])
        elif role_l == "user" and content and first_user is None:
            first_user = content[:2000]
            break
    if first_user is None and not system_parts:
        # tool-only / truncated history: use first two messages as weak root
        # so multi-turn tool rounds still tend to stick
        chunks: list[str] = []
        for m in messages[:3]:
            role, content = _msg_role_content(m)
            if content or role:
                chunks.append(f"{role}:{content[:800]}")
        return "prefix:" + "\n".join(chunks)
    return "sys:" + "\n".join(system_parts) + "\nuser:" + (first_user or "")


def _purge_locked(now: float | None = None) -> None:
    now = now or time.time()
    ttl = _ttl()
    dead = [k for k, v in _map.items() if now - float(v.get("last_seen") or 0) > ttl]
    for k in dead:
        _map.pop(k, None)
    max_n = _max_entries()
    if len(_map) > max_n:
        ordered = sorted(
            _map.items(), key=lambda kv: float(kv[1].get("last_seen") or 0)
        )
        for k, _ in ordered[: len(_map) - max_n]:
            _map.pop(k, None)


def get_affinity(fingerprint: str | None) -> str | None:
    """Return bound account_id if still valid."""
    if not fingerprint or not _enabled():
        return None
    if _redis_mode():
        try:
            from store import affinity_redis

            return affinity_redis.get(fingerprint, ttl_sec=_ttl())
        except Exception:
            pass  # fall through to file map
    with _lock:
        _ensure_loaded()
        _purge_locked()
        entry = _map.get(fingerprint)
        if not entry:
            return None
        aid = entry.get("account_id")
        if not aid:
            return None
        entry["last_seen"] = time.time()
        entry["hits"] = int(entry.get("hits") or 0) + 1
        _schedule_flush_locked()
        return str(aid)


def bind_affinity(fingerprint: str | None, account_id: str | None) -> None:
    """Pin conversation fingerprint to account after successful use."""
    if not fingerprint or not account_id or not _enabled():
        return
    if _redis_mode():
        try:
            from store import affinity_redis

            affinity_redis.bind(fingerprint, account_id, ttl_sec=_ttl())
            return
        except Exception:
            pass
    now = time.time()
    with _lock:
        _ensure_loaded()
        _purge_locked(now)
        prev = _map.get(fingerprint)
        if prev and prev.get("account_id") == account_id:
            prev["last_seen"] = now
            prev["hits"] = int(prev.get("hits") or 0) + 1
            _schedule_flush_locked()
            return
        _map[fingerprint] = {
            "account_id": account_id,
            "bound_at": now,
            "last_seen": now,
            "hits": 1 if not prev else int(prev.get("hits") or 0) + 1,
        }
        _schedule_flush_locked()


def clear_affinity(fingerprint: str | None) -> None:
    if not fingerprint:
        return
    if _redis_mode():
        try:
            from store import affinity_redis

            affinity_redis.clear(fingerprint)
            return
        except Exception:
            pass
    with _lock:
        _ensure_loaded()
        if fingerprint in _map:
            _map.pop(fingerprint, None)
            _schedule_flush_locked()


def rebind_on_failover(
    fingerprint: str | None, failed_account_id: str | None, new_account_id: str | None
) -> None:
    """
    Sticky account failed; rebind so later turns stay on the account that worked.
    """
    if not fingerprint or not new_account_id:
        return
    if _redis_mode():
        # In Redis mode, always rebind to the account that worked.
        cur = get_affinity(fingerprint)
        if cur and failed_account_id and cur != failed_account_id:
            return
        bind_affinity(fingerprint, new_account_id)
        return
    with _lock:
        _ensure_loaded()
        entry = _map.get(fingerprint)
        if entry and failed_account_id and entry.get("account_id") != failed_account_id:
            return
    bind_affinity(fingerprint, new_account_id)


def status() -> dict[str, Any]:
    if _redis_mode():
        try:
            from store import affinity_redis

            sample = affinity_redis.status_sample()
            return {
                "enabled": _enabled(),
                "ttl_sec": _ttl(),
                "max_entries": _max_entries(),
                "backend": "redis",
                "active": sample.get("active", 0),
                "persist_file": str(AFFINITY_FILE),
                "sample": sample.get("sample") or [],
            }
        except Exception as e:  # noqa: BLE001
            return {
                "enabled": _enabled(),
                "backend": "redis",
                "error": str(e),
                "active": 0,
                "sample": [],
            }
    with _lock:
        _ensure_loaded()
        _purge_locked()
        return {
            "enabled": _enabled(),
            "ttl_sec": _ttl(),
            "max_entries": _max_entries(),
            "backend": "file",
            "active": len(_map),
            "persist_file": str(AFFINITY_FILE),
            "sample": [
                {
                    "fp": k[:12] + "…",
                    "account_id": (v.get("account_id") or "")[:48],
                    "hits": v.get("hits"),
                    "age_sec": int(
                        time.time() - float(v.get("bound_at") or time.time())
                    ),
                }
                for k, v in list(_map.items())[:8]
            ],
        }


def extract_conversation_id_from_headers(headers: Any) -> str | None:
    """Read optional client conversation id from request headers."""
    if headers is None:
        return None
    try:
        get = headers.get
    except Exception:
        return None
    for name in (
        "x-grok2api-conversation-id",
        "x-conversation-id",
        "x-chat-id",
        "x-session-id",
    ):
        v = get(name)
        if v and str(v).strip():
            return str(v).strip()[:200]
    return None


def extract_conversation_id_from_body(req: Any) -> str | None:
    """Body conversation_id / metadata.conversation_id (OpenAI extras)."""
    if req is None:
        return None
    for attr in ("conversation_id", "conversationId", "chat_id", "session_id"):
        v = getattr(req, attr, None)
        if v is None and isinstance(req, dict):
            v = req.get(attr)
        if v and str(v).strip():
            return str(v).strip()[:200]
    meta = getattr(req, "metadata", None)
    if meta is None and isinstance(req, dict):
        meta = req.get("metadata")
    if isinstance(meta, dict):
        for key in (
            "conversation_id",
            "conversationId",
            "chat_id",
            "session_id",
            "thread_id",
        ):
            v = meta.get(key)
            if v and str(v).strip():
                return str(v).strip()[:200]
    # pydantic extra fields
    extra = getattr(req, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("conversation_id", "conversationId", "chat_id"):
            v = extra.get(key)
            if v and str(v).strip():
                return str(v).strip()[:200]
    return None


def extract_prompt_cache_key(req: Any) -> str | None:
    """OpenAI prompt_cache_key (body / metadata / extras / headers-like attrs).

    Used for sticky affinity so multi-turn cache-oriented clients stay on one
    account even when conversation_id is absent.
    """
    if req is None:
        return None

    def _take(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("null", "none", "undefined"):
            return None
        return s[:240]

    for attr in ("prompt_cache_key", "promptCacheKey", "cache_key", "cacheKey"):
        v = getattr(req, attr, None)
        if v is None and isinstance(req, dict):
            v = req.get(attr)
        got = _take(v)
        if got:
            return got

    meta = getattr(req, "metadata", None)
    if meta is None and isinstance(req, dict):
        meta = req.get("metadata")
    if isinstance(meta, dict):
        for key in (
            "prompt_cache_key",
            "promptCacheKey",
            "cache_key",
            "cacheKey",
            "session_id",
            "sessionId",
        ):
            got = _take(meta.get(key))
            if got:
                return got

    extra = getattr(req, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("prompt_cache_key", "promptCacheKey", "cache_key", "cacheKey"):
            got = _take(extra.get(key))
            if got:
                return got
    return None
