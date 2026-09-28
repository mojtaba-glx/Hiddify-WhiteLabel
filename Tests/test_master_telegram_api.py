"""Mocked getMe verification never needs live Telegram access."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from MasterBot.service import TokenVerificationError
from MasterBot.telegram_api import TelegramGetMeVerifier
from Tests.conftest import make_fake_token


def test_getme_verifier_returns_bot_identity(monkeypatch) -> None:
    class FakeBot:
        def __init__(self, token: str) -> None:
            assert token

        async def get_me(self):
            return SimpleNamespace(id=12345, username="verified_bot", is_bot=True)

    monkeypatch.setattr("MasterBot.telegram_api.Bot", FakeBot)
    identity = asyncio.run(TelegramGetMeVerifier().verify(make_fake_token("getme")))
    assert identity.telegram_bot_id == 12345
    assert identity.username == "verified_bot"


def test_getme_verifier_rejects_non_bot(monkeypatch) -> None:
    class FakeBot:
        def __init__(self, token: str) -> None:
            pass

        async def get_me(self):
            return SimpleNamespace(id=12345, username="person", is_bot=False)

    monkeypatch.setattr("MasterBot.telegram_api.Bot", FakeBot)
    with pytest.raises(TokenVerificationError):
        asyncio.run(TelegramGetMeVerifier().verify(make_fake_token("notbot")))


def test_getme_failure_does_not_leak_token(monkeypatch) -> None:
    token = make_fake_token("telegram-error")

    class FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def get_me(self):
            raise RuntimeError(f"network rejected {self.token}")

    monkeypatch.setattr("MasterBot.telegram_api.Bot", FakeBot)
    with pytest.raises(TokenVerificationError) as exc:
        asyncio.run(TelegramGetMeVerifier().verify(token))
    assert token not in str(exc.value)
