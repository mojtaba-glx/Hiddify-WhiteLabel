"""Reconcile many tenant bot workers inside one fixed shard process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from Gateway.catalog import RuntimeBotSpec, RuntimeCatalog
from Shared.redaction import get_logger

logger = get_logger(__name__)


class RuntimeWorker(Protocol):
    spec: RuntimeBotSpec

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class RuntimeWorkerFactory(Protocol):
    def create(self, spec: RuntimeBotSpec) -> RuntimeWorker: ...


@dataclass
class ReconcileReport:
    desired: int = 0
    running: int = 0
    started: int = 0
    stopped: int = 0
    restarted: int = 0
    rejected: int = 0
    errors: int = 0


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        catalog: RuntimeCatalog,
        worker_factory: RuntimeWorkerFactory,
        reconcile_seconds: int = 15,
        start_concurrency: int = 8,
    ) -> None:
        self.catalog = catalog
        self.worker_factory = worker_factory
        self.reconcile_seconds = max(5, int(reconcile_seconds))
        self.start_concurrency = max(1, min(int(start_concurrency), 32))
        self._workers: dict[int, RuntimeWorker] = {}
        self._lock = asyncio.Lock()

    @property
    def active_bot_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._workers))

    async def _stop_worker(self, bot_id: int, report: ReconcileReport) -> None:
        worker = self._workers.pop(int(bot_id), None)
        if worker is None:
            return
        try:
            await worker.stop()
            report.stopped += 1
        except Exception as exc:
            report.errors += 1
            logger.error(
                "Tenant worker stop failed bot=%s (%s)", bot_id, type(exc).__name__
            )

    async def _start_worker(
        self,
        bot_id: int,
        spec: RuntimeBotSpec,
        semaphore: asyncio.Semaphore,
    ) -> RuntimeWorker | None:
        worker: RuntimeWorker | None = None
        try:
            async with semaphore:
                worker = self.worker_factory.create(spec)
                await worker.start()
            return worker
        except asyncio.CancelledError:
            if worker is not None:
                try:
                    await worker.stop()
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.error(
                "Tenant worker start failed bot=%s tenant=%s (%s)",
                bot_id,
                spec.tenant_id,
                type(exc).__name__,
            )
            if worker is not None:
                try:
                    await worker.stop()
                except Exception as cleanup_exc:
                    logger.error(
                        "Tenant worker cleanup failed bot=%s (%s)",
                        bot_id,
                        type(cleanup_exc).__name__,
                    )
            return None

    async def reconcile(self) -> ReconcileReport:
        async with self._lock:
            report = ReconcileReport()
            try:
                snapshot = self.catalog.load()
            except Exception as exc:
                report.errors += 1
                report.running = len(self._workers)
                logger.error("Runtime catalog unavailable (%s)", type(exc).__name__)
                return report

            desired = {spec.bot_id: spec for spec in snapshot.specs}
            report.desired = len(desired)
            report.rejected = len(snapshot.rejected_bot_ids)
            for bot_id in snapshot.rejected_bot_ids:
                logger.error("Rejected unsafe runtime catalog row bot=%s", bot_id)

            restart_ids = {
                bot_id
                for bot_id, worker in self._workers.items()
                if bot_id in desired
                and worker.spec.revision != desired[bot_id].revision
            }
            stop_ids = (set(self._workers) - set(desired)) | restart_ids
            for bot_id in sorted(stop_ids):
                await self._stop_worker(bot_id, report)

            start_ids = sorted(set(desired) - set(self._workers))
            semaphore = asyncio.Semaphore(self.start_concurrency)
            results = await asyncio.gather(
                *(
                    self._start_worker(bot_id, desired[bot_id], semaphore)
                    for bot_id in start_ids
                )
            )
            for bot_id, worker in zip(start_ids, results):
                if worker is None:
                    report.errors += 1
                    continue
                self._workers[bot_id] = worker
                report.started += 1
                report.restarted += int(bot_id in restart_ids)
            report.running = len(self._workers)
            return report

    async def shutdown(self) -> None:
        async with self._lock:
            report = ReconcileReport()
            for bot_id in sorted(tuple(self._workers)):
                await self._stop_worker(bot_id, report)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                report = await self.reconcile()
                if report.errors or report.started or report.stopped:
                    logger.info(
                        "Runtime reconcile desired=%s running=%s started=%s stopped=%s errors=%s",
                        report.desired,
                        report.running,
                        report.started,
                        report.stopped,
                        report.errors,
                    )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=float(self.reconcile_seconds)
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.shutdown()
