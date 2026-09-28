"""One in-process Telegram Application for one tenant bot token."""

from __future__ import annotations

import sqlite3

from telegram import Update
from telegram.ext import Application

from Gateway.catalog import RuntimeBotSpec, RuntimeCatalog
from Gateway.policy import RuntimePolicy
from Shared.crypto import TokenCipher
from TenantRuntime.handlers import register_runtime_handlers
from TenantRuntime.state import StateScope, TenantStateStore
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import build_default_panel_adapter


class RuntimeWorkerError(RuntimeError):
    """A tenant bot could not start or its live identity mismatched the DB."""


def build_tenant_application(
    *,
    spec: RuntimeBotSpec,
    plain_token: str,
    conn: sqlite3.Connection,
    policy: RuntimePolicy,
    poll_timeout_seconds: int,
    secret_cipher: TokenCipher | None = None,
) -> Application:
    application = (
        Application.builder()
        .token(plain_token)
        .concurrent_updates(False)
        .job_queue(None)
        .connection_pool_size(2)
        .get_updates_connection_pool_size(1)
        .get_updates_read_timeout(float(poll_timeout_seconds + 5))
        .build()
    )
    application.bot_data["runtime_spec"] = spec
    application.bot_data["runtime_policy"] = policy
    application.bot_data["runtime_state"] = TenantStateStore(
        conn, scope=StateScope(tenant_id=spec.tenant_id, bot_role=spec.role)
    )
    application.bot_data["tenant_business"] = TenantBusinessService(
        conn,
        tenant_id=spec.tenant_id,
        owner_telegram_id=spec.owner_telegram_id,
        secret_cipher=secret_cipher,
        panel_adapter=build_default_panel_adapter(),
    )
    register_runtime_handlers(application)
    return application


class TenantApplicationWorker:
    def __init__(
        self,
        *,
        spec: RuntimeBotSpec,
        application: Application,
        poll_timeout_seconds: int,
    ) -> None:
        self.spec = spec
        self.application = application
        self.poll_timeout_seconds = int(poll_timeout_seconds)
        self.started = False
        self.initialized = False

    async def start(self) -> None:
        if self.started:
            return
        updater = self.application.updater
        if updater is None:
            raise RuntimeWorkerError("tenant application updater is unavailable")
        initialized = False
        try:
            await self.application.initialize()
            initialized = True
            self.initialized = True
            if int(self.application.bot.id) != self.spec.telegram_bot_id:
                raise RuntimeWorkerError("live Telegram bot identity mismatch")
            await updater.start_polling(
                timeout=self.poll_timeout_seconds,
                bootstrap_retries=0,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            await self.application.start()
            self.started = True
        except Exception:
            if updater.running:
                try:
                    await updater.stop()
                except Exception:
                    pass
            if self.application.running:
                try:
                    await self.application.stop()
                except Exception:
                    pass
            if initialized:
                try:
                    await self.application.shutdown()
                except Exception:
                    pass
                self.initialized = False
            raise RuntimeWorkerError("tenant bot failed to start") from None

    async def stop(self) -> None:
        updater = self.application.updater
        try:
            if updater is not None and updater.running:
                await updater.stop()
        finally:
            try:
                if self.application.running:
                    await self.application.stop()
            finally:
                if self.initialized:
                    await self.application.shutdown()
                    self.initialized = False
                self.started = False


class TenantApplicationFactory:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        catalog: RuntimeCatalog,
        policy: RuntimePolicy,
        poll_timeout_seconds: int,
    ) -> None:
        self.conn = conn
        self.catalog = catalog
        self.policy = policy
        self.poll_timeout_seconds = int(poll_timeout_seconds)

    def create(self, spec: RuntimeBotSpec) -> TenantApplicationWorker:
        token = self.catalog.decrypt_for_worker(spec)
        try:
            application = build_tenant_application(
                spec=spec,
                plain_token=token,
                conn=self.conn,
                policy=self.policy,
                poll_timeout_seconds=self.poll_timeout_seconds,
                secret_cipher=self.catalog.cipher,
            )
        finally:
            del token
        return TenantApplicationWorker(
            spec=spec,
            application=application,
            poll_timeout_seconds=self.poll_timeout_seconds,
        )
