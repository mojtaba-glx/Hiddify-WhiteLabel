"""Phase 17 receipt review chain: admin push, user DM texts, delivery loop."""

from __future__ import annotations

import pathlib

from TenantRuntime.business import TenantBusinessService
from TenantRuntime.UserBot.handlers import _receipt_report_content


def _service(conn, factories, owner=7601):
    tenant = factories.tenant(owner_telegram_id=owner)
    return tenant, TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=owner
    )


def _pending_order_receipt(service, customer_tg=7611, price=130000):
    plan = service.add_plan(
        7601, name="Review Plan", traffic_gb=10, duration_days=30,
        price=price, currency="IRR",
    )
    method = service.add_payment_method(
        7601, kind="card", title="Card", currency="IRR",
        destination="60379911 | Owner",
    )
    service.register_customer(
        customer_tg, display_name=f"Buyer{customer_tg}", username=None
    )
    order = service.create_order(customer_tg, int(plan["id"]))
    receipt = service.submit_receipt(
        customer_tg, order_id=int(order["id"]),
        method_id=int(method["id"]), reference="track-7 | 1234",
    )
    return order, receipt


def test_receipt_report_content_matches_sellbot() -> None:
    caption, markup = _receipt_report_content(
        payment_key="order:9",
        display_name="Buyer",
        customer_id=3,
        amount=130000,
        currency="IRR",
        last4="1234",
    )
    assert "💸گزارش تایید پرداخت🕊" in caption
    assert "🔖شیوه پرداخت:کارت به کارت" in caption
    assert "🔑شناسه تراکنش:order:9" in caption
    assert "💰مبلغ پرداخت:13,000 تومان" in caption
    assert "💳۴ رقم آخر کارت مبدا: 1234" in caption
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "userbot:pay:review:order:9:no" in callbacks
    assert "userbot:pay:review:order:9:yes" in callbacks
    assert "userbot:user:3" in callbacks

    plain, _ = _receipt_report_content(
        payment_key="wallet_topup:4", display_name="B",
        amount=50000, currency="IRR",
    )
    assert "۴ رقم آخر" not in plain
    assert "💰مبلغ پرداخت:5,000 تومان" in plain


def test_pending_fulfillment_and_attempt_tracking(conn, factories) -> None:
    _, service = _service(conn, factories)
    order, _ = _pending_order_receipt(service)
    assert service.list_pending_fulfillment_orders() == []

    pending = service.list_pending_fulfillment_orders()
    assert pending == []

    # Approve via payment review so the order becomes paid.
    receipts = service.list_receipts_admin(7601)
    assert len(receipts) == 1
    result = service.review_payment_admin(
        7601, payment_key=f"order:{int(receipts[0]['id'])}", approve=True
    )
    assert result["status"] == "paid"

    pending = service.list_pending_fulfillment_orders()
    assert len(pending) == 1
    assert pending[0]["customer_telegram_id"] == 7611
    assert int(pending[0]["fulfillment_attempts"] or 0) == 0

    service.record_fulfillment_attempt(order_id=int(order["id"]), error="boom")
    pending = service.list_pending_fulfillment_orders()
    assert int(pending[0]["fulfillment_attempts"]) == 1


def test_payment_detail_text_matches_sellbot() -> None:
    from TenantRuntime.AdminBot.userbot_management import (
        _payment_detail_text,
        _payment_user_label,
    )

    pay = {
        "payment_key": "order:4",
        "display_name": "Speed Vpn",
        "username": "SpeedlVpn",
        "telegram_user_id": 6119169885,
        "created_at": "2026-10-11T06:42:10.436710+00:00",
        "amount": 30000,
        "currency": "IRR",
        "status": "rejected",
        "provider_title": "کارت به کارت",
        "reference": "1234",
    }
    text = _payment_detail_text(pay, "1234")
    assert "◈ شناسه تراکنش: order:4" in text
    assert "👤 کاربر: Speed Vpn" in text
    assert "◈ نام کاربری: @SpeedlVpn" in text
    assert "◈ شناسه کاربر: 6119169885" in text
    assert "◈ مبلغ تراکنش: 3,000 تومان" in text
    assert "◈ وضعیت: ❌ رد شده" in text
    assert "◈ روش تراکنش: کارت به کارت" in text
    assert "◈ ۴ رقم آخر کارت مبدا: 1234" in text
    for old_row in ("◈ نوع:", "◈ Provider:", "◈ پیگیری:", "◈ یادداشت بررسی:"):
        assert old_row not in text

    assert _payment_user_label(pay) == "Speed Vpn"
    assert _payment_user_label({"username": "u", "telegram_user_id": 1}) == "@u"
    assert _payment_user_label({"telegram_user_id": 7}) == "7"


def test_receipt_back_returns_to_previous_screen() -> None:
    import pathlib

    userbot = pathlib.Path("TenantRuntime/UserBot/handlers.py").read_text(
        encoding="utf-8"
    )
    assert "async def _receipt_go_back(" in userbot
    assert "_send_order_summary" in userbot


