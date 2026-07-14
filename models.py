"""Model list helpers + upstream sync (PostgreSQL catalog only)."""

from __future__ import annotations

import time
from typing import Any

from config import (
    CLI_VERSION,
    CLIENT_IDENTIFIER,
    CLIENT_SURFACE,
    CLIENT_USER_AGENT,
    DEFAULT_MODEL,
    MODEL_ALIASES,
    UPSTREAM_BASE,
)


# Always-exposed local catalog entries. Upstream /v1/models often only returns
# grok-4.5, but cli-chat-proxy still accepts grok-build (and local aliases).
_EXTRA_MODELS: list[dict[str, Any]] = [
    {
        "id": "grok-build",
        "name": "Grok Build",
        "description": "Grok coding / build model (cli-chat-proxy)",
        "owned_by": "xai",
    },
    {
        "id": "grok-search",
        "name": "Grok Search",
        "description": "Grok with web search enabled (local alias)",
        "owned_by": "xai",
    },
]


def resolve_model(model: str | None) -> str:
    if not model:
        return DEFAULT_MODEL
    m = model.strip()
    # grok-search always routes to default model with web search enabled
    if m.lower() in ("grok-search", "web-search"):
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(m, MODEL_ALIASES.get(m.lower(), m))


def _sort_key(m: dict[str, Any]) -> tuple:
    mid = str(m.get("id") or "")
    if mid == DEFAULT_MODEL:
        return (0, mid)
    if mid == "grok-build":
        return (1, mid)
    return (2, mid)


def _sort_order_for(mid: str) -> int:
    if mid == DEFAULT_MODEL:
        return 0
    if mid == "grok-build":
        return 1
    if mid == "grok-search":
        return 2
    return 100


