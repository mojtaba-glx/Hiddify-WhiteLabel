#!/usr/bin/env python3
"""Purge soft-deleted subscriptions left over from the old delete behavior.

Old AdminBot/UserBot deletes only removed the panel user and kept a
`disabled` row for history. This script finishes the job SellBot-style:
remove the panel user (if still present) and drop the subscription row
plus its scoped data. Orders, receipts, wallet ledger, customers and
referral history are never touched.

Usage:
    python3 scripts/purge-deleted-subscriptions.py --db data/whitelabel.db
    python3 scripts/purge-deleted-subscriptions.py --db data/whitelabel.db --apply
    python3 scripts/purge-deleted-subscriptions.py --db data/whitelabel.db --apply --tenant 1

Without --apply this is a dry run: it only lists what would be purged.
Exit code is 0 on dry run, 1 if any purge failed with --apply.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Database.connection import connect
from Shared.crypto import FernetTokenCipher, TokenCipherError
from Shared.settings import load_database_path
from TenantRuntime.business import TenantBusinessError, TenantBusinessService


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge disabled (soft-deleted) subscriptions."
    )
    parser.add_argument("--db", default="", help="SQLite file path")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--tenant", type=int, default=0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it, only a dry-run list is shown.",
    )
    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        env = _load_env(Path(args.env_file))
        db_path = load_database_path(env) if env else "data/whitelabel.db"
    if not Path(db_path).is_absolute():
        db_path = str(ROOT / db_path)

    env = _load_env(Path(args.env_file))
    cipher = None
    key = str(env.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if key:
        try:
            cipher = FernetTokenCipher(key)
        except TokenCipherError as exc:
            print(f"WARNING: TOKEN_ENCRYPTION_KEY invalid ({exc}); panel removal will fail.")

    conn = connect(db_path)
    try:
        query = (
            "SELECT t.id AS tenant_id, t.owner_telegram_id, s.id AS sub_id,"
            " s.service_code, s.status, s.server_id, s.external_ref"
            " FROM tenant_subscriptions s"
            " JOIN tenants t ON t.id = s.tenant_id"
            " WHERE s.status = 'disabled'"
        )
        params: tuple = ()
        if args.tenant:
            query += " AND s.tenant_id = ?"
            params = (int(args.tenant),)
        query += " ORDER BY s.tenant_id, s.id"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()

    if not rows:
        print("Nothing to purge: no disabled subscriptions found.")
        return 0

    print(f"Found {len(rows)} disabled subscription(s):")
    for row in rows:
        code = row.get("service_code") or row["sub_id"]
        print(
            f"  tenant={row['tenant_id']} sub={row['sub_id']} code={code}"
            f" panel={'set' if row.get('external_ref') else 'none'}"
        )

    if not args.apply:
        print("\nDry run only. Re-run with --apply to purge them.")
        return 0

    failures = 0
    for row in rows:
        tenant_id = int(row["tenant_id"])
        owner = int(row["owner_telegram_id"] or 0)
        sid = int(row["sub_id"])
        if owner <= 0:
            print(f"  SKIP sub={sid}: tenant has no owner id")
            failures += 1
            continue
        conn = connect(db_path)
        try:
            business = TenantBusinessService(
                conn,
                tenant_id=tenant_id,
                owner_telegram_id=owner,
                secret_cipher=cipher,
            )
            result = business.delete_subscription_admin(owner, subscription_id=sid)
            print(
                f"  PURGED sub={sid}: panel_removed={result.get('panel_removed', 0)}"
            )
        except (TenantBusinessError, sqlite3.IntegrityError, ValueError) as exc:
            print(f"  SKIP sub={sid}: {str(exc)[:120]}")
            failures += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass
    print(f"Done: {len(rows) - failures} purged, {failures} skipped.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
