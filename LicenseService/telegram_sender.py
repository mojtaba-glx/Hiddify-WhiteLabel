"""Telegram delivery adapter for license warnings sent to the platform owner."""

from __future__ import annotations

from telegram import Bot

from LicenseService.jobs import LicenseWarning
from Shared.timeutils import format_tehran, parse_utc


class MasterTelegramWarningSender:
    def __init__(self, bot: Bot, *, master_admin_id: int, timezone_name: str) -> None:
        self.bot = bot
        self.master_admin_id = int(master_admin_id)
        self.timezone_name = str(timezone_name)

    async def send(self, warning: LicenseWarning) -> None:
        expiry = format_tehran(parse_utc(warning.expires_at), self.timezone_name)
        await self.bot.send_message(
            chat_id=self.master_admin_id,
            text=(
                "⚠️ هشدار انقضای لایسنس\n\n"
                f"مشتری: {warning.tenant_name} (#{warning.tenant_id})\n"
                f"لایسنس: #{warning.license_id}\n"
                f"زمان باقی‌مانده: {warning.days_before} روز\n"
                f"انقضا: {expiry}"
            ),
        )
