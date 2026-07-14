"""PostgreSQL admin operation audit log."""

from __future__ import annotations

import json
import time
from typing import Any

from store.pg import connection, json_dump, pg_enabled


def enabled() -> bool:
    return pg_enabled()


def write_log(
    *,
    action: str,
    summary: str = "",
    actor: str | None = "admin",
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    ok: bool = True,
) -> int | None:
    """Insert one audit row. Returns new id or None if store unavailable."""
    if not enabled() or not action:
        return None
    payload = detail if isinstance(detail, dict) else {}
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_audit_logs (
                      actor, action, target_type, target_id, summary, detail,
                      ip, user_agent, ok, created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s, now())
                    RETURNING id
                    """,
                    (
                        (actor or "admin")[:120],
                        str(action)[:120],
                        (target_type or None),
                        (str(target_id)[:256] if target_id is not None else None),
                        (summary or "")[:500],
                        json_dump(payload),
                        (ip or None),
                        ((user_agent or "")[:300] or None),
                        bool(ok),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception:
        return None


def list_logs(
    *,
    q: str = "",
    action: str = "",
    page: int = 1,
    page_size: int = 50,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> dict[str, Any]:
    if not enabled():
        return {
            "ok": True,
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": page_size,
            "total_pages": 1,
            "store_source": "none",
        }

    try:
        page_i = max(1, int(page))
    except Exception:
        page_i = 1
    try:
        size_i = int(page_size)
    except Exception:
        size_i = 50
    size_i = max(1, min(200, size_i if size_i > 0 else 50))

    where: list[str] = []
    params: list[Any] = []
    qq = (q or "").strip()
    if qq:
        where.append(
            "("
            "action ILIKE %s OR summary ILIKE %s OR COALESCE(target_id,'') ILIKE %s "
            "OR COALESCE(target_type,'') ILIKE %s OR COALESCE(actor,'') ILIKE %s "
            "OR COALESCE(ip,'') ILIKE %s"
            ")"
        )
        like = f"%{qq}%"
        params.extend([like, like, like, like, like, like])
    aa = (action or "").strip()
    if aa and aa != "all":
        where.append("action = %s")
        params.append(aa)
    if since_ts is not None:
        where.append("created_at >= to_timestamp(%s)")
        params.append(float(since_ts))
    if until_ts is not None:
        where.append("created_at <= to_timestamp(%s)")
        params.append(float(until_ts))
    wh = (" WHERE " + " AND ".join(where)) if where else ""

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM admin_audit_logs{wh}", params)
            total = int((cur.fetchone() or [0])[0] or 0)
            total_pages = max(1, (total + size_i - 1) // size_i) if total else 1
            page_i = min(page_i, total_pages)
            offset = (page_i - 1) * size_i
            cur.execute(
                f"""
                SELECT id, created_at, actor, action, target_type, target_id,
                       summary, detail, ip, user_agent, ok
                FROM admin_audit_logs
                {wh}
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, size_i, offset],
            )
            rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        detail = r[7]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                detail = {"raw": detail}
        created = r[1]
        try:
            created_ts = created.timestamp() if hasattr(created, "timestamp") else float(created or 0)
        except Exception:
            created_ts = time.time()
        items.append(
            {
                "id": int(r[0]),
                "created_at": created_ts,
                "actor": r[2],
                "action": r[3],
                "target_type": r[4],
                "target_id": r[5],
                "summary": r[6],
                "detail": detail if isinstance(detail, dict) else {},
                "ip": r[8],
                "user_agent": r[9],
                "ok": bool(r[10]),
            }
        )
    return {
        "ok": True,
        "items": items,
        "total": total,
        "page": page_i,
        "page_size": size_i,
        "total_pages": total_pages,
        "q": qq,
        "action": aa or "all",
        "store_source": "postgres",
    }


def list_actions(limit: int = 50) -> list[str]:
    if not enabled():
        return []
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, COUNT(*) AS c
                    FROM admin_audit_logs
                    GROUP BY action
                    ORDER BY c DESC, action ASC
                    LIMIT %s
                    """,
                    (max(1, min(200, int(limit))),),
                )
                return [str(r[0]) for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []
