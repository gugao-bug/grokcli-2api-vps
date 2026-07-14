"""Redis-backed conversation affinity (TTL keys)."""

from __future__ import annotations

import json
import time
from typing import Any

from store.redis_client import delete, get_str, key, redis_enabled, set_ex


def _k(fp: str) -> str:
    return key("affinity", fp)


def get(fingerprint: str, *, ttl_sec: float) -> str | None:
    if not redis_enabled() or not fingerprint:
        return None
    raw = get_str(_k(fingerprint))
    if not raw:
        return None
    account_id: str | None = None
    hits = 0
    bound_at = time.time()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            account_id = str(data.get("account_id") or "") or None
            hits = int(data.get("hits") or 0)
            bound_at = float(data.get("bound_at") or bound_at)
        else:
            account_id = str(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        account_id = str(raw)
    if not account_id:
        return None
    # touch: refresh TTL + hits
    payload = {
        "account_id": account_id,
        "bound_at": bound_at,
        "last_seen": time.time(),
        "hits": hits + 1,
    }
    set_ex(_k(fingerprint), json.dumps(payload, separators=(",", ":")), ttl_sec)
    return account_id


def bind(fingerprint: str, account_id: str, *, ttl_sec: float) -> None:
    if not redis_enabled() or not fingerprint or not account_id:
        return
    now = time.time()
    prev_hits = 0
    raw = get_str(_k(fingerprint))
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                prev_hits = int(data.get("hits") or 0)
                if data.get("account_id") == account_id:
                    bound_at = float(data.get("bound_at") or now)
                    payload = {
                        "account_id": account_id,
                        "bound_at": bound_at,
                        "last_seen": now,
                        "hits": prev_hits + 1,
                    }
                    set_ex(
                        _k(fingerprint),
                        json.dumps(payload, separators=(",", ":")),
                        ttl_sec,
                    )
                    return
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    payload = {
        "account_id": account_id,
        "bound_at": now,
        "last_seen": now,
        "hits": prev_hits + 1 if prev_hits else 1,
    }
    set_ex(_k(fingerprint), json.dumps(payload, separators=(",", ":")), ttl_sec)


def clear(fingerprint: str) -> None:
    if not redis_enabled() or not fingerprint:
        return
    delete(_k(fingerprint))


def status_sample(*, max_n: int = 8) -> dict[str, Any]:
    """Best-effort sample (SCAN). Costly on huge keyspaces — keep small."""
    if not redis_enabled():
        return {"active": 0, "sample": []}
    try:
        from store.redis_client import get_client

        c = get_client()
        if c is None:
            return {"active": 0, "sample": []}
        pattern = key("affinity", "*")
        sample: list[dict[str, Any]] = []
        count = 0
        for k in c.scan_iter(match=pattern, count=50):
            count += 1
            if len(sample) >= max_n:
                continue
            raw = c.get(k)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                data = {"account_id": str(raw)}
            fp = str(k).split(":")[-1]
            sample.append(
                {
                    "fp": fp[:12] + "…",
                    "account_id": str(data.get("account_id") or "")[:48],
                    "hits": data.get("hits"),
                    "age_sec": int(
                        time.time() - float(data.get("bound_at") or time.time())
                    ),
                }
            )
        return {"active": count, "sample": sample}
    except Exception as e:  # noqa: BLE001
        return {"active": 0, "sample": [], "error": str(e)}
