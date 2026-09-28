"""Renewal: extend, re-activate, audit; cancelled is terminal."""

from __future__ import annotations

from datetime import timedelta

import pytest

from LicenseService.service import LicenseTransitionError, renew_license
from Shared.timeutils import parse_utc, utcnow


def test_renew_expired_reactivates(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="expired")
    old_expires = parse_utc(lic["expires_at"])
    updated = renew_license(conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]), extra_days=30, grace_days=3, actor_id=7)
    conn.commit()
    assert updated["status"] == "active"
    assert parse_utc(updated["expires_at"]) > old_expires
    assert updated["grace_until"] is not None
    assert updated["suspended_at"] is None
    audits = conn.execute(
        "SELECT * FROM audit_events WHERE action='license.renew' AND entity_id=?",
        (str(lic["id"]),),
    ).fetchall()
    assert len(audits) == 1


def test_renew_suspended_reactivates(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="suspended")
    updated = renew_license(conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]), extra_days=15, actor_id=7)
    conn.commit()
    assert updated["status"] == "active"


def test_renew_anchors_on_future_expiry(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30)
    old_expires = parse_utc(lic["expires_at"])
    updated = renew_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        extra_days=10, actor_id=7, now=utcnow(),
    )
    conn.commit()
    assert parse_utc(updated["expires_at"]) == old_expires + timedelta(days=10)


def test_renew_cancelled_is_rejected(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="cancelled")
    with pytest.raises(LicenseTransitionError):
        renew_license(conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]), extra_days=30, actor_id=7)
    conn.rollback()


def test_renew_validates_input(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    with pytest.raises(ValueError):
        renew_license(conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]), extra_days=0, actor_id=7)
    with pytest.raises(ValueError):
        renew_license(
            conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
            extra_days=5, grace_days=-1, actor_id=7,
        )
    conn.rollback()
