"""Phase-2 MasterService: owner gate, CRUD, token verification and licenses."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta

import pytest

from Database.repositories import (
    AuditRepository,
    BotRepository,
    LicenseRepository,
    NotificationRepository,
    TenantRuntimeRepository,
)
from MasterBot.service import (
    BotIdentity,
    MasterService,
    MasterServiceError,
    TokenVerificationError,
)
from LicenseService.runtime import TenantRuntimeGate
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, fingerprint_token, generate_key
from Shared.timeutils import iso_utc, utcnow
from Tests.conftest import make_fake_token


class FakeVerifier:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.identity = BotIdentity(telegram_bot_id=700001, username="tenant_bot")

    async def verify(self, token: str) -> BotIdentity:
        self.calls.append(token)
        if self.fail:
            raise RuntimeError(f"rejected credential {token}")
        return self.identity


@pytest.fixture()
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture()
def master(conn, verifier) -> MasterService:
    return MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )


def test_non_owner_cannot_read_or_write(master, conn) -> None:
    with pytest.raises(AccessDenied):
        master.list_tenants(44)
    with pytest.raises(AccessDenied):
        master.create_tenant(44, name="Blocked", slug="blocked", owner_telegram_id=2)
    with pytest.raises(AccessDenied):
        master.statistics(44)
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0


def test_every_public_sync_use_case_repeats_owner_gate(master) -> None:
    calls = [
        lambda: master.get_tenant(44, 1),
        lambda: master.update_tenant(44, 1, name="x", slug="xxx", owner_telegram_id=1),
        lambda: master.set_tenant_status(44, 1, "active"),
        lambda: master.list_plans(44),
        lambda: master.get_plan(44, 1),
        lambda: master.create_plan(44, name="x", duration_days=1, price=0, max_servers=1, max_users=1),
        lambda: master.update_plan(44, 1, name="x", duration_days=1, price=0, max_servers=1, max_users=1),
        lambda: master.set_plan_status(44, 1, "active"),
        lambda: master.list_licenses(44),
        lambda: master.get_license(44, 1),
        lambda: master.create_license(44, tenant_id=1, plan_id=1),
        lambda: master.renew(44, 1, extra_days=1),
        lambda: master.suspend(44, 1),
        lambda: master.reactivate(44, 1),
        lambda: master.list_warnings(44),
        lambda: master.list_audit(44),
    ]
    for call in calls:
        with pytest.raises(AccessDenied):
            call()
    with pytest.raises(AccessDenied):
        asyncio.run(master.register_tenant_bot(44, 1, role="admin", plain_token="invalid"))


def test_tenant_create_update_status_are_audited(master, conn) -> None:
    tenant = master.create_tenant(9001, name="Alpha", slug="alpha-shop", owner_telegram_id=101)
    updated = master.update_tenant(
        9001, int(tenant["id"]), name="Alpha 2", slug="alpha-two", owner_telegram_id=202
    )
    suspended = master.set_tenant_status(9001, int(tenant["id"]), "suspended")
    assert updated["slug"] == "alpha-two"
    assert int(updated["owner_telegram_id"]) == 202
    assert suspended["status"] == "suspended"
    actions = [row["action"] for row in AuditRepository(conn).list_by_tenant(int(tenant["id"]))]
    assert actions == ["tenant.status", "tenant.update", "tenant.create"]


def test_tenant_pagination_and_search(master) -> None:
    for number in range(11):
        master.create_tenant(
            9001,
            name=f"Customer {number}",
            slug=f"customer-{number}",
            owner_telegram_id=1000 + number,
        )
    first = master.list_tenants(9001, page=0, page_size=5)
    second = master.list_tenants(9001, page=1, page_size=5)
    found = master.list_tenants(9001, query="Customer 10")
    assert len(first.items) == 5 and first.has_next and not first.has_previous
    assert len(second.items) == 5 and second.has_next and second.has_previous
    assert [row["name"] for row in found.items] == ["Customer 10"]


def test_plan_create_update_archive_are_audited(master, conn) -> None:
    plan = master.create_plan(
        9001, name="Starter", duration_days=30, price=100, max_servers=2, max_users=50
    )
    updated = master.update_plan(
        9001,
        int(plan["id"]),
        name="Starter Plus",
        duration_days=45,
        price=150,
        max_servers=3,
        max_users=75,
    )
    archived = master.set_plan_status(9001, int(plan["id"]), "archived")
    assert updated["name"] == "Starter Plus" and int(updated["duration_days"]) == 45
    assert archived["status"] == "archived"
    actions = [row["action"] for row in AuditRepository(conn).list_recent()]
    assert actions[:3] == ["plan.status", "plan.update", "plan.create"]
    assert master.list_plans(9001, query="Starter Plus").items[0]["id"] == plan["id"]


def test_bot_registration_verifies_encrypts_and_rotates(master, verifier, conn) -> None:
    tenant = master.create_tenant(9001, name="Bot Owner", slug="bot-owner", owner_telegram_id=10)
    token_a = make_fake_token("master-register-a")
    row_a = asyncio.run(master.register_tenant_bot(
        9001, int(tenant["id"]), role="admin", plain_token=token_a
    ))
    assert verifier.calls == [token_a]
    assert token_a not in str(dict(row_a))
    assert master.cipher.decrypt(row_a["encrypted_token"]) == token_a
    assert row_a["token_fingerprint"] == fingerprint_token(token_a)
    assert row_a["telegram_username"] == "tenant_bot"

    token_b = make_fake_token("master-register-b")
    verifier.identity = BotIdentity(telegram_bot_id=700002, username="rotated_bot")
    row_b = asyncio.run(master.register_tenant_bot(
        9001, int(tenant["id"]), role="admin", plain_token=token_b
    ))
    assert row_b["id"] == row_a["id"]
    assert row_b["token_fingerprint"] == fingerprint_token(token_b)
    assert int(row_b["telegram_bot_id"]) == 700002
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_bots WHERE tenant_id = ? AND role = 'admin'",
        (int(tenant["id"]),),
    ).fetchone()[0] == 1


def test_failed_getme_is_safe_and_writes_nothing(master, verifier, conn) -> None:
    tenant = master.create_tenant(9001, name="Safe", slug="safe-tenant", owner_telegram_id=10)
    verifier.fail = True
    token = make_fake_token("must-not-leak")
    with pytest.raises(TokenVerificationError) as exc:
        asyncio.run(master.register_tenant_bot(
            9001, int(tenant["id"]), role="user", plain_token=token
        ))
    assert token not in str(exc.value)
    assert BotRepository(conn).get_by_tenant_role(int(tenant["id"]), "user") is None


def test_duplicate_bot_token_rolls_back_second_tenant(master, verifier, conn) -> None:
    first = master.create_tenant(9001, name="First", slug="first-one", owner_telegram_id=1)
    second = master.create_tenant(9001, name="Second", slug="second-one", owner_telegram_id=2)
    token = make_fake_token("global-unique")
    asyncio.run(master.register_tenant_bot(9001, int(first["id"]), role="admin", plain_token=token))
    verifier.identity = BotIdentity(telegram_bot_id=700009, username="duplicate")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(master.register_tenant_bot(9001, int(second["id"]), role="admin", plain_token=token))
    assert BotRepository(conn).get_by_tenant_role(int(second["id"]), "admin") is None


def test_license_create_suspend_reactivate_and_renew(master) -> None:
    tenant = master.create_tenant(9001, name="Licensed", slug="licensed", owner_telegram_id=12)
    plan = master.create_plan(
        9001, name="Monthly", duration_days=30, price=50, max_servers=1, max_users=10
    )
    license_row = master.create_license(
        9001, tenant_id=int(tenant["id"]), plan_id=int(plan["id"]), grace_days=2
    )
    suspended = master.suspend(9001, int(license_row["id"]))
    assert suspended["status"] == "suspended" and suspended["suspended_at"]
    active = master.reactivate(9001, int(license_row["id"]))
    assert active["status"] == "active" and active["suspended_at"] is None
    before = active["expires_at"]
    renewed = master.renew(9001, int(license_row["id"]), extra_days=10, grace_days=1)
    assert renewed["status"] == "active"
    assert renewed["expires_at"] > before
    by_tenant_name = master.list_licenses(9001, query="Licensed")
    by_license_id = master.list_licenses(9001, query=str(license_row["id"]))
    assert by_tenant_name.items[0]["id"] == license_row["id"]
    assert by_license_id.items[0]["id"] == license_row["id"]


def test_expired_license_cannot_reactivate_without_renew(master, factories) -> None:
    tenant = master.create_tenant(9001, name="Expired", slug="expired-one", owner_telegram_id=9)
    plan = master.create_plan(
        9001, name="Old", duration_days=1, price=0, max_servers=1, max_users=1
    )
    expired = factories.license(
        int(tenant["id"]), int(plan["id"]), status="suspended", starts_in_days=-10, duration_days=1
    )
    with pytest.raises(MasterServiceError):
        master.reactivate(9001, int(expired["id"]))


def test_statistics_warnings_and_audit_pages(master, conn) -> None:
    tenant = master.create_tenant(9001, name="Stats", slug="stats-one", owner_telegram_id=7)
    NotificationRepository(conn).enqueue(
        tenant_id=int(tenant["id"]),
        event_type="license.expiring",
        event_key="phase2:warning:1",
        scheduled_at=iso_utc(utcnow() + timedelta(days=1)),
    )
    stats = master.statistics(9001)
    warnings = master.list_warnings(9001)
    audit = master.list_audit(9001)
    assert stats["tenants_total"] == 1 and stats["warnings_open"] == 1
    assert warnings.items[0]["event_key"] == "phase2:warning:1"
    assert audit.items and "safe_metadata" not in audit.items[0]
    assert isinstance(audit.items[0]["metadata"], dict)


def test_master_mutations_invalidate_runtime_cache(conn, verifier) -> None:
    gate = TenantRuntimeGate(conn, ttl_seconds=60)
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
        cache_invalidator=gate.invalidate,
    )
    tenant = service.create_tenant(9001, name="Cache", slug="cache-one", owner_telegram_id=1)
    TenantRuntimeRepository(conn).create(tenant_id=int(tenant["id"]), status="ready")
    plan = service.create_plan(
        9001, name="Cache Plan", duration_days=30, price=0, max_servers=1, max_users=1
    )
    license_row = service.create_license(
        9001, tenant_id=int(tenant["id"]), plan_id=int(plan["id"])
    )
    assert gate.is_allowed(int(tenant["id"]))
    service.suspend(9001, int(license_row["id"]))
    assert not gate.is_allowed(int(tenant["id"]))
    service.reactivate(9001, int(license_row["id"]))
    assert gate.is_allowed(int(tenant["id"]))
    service.set_tenant_status(9001, int(tenant["id"]), "suspended")
    assert not gate.is_allowed(int(tenant["id"]))
