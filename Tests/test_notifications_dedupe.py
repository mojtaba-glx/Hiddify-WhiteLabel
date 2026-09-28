"""Warning schedule: 7/3/1-day stages, one delivery per event_key."""

from __future__ import annotations

from datetime import timedelta

from Database.repositories import NotificationRepository
from LicenseService.states import WARNING_DAYS, due_warnings, warning_event_key
from Shared.timeutils import iso_utc, parse_utc, utcnow


def test_warning_days_constant() -> None:
    assert tuple(WARNING_DAYS) == (7, 3, 1)


def test_due_stages() -> None:
    now = utcnow()
    assert due_warnings(now=now, expires_at=now + timedelta(days=7, hours=1)) == [7]
    assert due_warnings(now=now, expires_at=now + timedelta(days=7)) == [7]
    assert due_warnings(now=now, expires_at=now + timedelta(days=2)) == [7, 3]
    assert due_warnings(now=now, expires_at=now - timedelta(days=1)) == []


def test_due_stages_exact() -> None:
    now = utcnow()
    assert 7 in due_warnings(now=now, expires_at=now + timedelta(days=6))
    assert 3 in due_warnings(now=now, expires_at=now + timedelta(days=3))
    assert 1 in due_warnings(now=now, expires_at=now + timedelta(days=1))
    assert 7 not in due_warnings(now=now, expires_at=now + timedelta(days=8))


def test_event_key_unique_per_stage() -> None:
    assert warning_event_key(10, 7) != warning_event_key(10, 3)
    assert warning_event_key(10, 7) != warning_event_key(11, 7)
    assert warning_event_key(10, 7) == "license:10:expiring:7d"


def test_enqueue_dedupes_by_event_key(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = iso_utc(utcnow())
    first = repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="license.expiring",
        event_key="license:1:expiring:7d", scheduled_at=now,
    )
    conn.commit()
    second = repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="license.expiring",
        event_key="license:1:expiring:7d", scheduled_at=now,
    )
    conn.commit()
    assert first is not None and second is not None
    assert int(first["id"]) == int(second["id"])
    count = conn.execute(
        "SELECT COUNT(*) AS total FROM notification_events WHERE event_key=?",
        ("license:1:expiring:7d",),
    ).fetchone()["total"]
    assert int(count) == 1


def test_sent_items_leave_pending_queue(conn, factories) -> None:
    tenant = factories.tenant()
    repo = NotificationRepository(conn)
    now = utcnow()
    repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="license.expiring",
        event_key="license:9:expiring:3d", scheduled_at=iso_utc(now),
    )
    conn.commit()
    assert len(repo.list_pending(now_iso=iso_utc(now))) == 1
    repo.mark_sent("license:9:expiring:3d", sent_at=iso_utc(now))
    conn.commit()
    assert repo.list_pending(now_iso=iso_utc(now)) == []
    row = conn.execute(
        "SELECT * FROM notification_events WHERE event_key='license:9:expiring:3d'"
    ).fetchone()
    assert row["status"] == "sent" and row["sent_at"]
    _ = parse_utc(row["scheduled_at"])  # stored timestamps stay UTC-parseable