def _extra_model_entries(now: int | None = None) -> list[dict[str, Any]]:
    ts = int(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    for item in _EXTRA_MODELS:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        entry = {
            "id": mid,
            "object": "model",
            "created": ts,
            "owned_by": item.get("owned_by") or "xai",
            "synthetic": True,
            "sort_order": _sort_order_for(mid),
        }
        if item.get("name"):
            entry["name"] = item["name"]
        if item.get("description"):
            entry["description"] = item["description"]
        out.append(entry)
    return out


def _merge_extra_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure built-in extras stay visible even after upstream sync."""
    have = {
        str(m.get("id") or "").strip().lower()
        for m in models
        if isinstance(m, dict) and m.get("id")
    }
    for extra in _extra_model_entries():
        mid = str(extra.get("id") or "").strip().lower()
        if mid and mid not in have:
            models.append(extra)
            have.add(mid)
    return models


def _public_model_entry(rec: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    """Normalize a catalog record into the OpenAI-style list item."""
    ts = int(now if now is not None else time.time())
    mid = str(rec.get("id") or "").strip()
    entry: dict[str, Any] = {
        "id": mid,
        "object": "model",
        "created": ts,
        "owned_by": rec.get("owned_by") or "xai",
    }
    if rec.get("name"):
        entry["name"] = rec["name"]
    if rec.get("description"):
        entry["description"] = rec["description"]
    if rec.get("context_window") is not None:
        entry["context_window"] = rec["context_window"]
    if rec.get("supports_reasoning_effort") is not None:
        entry["supports_reasoning_effort"] = rec["supports_reasoning_effort"]
    for k in (
        "max_completion_tokens",
        "reasoning_effort",
        "reasoning_efforts",
        "auto_compact_threshold_percent",
        "supported_in_api",
    ):
        if rec.get(k) is not None:
            entry[k] = rec[k]
        else:
            extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
            if extra.get(k) is not None:
                entry[k] = extra[k]
    return entry


def _fallback_models() -> list[dict[str, Any]]:
    """In-memory fallback when PostgreSQL is unavailable (should not happen in prod)."""
    now = int(time.time())
    models = [
        {
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": now,
            "owned_by": "xai",
        },
    ]
    models = _merge_extra_models(models)
    models.sort(key=_sort_key)
    return models


def _load_models_from_pg() -> list[dict[str, Any]] | None:
    """Return public model list from PG, or None when PG is unavailable/empty."""
    try:
        from store import models_pg
    except Exception:
        return None
    if not models_pg.enabled():
        return None
    try:
        rows = models_pg.list_models(include_hidden=False)
    except Exception:
        return None
    if not rows:
        return None
    now = int(time.time())
    models = [_public_model_entry(r, now=now) for r in rows if r.get("id")]
    models = _merge_extra_models(models)
    models.sort(key=_sort_key)
    return models


def load_models_from_cache(path: Any = None) -> list[dict[str, Any]]:
    """Load the public model catalog from PostgreSQL.

    ``path`` is accepted for backward-compatible call sites and ignored.
    Preference order:
      1. PostgreSQL ``models`` table
      2. In-memory DEFAULT_MODEL + local extras (only if PG is down/empty)
    """
    _ = path  # legacy arg; catalog no longer uses models_cache.json
    pg_models = _load_models_from_pg()
    if pg_models is not None:
        return pg_models
    return _fallback_models()


def _upstream_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-surface": CLIENT_SURFACE,
        "x-grok-client-identifier": CLIENT_IDENTIFIER,
        "User-Agent": CLIENT_USER_AGENT,
        "Accept": "application/json",
    }


def _items_from_upstream_payload(data_list: list[Any], *, fetched_at: Any = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in data_list:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("model")
        if not mid:
            continue
        model_id = str(mid).strip()
        if not model_id:
            continue
        rec: dict[str, Any] = {
            "id": model_id,
            "name": item.get("name") or model_id,
            "description": item.get("description"),
            "owned_by": item.get("owned_by") or "xai",
            "hidden": bool(item.get("hidden")),
            "synthetic": False,
            "context_window": item.get("context_window"),
            "supports_reasoning_effort": item.get("supports_reasoning_effort"),
            "model": item.get("model") or model_id,
            "sort_order": _sort_order_for(model_id),
            "fetched_at": fetched_at,
        }
        for k in (
            "max_completion_tokens",
            "reasoning_effort",
            "reasoning_efforts",
            "auto_compact_threshold_percent",
            "supported_in_api",
        ):
            if item.get(k) is not None:
                rec[k] = item[k]
        items.append(rec)
    return items


def _extra_items_for_pg(*, fetched_at: Any = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _EXTRA_MODELS:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        items.append(
            {
                "id": mid,
                "name": item.get("name") or mid,
                "description": item.get("description"),
                "owned_by": item.get("owned_by") or "xai",
                "hidden": False,
                "synthetic": True,
                "model": mid,
                "sort_order": _sort_order_for(mid),
                "fetched_at": fetched_at,
            }
        )
    return items


def _persist_items_to_pg(
    items: list[dict[str, Any]],
    *,
    origin: str | None = None,
    fetched_via: str | None = None,
    fetched_at_iso: str | None = None,
) -> dict[str, Any]:
    """Write catalog into PostgreSQL. Returns ok/error payload."""
    try:
        from store import models_pg
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"import models_pg: {e}"}
    if not models_pg.enabled():
        return {"ok": False, "error": "postgres unavailable (DATABASE_URL / models table)"}
    try:
        now = time.time()
        meta = {
            "fetched_at": fetched_at_iso
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "synced_at": now,
            "grok_version": CLI_VERSION,
            "auth_method": "session",
            "origin": origin,
            "fetched_via": fetched_via,
            "source": "upstream",
        }
        # Always keep local synthetic extras alongside upstream rows.
        have = {
            str(i.get("id") or "").strip().lower()
            for i in items
            if isinstance(i, dict) and i.get("id")
        }
        merged = list(items)
        for extra in _extra_items_for_pg(fetched_at=meta["fetched_at"]):
            mid = str(extra.get("id") or "").strip().lower()
            if mid and mid not in have:
                merged.append(extra)
                have.add(mid)
        n = models_pg.replace_all(merged, meta=meta, keep_synthetic=True)
        return {"ok": True, "count": n}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def sync_models_from_upstream(path: Any = None) -> dict[str, Any]:
    """
    GET cli-chat-proxy /v1/models and persist into PostgreSQL.
    Uses any live pool account.
    """
    import httpx

    from auth import AuthError
    import account_pool

    _ = path  # legacy arg; no longer writes models_cache.json
    try:
        creds = account_pool.acquire()
    except AuthError as e:
        return {"ok": False, "error": str(e)}

    url = f"{UPSTREAM_BASE}/models"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=_upstream_headers(creds.token))
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network: {e}"}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"upstream {resp.status_code}: {(resp.text or '')[:300]}",
            "status_code": resp.status_code,
        }

    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"parse: {e}"}

    data_list = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data_list, list):
        return {"ok": False, "error": "unexpected models payload"}

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    items = _items_from_upstream_payload(data_list, fetched_at=fetched_at)
    if not items:
        return {"ok": False, "error": "no models in upstream response"}

    pg_result = _persist_items_to_pg(
        items,
        origin=url,
        fetched_via=creds.email or creds.auth_key,
        fetched_at_iso=fetched_at,
    )
    if not pg_result.get("ok"):
        return {
            "ok": False,
            "error": pg_result.get("error") or "postgres write failed",
        }

    models = load_models_from_cache()
    return {
        "ok": True,
        "count": len(models),
        "pg_count": pg_result.get("count"),
        "fetched_via": creds.email or creds.auth_key,
        "models": models,
        "storage": "postgres",
        "origin": url,
    }


def ensure_models_catalog_seeded() -> dict[str, Any]:
    """Startup helper: seed PostgreSQL with baseline extras when table is empty."""
    try:
        from store import models_pg
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not models_pg.enabled():
        return {"ok": False, "error": "pg disabled"}
    try:
        n = models_pg.count(include_hidden=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if n > 0:
        return {"ok": True, "seeded": False, "count": n}

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    baseline: list[dict[str, Any]] = [
        {
            "id": DEFAULT_MODEL,
            "name": DEFAULT_MODEL,
            "owned_by": "xai",
            "hidden": False,
            "synthetic": False,
            "model": DEFAULT_MODEL,
            "sort_order": _sort_order_for(DEFAULT_MODEL),
            "fetched_at": fetched_at,
        }
    ]
    baseline.extend(_extra_items_for_pg(fetched_at=fetched_at))
    result = _persist_items_to_pg(
        baseline,
        origin="seed:baseline",
        fetched_at_iso=fetched_at,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "seed failed"}
    try:
        n2 = models_pg.count(include_hidden=True)
    except Exception:
        n2 = int(result.get("count") or 0)
    return {"ok": True, "seeded": True, "count": n2}
