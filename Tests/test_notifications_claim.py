"""Notification queue: atomic claim, retry with deadline, cross-tenant safety."""

from __future__ import annotations

from datetime import timedelta

import pytest

from Database.repositories import NotificationRepository, TenantMismatchError
from Shared.timeutils import iso_utc, utcnow


def _enqueue(repo, tenant_id, key, *, when=None):
    return repo.enqueue(
        tenant_id=tenant_id, event_type="license.expiring",
        event_key=key, scheduled_at=iso_utc(when or utcnow()),
    )


def test_claim_is_single_winner(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    _enqueue(repo, int(tenant["id"]), "license:5:expiring:7d")
    now = iso_utc(utcnow())
    assert repo.claim("license:5:expiring:7d", now_iso=now) is True
    assert repo.claim("license:5:expiring:7d", now_iso=now) is False  # second worker loses
    row = conn.execute(
        "SELECT * FROM notification_events WHERE event_key='license:5:expiring:7d'"
    ).fetchone()
    assert row["status"] == "processing" and row["locked_at"] == now


def test_failed_preserves_retry_deadline(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    _enqueue(repo, int(tenant["id"]), "license:6:expiring:3d")
    now = utcnow()
    repo.claim("license:6:expiring:3d", now_iso=iso_utc(now))
    deadline = iso_utc(now + timedelta(minutes=30))
    assert repo.mark_failed("license:6:expiring:3d", next_attempt_at=deadline) is True
    row = conn.execute(
        "SELECT * FROM notification_events WHERE event_key='license:6:expiring:3d'"
    ).fetchone()
    assert row["status"] == "failed"
    assert int(row["retry_count"]) == 1
    assert row["next_attempt_at"] == deadline
    assert row["locked_at"] is None


def test_retryable_listing_and_requeue(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    _enqueue(repo, int(tenant["id"]), "license:7:expiring:1d")
    now = utcnow()
    repo.claim("license:7:expiring:1d", now_iso=iso_utc(now))
    repo.mark_failed("license:7:expiring:1d", next_attempt_at=iso_utc(now + timedelta(minutes=5)))
    # Not yet due.
    assert repo.list_retryable(now_iso=iso_utc(now)) == []
    # Due now.
    later = iso_utc(now + timedelta(minutes=10))
    due = repo.list_retryable(now_iso=later)
    assert len(due) == 1 and due[0]["event_key"] == "license:7:expiring:1d"
    assert repo.requeue("license:7:expiring:1d", not_before=later) is True
    assert repo.claim("license:7:expiring:1d", now_iso=later) is True


def test_duplicate_event_other_tenant_rejected(conn, factories) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    repo = NotificationRepository(conn)
    first = _enqueue(repo, int(tenant_a["id"]), "license:8:expiring:7d")
    second = _enqueue(repo, int(tenant_a["id"]), "license:8:expiring:7d")
    assert int(first["id"]) == int(second["id"])  # idempotent same tenant
    with pytest.raises(TenantMismatchError):
        _enqueue(repo, int(tenant_b["id"]), "license:8:expiring:7d")
    # The original row is untouched and still owned by tenant A.
    row = conn.execute(
        "SELECT * FROM notification_events WHERE event_key='license:8:expiring:7d'"
    ).fetchone()
    assert int(row["tenant_id"]) == int(tenant_a["id"])


def test_mark_sent_only_from_live_states(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = iso_utc(utcnow())
    _enqueue(repo, int(tenant["id"]), "license:9:expiring:7d")
    assert repo.mark_sent("license:9:expiring:7d", sent_at=now) is True
    assert repo.mark_sent("license:9:expiring:7d", sent_at=now) is False  # already sent