def test_admin_push_sends_photo_when_bytes_available() -> None:
    """Repro for missing receipt photo: file_ids are bot-specific, bytes work."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from telegram.error import BadRequest

    import TenantRuntime.UserBot.handlers as user_handlers

    sent = {}

    class FakeBot:
        def __init__(self, token=""):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send_photo(self, **kwargs):
            photo = kwargs.get("photo")
            if isinstance(photo, str):
                # Cross-bot file_id reuse fails like the real API.
                raise BadRequest("wrong file identifier")
            sent["photo"] = True
            sent["caption"] = kwargs.get("caption")
            sent["markup"] = kwargs.get("reply_markup")
            return object()

        async def send_message(self, **kwargs):
            sent["message"] = True
            return object()

    class FakeBusiness:
        owner_telegram_id = 1
        secret_cipher = None

    async def run():
        with patch.object(user_handlers, "Bot", FakeBot):
            with patch.object(
                user_handlers,
                "_sibling_admin_bot_token",
                return_value="x",
            ):
                # Broken path (old behavior): file_id only -> text fallback.
                await user_handlers._notify_admin_receipt(
                    FakeBusiness(),
                    payment_key="order:5",
                    display_name="Buyer",
                    amount=34290,
                    currency="IRR",
                    last4="1234",
                    photo_file_id="userbot-side-file-id",
                )
                assert sent.get("message") is True
                assert sent.get("photo") is None

                sent.clear()

                # Fixed path: raw bytes -> photo delivered with caption.
                await user_handlers._notify_admin_receipt(
                    FakeBusiness(),
                    payment_key="order:5",
                    display_name="Buyer",
                    amount=34290,
                    currency="IRR",
                    last4="1234",
                    photo_file_id="userbot-side-file-id",
                    photo_bytes=b"fake-jpeg-bytes",
                )
                assert sent.get("photo") is True
                assert sent.get("message") is None
                assert "order:5" in sent["caption"]
                callbacks = [
                    button.callback_data
                    for row in sent["markup"].inline_keyboard
                    for button in row
                ]
                assert "userbot:pay:review:order:5:yes" in callbacks

    asyncio.run(run())


def test_download_receipt_bytes_uses_userbot() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    import TenantRuntime.UserBot.handlers as user_handlers

    async def run():
        tg_file = AsyncMock()
        tg_file.download_as_bytearray.return_value = bytearray(b"img-bytes")
        bot = AsyncMock()
        bot.get_file.return_value = tg_file
        context = AsyncMock()
        context.bot = bot
        data = await user_handlers._download_receipt_bytes(context, "fid-1")
        assert data == b"img-bytes"
        assert await user_handlers._download_receipt_bytes(context, "") == b""

    asyncio.run(run())


def test_subscription_service_code_is_7_digits(conn, factories) -> None:
    import re

    from TenantRuntime.business import TenantBusinessService

    tenant = factories.tenant(owner_telegram_id=7701)
    service = TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=7701
    )
    plan = service.add_plan(
        7701, name="Code Plan", traffic_gb=10, duration_days=30,
        price=130000, currency="IRR",
    )
    method = service.add_payment_method(
        7701, kind="card", title="Card", currency="IRR", destination="6037"
    )
    codes = set()
    for tg in (7711, 7712):
        service.register_customer(tg, display_name=f"B{tg}", username=None)
        order = service.create_order(tg, int(plan["id"]))
        receipt = service.submit_receipt(
            tg, order_id=int(order["id"]),
            method_id=int(method["id"]), reference="r",
        )
        service.review_payment_admin(
            7701, payment_key=f"order:{int(receipt['id'])}", approve=True
        )
        row = conn.execute(
            "SELECT service_code FROM tenant_subscriptions"
            " WHERE tenant_id=? AND order_id=?",
            (int(tenant["id"]), int(order["id"])),
        ).fetchone()
        assert re.fullmatch(r"\d{7}", str(row["service_code"] or ""))
        codes.add(str(row["service_code"]))
    assert len(codes) == 2


def test_subscription_detail_matches_sellbot() -> None:
    from TenantRuntime.UserBot.handlers import (
        _subscription_detail_text,
        _subscription_price,
    )

    item = {
        "id": 5,
        "service_code": "4829135",
        "service_name": "My Service",
        "plan_name": "Plan",
        "server_label": "لوکیشن ترکیه 🇹🇷",
        "usage_bytes": 0,
        "traffic_bytes": 10 * 1024 ** 3,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "subscription_price": 30000,
        "subscription_currency": "IRR",
    }
    text = _subscription_detail_text(item, {})
    assert "📄اطلاعات اشتراک شما" in text
    assert "👤نام: My Service" in text
    assert "📡سرور: لوکیشن ترکیه 🇹🇷" in text
    assert "📊میزان استفاده: 0.0 از 10.0 گیگ" in text
    assert "💰قیمت اشتراک: 3,000 تومان" in text
    assert "🔑شناسه: `4829135`" in text
    assert "<code>" not in text

    assert _subscription_price(
        {"subscription_price": 32430, "subscription_currency": "IRR"}
    ) == "3,000 تومان"
    assert _subscription_price({"subscription_price": None}) == "نامشخص"


def test_review_chain_wired_in_source() -> None:
    userbot = pathlib.Path("TenantRuntime/UserBot/handlers.py").read_text(
        encoding="utf-8"
    )
    assert userbot.count("_notify_receipt_submitted(") >= 4
    delivery = pathlib.Path(
        "TenantRuntime/UserBot/direct_buy_delivery.py"
    ).read_text(encoding="utf-8")
    assert "fulfill_paid_order(" in delivery
    assert "record_fulfillment_attempt(" in delivery
    assert "fulfill_order(" not in delivery
    admin = pathlib.Path(
        "TenantRuntime/AdminBot/userbot_management.py"
    ).read_text(encoding="utf-8")
    assert "_notify_reviewed_user(" in admin
    assert "سرویس شما آماده شد" in admin
    assert "فعال‌سازی ادامه دارد" in admin
    assert "پرداخت شما رد شد" in admin
    assert "کیف پول شما با موفقیت شارژ شد" in admin
