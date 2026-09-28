"""Telegram warning adapter formatting with no network access."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from LicenseService.jobs import LicenseWarning
from LicenseService.telegram_sender import MasterTelegramWarningSender


def test_master_warning_sender_targets_owner_and_formats_safe_fields() -> None:
    bot = AsyncMock()
    sender = MasterTelegramWarningSender(
        bot, master_admin_id=12345, timezone_name="Asia/Tehran"
    )
    warning = LicenseWarning(
        event_key="license:1:expiring:7d:0123456789abcdef",
        tenant_id=2,
        tenant_name="Customer",
        license_id=1,
        days_before=7,
        expires_at="2030-01-01T00:00:00+00:00",
    )
    asyncio.run(sender.send(warning))
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert "Customer" in kwargs["text"] and "7" in kwargs["text"]
    assert warning.event_key not in kwargs["text"]
