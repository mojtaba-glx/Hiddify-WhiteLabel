"""Periodic tenant subscription enforcement and durable reminder delivery.

Tenant ownership is deterministic by tenant_id modulo shard_count. Provider I/O
runs in a worker thread so Telegram polling stays responsive. Reminder sends are
async and use each tenant's own encrypted UserBot credential.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from Database.connection import connect
from Shared.crypto import TokenCipher
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.backup import (
    AutoBackupDelivery,
    AutoBackupSender,
    TelegramAutoBackupSender,
    complete_auto_backup_delivery,
    prepare_auto_backup_delivery,
)
from TenantRuntime.panels import PanelAdapter, build_default_panel_adapter
from TenantRuntime.reminders import (
    ReminderDelivery,
    ReminderSender,
    TelegramReminderSender,
    claim_due_deliveries,
    enqueue_due_reminders,
    finish_delivery,
    reconcile_reminder_queue_settings,
)


@dataclass
class LifecycleReport:
    tenants: int = 0
    scanned: int = 0
    synced: int = 0
    expired: int = 0
    enforcement_pending: int = 0
    reminders_enqueued: int = 0
    reminders_claimed: int = 0
    reminders_sent: int = 0
    reminders_retried: int = 0
    reminders_abandoned: int = 0
    reminders_skipped: int = 0
    backups_claimed: int = 0
    backups_sent: int = 0
    backups_failed: int = 0
    errors: int = 0


class TenantLifecycleCoordinator:
    def __init__(
        self,
        *,
        db_path: str,
        cipher: TokenCipher,
        shard_count: int,
        shard_index: int,
        panel_adapter_factory: Callable[[], PanelAdapter] = build_default_panel_adapter,
        reminder_sender: ReminderSender | None = None,
        backup_sender: AutoBackupSender | None = None,
        enforcer_batch_size: int = 30,
        hot_usage_ratio: float = 0.85,
        node_freeze_failures: int = 3,
        reminder_days: int = 3,
        reminder_remaining_gb: int = 3,
        reminder_lease_seconds: int = 120,
        reminder_max_retries: int = 5,
        reminder_retry_base_seconds: int = 30,
        reminder_batch_size: int = 100,
    ) -> None:
        self.db_path = str(db_path)
        self.cipher = cipher
        self.shard_count = int(shard_count)
        self.shard_index = int(shard_index)
        self.panel_adapter_factory = panel_adapter_factory
        self.reminder_sender = reminder_sender or TelegramReminderSender()
        self.backup_sender = backup_sender or TelegramAutoBackupSender()
        self.enforcer_batch_size = max(1, min(int(enforcer_batch_size), 500))
        self.hot_usage_ratio = min(max(float(hot_usage_ratio), 0.5), 1.0)
        self.node_freeze_failures = max(1, min(int(node_freeze_failures), 20))
        self.reminder_days = max(1, min(int(reminder_days), 30))
        self.reminder_remaining_gb = max(1, min(int(reminder_remaining_gb), 1000))
        self.reminder_lease_seconds = max(10, int(reminder_lease_seconds))
        self.reminder_max_retries = max(1, min(int(reminder_max_retries), 20))
        self.reminder_retry_base_seconds = max(
            1, min(int(reminder_retry_base_seconds), 3600)
        )
        self.reminder_batch_size = max(1, min(int(reminder_batch_size), 500))
        self._tenant_cursors: dict[int, int] = {}
        if self.shard_count <= 0 or not 0 <= self.shard_index < self.shard_count:
            raise ValueError("invalid lifecycle shard scope")

    async def run_once(self) -> LifecycleReport:
        report, deliveries, backup_deliveries = await asyncio.to_thread(
            self._prepare_sync
        )
        for delivery in deliveries:
            success = False
            try:
                await self.reminder_sender.send(delivery)
                success = True
            except Exception:
                success = False
            try:
                outcome = await asyncio.to_thread(
                    self._finish_delivery_sync,
                    delivery,
                    success,
                )
                report.reminders_sent += int(outcome.reminders_sent)
                report.reminders_retried += int(outcome.reminders_retried)
                report.reminders_abandoned += int(outcome.reminders_abandoned)
                report.errors += int(outcome.errors)
            except Exception:
                report.errors += 1

        for delivery in backup_deliveries:
            success = False
            error = ""
            try:
                await self.backup_sender.send(delivery)
                success = True
            except Exception as exc:
                error = type(exc).__name__
            try:
                await asyncio.to_thread(
                    self._finish_backup_delivery_sync,
                    delivery,
                    success,
                    error,
                )
                if success:
                    report.backups_sent += 1
                else:
                    report.backups_failed += 1
                    report.errors += 1
            except Exception:
                report.backups_failed += 1
                report.errors += 1
        return report

    def _prepare_sync(
        self,
    ) -> tuple[
        LifecycleReport,
        list[ReminderDelivery],
        list[AutoBackupDelivery],
    ]:
        conn = connect(self.db_path)
        report = LifecycleReport()
        adapter = self.panel_adapter_factory()
        backup_deliveries: list[AutoBackupDelivery] = []
        try:
            rows = conn.execute(
                "SELECT id, owner_telegram_id FROM tenants "
                "WHERE status='active' AND (id % ?) = ? ORDER BY id",
                (self.shard_count, self.shard_index),
            ).fetchall()
            for row in rows:
                tenant_id = int(row["id"])
                owner_id = int(row["owner_telegram_id"])
                report.tenants += 1
                try:
                    service = TenantBusinessService(
                        conn,
                        tenant_id=tenant_id,
                        owner_telegram_id=owner_id,
                        secret_cipher=self.cipher,
                        panel_adapter=adapter,
                    )
                    enforcement = service.enforce_subscription_batch(
                        owner_id,
                        limit=self.enforcer_batch_size,
                        hot_usage_ratio=self.hot_usage_ratio,
                        cursor=self._tenant_cursors.get(tenant_id, 0),
                        freeze_after=self.node_freeze_failures,
                    )
                    self._tenant_cursors[tenant_id] = int(
                        enforcement.get("next_cursor") or 0
                    )
                    report.scanned += int(enforcement["scanned"])
                    report.synced += int(enforcement["synced"])
                    report.expired += int(enforcement["expired"])
                    report.enforcement_pending += int(enforcement["pending"])
                    report.errors += int(enforcement["errors"])

                    reminder_values = service.runtime_userbot_settings()
                    reminder_enabled = bool(
                        reminder_values.get("reminder_enabled", True)
                    )
                    days_threshold = max(
                        1,
                        min(
                            int(
                                reminder_values.get(
                                    "reminder_days", self.reminder_days
                                )
                            ),
                            30,
                        ),
                    )
                    remaining_gb_threshold = max(
                        1,
                        min(
                            int(
                                reminder_values.get(
                                    "reminder_remaining_gb",
                                    self.reminder_remaining_gb,
                                )
                            ),
                            1000,
                        ),
                    )
                    report.reminders_skipped += reconcile_reminder_queue_settings(
                        conn,
                        tenant_id=tenant_id,
                        enabled=reminder_enabled,
                        days_threshold=days_threshold,
                        remaining_gb_threshold=remaining_gb_threshold,
                    )
                    if reminder_enabled:
                        report.reminders_enqueued += enqueue_due_reminders(
                            conn,
                            tenant_id=tenant_id,
                            days_threshold=days_threshold,
                            remaining_gb_threshold=remaining_gb_threshold,
                        )

                    # Auto backup is independent from Telegram polling and uses
                    # the same shard ownership as lifecycle/enforcement.
                    try:
                        backup_delivery = prepare_auto_backup_delivery(
                            conn,
                            tenant_id=tenant_id,
                            owner_telegram_id=owner_id,
                            cipher=self.cipher,
                            settings=reminder_values,
                            business=service,
                        )
                        if backup_delivery is not None:
                            backup_deliveries.append(backup_delivery)
                            report.backups_claimed += 1
                    except Exception:
                        report.backups_failed += 1
                        report.errors += 1
                except Exception:
                    # One tenant must never stop enforcement/reminders/backups
                    # for the remaining tenants.
                    report.errors += 1

            deliveries, queue = claim_due_deliveries(
                conn,
                cipher=self.cipher,
                shard_count=self.shard_count,
                shard_index=self.shard_index,
                lease_seconds=self.reminder_lease_seconds,
                max_retries=self.reminder_max_retries,
                retry_base_seconds=self.reminder_retry_base_seconds,
                limit=self.reminder_batch_size,
            )
            report.reminders_claimed += int(queue.claimed)
            report.reminders_retried += int(queue.retried)
            report.reminders_abandoned += int(queue.abandoned)
            report.reminders_skipped += int(queue.skipped)
            report.errors += int(queue.errors)
            return report, deliveries, backup_deliveries
        finally:
            conn.close()

    def _finish_delivery_sync(
        self,
        delivery: ReminderDelivery,
        success: bool,
    ) -> LifecycleReport:
        conn = connect(self.db_path)
        try:
            queue = finish_delivery(
                conn,
                event_key=delivery.event_key,
                success=bool(success),
                max_retries=self.reminder_max_retries,
                retry_base_seconds=self.reminder_retry_base_seconds,
            )
            return LifecycleReport(
                reminders_sent=int(queue.sent),
                reminders_retried=int(queue.retried),
                reminders_abandoned=int(queue.abandoned),
                errors=int(queue.errors),
            )
        finally:
            conn.close()


    def _finish_backup_delivery_sync(
        self,
        delivery: AutoBackupDelivery,
        success: bool,
        error: str,
    ) -> None:
        conn = connect(self.db_path)
        try:
            complete_auto_backup_delivery(
                conn,
                delivery=delivery,
                success=bool(success),
                error=str(error or ""),
            )
        finally:
            conn.close()
