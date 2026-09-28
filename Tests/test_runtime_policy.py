"""Every update is re-authorized against its current tenant route and license."""

from __future__ import annotations

from datetime import timedelta

from Database.repositories import BotRepository, TenantRepository
from Gateway.catalog import RuntimeCatalog
from Gateway.policy import RuntimePolicy
from LicenseService.runtime import TenantRuntimeGate
from Shared.timeutils import iso_utc, utcnow
from Tests.conftest import make_fake_token


def _licensed_spec(conn, factories, cipher, *, role: str, owner_id: int = 90001):
    tenant = factories.tenant(owner_telegram_id=owner_id)
    bot, _ = factories.bot(int(tenant["id"]), role, cipher)
    BotRepository(conn).set_telegram_identity(
        int(bot["id"]), telegram_bot_id=850000 + int(bot["id"])
    )
    plan = factories.plan()
    license_row = factories.license(int(tenant["id"]), int(plan["id"]), status="active")
    spec = next(
        item
        for item in RuntimeCatalog(
            conn, cipher=cipher, shard_count=1, shard_index=0
        ).load().specs
        if item.bot_id == int(bot["id"])
    )
    return tenant, bot, license_row, spec


def test_user_bot_allows_user_but_admin_bot_only_allows_owner(conn, factories, cipher) -> None:
    tenant, _, _, user_spec = _licensed_spec(
        conn, factories, cipher, role="user", owner_id=91001
    )
    policy = RuntimePolicy(conn, gate=TenantRuntimeGate(conn))
    assert policy.check(user_spec, telegram_user_id=123).allowed

    admin, _, _, admin_spec = _licensed_spec(
        conn, factories, cipher, role="admin", owner_id=92001
    )
    assert policy.check(admin_spec, telegram_user_id=92001).allowed
    denied = policy.check(admin_spec, telegram_user_id=123)
    assert not denied.allowed and denied.reason == "admin_access_denied"
    assert int(tenant["id"]) != int(admin["id"])


def test_token_rotation_invalidates_old_worker_route_immediately(conn, factories, cipher) -> None:
    tenant, bot, _, spec = _licensed_spec(conn, factories, cipher, role="user")
    policy = RuntimePolicy(conn, gate=TenantRuntimeGate(conn))
    assert policy.check(spec, telegram_user_id=1).allowed
    BotRepository(conn).rotate_token(
        int(bot["id"]), cipher=cipher, plain_token=make_fake_token("policy-rotate")
    )
    decision = policy.check(spec, telegram_user_id=1)
    assert not decision.allowed and decision.reason == "bot_route_changed"
    assert tenant


def test_tenant_disable_and_runtime_disable_fail_closed(conn, factories, cipher) -> None:
    tenant, _, _, spec = _licensed_spec(conn, factories, cipher, role="user")
    gate = TenantRuntimeGate(conn)
    policy = RuntimePolicy(conn, gate=gate)
    TenantRepository(conn).update_status(int(tenant["id"]), "disabled")
    gate.invalidate(int(tenant["id"]))
    assert policy.check(spec, telegram_user_id=1).reason == "bot_route_changed"


def test_expired_license_blocks_only_its_tenant(conn, factories, cipher) -> None:
    first, _, first_license, first_spec = _licensed_spec(
        conn, factories, cipher, role="user"
    )
    second, _, _, second_spec = _licensed_spec(conn, factories, cipher, role="user")
    now = utcnow()
    conn.execute(
        "UPDATE licenses SET expires_at=?, grace_until=NULL WHERE id=?",
        (iso_utc(now - timedelta(seconds=1)), int(first_license["id"])),
    )
    gate = TenantRuntimeGate(conn)
    policy = RuntimePolicy(conn, gate=gate)
    blocked = policy.check(first_spec, telegram_user_id=7)
    allowed = policy.check(second_spec, telegram_user_id=7)
    assert not blocked.allowed and blocked.license_status == "expired"
    assert allowed.allowed
    assert int(first["id"]) != int(second["id"])


def test_policy_missing_user_and_closed_database_fail_closed(conn, factories, cipher) -> None:
    _, _, _, spec = _licensed_spec(conn, factories, cipher, role="user")
    policy = RuntimePolicy(conn, gate=TenantRuntimeGate(conn))
    assert policy.check(spec, telegram_user_id=None).reason == "missing_user"
    conn.close()
    assert policy.check(spec, telegram_user_id=1).reason == "policy_error"
