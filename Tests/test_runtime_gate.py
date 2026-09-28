"""Phase-3 runtime gate stays isolated, cached briefly and fail-closed."""

from __future__ import annotations

from datetime import timedelta

from LicenseService.runtime import TenantRuntimeGate
from LicenseService.service import LicenseStatusCache
from Shared.timeutils import parse_utc, utcnow


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_gate_allows_active_and_grace_but_blocks_expired(conn, factories) -> None:
    plan = factories.plan()
    active_tenant = factories.tenant()
    grace_tenant = factories.tenant()
    expired_tenant = factories.tenant()
    active = factories.license(active_tenant["id"], plan["id"], status="active")
    grace = factories.license(
        grace_tenant["id"], plan["id"], status="active", starts_in_days=-10,
        duration_days=5, grace_days=10,
    )
    expired = factories.license(
        expired_tenant["id"], plan["id"], status="active", starts_in_days=-10,
        duration_days=1, grace_days=0,
    )
    gate = TenantRuntimeGate(conn)
    assert gate.is_allowed(int(active_tenant["id"]), now=utcnow())
    assert gate.is_allowed(
        int(grace_tenant["id"]), now=parse_utc(grace["expires_at"]) + timedelta(days=1)
    )
    assert not gate.is_allowed(
        int(expired_tenant["id"]), now=parse_utc(expired["expires_at"]) + timedelta(seconds=1)
    )
    assert active and expired  # fixtures are distinct and valid


def test_gate_blocks_missing_license_and_suspended_tenant(conn, factories) -> None:
    tenant = factories.tenant()
    gate = TenantRuntimeGate(conn)
    assert gate.check(int(tenant["id"]), now=utcnow()).reason == "license_not_found"
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant["id"],))
    decision = gate.check(int(tenant["id"]), now=utcnow())
    assert not decision.allowed and decision.reason == "tenant_disabled"


def test_future_active_license_is_blocked_until_start(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    row = factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=2, duration_days=30
    )
    gate = TenantRuntimeGate(conn)
    before = gate.check(int(tenant["id"]), now=utcnow())
    after = gate.check(
        int(tenant["id"]), now=parse_utc(row["starts_at"]) + timedelta(seconds=1)
    )
    assert not before.allowed and before.reason == "license_not_started"
    assert after.allowed


def test_cache_expires_and_observes_tenant_status_change(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    clock = FakeClock()
    cache = LicenseStatusCache(ttl_seconds=5, clock=clock)
    gate = TenantRuntimeGate(conn, cache=cache)
    assert gate.is_allowed(int(tenant["id"]))
    conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant["id"],))
    assert gate.is_allowed(int(tenant["id"]))  # still inside the short cache window
    clock.value += 6
    assert not gate.is_allowed(int(tenant["id"]))


def test_explicit_time_bypasses_cached_decision(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    row = factories.license(tenant["id"], plan["id"], status="active", grace_days=0)
    gate = TenantRuntimeGate(conn, ttl_seconds=60)
    assert gate.is_allowed(int(tenant["id"]))
    assert not gate.is_allowed(
        int(tenant["id"]), now=parse_utc(row["expires_at"]) + timedelta(seconds=1)
    )


def test_gate_isolates_tenants_and_fails_closed_on_database_error(conn, factories) -> None:
    plan = factories.plan()
    allowed = factories.tenant()
    blocked = factories.tenant()
    factories.license(allowed["id"], plan["id"], status="active")
    gate = TenantRuntimeGate(conn)
    assert gate.is_allowed(int(allowed["id"]), now=utcnow())
    assert not gate.is_allowed(int(blocked["id"]), now=utcnow())
    conn.close()
    assert not gate.is_allowed(int(allowed["id"]), now=utcnow())


def test_cache_manual_invalidation_applies_suspend_immediately(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    gate = TenantRuntimeGate(conn, ttl_seconds=60)
    assert gate.is_allowed(int(tenant["id"]))
    conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant["id"],))
    gate.invalidate(int(tenant["id"]))
    assert not gate.is_allowed(int(tenant["id"]))
