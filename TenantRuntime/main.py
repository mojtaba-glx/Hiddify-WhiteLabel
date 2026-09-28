"""Executable entry point for one Phase-5 TenantRuntime shard."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from dotenv import load_dotenv

from Database.connection import connect
from Database.migrate import migrate
from Gateway.catalog import RuntimeCatalog
from Gateway.policy import RuntimePolicy
from LicenseService.runtime import TenantRuntimeGate
from Ops.process_lock import ProcessLock
from Shared.crypto import FernetTokenCipher
from Shared.redaction import configure_safe_logging, get_logger, safe_format_exception
from Shared.settings import RuntimeSettings, load_runtime_from_env
from TenantRuntime.supervisor import RuntimeSupervisor
from TenantRuntime.worker import TenantApplicationFactory

logger = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def _database_path(settings: RuntimeSettings) -> str:
    path = Path(settings.database_path)
    return str(path if path.is_absolute() else ROOT / path)


def build_supervisor(settings: RuntimeSettings, *, connection=None):
    db_path = _database_path(settings)
    if connection is None:
        migrate(db_path)
        connection = connect(db_path)
    cipher = FernetTokenCipher(settings.token_encryption_key)
    catalog = RuntimeCatalog(
        connection,
        cipher=cipher,
        shard_count=settings.shard_count,
        shard_index=settings.shard_index,
    )
    gate = TenantRuntimeGate(
        connection, ttl_seconds=settings.license_cache_ttl_seconds
    )
    policy = RuntimePolicy(connection, gate=gate)
    factory = TenantApplicationFactory(
        connection,
        catalog=catalog,
        policy=policy,
        poll_timeout_seconds=settings.poll_timeout_seconds,
    )
    supervisor = RuntimeSupervisor(
        catalog=catalog,
        worker_factory=factory,
        reconcile_seconds=settings.reconcile_seconds,
        start_concurrency=settings.start_concurrency,
    )
    return supervisor, connection


async def run_runtime(settings: RuntimeSettings) -> None:
    lock_name = f"runtime-{settings.shard_count}-{settings.shard_index}"
    with ProcessLock(ROOT / "runtime" / "locks", lock_name):
        supervisor, connection = build_supervisor(settings)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        logger.info(
            "TenantRuntime shard starting index=%s count=%s",
            settings.shard_index,
            settings.shard_count,
        )
        try:
            await supervisor.run_forever(stop_event)
        finally:
            connection.close()


def main() -> None:
    configure_safe_logging()
    load_dotenv(ROOT / ".env")
    settings = load_runtime_from_env()
    try:
        asyncio.run(run_runtime(settings))
    except KeyboardInterrupt:
        return
    except Exception as exc:
        logger.error("TenantRuntime stopped: %s", safe_format_exception(exc))
        raise


if __name__ == "__main__":
    main()
