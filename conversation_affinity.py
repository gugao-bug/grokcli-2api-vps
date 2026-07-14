"""Conversation → account sticky affinity.

Keeps multi-turn chats on the **same Grok account** so rotating the pool
(round_robin / least_used / random) does not interrupt prior memory mid-chat.

Fingerprint priority (callers may re-order for Responses):
  1. Explicit conversation id (header or body `conversation_id` / metadata)
  2. OpenAI ``prompt_cache_key`` (alone — do not fold message root)
  3. Responses ``previous_response_id`` chain → linked session_fp + account
  4. OpenAI `user` + conversation root
  5. Stable hash of conversation root (first user + weak system salt)

When REDIS_URL is set (production hybrid), bindings live in Redis (TTL keys)
so multi-worker processes share sticky sessions. No affinity.json is written.

affinity.json is only a single-process file-mode fallback when Redis is off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    """Load affinity.json only for file-mode fallback (Redis off)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if _redis_mode():
        return
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
                **(
                    {"session_fp": str(v["session_fp"])}
                    if v.get("session_fp")
                    else {}
                ),
            }
    except Exception:
        pass


def _schedule_flush_locked() -> None:
    global _dirty, _last_flush
    if _redis_mode():
        return
    _dirty = True
    now = time.time()
    if now - _last_flush >= _flush_interval():
        _flush_locked()


def _flush_locked() -> None:
    """Persist in-memory map to affinity.json (file-mode only)."""
    global _dirty, _last_flush
    if _redis_mode():
        _dirty = False
        return
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
    if _redis_mode():
        return
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
    Stable id for one multi-turn chat. Same sticky identity → same fingerprint
    across turns; different chats → new id.

    Priority for sticky identity:
      1. conversation_id (explicit client session / chat id)
      2. prompt_cache_key (OpenAI / sub2api / Claude Code cache sticky key)
      3. user + conversation root
      4. conversation root alone

    When ``prompt_cache_key`` is present it is used *alone* (plus optional
    api_key_id). We intentionally do **not** fold conversation root into the
    fingerprint: Responses / partial-history clients change root every turn,
    and mixing root would break account stickiness that prompt caching needs.
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
        # Stable cache key is the multi-turn identity. Do not mix message root —
        # partial histories / Responses input would change it every turn.
        parts.append(f"pck:{pck}")
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


def response_chain_fingerprint(
    response_id: str | None,
    *,
    api_key_id: str | None = None,
) -> str | None:
    """Sticky key for OpenAI Responses ``previous_response_id`` chains.

    Each Responses turn mints a new response_id. Binding the *emitted*
    response_id → account (+ optional session_fp) lets the next turn's
    previous_response_id pin the same multi-turn identity even when
    prompt_cache_key / full history root are absent or unstable.
    """
    if not _enabled():
        return None
    rid = (response_id or "").strip()
    if not rid:
        return None
    parts: list[str] = []
    if api_key_id:
        parts.append(f"key:{api_key_id}")
    parts.append(f"resp:{rid}")
    return "fp:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# Lines Claude Code / agent harnesses often rewrite every turn — strip for root.
_VOLATILE_SYSTEM_LINE = re.compile(
    r"(?i)^("
    r"current\s+date|today'?s\s+date|date\s*:|time\s*:|"
    r"cwd\s*:|working\s+directory|present\s+working\s+directory|"
    r"git\s+status|git\s+branch|branch\s*:|"
    r"model\s*:|session\s*id\s*:|"
    r"\d{4}-\d{2}-\d{2}([t\s]\d{2}:\d{2})?"  # bare ISO dates
    r")"
)


