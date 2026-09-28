"""Expiry and grace evaluation across the clock (no writes)."""

from __future__ import annotations

from datetime import timedelta

from LicenseService.service import evaluate_license, is_tenant_usable
from Shared.timeutils import parse_utc, utcnow


def _at(lic, **kwargs):
    row = dict(lic)
    row.update(kwargs)
    return row


def test_active_before_expiry(factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    assert evaluate_license(lic, now=utcnow()) == "active"
    assert is_tenant_usable(lic, now=utcnow()) is True


def test_grace_between_expiry_and_grace_end(factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30, grace_days=7)
    expires = parse_utc(lic["expires_at"])
    assert evaluate_license(lic, now=expires + timedelta(days=1)) == "grace"
    assert is_tenant_usable(lic, now=expires + timedelta(days=1)) is True


def test_expired_after_grace_without_grace_config(factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30, grace_days=0)
    expires = parse_utc(lic["expires_at"])
    assert evaluate_license(lic, now=expires + timedelta(seconds=1)) == "expired"
    assert is_tenant_usable(lic, now=expires + timedelta(seconds=1)) is False


def test_expired_after_grace_end(factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active", duration_days=30, grace_days=3)
    grace_end = parse_utc(lic["grace_until"])
    assert evaluate_license(lic, now=grace_end + timedelta(seconds=1)) == "expired"


def test_manual_states_are_sticky(factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    for status, usable in (("pending", False), ("suspended", False), ("cancelled", False), ("expired", False)):
        lic = factories.license(tenant["id"], plan["id"], status=status)
        assert evaluate_license(lic, now=utcnow()) == status
        assert is_tenant_usable(lic, now=utcnow()) is usable
    assert is_tenant_usable(None, now=utcnow()) is False
