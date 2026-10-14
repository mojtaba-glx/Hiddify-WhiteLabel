"""SellBot parity for the wallet purchase path (UserBot).

Covers the exact SellBot texts, QR delivery and AdminBot report for
shop:walletpay / shop:retryorder:
- user confirmation matches SellBot wallet flow
- single enabled link -> QR photo with SellBot caption + guide/back
- multi/zero links -> status detail fallback (no raw URL paste)
- admin caption matches SellBot _build_subscription_created_caption
"""

from __future__ import annotations

import asyncio
import pathlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from TenantRuntime.UserBot import handlers as user_handlers


def test_wallet_confirm_text_matches_sellbot() -> None:
    text = user_handlers._wallet_confirm_text(26)
    assert "🎉 تراکنش شما تایید شد" in text
    assert "📊وضعیت اشتراک" in text
    assert "🎁 شناسه تراکنش:26" in text
    # Old WhiteLabel wording must be gone from the helper.
    assert "با کیف پول پرداخت و فعال شد" not in text


def test_wallet_qr_caption_matches_sellbot() -> None:
    caption = user_handlers._wallet_qr_caption(
        "🔗 لینک اشتراک:", "https://example.com/sub/all.txt"
    )
    assert "🎉 اشتراک شما آماده‌ی استفاده است ✅" in caption
    assert "🔗 لینک اشتراک:" in caption
    assert "<code>https://example.com/sub/all.txt</code>" in caption
    assert "📄 جهت کپی شدن لینک کافیست" in caption


def test_wallet_qr_keyboard_has_guide_and_back() -> None:
    markup = user_handlers._wallet_qr_keyboard(77)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    texts = [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]
    # Subscription-aware guide (SellBot guide:back:s{id} parity).
    assert "shop:guidesub:77" in callbacks
    assert "shop:subrefresh:77" in callbacks
    assert any("راهنمای اتصال" in t for t in texts)
    assert any("بازگشت" in t for t in texts)


def test_wallet_admin_caption_prefers_7digit_service_code() -> None:
    order = {
        "id": 26,
        "plan_name": "Pro",
        "selected_server_label": "آلمان",
        "traffic_gb": 10,
        "duration_days": 30,
        "amount": 130000,
        "currency": "IRR",
    }
    long_token = "5ZorKZRu1BMOx_StFPeK55MOYZbla7hf"
    result = {"operation": "purchase", "smart_code": long_token, "id": 55}
    caption = user_handlers._build_wallet_admin_caption(
        order=order, result=result, service_name="vpn-123456",
        service_code="4829135", wallet_balance_after=50000,
    )
    assert "📄 گزارش ایجاد اشتراک" in caption
    assert "👤اشتراک: vpn-123456" in caption
    assert "🛰سرور: آلمان" in caption
    assert "📊حجم: 10.0 گیگابایت" in caption
    assert "⏳زمان: 30 روز" in caption
    # IRR 130000 -> 13,000 تومان (same conversion as receipt reports).
    assert "💰مبلغ پرداختی: 13,000 تومان" in caption
    assert "💳پرداخت از کیف پول کاربر — مبلغ کسر شد" in caption
    assert "💼مانده کیف پول کاربر: 5,000 تومان" in caption
    # 7-digit service code wins; the smart-link token must never leak here.
    assert "🔑شناسه اشتراک:4829135" in caption
    assert long_token not in caption


def test_wallet_admin_caption_renew_matches_sellbot() -> None:
    order = {
        "id": 27, "plan_name": "Pro", "selected_server_label": "ترکیه",
        "traffic_gb": 20, "duration_days": 60, "amount": 200000,
        "currency": "IRR",
    }
    result = {"operation": "renewal", "id": 99}
    caption = user_handlers._build_wallet_admin_caption(
        order=order, result=result, service_name="vpn-999999",
        wallet_balance_after=None,
    )
    assert "📄 گزارش تمدید اشتراک" in caption
    assert "🔑شناسه اشتراک:99" in caption
    # No balance line when the balance is unknown.
    assert "مانده کیف پول" not in caption


def test_walletpay_handler_uses_sellbot_delivery_and_admin_report() -> None:
    source = pathlib.Path(
        "TenantRuntime/UserBot/handlers.py"
    ).read_text(encoding="utf-8")
    wallet_section = source[source.index('if data.startswith("shop:walletpay:"):'):]
    wallet_section = wallet_section[: wallet_section.index('if data.startswith("shop:paymethods:"):')]
    assert "_wallet_confirm_text(" in wallet_section
    assert "_deliver_wallet_purchase(" in wallet_section
    assert "_notify_admin_wallet_purchase(" in wallet_section
    assert "_send_purchase_event_report(" in wallet_section
    # Old plain-URL paste must be gone from the wallet success path.
    assert "با کیف پول پرداخت و فعال شد" not in wallet_section


