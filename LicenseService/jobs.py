"""Periodic license evaluation and durable warning delivery."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from Database.repositories import LicenseRepository, NotificationRepository, TenantRepository
from LicenseService import states
from LicenseService.runtime import TenantRuntimeGate
from LicenseService.service import LicenseTransitionError, evaluate_license, transition_license
from Shared.redaction import get_logger
from Shared.timeutils import ensure_utc, iso_utc, parse_utc, utcnow

logger = get_logger(__name__)
SYSTEM_ACTOR_ID = 0
_WARNING_KEY = re.compile(
    r"^license:(?P<license_id>[1-9][0-9]*):expiring:(?P<days>7|3|1)d:"
    r"(?P<period>[a-f0-9]{16})$"
)


@dataclass(frozen=True)
class LicenseWarning:
    event_key: str
    tenant_id: int
    tenant_name: str
    license_id: int
    days_before: int
    expires_at: str


class NotificationSender(Protocol):
    async def send(self, warning: LicenseWarning) -> None:
        """Deliver one warning or raise a safe exception for retry."""


@dataclass
class JobReport:
    evaluated: int = 0
    transitioned: int = 0
    suspended: int = 0
    enqueued: int = 0
    recovered: int = 0
    requeued: int = 0
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    abandoned: int = 0
    skipped: int = 0
    errors: int = 0


class LicenseJobRunner:
    """Idempotent evaluator plus at-least-once notification worker."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        sender: NotificationSender,
        runtime_gate: Optional[TenantRuntimeGate] = None,
        system_actor_id: int = SYSTEM_ACTOR_ID,
        lease_seconds: int = 120,
        max_retries: int = 5,
        retry_base_seconds: int = 30,
        batch_size: int = 200,
        auto_suspend_expired: bool = True,
    ) -> None:
        self.conn = conn
        self.sender = sender
        self.runtime_gate = runtime_gate
        self.system_actor_id = int(system_actor_id)
        self.lease_seconds = max(10, int(lease_seconds))
        self.max_retries = max(1, int(max_retries))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.batch_size = max(1, min(int(batch_size), 500))
        self.auto_suspend_expired = bool(auto_suspend_expired)

    def _move(
        self,
        license_id: int,
        tenant_id: int,
        *,
        expected: str,
        target: str,
    ) -> tuple[dict[str, Any], bool]:
        current = LicenseRepository(self.conn).get_by_id(license_id)
        if current is None:
            raise LicenseTransitionError("license not found")
        if current["status"] != expected:
            return current, False
        try:
            moved = transition_license(
                self.conn,
                license_id=license_id,
                tenant_id=tenant_id,
                new_status=target,
                actor_id=self.system_actor_id,
            )
            if self.runtime_gate is not None:
                self.runtime_gate.invalidate(tenant_id)
            return moved, True
        except LicenseTransitionError:
            # A second runner may have won the CAS. Treat its committed state
            # as progress instead of turning one tenant race into a job crash.
            fresh = LicenseRepository(self.conn).get_by_id(license_id)
            if fresh is not None and fresh["status"] != expected:
                return fresh, False
            raise

    def _enqueue_due_warnings(
        self, row: dict[str, Any], *, now: datetime, report: JobReport
    ) -> None:
        if row["status"] != states.ACTIVE:
            return
        if now < parse_utc(str(row["starts_at"])):
            return
        expires = parse_utc(str(row["expires_at"]))
        if expires < now:
            return
        repo = NotificationRepository(self.conn)
        for days in states.due_warnings(now=now, expires_at=expires):
            key = states.warning_event_key(
                int(row["id"]), days, expires_at=str(row["expires_at"])
            )
            changes = self.conn.total_changes
            repo.enqueue(
                tenant_id=int(row["tenant_id"]),
                event_type="license.expiring",
                event_key=key,
                scheduled_at=iso_utc(now),
            )
            if self.conn.total_changes > changes:
                report.enqueued += 1

    def evaluate_licenses(
        self, *, now: Optional[datetime] = None, report: Optional[JobReport] = None
    ) -> JobReport:
        moment = ensure_utc(now) if now is not None else utcnow()
        result = report or JobReport()
        after_id = 0
        while True:
            try:
                batch = LicenseRepository(self.conn).list_for_evaluation(
                    after_id=after_id, limit=self.batch_size
                )
            except Exception:
                result.errors += 1
                logger.error("License evaluation query failed")
                return result
            if not batch:
                break
            for snapshot in batch:
                after_id = max(after_id, int(snapshot["id"]))
                result.evaluated += 1
                try:
                    current = LicenseRepository(self.conn).get_by_id(int(snapshot["id"]))
                    if current is None:
                        continue
                    self._enqueue_due_warnings(current, now=moment, report=result)
                    effective = evaluate_license(current, now=moment)
                    if effective != current["status"]:
                        current, changed = self._move(
                            int(current["id"]),
                            int(current["tenant_id"]),
                            expected=str(current["status"]),
                            target=effective,
                        )
                        result.transitioned += int(changed)
                    if self.auto_suspend_expired and current["status"] == states.EXPIRED:
                        current, changed = self._move(
                            int(current["id"]),
                            int(current["tenant_id"]),
                            expected=states.EXPIRED,
                            target=states.SUSPENDED,
                        )
                        result.suspended += int(changed)
                except Exception as exc:
                    result.errors += 1
                    logger.error(
                        "License evaluation failed for id=%s (%s)",
                        int(snapshot["id"]),
                        type(exc).__name__,
                    )
            if len(batch) < self.batch_size:
                break
        return result

    def _build_warning(
        self, event: dict[str, Any], *, now: datetime
    ) -> Optional[LicenseWarning]:
        match = _WARNING_KEY.fullmatch(str(event["event_key"]))
        if match is None:
            return None
        license_id = int(match.group("license_id"))
        days = int(match.group("days"))
        license_row = LicenseRepository(self.conn).get_by_id(license_id)
        if license_row is None or int(license_row["tenant_id"]) != int(event["tenant_id"]):
            return None
        expected_key = states.warning_event_key(
            license_id, days, expires_at=str(license_row["expires_at"])
        )
        if expected_key != event["event_key"]:
            return None
        if license_row["status"] not in (states.ACTIVE, states.GRACE):
            return None
        expires = parse_utc(str(license_row["expires_at"]))
        if expires < now or days not in states.due_warnings(now=now, expires_at=expires):
            return None
        tenant = TenantRepository(self.conn).get_by_id(int(event["tenant_id"]))
        if tenant is None:
            return None
        return LicenseWarning(
            event_key=str(event["event_key"]),
            tenant_id=int(event["tenant_id"]),
            tenant_name=str(tenant["name"]),
            license_id=license_id,
            days_before=days,
            expires_at=str(license_row["expires_at"]),
        )

    async def deliver_notifications(
        self, *, now: Optional[datetime] = None, report: Optional[JobReport] = None
    ) -> JobReport:
        moment = ensure_utc(now) if now is not None else utcnow()
        result = report or JobReport()
        repo = NotificationRepository(self.conn)
        now_iso = iso_utc(moment)
        try:
            stale_before = iso_utc(moment - timedelta(seconds=self.lease_seconds))
            result.recovered += repo.recover_stale(
                stale_before=stale_before, retry_at=now_iso
            )
            result.requeued += repo.requeue_due(now_iso=now_iso, limit=self.batch_size)
            pending = repo.list_pending(now_iso=now_iso, limit=self.batch_size)
        except Exception:
            result.errors += 1
            logger.error("Notification queue preparation failed")
            return result

        for candidate in pending:
            key = str(candidate["event_key"])
            try:
                if not repo.claim(key, now_iso=now_iso):
                    continue
                result.claimed += 1
                event = repo.get_by_key(key)
                if event is None:
                    continue
                if int(event["retry_count"]) >= self.max_retries:
                    repo.abandon(key)
                    result.abandoned += 1
                    continue
                warning = self._build_warning(event, now=moment)
                if warning is None:
                    repo.mark_skipped(key)
                    result.skipped += 1
                    continue
                try:
                    await self.sender.send(warning)
                except Exception as exc:
                    attempt = int(event["retry_count"]) + 1
                    retry_at = None
                    if attempt < self.max_retries:
                        delay = self.retry_base_seconds * (2 ** min(attempt - 1, 6))
                        retry_at = iso_utc(moment + timedelta(seconds=delay))
                    repo.mark_failed(key, next_attempt_at=retry_at)
                    result.retried += int(retry_at is not None)
                    result.abandoned += int(retry_at is None)
                    logger.warning(
                        "Notification delivery failed for event id=%s (%s)",
                        int(event["id"]),
                        type(exc).__name__,
                    )
                    continue
                if repo.mark_sent(key, sent_at=now_iso):
                    result.sent += 1
                else:
                    result.errors += 1
            except Exception as exc:
                result.errors += 1
                logger.error(
                    "Notification processing failed for event id=%s (%s)",
                    int(candidate["id"]),
                    type(exc).__name__,
                )
        return result

    async def run_once(self, *, now: Optional[datetime] = None) -> JobReport:
        report = self.evaluate_licenses(now=now)
        return await self.deliver_notifications(now=now, report=report)
