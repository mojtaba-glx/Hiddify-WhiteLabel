"""Small Telegram API seam used by MasterBot token registration."""

from __future__ import annotations

from telegram import Bot

from MasterBot.service import BotIdentity, TokenVerificationError


class TelegramGetMeVerifier:
    """Verify a token using Telegram ``getMe`` without logging the token."""

    async def verify(self, token: str) -> BotIdentity:
        try:
            bot = Bot(token=str(token or ""))
            user = await bot.get_me()
            if not bool(user.is_bot) or int(user.id) <= 0:
                raise ValueError("identity is not a bot")
            username = str(user.username).strip() if user.username else None
            return BotIdentity(telegram_bot_id=int(user.id), username=username)
        except Exception:
            raise TokenVerificationError("Telegram bot token verification failed") from None
