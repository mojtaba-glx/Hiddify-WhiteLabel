"""Repository primitives for Phase-3 lease recovery and bounded retry."""

from __future__ import annotations

from datetime import timedelta

from Database.repositories import NotificationRepository
from Shared.timeutils import iso_utc, utcnow


def test_recover_stale_does_not_touch_fresh_lease(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = utcnow()
    for suffix, lock_time in (("old", now - timedelta(minutes=5)), ("fresh", now)):
        key = f"lease:{suffix}"
        repo.enqueue(
            tenant_id=int(tenant["id"]), event_type="test", event_key=key,
            scheduled_at=iso_utc(lock_time),
        )
        repo.claim(key, now_iso=iso_utc(lock_time))
    changed = repo.recover_stale(
        stale_before=iso_utc(now - timedelta(minutes=2)), retry_at=iso_utc(now)
    )
    assert changed == 1
    assert repo.get_by_key("lease:old")["status"] == "failed"
    assert repo.get_by_key("lease:old")["retry_count"] == 1
    assert repo.get_by_key("lease:fresh")["status"] == "processing"


def test_requeue_due_respects_deadline_and_limit(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = utcnow()
    for number in range(3):
        key = f"retry:{number}"
        repo.enqueue(
            tenant_id=int(tenant["id"]), event_type="test", event_key=key,
            scheduled_at=iso_utc(now - timedelta(minutes=1)),
        )
        repo.claim(key, now_iso=iso_utc(now))
        repo.mark_failed(
            key,
            next_attempt_at=iso_utc(now if number < 2 else now + timedelta(minutes=5)),
        )
    assert repo.requeue_due(now_iso=iso_utc(now), limit=1) == 1
    statuses = [repo.get_by_key(f"retry:{number}")["status"] for number in range(3)]
    assert statuses.count("pending") == 1 and statuses.count("failed") == 2
