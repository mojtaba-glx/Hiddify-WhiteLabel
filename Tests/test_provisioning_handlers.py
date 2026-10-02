"""MasterBot's staged provisioning flow never retains plain bot tokens."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

from MasterBot.handlers import on_callback, on_text
from MasterBot.service import BotIdentity, MasterService
from Shared.crypto import FernetTokenCipher, generate_key
from Tests.conftest import make_fake_token


class MutableVerifier:
    def __init__(self) -> None:
        self.identity = BotIdentity(710001, "admin_bot")
        self.fail = False

    async def verify(self, token: str) -> BotIdentity:
        if self.fail:
            raise RuntimeError(f"bad token {token}")
        return self.identity


def _update(text: str):
    message = SimpleNamespace(
        text=text,
        delete=AsyncMock(),
        reply_text=AsyncMock(),
    )
    chat = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=9001),
        effective_message=message,
        effective_chat=chat,
        callback_query=None,
    )


def _callback_update(data: str):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    chat = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=9001),
        effective_message=None,
        effective_chat=chat,
        callback_query=query,
    )


def test_staged_full_provision_keeps_only_encrypted_admin_draft(conn) -> None:
    verifier = MutableVerifier()
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={"flow": {"kind": "provision_name"}},
    )

    asyncio.run(on_text(_update("Customer"), context))
    assert context.user_data["flow"]["kind"] == "provision_owner"
    assert context.user_data["flow"]["name"] == "Customer"

    asyncio.run(on_text(_update("88001"), context))
    assert context.user_data["flow"]["kind"] == "provision_slug"
    assert context.user_data["flow"]["owner_telegram_id"] == 88001

    asyncio.run(on_text(_update("customer-one"), context))
    assert context.user_data["flow"]["kind"] == "provision_admin_token"
    assert context.user_data["flow"]["slug"] == "customer-one"

    admin_token = make_fake_token("handler-admin")
    admin_update = _update(admin_token)
    asyncio.run(on_text(admin_update, context))
    admin_update.effective_message.delete.assert_awaited_once()
    assert context.user_data["flow"]["kind"] == "provision_user_token"
    assert admin_token not in repr(context.user_data)

    verifier.identity = BotIdentity(710002, "user_bot")
    user_token = make_fake_token("handler-user")
    user_update = _update(user_token)
    asyncio.run(on_text(user_update, context))
    user_update.effective_message.delete.assert_awaited_once()
    assert "flow" not in context.user_data
    calls = user_update.effective_chat.send_message.await_args_list
    assert len(calls) == 3
    secret_calls = [
        call for call in calls if call.kwargs.get("protect_content") is True
    ]
    assert len(secret_calls) == 1
    remove_calls = [
        call for call in calls
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    ]
    assert len(remove_calls) == 1
    rendered = " ".join(str(call) for call in calls)
    assert admin_token not in rendered and user_token not in rendered
    dump = "\n".join(conn.iterdump())
    assert admin_token not in dump and user_token not in dump
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0] == 2


def test_failed_second_token_leaves_no_partial_tenant(conn) -> None:
    verifier = MutableVerifier()
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={
            "flow": {
                "kind": "provision_admin_token",
                "name": "Rollback",
                "slug": "rollback-handler",
                "owner_telegram_id": 77,
            }
        },
    )
    admin_token = make_fake_token("flow-admin")
    asyncio.run(on_text(_update(admin_token), context))
    assert admin_token not in repr(context.user_data)

    verifier.fail = True
    user_token = make_fake_token("flow-user-fail")
    failed = _update(user_token)
    asyncio.run(on_text(failed, context))
    assert user_token not in repr(context.user_data)
    failed.effective_chat.send_message.assert_awaited_once()
    failed.effective_message.reply_text.assert_not_awaited()
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0] == 0


def test_license_flow_uses_tenant_and_plan_buttons_with_duplicate_owner_ids(conn) -> None:
    verifier = MutableVerifier()
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    first = service.create_tenant(
        9001,
        name="Test",
        slug="test-one",
        owner_telegram_id=6119169885,
    )
    second = service.create_tenant(
        9001,
        name="Test Two",
        slug="test-two",
        owner_telegram_id=6119169885,
    )
    plan = service.create_plan(
        9001,
        name="Panel Test",
        duration_days=30,
        price=0,
        max_servers=3,
        max_users=500,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={},
    )

    start_update = _callback_update("license:new")
    asyncio.run(on_callback(start_update, context))
    assert context.user_data["flow"]["kind"] == "license_new_tenant"
    assert context.user_data["flow"]["cancel_target"] == "licenses"
    start_update.effective_chat.send_message.assert_awaited_once()
    cancel_markup = start_update.effective_chat.send_message.await_args.kwargs["reply_markup"]
    assert isinstance(cancel_markup, ReplyKeyboardMarkup)
    assert cancel_markup.keyboard[0][0].text == "❌ لغو"
    picker_markup = start_update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    picker_callbacks = [
        button.callback_data
        for row in picker_markup.inline_keyboard
        for button in row
    ]
    assert f"license:new:tenant:{int(first['id'])}" in picker_callbacks
    assert f"license:new:tenant:{int(second['id'])}" in picker_callbacks

    tenant_update = _callback_update(f"license:new:tenant:{int(first['id'])}")
    asyncio.run(on_callback(tenant_update, context))
    assert context.user_data["flow"]["kind"] == "license_new_plan"
    assert int(context.user_data["flow"]["tenant_id"]) == int(first["id"])
    plan_markup = tenant_update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    plan_callbacks = [
        button.callback_data
        for row in plan_markup.inline_keyboard
        for button in row
    ]
    assert f"license:new:plan:{int(plan['id'])}" in plan_callbacks

    plan_update = _callback_update(f"license:new:plan:{int(plan['id'])}")
    asyncio.run(on_callback(plan_update, context))
    assert context.user_data["flow"]["kind"] == "license_new_grace"
    assert int(context.user_data["flow"]["plan_id"]) == int(plan["id"])

    final_update = _update("0")
    asyncio.run(on_text(final_update, context))
    assert "flow" not in context.user_data
    license_row = conn.execute(
        "SELECT tenant_id, plan_id, status FROM licenses"
    ).fetchone()
    assert int(license_row["tenant_id"]) == int(first["id"])
    assert int(license_row["plan_id"]) == int(plan["id"])
    assert license_row["status"] == "active"
    remove_calls = [
        call for call in final_update.effective_chat.send_message.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    ]
    assert remove_calls


def test_bottom_cancel_button_clears_staged_flow(conn) -> None:
    verifier = MutableVerifier()
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={"flow": {"kind": "plan_new_name", "cancel_target": "plans"}},
    )
    update = _update("❌ لغو")
    asyncio.run(on_text(update, context))
    assert "flow" not in context.user_data
    remove_calls = [
        call for call in update.effective_chat.send_message.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    ]
    assert remove_calls


def test_webhook_rotation_requires_confirmation_and_returns_secret_once(conn) -> None:
    verifier = MutableVerifier()
    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    admin = make_fake_token("rotate-handler-admin")
    user = make_fake_token("rotate-handler-user")
    verifier.identity = BotIdentity(720001, "rotate_admin")
    prepared_admin = asyncio.run(
        service.prepare_tenant_bot(9001, role="admin", plain_token=admin)
    )
    verifier.identity = BotIdentity(720002, "rotate_user")
    prepared_user = asyncio.run(
        service.prepare_tenant_bot(9001, role="user", plain_token=user)
    )
    result = service.provision_tenant_prepared(
        9001,
        name="Rotate UI",
        slug="rotate-ui",
        owner_telegram_id=42,
        admin_bot=prepared_admin,
        user_bot=prepared_user,
    )
    query = SimpleNamespace(
        data=f"tenant:webhook:{result.tenant_id}:admin",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    chat = SimpleNamespace(send_message=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9001),
        effective_message=None,
        effective_chat=chat,
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={},
    )
    asyncio.run(on_callback(update, context))
    assert context.user_data["confirm"]["kind"] == "webhook_rotate"
    assert chat.send_message.await_count == 0

    query.data = "confirm:webhook_rotate"
    asyncio.run(on_callback(update, context))
    assert "confirm" not in context.user_data
    chat.send_message.assert_awaited_once()
    assert chat.send_message.await_args.kwargs["protect_content"] is True
