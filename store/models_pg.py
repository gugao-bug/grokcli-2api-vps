"""PostgreSQL backend for the upstream model catalog.

Rows come from cli-chat-proxy GET /v1/models (plus local synthetic extras).
"""

from __future__ import annotations

import json
import time
from typing import Any

from store.pg import _ts, _unix, connection, json_dump, pg_enabled

META_SETTING_KEY = "models_catalog_meta"


def enabled() -> bool:
    return pg_enabled()


def _parse_extra(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return dict(val)
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _row_to_record(r: tuple[Any, ...]) -> dict[str, Any]:
    # id, name, description, owned_by, hidden, synthetic, context_window,
    # supports_reasoning_effort, extra, sort_order, fetched_at, updated_at
    extra = _parse_extra(r[8])
    rec: dict[str, Any] = {
        "id": r[0],
        "name": r[1],
        "description": r[2],
        "owned_by": r[3] or "xai",
        "hidden": bool(r[4]),
        "synthetic": bool(r[5]),
        "context_window": r[6],
        "supports_reasoning_effort": r[7],
        "extra": extra,
        "sort_order": int(r[9] or 100),
        "fetched_at": _unix(r[10]),
        "updated_at": _unix(r[11]),
    }
    # Flatten a few commonly exposed fields from extra for OpenAI-style lists.
    for k in (
        "max_completion_tokens",
        "reasoning_effort",
        "reasoning_efforts",
        "auto_compact_threshold_percent",
        "supported_in_api",
        "model",
    ):
        if k in extra and k not in rec:
            rec[k] = extra[k]
    return rec


def count(*, include_hidden: bool = True) -> int:
    if not enabled():
        return 0
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                if include_hidden:
                    cur.execute("SELECT COUNT(*) FROM models")
                else:
                    cur.execute("SELECT COUNT(*) FROM models WHERE hidden = false")
                row = cur.fetchone()
        return int((row[0] if row else 0) or 0)
    except Exception:
        return 0


def list_models(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    """Return catalog rows ordered by sort_order, id."""
    if not enabled():
        return []
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                if include_hidden:
                    cur.execute(
                        """
                        SELECT id, name, description, owned_by, hidden, synthetic,
                               context_window, supports_reasoning_effort, extra,
                               sort_order, fetched_at, updated_at
                        FROM models
                        ORDER BY sort_order ASC, id ASC
                        """
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, name, description, owned_by, hidden, synthetic,
                               context_window, supports_reasoning_effort, extra,
                               sort_order, fetched_at, updated_at
                        FROM models
                        WHERE hidden = false
                        ORDER BY sort_order ASC, id ASC
                        """
                    )
                rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]
    except Exception:
        return []


def get_meta() -> dict[str, Any]:
    if not enabled():
        return {}
    try:
        from store.settings_pg import get_setting

        raw = get_setting(META_SETTING_KEY, None)
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def set_meta(meta: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        from store.settings_pg import set_setting

        set_setting(META_SETTING_KEY, dict(meta or {}))
    except Exception:
        pass


def _normalize_record(item: dict[str, Any], *, default_fetched_at: Any = None) -> dict[str, Any] | None:
    info_early = item.get("info") if isinstance(item.get("info"), dict) else None
    mid = str(
        item.get("id")
        or item.get("model")
        or ((info_early or {}).get("id") if info_early else None)
        or ((info_early or {}).get("model") if info_early else None)
        or ""
    ).strip()
    if not mid:
        return None

    # Prefer explicit extra dict; otherwise pack remaining known fields.
    extra_in = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    extra = dict(extra_in)
    for k in (
        "max_completion_tokens",
        "reasoning_effort",
        "reasoning_efforts",
        "auto_compact_threshold_percent",
        "supported_in_api",
        "model",
        "api_key",
        "env_key",
    ):
        if item.get(k) is not None and k not in extra:
            extra[k] = item[k]

    # Nested info blob from models_cache.json shape.
    info = info_early
    if info:
        mid = str(info.get("id") or info.get("model") or mid).strip() or mid
        name = info.get("name") or item.get("name") or mid
        description = info.get("description") if info.get("description") is not None else item.get("description")
        owned_by = info.get("owned_by") or item.get("owned_by") or "xai"
        hidden = bool(info.get("hidden")) if "hidden" in info else bool(item.get("hidden"))
        synthetic = bool(info.get("synthetic")) if "synthetic" in info else bool(item.get("synthetic"))
        context_window = info.get("context_window", item.get("context_window"))
        supports_reasoning_effort = info.get(
            "supports_reasoning_effort", item.get("supports_reasoning_effort")
        )
        for k in (
            "max_completion_tokens",
            "reasoning_effort",
            "reasoning_efforts",
            "auto_compact_threshold_percent",
            "supported_in_api",
            "model",
        ):
            if info.get(k) is not None:
                extra[k] = info[k]
    else:
        name = item.get("name") or mid
        description = item.get("description")
        owned_by = item.get("owned_by") or "xai"
        hidden = bool(item.get("hidden"))
        synthetic = bool(item.get("synthetic"))
        context_window = item.get("context_window")
        supports_reasoning_effort = item.get("supports_reasoning_effort")

    try:
        sort_order = int(item.get("sort_order") if item.get("sort_order") is not None else 100)
    except (TypeError, ValueError):
        sort_order = 100

    fetched_at = item.get("fetched_at", default_fetched_at)
    return {
        "id": mid,
        "name": name,
        "description": description,
        "owned_by": owned_by or "xai",
        "hidden": hidden,
        "synthetic": synthetic,
        "context_window": context_window,
        "supports_reasoning_effort": supports_reasoning_effort,
        "extra": extra,
        "sort_order": sort_order,
        "fetched_at": fetched_at,
    }


def _upsert(cur: Any, rec: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO models (
          id, name, description, owned_by, hidden, synthetic,
          context_window, supports_reasoning_effort, extra, sort_order,
          fetched_at, updated_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s,
          %s, %s, %s::jsonb, %s,
          %s, now()
        )
        ON CONFLICT (id) DO UPDATE SET
          name = EXCLUDED.name,
          description = EXCLUDED.description,
          owned_by = EXCLUDED.owned_by,
          hidden = EXCLUDED.hidden,
          synthetic = EXCLUDED.synthetic,
          context_window = EXCLUDED.context_window,
          supports_reasoning_effort = EXCLUDED.supports_reasoning_effort,
          extra = EXCLUDED.extra,
          sort_order = EXCLUDED.sort_order,
          fetched_at = COALESCE(EXCLUDED.fetched_at, models.fetched_at),
          updated_at = now()
        """,
        (
            rec["id"],
            rec.get("name"),
            rec.get("description"),
            rec.get("owned_by") or "xai",
            bool(rec.get("hidden")),
            bool(rec.get("synthetic")),
            rec.get("context_window"),
            rec.get("supports_reasoning_effort"),
            json_dump(rec.get("extra") or {}),
            int(rec.get("sort_order") or 100),
            _ts(rec.get("fetched_at")),
        ),
    )


def upsert(item: dict[str, Any]) -> bool:
    if not enabled():
        return False
    rec = _normalize_record(item)
    if not rec:
        return False
    with connection() as conn:
        with conn.cursor() as cur:
            _upsert(cur, rec)
        conn.commit()
    return True


def replace_all(
    items: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    keep_synthetic: bool = True,
) -> int:
    """Replace catalog with the given items.

    When keep_synthetic is True, synthetic local extras not present in ``items``
    are retained (so a partial upstream list cannot drop grok-build).
    """
    if not enabled():
        return 0

    now_ts = time.time()
    fetched_at = None
    if isinstance(meta, dict):
        fetched_at = meta.get("fetched_at") or meta.get("synced_at")
    if fetched_at is None:
        fetched_at = now_ts

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        rec = _normalize_record(raw, default_fetched_at=fetched_at)
        if not rec:
            continue
        mid = rec["id"]
        if mid in seen:
            continue
        seen.add(mid)
        normalized.append(rec)

    with connection() as conn:
        with conn.cursor() as cur:
            retained: list[dict[str, Any]] = []
            if keep_synthetic:
                cur.execute(
                    """
                    SELECT id, name, description, owned_by, hidden, synthetic,
                           context_window, supports_reasoning_effort, extra,
                           sort_order, fetched_at, updated_at
                    FROM models
                    WHERE synthetic = true
                    """
                )
                for r in cur.fetchall():
                    old = _row_to_record(r)
                    mid = str(old.get("id") or "")
                    if mid and mid not in seen:
                        retained.append(
                            {
                                "id": mid,
                                "name": old.get("name") or mid,
                                "description": old.get("description"),
                                "owned_by": old.get("owned_by") or "xai",
                                "hidden": bool(old.get("hidden")),
                                "synthetic": True,
                                "context_window": old.get("context_window"),
                                "supports_reasoning_effort": old.get(
                                    "supports_reasoning_effort"
                                ),
                                "extra": old.get("extra") or {},
                                "sort_order": int(old.get("sort_order") or 100),
                                "fetched_at": old.get("fetched_at") or fetched_at,
                            }
                        )
                        seen.add(mid)

            cur.execute("DELETE FROM models")
            for rec in normalized + retained:
                _upsert(cur, rec)
            n = len(normalized) + len(retained)
        conn.commit()

    if meta is not None:
        payload = dict(meta)
        payload.setdefault("count", n)
        payload.setdefault("synced_at", now_ts)
        set_meta(payload)
    return n


def import_bucket(
    bucket: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> int:
    """Import models_cache.json-style ``models`` mapping into PG."""
    if not enabled() or not isinstance(bucket, dict):
        return 0
    items: list[dict[str, Any]] = []
    for mid, meta_row in bucket.items():
        key = str(mid).strip()
        if not key:
            continue
        if not isinstance(meta_row, dict):
            items.append({"id": key})
            continue
        row = dict(meta_row)
        info = row.get("info") if isinstance(row.get("info"), dict) else None
        if info is None:
            # Treat the row itself as the info blob.
            info = {k: v for k, v in row.items() if k not in ("api_key", "env_key")}
        else:
            info = dict(info)
        if not info.get("id"):
            info["id"] = key
        items.append(
            {
                "id": key,
                "info": info,
                "api_key": row.get("api_key"),
                "env_key": row.get("env_key"),
                "synthetic": bool(info.get("synthetic")),
            }
        )
    return replace_all(items, meta=meta, keep_synthetic=True)
