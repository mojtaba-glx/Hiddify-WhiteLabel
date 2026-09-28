"""Periodic tenant subscription lifecycle maintenance.

One shard owns a tenant deterministically by tenant_id modulo shard_count.  The
coordinator opens its own SQLite connection inside a worker thread so panel
network I/O never blocks Telegram update handling and SQLite connections never
cross threads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from Database.connection import connect
from Shared.crypto import TokenCipher
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import PanelAdapter, build_default_panel_adapter


@dataclass(frozen=True)
class LifecycleReport:
    tenants: int = 0
    synced: int = 0
    expired: int = 0
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
    ) -> None:
        self.db_path = str(db_path)
        self.cipher = cipher
        self.shard_count = int(shard_count)
        self.shard_index = int(shard_index)
        self.panel_adapter_factory = panel_adapter_factory
        if self.shard_count <= 0 or not 0 <= self.shard_index < self.shard_count:
            raise ValueError("invalid lifecycle shard scope")

    async def run_once(self) -> LifecycleReport:
        return await asyncio.to_thread(self._run_sync)

    def _run_sync(self) -> LifecycleReport:
        conn = connect(self.db_path)
        tenants = synced = expired = errors = 0
        adapter = self.panel_adapter_factory()
        try:
            rows = conn.execute(
                "SELECT id, owner_telegram_id FROM tenants "
                "WHERE status='active' AND (id % ?) = ? ORDER BY id",
                (self.shard_count, self.shard_index),
            ).fetchall()
            for row in rows:
                tenants += 1
                try:
                    service = TenantBusinessService(
                        conn,
                        tenant_id=int(row["id"]),
                        owner_telegram_id=int(row["owner_telegram_id"]),
                        secret_cipher=self.cipher,
                        panel_adapter=adapter,
                    )
                    expiry = service.expire_due_subscriptions(
                        int(row["owner_telegram_id"])
                    )
                    expired += int(expiry["expired"])
                    errors += int(expiry["errors"])
                    usage = service.sync_all_subscriptions(
                        int(row["owner_telegram_id"])
                    )
                    synced += int(usage["synced"])
                    expired += int(usage["expired"])
                    errors += int(usage["errors"])
                except Exception:
                    # One tenant must never stop lifecycle work for the others.
                    errors += 1
            return LifecycleReport(
                tenants=tenants,
                synced=synced,
                expired=expired,
                errors=errors,
            )
        finally:
            conn.close()
