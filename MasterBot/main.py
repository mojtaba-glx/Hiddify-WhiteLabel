"""Executable MasterBot entry point with the Phase-3 license scheduler."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application

from Database.connection import connect
from Database.migrate import migrate
from LicenseService.jobs import LicenseJobRunner, NotificationSender
from LicenseService.runtime import TenantRuntimeGate
from LicenseService.telegram_sender import MasterTelegramWarningSender
from Ops.process_lock import ProcessLock
from MasterBot.handlers import register_handlers
from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import BotTokenVerifier, MasterService
from MasterBot.telegram_api import TelegramGetMeVerifier
from Shared.crypto import FernetTokenCipher
from Shared.redaction import configure_safe_logging, get_logger, safe_format_exception
from Shared.settings import Settings, load_from_env

logger = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def build_application(
    settings: Settings,
    *,
    connection=None,
    verifier: BotTokenVerifier | None = None,
    warning_sender: NotificationSender | None = None,
) -> tuple[Application, object]:
    """Build without starting network I/O; suitable for offline tests."""
    if connection is None:
        migrate(settings.database_path)
        connection = connect(settings.database_path)
    application = Application.builder().token(settings.master_bot_token).build()
    runtime_gate = TenantRuntimeGate(
        connection, ttl_seconds=settings.runtime_cache_ttl_seconds
    )
    service = MasterService(
        connection,
        master_admin_id=settings.master_admin_id,
        cipher=FernetTokenCipher(settings.token_encryption_key),
        bot_verifier=verifier or TelegramGetMeVerifier(),
        cache_invalidator=runtime_gate.invalidate,
    )
    sender = warning_sender or MasterTelegramWarningSender(
        application.bot,
        master_admin_id=settings.master_admin_id,
        timezone_name=settings.display_timezone,
    )
    runner = LicenseJobRunner(
        connection,
        sender=sender,
        runtime_gate=runtime_gate,
        lease_seconds=settings.notification_lease_seconds,
        max_retries=settings.max_notification_retries,
        retry_base_seconds=settings.notification_retry_base_seconds,
    )
    application.bot_data["master_service"] = service
    application.bot_data["customer_portal_service"] = CustomerPortalService(
        connection, master_service=service, cache_invalidator=runtime_gate.invalidate
    )
    application.bot_data["display_timezone"] = settings.display_timezone
    application.bot_data["license_job_runner"] = runner
    application.bot_data["tenant_runtime_gate"] = runtime_gate
    register_handlers(application)
    if application.job_queue is None:
        raise RuntimeError("python-telegram-bot job queue is unavailable")
    application.job_queue.run_repeating(
        run_license_job,
        interval=settings.license_job_interval_seconds,
        first=settings.license_job_interval_seconds,
        name="license-service",
    )
    return application, connection


async def run_license_job(context) -> None:
    runner = context.application.bot_data.get("license_job_runner")
    if not isinstance(runner, LicenseJobRunner):
        logger.error("License job runner is unavailable")
        return
    try:
        report = await runner.run_once()
        if report.errors:
            logger.warning("License job completed with %s isolated error(s)", report.errors)
    except Exception as exc:
        # RuntimeGate remains independent and fail-closed even if this cycle
        # cannot reach the database or Telegram.
        logger.error("License job cycle failed: %s", safe_format_exception(exc))


def main() -> None:
    configure_safe_logging()
    load_dotenv(ROOT / ".env")
    settings = load_from_env()
    with ProcessLock(ROOT / "runtime" / "locks", "master"):
        application, connection = build_application(settings)
        try:
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
        except Exception as exc:
            logger.error("MasterBot stopped: %s", safe_format_exception(exc))
            raise
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    main()