def test_retryorder_handler_uses_sellbot_delivery() -> None:
    source = pathlib.Path(
        "TenantRuntime/UserBot/handlers.py"
    ).read_text(encoding="utf-8")
    retry_section = source[source.index('if data.startswith("shop:retryorder:"):'):]
    retry_section = retry_section[: retry_section.index('if data.startswith("shop:cancelorder:"):')]
    assert "_deliver_wallet_purchase(" in retry_section
    assert "_delivery_access_text(business, actor, result, settings)" not in retry_section


def test_deliver_wallet_single_link_sends_qr_photo() -> None:
    async def run():
        sent: dict = {}

        class FakeBot:
            async def send_photo(self, **kwargs):
                sent["photo"] = True
                sent["caption"] = kwargs.get("caption")
                sent["markup"] = kwargs.get("reply_markup")

            async def send_message(self, **kwargs):
                sent["message"] = kwargs.get("text")

        context = SimpleNamespace(bot=FakeBot())

        class FakeBusiness:
            def subscription_link(self, actor, subscription_id=0):
                return "https://example.com/sub/all.txt"

            def customer_subscription_status(
                self, actor, subscription_id=0, refresh=False
            ):
                return {"id": subscription_id, "service_name": "vpn-1"}

        business = FakeBusiness()
        settings = {
            "show_sub_link": True,
            "show_auto_sub_link": False,
            "show_sub_link_b64": False,
            "show_multi_server": False,
            "show_multi_server_b64": False,
            "show_direct_config": False,
            "show_user_page_link": False,
        }
        result = {"subscription_id": 55, "id": 55}
        await user_handlers._deliver_wallet_purchase(
            context, business, 12345, settings, result
        )
        assert sent.get("photo") is True
        assert "🎉 اشتراک شما آماده‌ی استفاده است ✅" in sent["caption"]
        assert "<code>https://example.com/sub/all.txt</code>" in sent["caption"]
        callbacks = [
            button.callback_data
            for row in sent["markup"].inline_keyboard
            for button in row
        ]
        assert "shop:guidesub:55" in callbacks
        assert "shop:subrefresh:55" in callbacks

    asyncio.run(run())


def test_deliver_wallet_multi_link_sends_status_detail() -> None:
    async def run():
        sent: dict = {}

        class FakeBot:
            async def send_photo(self, **kwargs):
                sent["photo"] = True

            async def send_message(self, **kwargs):
                sent["message"] = kwargs.get("text")
                sent["markup"] = kwargs.get("reply_markup")
                sent["parse_mode"] = kwargs.get("parse_mode")

        context = SimpleNamespace(bot=FakeBot())

        class FakeBusiness:
            def subscription_link(self, actor, subscription_id=0):
                return "https://a.example/sub"

            def automatic_subscription_link(self, actor, subscription_id=0):
                return "https://a.example/auto"

            def customer_subscription_status(
                self, actor, subscription_id=0, refresh=False
            ):
                return {
                    "id": subscription_id,
                    "service_code": "1234567",
                    "service_name": "vpn-1",
                    "plan_name": "Pro",
                    "server_label": "آلمان",
                    "usage_bytes": 0,
                    "traffic_bytes": 10 * 1024 ** 3,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "subscription_price": 130000,
                    "subscription_currency": "IRR",
                }

        business = FakeBusiness()
        settings = {
            "show_sub_link": True,
            "show_auto_sub_link": True,
            "show_sub_link_b64": False,
            "show_multi_server": False,
            "show_multi_server_b64": False,
            "show_direct_config": True,
            "show_user_page_link": False,
            "show_username": True,
        }
        result = {"subscription_id": 56, "id": 56}
        await user_handlers._deliver_wallet_purchase(
            context, business, 12345, settings, result
        )
        # Multi-link must NOT paste raw URLs; it shows the status screen.
        assert sent.get("photo") is None
        assert "📄اطلاعات اشتراک شما" in sent["message"]
        assert "https://a.example" not in sent["message"]

    asyncio.run(run())


