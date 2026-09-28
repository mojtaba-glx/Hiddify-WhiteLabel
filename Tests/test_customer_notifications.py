"""Customer-to-master operational notification tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from MasterBot.customer_handlers import (
    _notify_master_new_receipt,
    _notify_master_provisioned,
)
from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import MasterService
from Shared.crypto import FernetTokenCipher, generate_key


def _context(conn):
    master = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
    )
    portal = CustomerPortalService(conn, master_service=master)
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"master_service": master, "customer_portal_service": portal}
        ),
        bot=bot,
    )
    return portal, context


def _update(user_id: int = 101):
    return SimpleNamespace(
        effective_user=SimpleNamespace(
            id=user_id,
            username="buyer_user",
            full_name="Buyer User",
            first_name="Buyer",
        )
    )


def test_new_receipt_notification_goes_to_master_with_review_button(conn) -> None:
    portal, context = _context(conn)
    update = _update()
    asyncio.run(
        _notify_master_new_receipt(
            update,
            context,
            receipt={"id": 17},
            order={"public_id": "ord-test", "amount": 250, "currency": "USD"},
            method={"title": "Main Card"},
        )
    )

    context.bot.send_message.assert_awaited_once()
    call = context.bot.send_message.await_args
    assert call.kwargs["chat_id"] == 9001
    assert "ord-test" in call.kwargs["text"]
    assert "250 USD" in call.kwargs["text"]
    keyboard = call.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "payment:receipt:17"


def test_provisioning_notification_goes_to_master_without_secrets(conn) -> None:
    _, context = _context(conn)
    update = _update(202)
    asyncio.run(
        _notify_master_provisioned(
            update,
            context,
            tenant_id=44,
            tenant_name="Speed Store",
            admin_username="speed_admin_bot",
            user_username="speed_user_bot",
            plan_name="Pro",
        )
    )

    context.bot.send_message.assert_awaited_once()
    call = context.bot.send_message.await_args
    rendered = call.kwargs["text"]
    assert "Speed Store" in rendered
    assert "@speed_admin_bot" in rendered
    assert "@speed_user_bot" in rendered
    assert "secret" not in rendered.lower()
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "tenant:view:44"
