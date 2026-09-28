"""Shared pytest fixtures: tmp SQLite (migrated), fake tokens, test cipher."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Database.connection import connect
from Database.migrate import migrate
from Database.repositories import (
    LicenseRepository,
    PlanRepository,
    TenantRepository,
    TenantRuntimeRepository,
)
from Shared.crypto import FernetTokenCipher, generate_key
from Shared.timeutils import iso_utc, utcnow


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    migrate(path)
    return path


@pytest.fixture()
def conn(db_path: str):
    connection = connect(db_path)
    yield connection
    try:
        connection.rollback()
    except Exception:
        pass
    connection.close()


@pytest.fixture()
def enc_key() -> str:
    return generate_key()


@pytest.fixture()
def cipher(enc_key: str) -> FernetTokenCipher:
    return FernetTokenCipher(enc_key)


def make_fake_token(tag: str = "t") -> str:
    """Fake offline bot token matching the redaction pattern shape."""
    return f"990001:FAKE_test_token_abcdefghijklmnopqrstuvwxyz_{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def factories(conn):  # type: ignore[no-untyped-def]
    tenants = TenantRepository(conn)
    runtimes = TenantRuntimeRepository(conn)
    plans = PlanRepository(conn)
    licenses = LicenseRepository(conn)

    class Factories:
        def tenant(self, **overrides: Any) -> dict[str, Any]:
            row = tenants.create(
                name=overrides.get("name") or _uniq("Tenant"),
                slug=overrides.get("slug") or _uniq("tenant").lower().replace("_", "-"),
                owner_telegram_id=overrides.get("owner_telegram_id") or 100001,
                status=overrides.get("status") or "active",
            )
            runtime_status = overrides.get("runtime_status", "ready")
            if runtime_status is not None:
                runtimes.create(
                    tenant_id=int(row["id"]), status=str(runtime_status)
                )
            conn.commit()
            return row

        def plan(self, **overrides: Any) -> dict[str, Any]:
            row = plans.create(
                name=overrides.get("name") or _uniq("Plan"),
                duration_days=overrides.get("duration_days") or 30,
                price=overrides.get("price") if overrides.get("price") is not None else 1000,
                max_servers=overrides.get("max_servers") or 5,
                max_users=overrides.get("max_users") or 100,
                features=overrides.get("features"),
            )
            conn.commit()
            return row

        def license(
            self,
            tenant_id: int,
            plan_id: int,
            *,
            status: str = "active",
            starts_in_days: int = 0,
            duration_days: int = 30,
            grace_days: int = 3,
        ) -> dict[str, Any]:
            now = utcnow()
            starts = now + timedelta(days=starts_in_days)
            expires = starts + timedelta(days=duration_days)
            grace = expires + timedelta(days=grace_days) if grace_days > 0 else None
            row = licenses.create(
                tenant_id=tenant_id,
                plan_id=plan_id,
                status=status,
                starts_at=iso_utc(starts),
                expires_at=iso_utc(expires),
                grace_until=iso_utc(grace) if grace else None,
            )
            conn.commit()
            return row

        def bot(
            self,
            tenant_id: int,
            role: str,
            test_cipher: FernetTokenCipher,
            token: Optional[str] = None,
        ) -> tuple[dict[str, Any], str]:
            from Database.repositories import BotRepository
            from Shared.crypto import (
                generate_webhook_secret,
                hash_webhook_secret,
            )

            plain = token or make_fake_token(role)
            row = BotRepository(conn).register(
                tenant_id=tenant_id,
                role=role,
                cipher=test_cipher,
                plain_token=plain,
                webhook_secret_hash=hash_webhook_secret(generate_webhook_secret()),
            )
            conn.commit()
            return row, plain

    return Factories()
