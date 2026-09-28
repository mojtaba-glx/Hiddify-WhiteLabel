"""A fixed shard reconciles independent tenant workers without cascade failure."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from Gateway.catalog import CatalogSnapshot, RuntimeBotSpec, RuntimeCatalogError
from TenantRuntime.supervisor import RuntimeSupervisor


def _spec(bot_id: int, tenant_id: int | None = None, *, name: str = "Tenant"):
    tenant = int(tenant_id or bot_id)
    return RuntimeBotSpec(
        bot_id=bot_id,
        tenant_id=tenant,
        role="admin" if bot_id % 2 else "user",
        telegram_bot_id=700000 + bot_id,
        telegram_username=f"bot_{bot_id}",
        tenant_name=name,
        owner_telegram_id=900000 + tenant,
        data_namespace=f"namespace-{tenant}",
        token_tail=f"{bot_id:04d}"[-4:],
        encrypted_token=f"encrypted-{bot_id}",
        token_fingerprint=f"{bot_id:064x}"[-64:],
    )


class FakeCatalog:
    def __init__(self, snapshot: CatalogSnapshot):
        self.snapshot = snapshot
        self.error: Exception | None = None

    def load(self):
        if self.error is not None:
            raise self.error
        return self.snapshot


class FakeWorker:
    def __init__(self, spec, *, fail_start=False, fail_stop=False):
        self.spec = spec
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("start failed")

    async def stop(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("stop failed")


class FakeFactory:
    def __init__(self):
        self.created: list[FakeWorker] = []
        self.fail_ids: set[int] = set()

    def create(self, spec):
        worker = FakeWorker(spec, fail_start=spec.bot_id in self.fail_ids)
        self.created.append(worker)
        return worker


def _run(awaitable):
    return asyncio.run(awaitable)


def test_reconcile_starts_once_and_stops_removed_worker() -> None:
    specs = (_spec(1), _spec(2))
    catalog = FakeCatalog(CatalogSnapshot(specs))
    factory = FakeFactory()
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)

    first = _run(supervisor.reconcile())
    second = _run(supervisor.reconcile())
    assert (first.started, first.running) == (2, 2)
    assert (second.started, second.stopped, second.running) == (0, 0, 2)
    assert len(factory.created) == 2

    removed = factory.created[0]
    catalog.snapshot = CatalogSnapshot((specs[1],))
    third = _run(supervisor.reconcile())
    assert (third.stopped, third.running) == (1, 1)
    assert removed.stop_calls == 1
    _run(supervisor.shutdown())
    assert supervisor.active_bot_ids == ()


def test_revision_change_restarts_only_changed_bot() -> None:
    first, second = _spec(1), _spec(2)
    catalog = FakeCatalog(CatalogSnapshot((first, second)))
    factory = FakeFactory()
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)
    _run(supervisor.reconcile())
    original_first, original_second = factory.created

    catalog.snapshot = CatalogSnapshot((replace(first, tenant_name="Renamed"), second))
    report = _run(supervisor.reconcile())
    assert (report.started, report.stopped, report.restarted, report.running) == (1, 1, 1, 2)
    assert original_first.stop_calls == 1
    assert original_second.stop_calls == 0
    assert len(factory.created) == 3


def test_rejected_bot_stops_only_that_worker() -> None:
    first, second = _spec(1), _spec(2)
    catalog = FakeCatalog(CatalogSnapshot((first, second)))
    factory = FakeFactory()
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)
    _run(supervisor.reconcile())

    catalog.snapshot = CatalogSnapshot((second,), rejected_bot_ids=(1,))
    report = _run(supervisor.reconcile())
    assert (report.rejected, report.stopped, report.running) == (1, 1, 1)
    assert supervisor.active_bot_ids == (2,)


def test_catalog_outage_keeps_processes_but_updates_still_fail_closed_in_policy() -> None:
    catalog = FakeCatalog(CatalogSnapshot((_spec(1),)))
    factory = FakeFactory()
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)
    _run(supervisor.reconcile())
    worker = factory.created[0]

    catalog.error = RuntimeCatalogError("database unavailable")
    report = _run(supervisor.reconcile())
    assert (report.errors, report.running) == (1, 1)
    assert worker.stop_calls == 0


def test_one_worker_start_failure_does_not_block_other_tenants() -> None:
    catalog = FakeCatalog(CatalogSnapshot((_spec(1), _spec(2), _spec(3))))
    factory = FakeFactory()
    factory.fail_ids.add(2)
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)
    report = _run(supervisor.reconcile())
    assert (report.started, report.errors, report.running) == (2, 1, 2)
    assert supervisor.active_bot_ids == (1, 3)
    failed = next(worker for worker in factory.created if worker.spec.bot_id == 2)
    assert failed.stop_calls == 1


def test_shutdown_continues_when_one_worker_stop_fails() -> None:
    catalog = FakeCatalog(CatalogSnapshot((_spec(1), _spec(2))))
    factory = FakeFactory()
    supervisor = RuntimeSupervisor(catalog=catalog, worker_factory=factory)
    _run(supervisor.reconcile())
    factory.created[0].fail_stop = True
    _run(supervisor.shutdown())
    assert supervisor.active_bot_ids == ()
    assert [worker.stop_calls for worker in factory.created] == [1, 1]


def test_worker_start_concurrency_is_bounded() -> None:
    class CountingFactory:
        def __init__(self):
            self.active = 0
            self.peak = 0

        def create(factory_self, spec):
            class CountingWorker:
                def __init__(self):
                    self.spec = spec

                async def start(self):
                    factory_self.active += 1
                    factory_self.peak = max(factory_self.peak, factory_self.active)
                    await asyncio.sleep(0.01)
                    factory_self.active -= 1

                async def stop(self):
                    return None

            return CountingWorker()

    catalog = FakeCatalog(CatalogSnapshot(tuple(_spec(i) for i in range(1, 7))))
    factory = CountingFactory()
    supervisor = RuntimeSupervisor(
        catalog=catalog, worker_factory=factory, start_concurrency=2
    )
    report = _run(supervisor.reconcile())
    assert report.running == 6
    assert factory.peak == 2