def test_notify_admin_wallet_purchase_sends_sellbot_caption() -> None:
    async def run():
        sent: dict = {}

        class FakeBot:
            def __init__(self, token=""):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send_message(self, **kwargs):
                sent["text"] = kwargs.get("text")
                sent["chat_id"] = kwargs.get("chat_id")
                sent["markup"] = kwargs.get("reply_markup")

        class FakeBusiness:
            owner_telegram_id = 9001
            secret_cipher = object()

            def customer_subscription_status(
                self, actor, subscription_id=0, refresh=False
            ):
                return {"service_name": "vpn-123456"}

        order = {
            "id": 26, "customer_id": 44, "plan_name": "Pro",
            "selected_server_label": "آلمان", "traffic_gb": 10,
            "duration_days": 30, "amount": 130000, "currency": "IRR",
        }
        result = {"operation": "purchase", "subscription_id": 55, "smart_code": "4829135"}
        with patch.object(
            user_handlers, "_sibling_admin_bot_token", return_value="tok"
        ):
            with patch.object(user_handlers, "Bot", FakeBot):
                await user_handlers._notify_admin_wallet_purchase(
                    FakeBusiness(), actor=12345, order=order, result=result,
                    display_name="Buyer", customer_id=44,
                    wallet_balance_after=50000,
                )
        class FakeBusiness:
            owner_telegram_id = 9001
            secret_cipher = object()

            def customer_subscription_status(
                self, actor, subscription_id=0, refresh=False
            ):
                return {"service_name": "vpn-123456", "service_code": "7654321"}

        order = {
            "id": 26, "customer_id": 44, "plan_name": "Pro",
            "selected_server_label": "آلمان", "traffic_gb": 10,
            "duration_days": 30, "amount": 130000, "currency": "IRR",
        }
        long_token = "5ZorKZRu1BMOx_StFPeK55MOYZbla7hf"
        result = {
            "operation": "purchase", "subscription_id": 55,
            "smart_code": long_token,
        }
        with patch.object(
            user_handlers, "_sibling_admin_bot_token", return_value="tok"
        ):
            with patch.object(user_handlers, "Bot", FakeBot):
                await user_handlers._notify_admin_wallet_purchase(
                    FakeBusiness(), actor=12345, order=order, result=result,
                    display_name="Buyer", customer_id=44,
                    wallet_balance_after=50000,
                )
        assert sent["chat_id"] == 9001
        assert "📄 گزارش ایجاد اشتراک" in sent["text"]
        assert "💳پرداخت از کیف پول کاربر" in sent["text"]
        # 7-digit service code from the subscription, never the smart token.
        assert "🔑شناسه اشتراک:7654321" in sent["text"]
        assert long_token not in sent["text"]
        callbacks = [
            button.callback_data
            for row in sent["markup"].inline_keyboard
            for button in row
        ]
        assert "userbot:user:44" in callbacks

    asyncio.run(run())


def test_wallet_confirm_carries_no_inline_menu() -> None:
    # SellBot parity: the confirmation message has no inline buttons;
    # the QR photo below it owns guide/back.
    source = pathlib.Path(
        "TenantRuntime/UserBot/handlers.py"
    ).read_text(encoding="utf-8")
    wallet_section = source[source.index('if data.startswith("shop:walletpay:"):'):]
    wallet_section = wallet_section[: wallet_section.index('if data.startswith("shop:paymethods:"):')]
    success_edit = wallet_section[wallet_section.index("_wallet_confirm_text(order_id),"):]
    success_edit = success_edit[: success_edit.index("return")]
    assert "reply_markup" not in success_edit


def test_guidesub_handlers_are_wired() -> None:
    source = pathlib.Path(
        "TenantRuntime/UserBot/handlers.py"
    ).read_text(encoding="utf-8")
    assert 'if data.startswith("shop:guidesub:"):' in source
    assert "shop:subrefresh:{subscription_id}" in source
    # Photo-safe editing (SellBot _safe_edit_message_text parity).
    assert "async def _safe_edit_text(" in source
    assert "_is_photo_edit_error(" in source


def test_safe_edit_text_replaces_photo_with_new_message() -> None:
    from telegram.error import BadRequest

    async def run():
        calls: dict = {}

        class FakeMessage:
            async def delete(self):
                calls["deleted"] = True

            async def reply_text(self, text, **kwargs):
                calls["text"] = text
                calls["markup"] = kwargs.get("reply_markup")

        class FakeQuery:
            message = FakeMessage()

            async def edit_message_text(self, *args, **kwargs):
                raise BadRequest("There is no text in the message to edit")

        markup = object()
        await user_handlers._safe_edit_text(
            FakeQuery(), "hello", reply_markup=markup
        )
        assert calls.get("deleted") is True
        assert calls.get("text") == "hello"
        assert calls.get("markup") is markup

    asyncio.run(run())


def test_safe_edit_text_ignores_not_modified() -> None:
    from telegram.error import BadRequest

    async def run():
        class FakeQuery:
            message = SimpleNamespace(delete=None)

            async def edit_message_text(self, *args, **kwargs):
                raise BadRequest("Message is not modified")

        # Must not raise.
        await user_handlers._safe_edit_text(FakeQuery(), "same")

    asyncio.run(run())
