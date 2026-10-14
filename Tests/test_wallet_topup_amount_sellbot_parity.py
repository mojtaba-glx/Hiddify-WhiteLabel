"""SellBot parity for the wallet top-up amount prompt (UserBot).

SellBot (UserBot/main.py wallet:card + WAIT_WALLET_TOPUP_AMOUNT):
- deletes the inline menu and sends
  "🔻 لطفا مبلغی که قصد شارژ حساب خود دارید را به تومان وارد کنید:"
  with a ReplyKeyboard [[بازگشت]] (cancel_keyboard), not an inline button.
- بازگشت/لغو cancels with "عملیات لغو شد." + main menu.
- non-digit / <=0 re-prompts the same amount text with the بازگشت keyboard.
- below min_transaction_toman replies the min error with the same keyboard.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import ReplyKeyboardMarkup

from TenantRuntime.UserBot import handlers as user_handlers


def test_cancel_keyboard_is_reply_back_button() -> None:
    markup = user_handlers._cancel_keyboard()
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert len(markup.keyboard) == 1
    assert [b.text for b in markup.keyboard[0]] == ["بازگشت"]
    # It must be a reply keyboard (main keyboard area), not inline.
    assert not hasattr(markup, "inline_keyboard")


def test_is_cancel_text_matches_sellbot() -> None:
    for text in ("بازگشت", "🔙بازگشت", "🔙 بازگشت", "لغو", "❌لغو", "❌ لغو", "/cancel"):
        assert user_handlers._is_cancel_text(text) is True
    assert user_handlers._is_cancel_text("50000") is False
    assert user_handlers._is_cancel_text("💰کیف پول") is False


def test_wallettopupstart_uses_reply_back_not_inline_wallet() -> None:
    source = pathlib.Path("TenantRuntime/UserBot/handlers.py").read_text(encoding="utf-8")
    section = source[source.index('if data.startswith("shop:wallettopupstart:"):'):]
    section = section[: section.index('if data.startswith("shop:wallettopupmethod:"):')]
    # SellBot parity: delete inline menu + send new message with reply keyboard.
    assert "await update.callback_query.message.delete()" in section
    assert "await context.bot.send_message(" in section
    assert "_cancel_keyboard()" in section
    assert "به {_wallet_currency_title(currency)} وارد کنید:" in section
    # Old inline wallet button must be gone from this prompt.
    assert 'InlineKeyboardButton("↩️ کیف پول"' not in section
    assert 'callback_data="shop:wallet"' not in section


def test_wallet_topup_amount_flow_uses_cancel_keyboard() -> None:
    source = pathlib.Path("TenantRuntime/UserBot/handlers.py").read_text(encoding="utf-8")
    section = source[source.index('if kind == "wallet_topup_amount":'):]
    section = section[: section.index('if kind == "wallet_topup_create":')]
    assert "_is_cancel_text(text)" in section
    assert '"عملیات لغو شد."' in section
    assert "_cancel_keyboard()" in section
    # Invalid input re-prompts the SellBot amount text.
    assert "مبلغی که قصد شارژ" in section
    # Min-transaction error keeps the reply keyboard, not inline wallet.
    assert "حداقل مبلغ مجاز تراکنش" in section
    # The cancel/invalid/min branches must not use the old inline wallet button.
    validation = section[: section.index("method_id = int(flow[")]
    assert 'InlineKeyboardButton("↩️ کیف پول"' not in validation


def _make_flow_context(initial_flow: dict):
    sent: dict = {}

    class FakeMessage:
        text = ""

        async def reply_text(self, text, **kwargs):
            sent["text"] = text
            sent["markup"] = kwargs.get("reply_markup")
            return None

    class FakeBusiness:
        def runtime_userbot_settings(self):
            return {"min_transaction_toman": 10000}

        def _ensure_growth_settings(self):
            return {}

    spec = SimpleNamespace(role="user")
    business = FakeBusiness()
    context = SimpleNamespace(user_data={"biz_flow": dict(initial_flow)})
    return sent, spec, business, context, FakeMessage


def test_wallet_amount_back_cancels_to_main_menu() -> None:
    async def run():
        sent, spec, business, context, FakeMessage = _make_flow_context(
            {"kind": "wallet_topup_amount", "method_id": 1, "currency": "IRR"}
        )
        message = FakeMessage()
        message.text = "بازگشت"
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        with patch.object(user_handlers, "_services", return_value=(spec, None, None, business)):
            with patch.object(user_handlers, "_force_join_allowed", new=AsyncMock(return_value=True)):
                await user_handlers.unknown_text(update, context)
        assert sent["text"] == "عملیات لغو شد."
        assert isinstance(sent["markup"], ReplyKeyboardMarkup)
        assert context.user_data.get("biz_flow") is None

    asyncio.run(run())


def test_wallet_amount_invalid_reprompts_with_back_keyboard() -> None:
    async def run():
        sent, spec, business, context, FakeMessage = _make_flow_context(
            {"kind": "wallet_topup_amount", "method_id": 1, "currency": "IRR"}
        )
        message = FakeMessage()
        message.text = "abc"
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        with patch.object(user_handlers, "_services", return_value=(spec, None, None, business)):
            with patch.object(user_handlers, "_force_join_allowed", new=AsyncMock(return_value=True)):
                await user_handlers.unknown_text(update, context)
        assert "مبلغی که قصد شارژ" in sent["text"]
        assert isinstance(sent["markup"], ReplyKeyboardMarkup)
        assert [b.text for b in sent["markup"].keyboard[0]] == ["بازگشت"]
        # Flow stays alive so the user can retry.
        assert context.user_data.get("biz_flow", {}).get("kind") == "wallet_topup_amount"

    asyncio.run(run())


def test_wallet_amount_below_min_uses_back_keyboard() -> None:
    async def run():
        sent, spec, business, context, FakeMessage = _make_flow_context(
            {"kind": "wallet_topup_amount", "method_id": 1, "currency": "IRR"}
        )
        message = FakeMessage()
        message.text = "5000"
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        with patch.object(user_handlers, "_services", return_value=(spec, None, None, business)):
            with patch.object(user_handlers, "_force_join_allowed", new=AsyncMock(return_value=True)):
                await user_handlers.unknown_text(update, context)
        assert "حداقل مبلغ مجاز تراکنش" in sent["text"]
        assert "10,000" in sent["text"]
        assert isinstance(sent["markup"], ReplyKeyboardMarkup)
        assert [b.text for b in sent["markup"].keyboard[0]] == ["بازگشت"]

    asyncio.run(run())
