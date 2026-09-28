"""Transactions: rollback on failure; audited writes stay atomic."""

from __future__ import annotations

import pytest

from Database.connection import transaction
from Database.repositories import AuditRepository, LicenseRepository, TenantRepository
from LicenseService.service import renew_license


def test_failed_multi_step_rolls_back(conn, factories) -> None:
    tenants = TenantRepository(conn)
    before = tenants.count()
    try:
        with transaction(conn):
            tenants.create(
                name="Rollback",
                slug="rollback-tenant",
                owner_telegram_id=42,
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert tenants.count() == before


def test_sensitive_audit_aborts_status_change(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    repo = LicenseRepository(conn)
    audits = AuditRepository(conn)
    with pytest.raises(ValueError):
        with transaction(conn):
            assert repo.transition(int(lic["id"]), expected="active", new="suspended") is True
            audits.append(
                actor_id=1, tenant_id=int(tenant["id"]), action="license.transition",
                entity_type="license", entity_id=str(lic["id"]),
                metadata={"bot_token": "999:should-never-persist-abcdefghijklmnopqrstuvwxyz"},
            )
    assert repo.get_by_id(int(lic["id"]))["status"] == "active"
    assert audits.list_by_tenant(int(tenant["id"])) == []


def test_renew_is_atomic_with_audit(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="grace")
    updated = renew_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        extra_days=30, grace_days=2, actor_id=3,
    )
    conn.commit()
    assert updated["status"] == "active"
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE action='license.renew' AND entity_id=?",
        (str(lic["id"]),),
    ).fetchall()
    assert len(rows) == 1


def test_audit_rejects_sensitive_metadata(conn, factories) -> None:
    tenant = factories.tenant()
    audits = AuditRepository(conn)
    with pytest.raises(ValueError):
        audits.append(
            actor_id=1, tenant_id=int(tenant["id"]), action="test",
            entity_type="tenant", entity_id=str(tenant["id"]),
            metadata={"webhook_secret": "abc"},
        )
    conn.rollback()
    assert audits.list_by_tenant(int(tenant["id"])) == []
