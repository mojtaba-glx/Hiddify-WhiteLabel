#!/usr/bin/env python3
"""Verify a Telegram bot token from an environment variable without printing it."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from MasterBot.telegram_api import TelegramGetMeVerifier
from MasterBot.service import TokenVerificationError


async def _main() -> int:
    token = os.environ.get("WL_BOT_TOKEN", "")
    try:
        identity = await TelegramGetMeVerifier().verify(token)
    except TokenVerificationError:
        print("Telegram rejected the bot token or could not be reached.", file=sys.stderr)
        return 1
    label = f"@{identity.username}" if identity.username else str(identity.telegram_bot_id)
    print(f"Telegram bot verified: {label} ({identity.telegram_bot_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
