"""Concurrent renewal must serialize and never lose an increment."""

from __future__ import annotations

from datetime import timedelta

from Database.connection import connect
from Database.repositories import LicenseRepository
from LicenseService.service import renew_license
from Shared.timeutils import parse_utc, utcnow


def test_two_sequential_renewals_accumulate(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30)
    base = parse_utc(lic["expires_at"])
    first = renew_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        extra_days=10, actor_id=1,
    )
    second = renew_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        extra_days=10, actor_id=1,
    )
    assert parse_utc(first["expires_at"]) == base + timedelta(days=10)
    assert parse_utc(second["expires_at"]) == base + timedelta(days=20)


def test_cas_rejects_stale_expiry(conn, factories) -> None:
    """Simulates the pre-write of a concurrent renew: the second CAS must fail."""
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30)
    stale_expires = lic["expires_at"]
    repo = LicenseRepository(conn)
    # Writer A commits +5 days.
    assert repo.cas_renew(
        int(lic["id"]), expected_status="active", expected_expires_at=stale_expires,
        new_expires_at="2099-01-05T00:00:00+00:00", new_grace_until=None,
    ) is True
    # Writer B still holds the stale expiry -> CAS refused, no lost update.
    assert repo.cas_renew(
        int(lic["id"]), expected_status="active", expected_expires_at=stale_expires,
        new_expires_at="2099-01-06T00:00:00+00:00", new_grace_until=None,
    ) is False


def test_two_connections_renew_do_not_lose_update(db_path, factories) -> None:
    """Two independent connections renew the same license; both increments land."""
    writer_a = connect(db_path)
    writer_b = connect(db_path)
    try:
        tenant = factories.tenant()
        plan = factories.plan()
        lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30)
        base = parse_utc(lic["expires_at"])

        first = renew_license(
            writer_a, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
            extra_days=10, actor_id=1,
        )
        second = renew_license(
            writer_b, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
            extra_days=10, actor_id=1,
        )
        assert parse_utc(second["expires_at"]) > parse_utc(first["expires_at"])
        final = LicenseRepository(connect(db_path)).get_by_id(int(lic["id"]))
        assert parse_utc(final["expires_at"]) == base + timedelta(days=20)
    finally:
        writer_a.close()
        writer_b.close()
