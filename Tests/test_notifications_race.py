"""Notification: race-safe enqueue, claim checks scheduled_at."""

from __future__ import annotations

from datetime import timedelta

import pytest

from Database.connection import connect
from Database.repositories import NotificationRepository
from Shared.timeutils import iso_utc, utcnow


def test_two_connections_enqueue_same_key_first_wins(db_path, factories) -> None:
    """Two separate connections racing for the same event_key. Both calls succeed
    (the second sees the first's row after IntegrityError) and return the same row."""
    tenant = factories.tenant()
    key = "license:100:expiring:7d"
    now = iso_utc(utcnow())
    repo_a = NotificationRepository(connect(db_path))
    repo_b = NotificationRepository(connect(db_path))
    try:
        row_a = repo_a.enqueue(
            tenant_id=int(tenant["id"]), event_type="x", event_key=key, scheduled_at=now,
        )
        row_b = repo_b.enqueue(
            tenant_id=int(tenant["id"]), event_type="x", event_key=key, scheduled_at=now,
        )
        assert row_a and row_b
        assert row_a["id"] == row_b["id"]
        assert row_a["tenant_id"] == row_b["tenant_id"] == int(tenant["id"])
    finally:
        repo_a.conn.close()
        repo_b.conn.close()


def test_claim_respects_scheduled_at(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = utcnow()
    future = iso_utc(now + timedelta(hours=1))
    past = iso_utc(now - timedelta(minutes=5))
    repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="l", event_key="license:8:expiring:7d",
        scheduled_at=future,
    )
    repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="l", event_key="license:8:expiring:1d",
        scheduled_at=past,
    )
    conn.commit()
    # Claim with "now" before the future event — should only claim the past one
    now_iso = iso_utc(now)
    assert repo.claim("license:8:expiring:7d", now_iso=now_iso) is False
    assert repo.claim("license:8:expiring:1d", now_iso=now_iso) is True
    assert repo.claim("license:8:expiring:1d", now_iso=now_iso) is False  # already processing