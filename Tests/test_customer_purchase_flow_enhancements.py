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
