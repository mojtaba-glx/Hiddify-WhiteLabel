"""Tests for TenantRuntime.UserBot.qr_and_configs module."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from TenantRuntime.UserBot import qr_and_configs
from TenantRuntime.business import TenantBusinessError


class FakeMessage:
    def __init__(self):
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.sent.append((text, kwargs))


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list[tuple[Any, Any]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.message.sent.append((text, kwargs))


def test_import_and_exports() -> None:
    """Verify TenantRuntime.UserBot.qr_and_configs imports clean without ImportError."""
    assert callable(qr_and_configs.send_qr_code)
    assert callable(qr_and_configs.send_direct_configs)
    assert callable(qr_and_configs.send_smart_sub_link)
    assert callable(qr_and_configs.send_sub_link)
    assert callable(qr_and_configs._extract_config_items)
    assert callable(qr_and_configs.start_tenant_sub_server)


def test_extract_config_items() -> None:
    """Verify config URI parser differentiates single base64 from multiple URIs."""
    # Empty
    assert qr_and_configs._extract_config_items("") == []

    # Multiple URIs
    multi = "vless://uuid@host:443\nvmess://uuid@host:443\ntrojan://pass@host:443"
    extracted = qr_and_configs._extract_config_items(multi)
    assert len(extracted) == 3
    assert extracted[0] == "vless://uuid@host:443"

    # Base64 block
    b64 = "dmxlc3M6Ly91dWlkQGhvc3Q6NDQzCg=="
    assert qr_and_configs._extract_config_items(b64) == [b64]


def test_send_sub_link_disabled() -> None:
    """Verify sub link respects show_sub_link setting."""
    message = FakeMessage()
    cb = FakeCallbackQuery("shop:sublink:1", message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123),
        effective_message=message,
        callback_query=cb,
    )
    context = SimpleNamespace(bot=SimpleNamespace())

    class DummyBiz:
        pass

    asyncio.run(
        qr_and_configs.send_sub_link(
            update,
            context,
            business=DummyBiz(),
            actor=123,
            subscription_id=1,
            settings={"show_sub_link": False},
        )
    )

    assert len(cb.answers) == 1
    assert "غیرفعال است" in cb.answers[0][0][0]
    assert len(message.sent) == 0


def test_send_sub_link_success() -> None:
    """Verify send_sub_link formats HTML and back button."""
    message = FakeMessage()
    cb = FakeCallbackQuery("shop:sublink:1", message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123),
        effective_message=message,
        callback_query=cb,
    )
    context = SimpleNamespace(bot=SimpleNamespace())

    class DummyBiz:
        def subscription_link(self, actor, subscription_id):
            return "https://example.com/sub/123"

    asyncio.run(
        qr_and_configs.send_sub_link(
            update,
            context,
            business=DummyBiz(),
            actor=123,
            subscription_id=1,
            settings={"show_sub_link": True},
        )
    )

    assert len(message.sent) == 1
    text, kwargs = message.sent[0]
    assert "https://example.com/sub/123" in text
    assert kwargs.get("parse_mode") == "HTML"
    buttons = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
    assert buttons == ["shop:configmenu:1"]


def test_send_direct_configs_offloaded(monkeypatch) -> None:
    """Verify send_direct_configs delegates subscription_raw_content via asyncio.to_thread."""
    message = FakeMessage()
    cb = FakeCallbackQuery("shop:configs:1", message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123),
        effective_message=message,
        callback_query=cb,
    )

    sent_bot_messages: list[tuple[int, str]] = []

    async def fake_send_message(chat_id: int, text: str, **kwargs: Any) -> None:
        sent_bot_messages.append((chat_id, text))

    context = SimpleNamespace(bot=SimpleNamespace(send_message=fake_send_message))

    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

    class DummyBiz:
        def subscription_raw_content(self, actor, subscription_id):
            return "vless://a@b:443\nvless://c@d:443"

    asyncio.run(
        qr_and_configs.send_direct_configs(
            update,
            context,
            business=DummyBiz(),
            actor=123,
            subscription_id=1,
            settings={"show_direct_config": True},
        )
    )

    assert any("subscription_raw_content" in name for name in to_thread_calls)
    assert len(sent_bot_messages) == 2
    assert "vless://" in sent_bot_messages[0][1]
