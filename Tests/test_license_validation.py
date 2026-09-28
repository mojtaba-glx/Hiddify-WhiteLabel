"""License date validation and the single-current-license policy."""

from __future__ import annotations

import pytest

from Database.repositories import (
    LicenseConflictError,
    canonical_license_range,
    LicenseRepository,
)


def test_canonical_range_normalizes_to_utc() -> None:
    starts, expires, grace = canonical_license_range(
        "2026-01-01T03:30:00+03:30",  # equals 2026-01-01T00:00:00Z
        "2026-02-01T03:30:00+03:30",
        "2026-02-04T03:30:00+03:30",
    )
    assert starts == "2026-01-01T00:00:00+00:00"
    assert expires == "2026-02-01T00:00:00+00:00"
    assert grace == "2026-02-04T00:00:00+00:00"


def test_expires_must_beat_starts() -> None:
    with pytest.raises(ValueError):
        canonical_license_range("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        canonical_license_range("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")


def test_grace_must_not_precede_expiry() -> None:
    with pytest.raises(ValueError):
        canonical_license_range(
            "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-01-15T00:00:00Z",
        )


def test_invalid_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_license_range("not-a-date", "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        canonical_license_range("2026-01-01T00:00:00Z", "")
    with pytest.raises(ValueError):
        canonical_license_range("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "garbage")


def test_repository_rejects_bad_range(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    from Database.repositories import LicenseRepository as LR

    with pytest.raises(ValueError):
        LR(conn).create(
            tenant_id=int(tenant["id"]), plan_id=int(plan["id"]), status="pending",
            starts_at="2026-02-01T00:00:00Z", expires_at="2026-01-01T00:00:00Z",
        )


def test_one_current_license_per_tenant_rejected(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    with pytest.raises(LicenseConflictError):
        LicenseRepository(conn).create(
            tenant_id=int(tenant["id"]), plan_id=int(plan["id"]), status="grace",
            starts_at="2030-01-01T00:00:00Z", expires_at="2030-02-01T00:00:00Z",
        )


def test_history_rows_allowed(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="expired")
    factories.license(tenant["id"], plan["id"], status="cancelled")
    factories.license(tenant["id"], plan["id"], status="active")
    rows = LicenseRepository(conn).list_by_tenant(int(tenant["id"]))
    assert len(rows) == 3
    assert LicenseRepository(conn).current_usable(int(tenant["id"]))["status"] == "active"
