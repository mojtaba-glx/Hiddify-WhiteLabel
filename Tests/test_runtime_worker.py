"""Tenant worker lifecycle and application wiring stay offline and isolated."""

from __future__ import annotations

import asyncio

import pytest

from Gateway.catalog import RuntimeBotSpec
from TenantRuntime.handlers import runtime_access_gate
from TenantRuntime.worker import (
    RuntimeWorkerError,
    TenantApplicationWorker,
    build_tenant_application,
)


def _spec(bot_id: int = 1, telegram_bot_id: int = 700001):
    return RuntimeBotSpec(
        bot_id=bot_id,
        tenant_id=11,
        role="user",
        telegram_bot_id=telegram_bot_id,
        telegram_username="sample_bot",
        tenant_name="Sample",
        owner_telegram_id=900001,
        data_namespace="namespace-sample",
        token_tail="ABCD",
        encrypted_token="encrypted",
        token_fingerprint="a" * 64,
    )


class FakeBot:
    def __init__(self, bot_id):
        self.id = bot_id


class FakeUpdater:
    def __init__(self, *, fail_start=False):
        self.running = False
        self.fail_start = fail_start
        self.start_calls = 0
        self.stop_calls = 0

    async def start_polling(self, **kwargs):
        self.start_calls += 1
        self.running = True
        if self.fail_start:
            raise RuntimeError("polling failed")

    async def stop(self):
        self.stop_calls += 1
        self.running = False


class FakeApplication:
    def __init__(self, bot_id, *, fail_start=False, fail_polling=False):
        self.bot = FakeBot(bot_id)
        self.updater = FakeUpdater(fail_start=fail_polling)
        self.running = False
        self.fail_start = fail_start
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0

    async def initialize(self):
        self.initialize_calls += 1

    async def start(self):
        self.start_calls += 1
        self.running = True
        if self.fail_start:
            raise RuntimeError("application failed")

    async def stop(self):
        self.stop_calls += 1
        self.running = False

    async def shutdown(self):
        self.shutdown_calls += 1


def test_application_has_access_gate_and_scoped_state(conn) -> None:
    spec = _spec()

    class AllowPolicy:
        pass

    application = build_tenant_application(
        spec=spec,
        plain_token="700001:FAKE_runtime_worker_abcdefghijklmnopqrstuvwxyz",
        conn=conn,
        policy=AllowPolicy(),  # handlers validate the real object only when invoked
        poll_timeout_seconds=20,
    )
    assert application.bot_data["runtime_spec"] is spec
    state = application.bot_data["runtime_state"]
    assert state.scope.tenant_id == 11 and state.scope.bot_role == "user"
    assert -1 in application.handlers
    assert any(handler.callback is runtime_access_gate for handler in application.handlers[-1])


def test_worker_lifecycle_starts_and_stops_cleanly() -> None:
    application = FakeApplication(700001)
    worker = TenantApplicationWorker(
        spec=_spec(), application=application, poll_timeout_seconds=20
    )
    asyncio.run(worker.start())
    assert worker.started and worker.initialized
    assert application.updater.running and application.running
    asyncio.run(worker.stop())
    assert not worker.started and not worker.initialized
    assert (application.updater.stop_calls, application.stop_calls, application.shutdown_calls) == (1, 1, 1)


def test_live_bot_identity_mismatch_fails_and_cleans_up() -> None:
    application = FakeApplication(999999)
    worker = TenantApplicationWorker(
        spec=_spec(), application=application, poll_timeout_seconds=20
    )
    with pytest.raises(RuntimeWorkerError, match="failed to start"):
        asyncio.run(worker.start())
    assert not worker.started and not worker.initialized
    assert application.updater.start_calls == 0
    assert application.shutdown_calls == 1


def test_application_start_failure_stops_polling_and_application() -> None:
    application = FakeApplication(700001, fail_start=True)
    worker = TenantApplicationWorker(
        spec=_spec(), application=application, poll_timeout_seconds=20
    )
    with pytest.raises(RuntimeWorkerError):
        asyncio.run(worker.start())
    assert application.updater.stop_calls == 1
    assert application.stop_calls == 1
    assert application.shutdown_calls == 1
    assert not worker.started and not worker.initialized


def test_partial_polling_start_failure_is_cleaned_up() -> None:
    application = FakeApplication(700001, fail_polling=True)
    worker = TenantApplicationWorker(
        spec=_spec(), application=application, poll_timeout_seconds=20
    )
    with pytest.raises(RuntimeWorkerError):
        asyncio.run(worker.start())
    assert application.updater.stop_calls == 1
    assert application.shutdown_calls == 1
    assert not worker.started and not worker.initialized
