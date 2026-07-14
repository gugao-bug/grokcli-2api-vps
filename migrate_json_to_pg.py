#!/usr/bin/env python3
"""Import data/*.json into PostgreSQL (accounts / keys / settings / pool status).

Usage:
  export DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/grok2api
  python migrate_json_to_pg.py
  python migrate_json_to_pg.py --data-dir ./data --database-url "$DATABASE_URL"
  python migrate_json_to_pg.py --data-dir /app/data --merge-pool

Requires: pip install -r requirements-store.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


_SCALAR_KEYS = (
    "account_mode",
    "token_maintain_enabled",
    "model_health_enabled",
    "reasoning_compat",
    "outbound_max_tools",
    "outbound_tool_gap_sec",
    "history_compact_enabled",
    "sse_keepalive",
    "conversation_affinity_enabled",
    "default_model",
    "cooldown_default_sec",
    "cooldown_auth_sec",
    "cooldown_rate_limit_sec",
    "cooldown_server_error_sec",
    "cooldown_max_sec",
    "soft_model_block_ttl_sec",
    "durable_model_block_ttl_sec",
    "probe_fail_kick_streak",
    "probe_fail_disable_streak",
    "probe_kick_cooldown_sec",
    "max_failover_attempts",
    "registration_config",
)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"WARN: cannot parse {path}: {e}", file=sys.stderr)
        return None


def _normalize_pool_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Ensure durable status fields exist for account_pool rows."""
    out = dict(meta or {})
    out.setdefault("enabled", True)
    out.setdefault("weight", 1)
    out.setdefault("blocked_models", {})
    # Derive status from existing fields without inventing cooldowns.
    try:
        cd_count = int(out.get("cooldown_count") or 0)
    except (TypeError, ValueError):
        cd_count = 0
    stack = out.get("status_stack")
    if isinstance(stack, list) and stack:
        cd_count = max(cd_count, len(stack))
    out["cooldown_count"] = max(0, cd_count)
    if out.get("disabled_for_quota"):
        out["pool_status"] = "quota_disabled"
    elif out.get("enabled") is False:
        out["pool_status"] = "disabled"
    elif out.get("pool_status") in (
        "normal",
        "cooldown",
        "disabled",
        "quota_disabled",
        "model_blocked",
    ):
        pass
    elif cd_count > 0 or out.get("cooldown_until"):
        out["pool_status"] = "cooldown"
    elif out.get("blocked_models"):
        out["pool_status"] = "model_blocked"
    else:
        out["pool_status"] = "normal"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Migrate grokcli-2api JSON data → PostgreSQL")
    p.add_argument("--data-dir", default=os.getenv("GROK2API_DATA_DIR", "./data"))
    p.add_argument(
        "--database-url",
        default=os.getenv("GROK2API_DATABASE_URL") or os.getenv("DATABASE_URL") or "",
    )
    p.add_argument(
        "--merge-pool",
        action="store_true",
        help="Merge JSON account_pool into existing PG rows instead of full replace",
    )
    p.add_argument(
        "--skip-accounts",
        action="store_true",
        help="Do not import auth.json accounts",
    )
    p.add_argument(
        "--skip-keys",
        action="store_true",
        help="Do not import keys.json",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.database_url:
        print("ERROR: --database-url or DATABASE_URL required", file=sys.stderr)
        return 2

    os.environ["DATABASE_URL"] = args.database_url
    os.environ["GROK2API_DATABASE_URL"] = args.database_url

    # Force config reload values
    import config

    config.DATABASE_URL = args.database_url

    data_dir = Path(args.data_dir)
    auth_path = data_dir / "auth.json"
    keys_path = data_dir / "keys.json"
    settings_path = data_dir / "settings.json"

    from store import accounts_pg, keys_pg, settings_pg
    from store.pg import get_pool, ping

    print(f"data_dir={data_dir}")
    print(f"database={args.database_url.split('@')[-1] if '@' in args.database_url else '…'}")
    get_pool()
    if not ping(force=True):
        print("ERROR: cannot ping PostgreSQL", file=sys.stderr)
        return 3

    # Schema migrations (status columns etc.) run on pool connect.
    print("schema: ready")

    # accounts
    accounts: dict[str, Any] = {}
    if not args.skip_accounts:
        raw_accounts = _load_json(auth_path)
        if isinstance(raw_accounts, dict):
            # unwrap export wrapper if present
            if "auth" in raw_accounts and isinstance(raw_accounts.get("auth"), dict):
                accounts = raw_accounts["auth"]
            else:
                accounts = raw_accounts
        print(f"accounts: {len(accounts)} from {auth_path}")
        if not args.dry_run and accounts:
            accounts_pg.write_auth_map(accounts)
            print(f"accounts: wrote {len(accounts)} rows")
    else:
        print("accounts: skipped")

    # keys
    keys: list[Any] = []
    if not args.skip_keys:
        raw_keys = _load_json(keys_path)
        if isinstance(raw_keys, dict):
            keys = list(raw_keys.get("keys") or [])
        elif isinstance(raw_keys, list):
            keys = list(raw_keys)
        print(f"api_keys: {len(keys)} from {keys_path}")
        if not args.dry_run and keys:
            keys_pg.replace_all(keys)
            print(f"api_keys: wrote {len(keys)} rows")
    else:
        print("api_keys: skipped")

    # settings / pool
    settings = _load_json(settings_path)
    if not isinstance(settings, dict):
        settings = {}
    admin = {
        k: settings[k]
        for k in ("admin_password_hash", "admin_password_salt")
        if k in settings
    }
    pool = settings.get("account_pool") if isinstance(settings.get("account_pool"), dict) else {}
    print(
        f"settings: admin_password={'yes' if admin else 'no'} "
        f"mode={settings.get('account_mode')!r} pool_entries={len(pool)}"
    )

    if not args.dry_run:
        if admin:
            settings_pg.set_setting("admin_password", admin)
            print("settings: admin_password imported")
        for key in _SCALAR_KEYS:
            if key in settings and settings.get(key) is not None:
                settings_pg.set_setting(key, settings.get(key))
        # Also import any remaining simple settings keys (non-blob).
        for key, val in settings.items():
            if key in (
                "account_pool",
                "sessions",
                "admin_password_hash",
                "admin_password_salt",
                "updated_at",
            ):
                continue
            if key in _SCALAR_KEYS:
                continue
            if isinstance(val, (str, int, float, bool, dict, list)) or val is None:
                try:
                    settings_pg.set_setting(key, val)
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: skip setting {key}: {e}", file=sys.stderr)

        if pool:
            normalized = {
                str(aid): _normalize_pool_meta(meta if isinstance(meta, dict) else {})
                for aid, meta in pool.items()
                if str(aid).strip()
            }
            if args.merge_pool:
                existing = settings_pg.get_account_pool_state() or {}
                merged = dict(existing)
                for aid, meta in normalized.items():
                    cur = dict(merged.get(aid) or {})
                    cur.update(meta)
                    merged[aid] = _normalize_pool_meta(cur)
                settings_pg.save_account_pool_state(merged)
                print(f"account_pool: merged {len(normalized)} from JSON into {len(merged)} PG rows")
            else:
                # Prefer not to wipe PG-only accounts: merge by default when PG already has rows.
                existing = settings_pg.get_account_pool_state() or {}
                if existing and not args.merge_pool:
                    merged = dict(existing)
                    for aid, meta in normalized.items():
                        cur = dict(merged.get(aid) or {})
                        cur.update(meta)
                        merged[aid] = _normalize_pool_meta(cur)
                    settings_pg.save_account_pool_state(merged)
                    print(
                        f"account_pool: auto-merged {len(normalized)} JSON → "
                        f"{len(merged)} PG rows (existing PG data preserved)"
                    )
                else:
                    settings_pg.save_account_pool_state(normalized)
                    print(f"account_pool: wrote {len(normalized)} rows")
        else:
            # Ensure every account has a pool row with default normal status.
            try:
                auth_map = accounts_pg.read_auth_map() if hasattr(accounts_pg, "read_auth_map") else {}
            except Exception:
                auth_map = {}
            if not auth_map and accounts:
                auth_map = accounts
            if auth_map:
                existing = settings_pg.get_account_pool_state() or {}
                added = 0
                for aid in auth_map.keys():
                    sid = str(aid)
                    if sid not in existing:
                        existing[sid] = _normalize_pool_meta({"enabled": True, "weight": 1})
                        added += 1
                if added and not args.dry_run:
                    settings_pg.save_account_pool_state(existing)
                    print(f"account_pool: ensured default rows for {added} accounts")
                else:
                    print(f"account_pool: no JSON pool; existing PG rows={len(existing)}")

        # Snapshot summary after migration.
        try:
            snap = settings_pg.refresh_pool_summary_snapshot()
            print(
                "pool_summary:",
                f"total={snap.get('total')} live={snap.get('live')} "
                f"enabled={snap.get('enabled')} cooldown={snap.get('in_cooldown')}",
            )
        except Exception as e:  # noqa: BLE001
            print(f"WARN: pool summary refresh failed: {e}", file=sys.stderr)

    if args.dry_run:
        print("dry-run: no writes performed")
    else:
        print(f"done: imported into PostgreSQL at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("note: account status/cooldown now live in account_pool table (not settings.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
