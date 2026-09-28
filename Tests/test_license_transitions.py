"""License transition rules: allowed map + atomic compare-and-set."""

from __future__ import annotations

import pytest

from Database.repositories import LicenseRepository
from LicenseService import states
from LicenseService.service import LicenseTransitionError, transition_license


def test_allowed_matrix() -> None:
    assert states.can_transition("pending", "active")
    assert states.can_transition("pending", "cancelled")
    assert states.can_transition("active", "grace")
    assert states.can_transition("active", "expired")
    assert states.can_transition("grace", "active")
    assert states.can_transition("expired", "active")
    assert states.can_transition("suspended", "active")


def test_illegal_matrix() -> None:
    assert not states.can_transition("pending", "grace")
    assert not states.can_transition("pending", "expired")
    assert not states.can_transition("active", "pending")
    assert not states.can_transition("cancelled", "active")
    assert not states.can_transition("active", "active")
    assert not states.can_transition("nope", "active")
    assert not states.can_transition("active", "nope")


def test_legal_transition_writes_and_audits(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="pending")
    updated = transition_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        new_status="active", actor_id=1,
    )
    conn.commit()
    assert updated["status"] == "active"
    audits = conn.execute(
        "SELECT * FROM audit_events WHERE entity_type='license' AND entity_id=?",
        (str(lic["id"]),),
    ).fetchall()
    assert len(audits) == 1
    assert "active" in (audits[0]["safe_metadata"] or "")


def test_illegal_transition_raises_and_keeps_status(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="pending")
    with pytest.raises(LicenseTransitionError):
        transition_license(
            conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
            new_status="grace", actor_id=1,
        )
    conn.rollback()
    assert LicenseRepository(conn).get_by_id(int(lic["id"]))["status"] == "pending"


def test_atomic_compare_and_set_loses_race(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    repo = LicenseRepository(conn)
    # Simulate a concurrent change: status moved under us.
    assert repo.transition(int(lic["id"]), expected="active", new="suspended") is True
    # Stale expectation no longer matches -> False, no write.
    assert repo.transition(int(lic["id"]), expected="active", new="grace") is False
    conn.commit()
    assert repo.get_by_id(int(lic["id"]))["status"] == "suspended"