def _stable_system_salt(text: str) -> str:
    """Weak, churn-resistant salt from system text (not full identity)."""
    if not text:
        return ""
    keep: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _VOLATILE_SYSTEM_LINE.search(line):
            continue
        # Skip pure absolute paths / file lists that agents re-dump.
        if line.startswith("/") and " " not in line[:4]:
            continue
        keep.append(line[:160])
        if len(keep) >= 24:
            break
    joined = "\n".join(keep)
    if not joined:
        # Fall back to a short head hash so empty-after-strip still salts a bit.
        head = re.sub(r"\s+", " ", text).strip()[:240]
        if not head:
            return ""
        return hashlib.sha256(head.encode("utf-8")).hexdigest()[:12]
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _conversation_root(messages: list[Any] | None) -> str:
    """Root identity of a chat for affinity when no explicit session key exists.

    Claude Code / agent harnesses often rewrite large system blocks every turn
    (date, cwd, git). Using the full system text as identity shatters stickiness.

    Strategy:
      - Primary: first user message (stable for the whole chat)
      - Secondary: weak system salt (volatile lines stripped, then hashed)
      - Later assistant/tool turns never affect the root
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
    sys_salt = _stable_system_salt("\n".join(system_parts))
    if first_user is not None:
        if sys_salt:
            return f"user:{first_user}|sys:{sys_salt}"
        return f"user:{first_user}"
    if system_parts:
        return f"sys:{sys_salt or _stable_system_salt(system_parts[0])}"
    # tool-only / truncated history: use first few messages as weak root
    chunks: list[str] = []
    for m in messages[:3]:
        role, content = _msg_role_content(m)
        if content or role:
            chunks.append(f"{role}:{content[:800]}")
    return "prefix:" + "\n".join(chunks)


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


def get_affinity_entry(fingerprint: str | None) -> dict[str, Any] | None:
    """Return full affinity entry ``{account_id, session_fp?, hits, ...}`` or None."""
    if not fingerprint or not _enabled():
        return None
    if _redis_mode():
        try:
            from store import affinity_redis

            entry = affinity_redis.get_entry(fingerprint, ttl_sec=_ttl())
            if isinstance(entry, dict) and entry.get("account_id"):
                return entry
            return None
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
        out = {
            "account_id": str(aid),
            "hits": int(entry.get("hits") or 0),
            "bound_at": entry.get("bound_at"),
            "last_seen": entry.get("last_seen"),
        }
        sfp = entry.get("session_fp")
        if sfp:
            out["session_fp"] = str(sfp)
        return out


def get_affinity(fingerprint: str | None) -> str | None:
    """Return bound account_id if still valid."""
    entry = get_affinity_entry(fingerprint)
    if not entry:
        return None
    aid = entry.get("account_id")
    return str(aid) if aid else None


def bind_affinity(
    fingerprint: str | None,
    account_id: str | None,
    *,
    session_fp: str | None = None,
) -> None:
    """Pin conversation fingerprint to account after successful use.

    Optional ``session_fp`` is stored on the entry (used by response-chain
    links so later turns recover the multi-turn identity, not just the account).
    """
    if not fingerprint or not account_id or not _enabled():
        return
    sfp = (session_fp or "").strip() or None
    if _redis_mode():
        try:
            from store import affinity_redis

            affinity_redis.bind(
                fingerprint, account_id, ttl_sec=_ttl(), session_fp=sfp
            )
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
            if sfp:
                prev["session_fp"] = sfp
            elif not prev.get("session_fp") and fingerprint.startswith("fp:"):
                # Self-link when binding a session identity entry.
                prev.setdefault("session_fp", fingerprint)
            _schedule_flush_locked()
            return
        entry = {
            "account_id": account_id,
            "bound_at": now,
            "last_seen": now,
            "hits": 1 if not prev else int(prev.get("hits") or 0) + 1,
        }
        if sfp:
            entry["session_fp"] = sfp
        elif prev and prev.get("session_fp"):
            entry["session_fp"] = prev.get("session_fp")
        _map[fingerprint] = entry
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
    fingerprint: str | None,
    failed_account_id: str | None,
    new_account_id: str | None,
    *,
    session_fp: str | None = None,
) -> None:
    """
    Sticky account failed; rebind so later turns stay on the account that worked.
    """
    if not fingerprint or not new_account_id:
        return
    # Preserve session_fp across failover when present.
    sfp = (session_fp or "").strip() or None
    if not sfp:
        entry = get_affinity_entry(fingerprint)
        if entry and entry.get("session_fp"):
            sfp = str(entry["session_fp"])
    if _redis_mode():
        # In Redis mode, always rebind to the account that worked.
        cur = get_affinity(fingerprint)
        if cur and failed_account_id and cur != failed_account_id:
            return
        bind_affinity(fingerprint, new_account_id, session_fp=sfp)
        return
    with _lock:
        _ensure_loaded()
        entry = _map.get(fingerprint)
        if entry and failed_account_id and entry.get("account_id") != failed_account_id:
            return
    bind_affinity(fingerprint, new_account_id, session_fp=sfp)


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
                "persist_file": None,
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
                    "session_fp": (
                        (str(v.get("session_fp") or "")[:16] + "…")
                        if v.get("session_fp")
                        else None
                    ),
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


def extract_prompt_cache_key_from_headers(headers: Any) -> str | None:
    """Optional client prompt-cache sticky key from request headers."""
    if headers is None:
        return None
    try:
        get = headers.get
    except Exception:
        return None
    for name in (
        "x-prompt-cache-key",
        "x-openai-prompt-cache-key",
        "x-grok2api-prompt-cache-key",
        "x-cache-key",
    ):
        v = get(name)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("null", "none", "undefined"):
            return s[:240]
    return None


def bind_response_chain(
    response_id: str | None,
    account_id: str | None,
    *,
    api_key_id: str | None = None,
    session_fp: str | None = None,
) -> None:
    """Pin an emitted Responses id so the next previous_response_id sticks.

    Also stores ``session_fp`` (the multi-turn conversation fingerprint) so the
    next turn can recover a stable sticky identity even when message roots
    churn. The session_fp entry itself is also refreshed on the account.
    """
    if not response_id or not account_id:
        return
    chain_fp = response_chain_fingerprint(response_id, api_key_id=api_key_id)
    if not chain_fp:
        return
    sfp = (session_fp or "").strip() or None
    # Link response_id → account + session_fp.
    bind_affinity(chain_fp, account_id, session_fp=sfp)
    # Keep the stable session identity warm so direct get_affinity(session_fp) works.
    if sfp:
        bind_affinity(sfp, account_id, session_fp=sfp)


def get_response_chain_affinity(
    previous_response_id: str | None,
    *,
    api_key_id: str | None = None,
) -> str | None:
    """Lookup account bound to a previous Responses id."""
    entry = get_response_chain_entry(
        previous_response_id, api_key_id=api_key_id
    )
    if not entry:
        return None
    aid = entry.get("account_id")
    return str(aid) if aid else None


def get_response_chain_entry(
    previous_response_id: str | None,
    *,
    api_key_id: str | None = None,
) -> dict[str, Any] | None:
    """Lookup full entry for a previous Responses id (account + session_fp)."""
    fp = response_chain_fingerprint(previous_response_id, api_key_id=api_key_id)
    return get_affinity_entry(fp) if fp else None

def resolve_responses_affinity(
    messages: list[Any] | None,
    *,
    user: str | None = None,
    conversation_id: str | None = None,
    api_key_id: str | None = None,
    prompt_cache_key: str | None = None,
    previous_response_id: str | None = None,
) -> tuple[str | None, str | None, str]:
    """Resolve sticky (session_fp, prefer_account, source) for Responses turns.

    Priority:
      1. explicit conversation_id
      2. prompt_cache_key
      3. previous_response_id chain (account + linked session_fp)
      4. user / message root fingerprint

    When a previous_response_id hits, the returned session_fp is the *linked*
    multi-turn identity (not the per-response chain key and not a fresh root
    hash). Callers must bind both session_fp and the newly emitted response_id
    with that same session_fp so the chain continues.
    """
    if not _enabled():
        return None, None, "disabled"

    # 1–2 / 4: ordinary conversation fingerprint (cid / pck / root).
    base_fp = conversation_fingerprint(
        messages,
        user=user,
        conversation_id=conversation_id,
        api_key_id=api_key_id,
        prompt_cache_key=prompt_cache_key,
    )

    # Prefer explicit cid / pck — they are already stable multi-turn keys.
    if conversation_id and str(conversation_id).strip() and base_fp:
        prefer = get_affinity(base_fp)
        return base_fp, prefer, "conversation_id" if prefer else "conversation_id_new"
    if prompt_cache_key and str(prompt_cache_key).strip() and base_fp:
        prefer = get_affinity(base_fp)
        return base_fp, prefer, "prompt_cache_key" if prefer else "prompt_cache_key_new"

    # 3. previous_response_id chain — recover linked session_fp when present.
    prev = (previous_response_id or "").strip()
    if prev:
        entry = get_response_chain_entry(prev, api_key_id=api_key_id)
        if entry and entry.get("account_id"):
            account = str(entry["account_id"])
            linked = (entry.get("session_fp") or "").strip() or None
            if linked:
                # Touch the session identity entry as well.
                get_affinity(linked)  # refresh TTL/hits when present
                return linked, account, "previous_response_id"
            # Legacy chain entry without session_fp: still stick the account,
            # but adopt base_fp (or a synthetic chain-derived session) going forward.
            session = base_fp or response_chain_fingerprint(prev, api_key_id=api_key_id)
            return session, account, "previous_response_id_legacy"

    # 4. root / user fingerprint (may be weak under Claude Code system churn).
    if base_fp:
        prefer = get_affinity(base_fp)
        return base_fp, prefer, "root" if prefer else "root_new"
    return None, None, "none"
