"""Tests for enhanced customer purchase and order recovery flows in MasterBot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from MasterBot import customer_handlers, customer_views
from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import BotIdentity, MasterService
from Shared.crypto import FernetTokenCipher, generate_key


class DummyVerifier:
    async def verify(self, token: str) -> BotIdentity:
        return BotIdentity(telegram_bot_id=12345, username="test_bot")


@pytest.fixture()
def portal(conn) -> CustomerPortalService:
    master = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key()), bot_verifier=DummyVerifier()
    )
    return CustomerPortalService(conn, master_service=master)


def test_orders_keyboard_has_pay_and_setup_buttons() -> None:
    orders = [
        {"id": 1, "public_id": "ord-1", "status": "pending_payment", "kind": "purchase"},
        {"id": 2, "public_id": "ord-2", "status": "paid", "kind": "purchase"},
        {"id": 3, "public_id": "ord-3", "status": "fulfilled", "kind": "purchase"},
    ]
    keyboard = customer_views.orders_keyboard(orders)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "customer:payorder:1" in callbacks
    assert "customer:cancelorder:1" in callbacks
    assert "customer:setup:2" in callbacks
    assert "customer:home" in callbacks


def test_plan_keyboard_single_plan_back_to_home() -> None:
    kb_single = customer_views.plan_keyboard(10, has_multiple_plans=False)
    single_callbacks = [btn.callback_data for row in kb_single.inline_keyboard for btn in row]
    assert "customer:order:10" in single_callbacks
    assert "customer:home" in single_callbacks
    assert "customer:plans" not in single_callbacks

    kb_multi = customer_views.plan_keyboard(10, has_multiple_plans=True)
    multi_callbacks = [btn.callback_data for row in kb_multi.inline_keyboard for btn in row]
    assert "customer:order:10" in multi_callbacks
    assert "customer:plans" in multi_callbacks


def test_payment_keyboard_has_back_and_cancel_buttons() -> None:
    order = {"id": 1, "public_id": "ord-1", "amount": 1000}
    methods = [{"id": 5, "kind": "card", "title": "Card 1"}]
    kb = customer_views.payment_keyboard(order, methods, wallet_balance=0)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "customer:pay:1:5" in callbacks
    assert "customer:plans" in callbacks
    assert "customer:cancelorder:1" in callbacks
    assert "customer:orders" in callbacks


def test_payorder_callback_shows_payment_options(portal, factories) -> None:
    plan = factories.plan(price=50000, duration_days=30)
    portal.configure_plan_commerce(9001, int(plan["id"]), currency="IRR", is_public=True, trial_days=0)
    portal.register_customer(101, display_name="Test Customer")
    portal.add_payment_method(9001, kind="card", title="Bank Card", currency="IRR", destination="6037...")

    order = portal.create_purchase_order(101, int(plan["id"]))

    rendered_messages = []
    async def fake_render(update, text, keyboard):
        rendered_messages.append((text, keyboard))

    query = SimpleNamespace(
        data=f"customer:payorder:{int(order['id'])}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101, full_name="Test Customer"),
        effective_message=SimpleNamespace(),
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={"customer_flow": {"kind": "stale"}},
    )

    asyncio.run(customer_handlers.handle_customer_callback(update, context, query.data))

    assert "customer_flow" not in context.user_data
    assert query.edit_message_text.call_count >= 1


def test_cancel_text_cancels_active_flow(portal) -> None:
    sent_replies = []
    message = SimpleNamespace(
        text="لغو",
        reply_text=AsyncMock(side_effect=lambda text, **kw: sent_replies.append(text)),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={"customer_flow": {"kind": "setup_details"}},
    )

    handled = asyncio.run(customer_handlers.handle_customer_text(update, context))
    assert handled is True
    assert "customer_flow" not in context.user_data
    assert any("لغو شد" in msg for msg in sent_replies)


def test_persian_digits_in_wallet_topup(portal) -> None:
    portal.register_customer(101, display_name="Persian Customer")
    portal.add_payment_method(9001, kind="card", title="Card", currency="IRR", destination="1234")

    sent_replies = []
    message = SimpleNamespace(
        text="۵۰۰۰۰",
        reply_text=AsyncMock(side_effect=lambda text, **kw: sent_replies.append(text)),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101, full_name="Persian Customer"),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={"customer_flow": {"kind": "wallet_topup_amount", "currency": "IRR"}},
    )

    handled = asyncio.run(customer_handlers.handle_customer_text(update, context))
    assert handled is True
    assert "customer_flow" not in context.user_data
    orders = portal.list_orders(101)
    assert len(orders) == 1
    assert int(orders[0]["amount"]) == 50000


def test_wallet_available_currencies_discovers_toman_and_card_methods(portal) -> None:
    portal.register_customer(101, display_name="Test Customer")
    portal.add_payment_method(
        9001, kind="card", title="بانک ملی (کارت)", currency="TOMAN", destination="6037991234567890", recipient="مدیر فروشگاه"
    )
    currencies = portal.list_available_currencies(101)
    assert "TOMAN" in currencies
    assert currencies[0] == "TOMAN"


def test_wallet_topup_preset_keyboard_and_text() -> None:
    kb_toman = customer_views.wallet_topup_keyboard("TOMAN")
    callbacks = [btn.callback_data for row in kb_toman.inline_keyboard for btn in row]
    assert "customer:topuppreset:TOMAN:50000" in callbacks
    assert "customer:topuppreset:TOMAN:100000" in callbacks
    assert "customer:topuppreset:TOMAN:200000" in callbacks
    assert "customer:topuppreset:TOMAN:500000" in callbacks
    assert "customer:topuppreset:TOMAN:1000000" in callbacks
    assert "customer:topuppreset:TOMAN:2000000" in callbacks
    assert "customer:wallet" in callbacks
    assert "customer:home" in callbacks

    text_toman = customer_views.wallet_topup_text("TOMAN")
    assert "تومان" in text_toman
    assert "100000" in text_toman

    kb_usd = customer_views.wallet_topup_keyboard("USD")
    callbacks_usd = [btn.callback_data for row in kb_usd.inline_keyboard for btn in row]
    assert "customer:topuppreset:USD:10" in callbacks_usd
    assert "customer:topuppreset:USD:25" in callbacks_usd


def test_card_payment_instructions_formatting() -> None:
    method = {
        "id": 10,
        "kind": "card",
        "title": "کارت سامان",
        "destination": "6219861012345678",
        "recipient": "محمد رضایی",
        "instructions": "لطفاً ۴ رقم آخر کارت را ارسال کنید",
    }
    order = {"public_id": "top_xyz", "amount": 100000, "currency": "TOMAN"}
    info = customer_views.payment_instructions(method, order)
    assert "شماره کارت: 6219861012345678" in info
    assert "به نام: محمد رضایی" in info
    assert "100,000 تومان" in info
    assert "کارت سامان" in info
    assert "تصویر رسید" in info


def test_topup_preset_callback_creates_topup_order(portal) -> None:
    portal.register_customer(101, display_name="Test Customer")
    portal.add_payment_method(
        9001, kind="card", title="کارت به کارت", currency="TOMAN", destination="6037...", recipient="مدیر"
    )

    rendered_messages = []
    query = SimpleNamespace(
        data="customer:topuppreset:TOMAN:200000",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=lambda **kw: rendered_messages.append(kw.get("text", ""))),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101, full_name="Test Customer"),
        effective_message=SimpleNamespace(),
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={"customer_flow": {"kind": "wallet_topup_amount", "currency": "TOMAN"}},
    )

    handled = asyncio.run(customer_handlers.handle_customer_callback(update, context, query.data))
    assert handled is True
    assert "customer_flow" not in context.user_data
    orders = portal.list_orders(101)
    assert len(orders) == 1
    assert int(orders[0]["amount"]) == 200000
    assert orders[0]["currency"] == "TOMAN"
    assert orders[0]["kind"] == "wallet_topup"
    assert any("200,000 تومان" in msg for msg in rendered_messages)


def test_card_to_card_wallet_topup_receipt_and_purchase_e2e(portal, factories) -> None:
    plan = factories.plan(price=150000, duration_days=30)
    portal.configure_plan_commerce(9001, int(plan["id"]), currency="TOMAN", is_public=True, trial_days=0)
    portal.register_customer(101, display_name="Iranian Customer")
    card_method = portal.add_payment_method(
        9001, kind="card", title="کارت به کارت", currency="TOMAN", destination="6037991234567890", recipient="مدیر"
    )

    # 1. Customer initiates top-up for 200,000 Toman
    topup_order = portal.create_wallet_topup(101, currency="TOMAN", amount=200000)
    assert topup_order["status"] == "pending_payment"

    # 2. Customer submits tracking reference for card-to-card
    receipt = portal.submit_receipt(
        101, order_id=int(topup_order["id"]), payment_method_id=int(card_method["id"]), reference="12345678"
    )
    assert receipt["status"] == "pending"

    # 3. Master admin approves the card-to-card receipt
    reviewed = portal.review_receipt(9001, int(receipt["id"]), approve=True)
    assert reviewed["order_status"] == "fulfilled"
    assert portal.wallet_balance(101, "TOMAN") == 200000

    # 4. Customer buys the 150,000 Toman plan using wallet balance
    purchase_order = portal.create_purchase_order(101, int(plan["id"]))
    paid = portal.pay_order_from_wallet(101, int(purchase_order["id"]))
    assert paid["status"] == "paid"
    assert portal.wallet_balance(101, "TOMAN") == 50000


def test_customer_submits_card_receipt_photo(portal) -> None:
    portal.register_customer(101, display_name="Photo Customer")
    card_method = portal.add_payment_method(
        9001, kind="card", title="کارت به کارت", currency="TOMAN", destination="6037...", recipient="مدیر"
    )
    topup_order = portal.create_wallet_topup(101, currency="TOMAN", amount=100000)

    photo_item = SimpleNamespace(file_id="telegram_photo_12345")
    sent_replies = []
    message = SimpleNamespace(
        photo=[photo_item],
        caption="فیش کارت به کارت بانک سامان",
        reply_text=AsyncMock(side_effect=lambda text, **kw: sent_replies.append(text)),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101, full_name="Photo Customer"),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={
            "customer_flow": {
                "kind": "receipt",
                "order_id": int(topup_order["id"]),
                "payment_method_id": int(card_method["id"]),
            }
        },
    )

    handled = asyncio.run(customer_handlers.handle_customer_photo(update, context))
    assert handled is True
    assert "customer_flow" not in context.user_data
    assert any("تصویر رسید" in msg for msg in sent_replies)

    receipts = portal.conn.execute("SELECT * FROM payment_receipts WHERE order_id = ?", (int(topup_order["id"]),)).fetchall()
    assert len(receipts) == 1
    assert receipts[0]["telegram_file_id"] == "telegram_photo_12345"
    assert receipts[0]["reference"] == "فیش کارت به کارت بانک سامان"


def test_customer_wallet_callback_renders_wallet(portal) -> None:
    portal.register_customer(101, display_name="Test Customer")
    rendered_messages = []
    query = SimpleNamespace(
        data="customer:wallet",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=lambda **kw: rendered_messages.append(kw.get("text", ""))),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101, full_name="Test Customer"),
        effective_message=SimpleNamespace(),
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"customer_portal_service": portal}),
        user_data={"customer_flow": {"kind": "wallet_topup_amount"}},
    )

    handled = asyncio.run(customer_handlers.handle_customer_callback(update, context, query.data))
    assert handled is True
    assert "customer_flow" not in context.user_data
    assert any("کیف پول" in msg for msg in rendered_messages)
