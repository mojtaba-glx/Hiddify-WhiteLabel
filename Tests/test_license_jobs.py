"""Phase-3 evaluator, durable warnings, retry and stale-period handling."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from Database.repositories import AuditRepository, NotificationRepository
from LicenseService.jobs import LicenseJobRunner
from LicenseService.states import warning_event_key
from LicenseService.service import renew_license
from Shared.timeutils import iso_utc, parse_utc, utcnow


class RecordingSender:
    def __init__(self) -> None:
        self.sent = []
        self.fail = False
        self.fail_keys: set[str] = set()

    async def send(self, warning) -> None:
        if self.fail or warning.event_key in self.fail_keys:
            raise RuntimeError("master delivery unavailable")
        self.sent.append(warning)


def _runner(conn, sender=None, **kwargs):
    return LicenseJobRunner(
        conn,
        sender=sender or RecordingSender(),
        retry_base_seconds=30,
        **kwargs,
    )


def test_warning_scheduler_is_idempotent_and_delivers(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    license_row = factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=7
    )
    sender = RecordingSender()
    runner = _runner(conn, sender)
    now = utcnow()
    first = asyncio.run(runner.run_once(now=now))
    second = asyncio.run(runner.run_once(now=now + timedelta(seconds=1)))
    assert first.enqueued == 1 and first.sent == 1
    assert second.enqueued == 0 and second.sent == 0
    assert len(sender.sent) == 1 and sender.sent[0].days_before == 7
    rows = conn.execute(
        "SELECT * FROM notification_events WHERE tenant_id = ?", (int(tenant["id"]),)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "sent"
    assert rows[0]["event_key"] == warning_event_key(
        int(license_row["id"]), 7, expires_at=str(license_row["expires_at"])
    )


def test_future_license_does_not_warn_before_start(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=1, duration_days=1
    )
    report = _runner(conn).evaluate_licenses(now=utcnow())
    assert report.enqueued == 0
    assert conn.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == 0


def test_expired_license_is_suspended_without_affecting_other_tenant(conn, factories) -> None:
    plan = factories.plan()
    expired_tenant = factories.tenant()
    healthy_tenant = factories.tenant()
    expired = factories.license(
        expired_tenant["id"], plan["id"], status="active", starts_in_days=-10, duration_days=1
    )
    healthy = factories.license(
        healthy_tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=30
    )
    report = _runner(conn).evaluate_licenses(now=utcnow())
    expired_now = conn.execute("SELECT * FROM licenses WHERE id = ?", (expired["id"],)).fetchone()
    healthy_now = conn.execute("SELECT * FROM licenses WHERE id = ?", (healthy["id"],)).fetchone()
    assert expired_now["status"] == "suspended" and expired_now["suspended_at"]
    assert healthy_now["status"] == "active"
    assert report.transitioned == 1 and report.suspended == 1 and report.errors == 0
    transitions = AuditRepository(conn).list_by_tenant(int(expired_tenant["id"]))
    assert [row["action"] for row in transitions] == ["license.transition", "license.transition"]


def test_active_enters_grace_then_suspends_after_grace(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    row = factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-10,
        duration_days=5, grace_days=10,
    )
    runner = _runner(conn)
    during_grace = parse_utc(row["expires_at"]) + timedelta(days=1)
    first = runner.evaluate_licenses(now=during_grace)
    assert conn.execute("SELECT status FROM licenses WHERE id=?", (row["id"],)).fetchone()[0] == "grace"
    assert first.transitioned == 1 and first.suspended == 0
    after_grace = parse_utc(row["grace_until"]) + timedelta(seconds=1)
    second = runner.evaluate_licenses(now=after_grace)
    assert conn.execute("SELECT status FROM licenses WHERE id=?", (row["id"],)).fetchone()[0] == "suspended"
    assert second.transitioned == 1 and second.suspended == 1


def test_renewal_gets_new_warning_period_and_old_events_are_skipped(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    row = factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=3
    )
    sender = RecordingSender()
    runner = _runner(conn, sender)
    now = utcnow()
    old_report = runner.evaluate_licenses(now=now)
    assert old_report.enqueued == 3  # 7d, 3d and 1d catch-up stages
    renewed = renew_license(
        conn,
        license_id=int(row["id"]), tenant_id=int(tenant["id"]), extra_days=1,
        actor_id=99, now=now,
    )
    new_report = runner.evaluate_licenses(now=now)
    assert new_report.enqueued == 2
    report = asyncio.run(runner.deliver_notifications(now=now))
    assert report.skipped == 3 and report.sent == 2
    assert len(sender.sent) == 2
    assert all(item.expires_at == renewed["expires_at"] for item in sender.sent)


def test_sender_failure_retries_with_backoff_then_succeeds(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=7
    )
    sender = RecordingSender()
    sender.fail = True
    runner = _runner(conn, sender, max_retries=3)
    now = utcnow()
    first = asyncio.run(runner.run_once(now=now))
    event = conn.execute("SELECT * FROM notification_events").fetchone()
    assert first.retried == 1 and event["status"] == "failed" and event["retry_count"] == 1
    assert parse_utc(event["next_attempt_at"]) == now + timedelta(seconds=30)
    early = asyncio.run(runner.deliver_notifications(now=now + timedelta(seconds=29)))
    assert early.claimed == 0
    sender.fail = False
    later = asyncio.run(runner.deliver_notifications(now=now + timedelta(seconds=30)))
    assert later.requeued == 1 and later.sent == 1


def test_retry_limit_leaves_failed_event_without_deadline(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=7
    )
    sender = RecordingSender()
    sender.fail = True
    runner = _runner(conn, sender, max_retries=2)
    now = utcnow()
    asyncio.run(runner.run_once(now=now))
    final = asyncio.run(runner.deliver_notifications(now=now + timedelta(seconds=30)))
    event = conn.execute("SELECT * FROM notification_events").fetchone()
    assert final.abandoned == 1
    assert event["status"] == "failed" and event["retry_count"] == 2
    assert event["next_attempt_at"] is None


def test_stale_processing_lease_is_recovered(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    row = factories.license(
        tenant["id"], plan["id"], status="active", starts_in_days=-1, duration_days=7
    )
    now = utcnow()
    key = warning_event_key(int(row["id"]), 7, expires_at=str(row["expires_at"]))
    repo = NotificationRepository(conn)
    repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="license.expiring",
        event_key=key, scheduled_at=iso_utc(now - timedelta(minutes=5)),
    )
    repo.claim(key, now_iso=iso_utc(now - timedelta(minutes=5)))
    sender = RecordingSender()
    report = asyncio.run(_runner(conn, sender, lease_seconds=60).deliver_notifications(now=now))
    assert report.recovered == 1 and report.requeued == 1 and report.sent == 1
    assert len(sender.sent) == 1


def test_malformed_or_stale_notification_is_skipped(conn, factories) -> None:
    tenant = factories.tenant()
    now = utcnow()
    repo = NotificationRepository(conn)
    repo.enqueue(
        tenant_id=int(tenant["id"]), event_type="bad", event_key="bad:event",
        scheduled_at=iso_utc(now),
    )
    report = asyncio.run(_runner(conn).deliver_notifications(now=now))
    assert report.skipped == 1
    assert repo.get_by_key("bad:event")["status"] == "skipped"


def test_one_delivery_failure_does_not_stop_other_tenants(conn, factories) -> None:
    plan = factories.plan()
    tenants = [factories.tenant(), factories.tenant()]
    rows = [
        factories.license(t["id"], plan["id"], status="active", starts_in_days=-1, duration_days=7)
        for t in tenants
    ]
    sender = RecordingSender()
    sender.fail_keys.add(warning_event_key(
        int(rows[0]["id"]), 7, expires_at=str(rows[0]["expires_at"])
    ))
    report = asyncio.run(_runner(conn, sender).run_once(now=utcnow()))
    assert report.sent == 1 and report.retried == 1 and report.errors == 0
    assert sender.sent[0].tenant_id == int(tenants[1]["id"])


def test_closed_database_is_reported_without_job_crash(conn) -> None:
    runner = _runner(conn)
    conn.close()
    report = asyncio.run(runner.run_once(now=utcnow()))
    assert report.errors >= 2
