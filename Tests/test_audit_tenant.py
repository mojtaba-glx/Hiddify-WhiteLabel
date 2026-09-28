"""License operations must reject a tenant_id that does not own the row."""

from __future__ import annotations

import pytest

from Database.repositories import LicenseRepository, TenantMismatchError
from LicenseService.service import renew_license, transition_license


def test_transition_wrong_tenant_no_write(conn, factories) -> None:
    owner = factories.tenant()
    other = factories.tenant()
    plan = factories.plan()
    lic = factories.license(owner["id"], plan["id"], status="pending")
    with pytest.raises(TenantMismatchError):
        transition_license(
            conn, license_id=int(lic["id"]), tenant_id=int(other["id"]),
            new_status="active", actor_id=1,
        )
    assert LicenseRepository(conn).get_by_id(int(lic["id"]))["status"] == "pending"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE entity_id=?", (str(lic["id"]),)
    ).fetchone()["n"] == 0


def test_renew_wrong_tenant_no_write(conn, factories) -> None:
    owner = factories.tenant()
    other = factories.tenant()
    plan = factories.plan()
    lic = factories.license(owner["id"], plan["id"], status="active")
    original = lic["expires_at"]
    with pytest.raises(TenantMismatchError):
        renew_license(
            conn, license_id=int(lic["id"]), tenant_id=int(other["id"]),
            extra_days=30, actor_id=1,
        )
    assert LicenseRepository(conn).get_by_id(int(lic["id"]))["expires_at"] == original


def test_correct_tenant_audit_records_real_owner(conn, factories) -> None:
    owner = factories.tenant()
    plan = factories.plan()
    lic = factories.license(owner["id"], plan["id"], status="pending")
    transition_license(
        conn, license_id=int(lic["id"]), tenant_id=int(owner["id"]),
        new_status="active", actor_id=1,
    )
    audit = conn.execute(
        "SELECT * FROM audit_events WHERE entity_id=?", (str(lic["id"]),)
    ).fetchone()
    assert int(audit["tenant_id"]) == int(owner["id"])
