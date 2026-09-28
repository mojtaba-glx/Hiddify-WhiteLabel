"""Phase-2 presentation and application wiring stay safe and deterministic."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop, TypeHandler

from MasterBot.handlers import access_gate, on_callback, on_text
from MasterBot.customer_views import CUSTOMER_BUTTONS, customer_main_keyboard
from MasterBot.main import build_application
from MasterBot.service import BotIdentity, MasterService, Page
from MasterBot.views import (
    MAIN_MENU,
    audit_text,
    license_detail,
    main_menu_keyboard,
    tenant_detail,
    tenants_view,
)
from Shared.crypto import FernetTokenCipher, generate_key
from Shared.settings import Settings


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_main_menu_has_all_phase2_sections_and_short_callbacks() -> None:
    labels = [label for label, _ in MAIN_MENU]
    assert labels == [
        "👤 کاربران فروشگاه", "🤖 ربات‌های مشتریان", "🔐 لایسنس‌ها",
        "📦 پلن‌ها", "📊 آمار", "💳 پرداخت‌ها", "⚠️ هشدارها",
        "🧾 تاریخچه", "⚙️ تنظیمات",
    ]
    assert all(len(value.encode("utf-8")) <= 64 for value in _callbacks(main_menu_keyboard()))


def test_tenant_list_pagination_callbacks_are_bounded() -> None:
    page = Page(
        items=[{"id": 5, "name": "Customer", "slug": "customer", "status": "active"}],
        page=3, page_size=8, has_previous=True, has_next=True,
    )
    _, keyboard = tenants_view(page)
    assert "tenant:page:2" in _callbacks(keyboard)
    assert "tenant:page:4" in _callbacks(keyboard)
    assert "tenant:provision" in _callbacks(keyboard)
    assert all(len(value.encode("utf-8")) <= 64 for value in _callbacks(keyboard))


def test_details_never_render_encrypted_credentials() -> None:
    token = "123456789:" + "A" * 40
    tenant = {
        "id": 1, "name": "Tenant", "slug": "tenant", "owner_telegram_id": 5,
        "status": "active", "encrypted_token": token, "token_fingerprint": "f" * 64,
    }
    text, _ = tenant_detail(tenant, {"admin": True, "user": False, "ready": False})
    assert token not in text and "f" * 64 not in text


def test_license_detail_displays_local_time_without_hidden_fields() -> None:
    row = {
        "id": 1, "tenant_id": 2, "tenant_name": "Tenant", "plan_name": "Monthly",
        "status": "active", "expires_at": "2030-01-01T00:00:00+00:00",
    }
    text, keyboard = license_detail(row, timezone_name="Asia/Tehran")
    assert "2030-01-01" in text and "Monthly" in text
    assert "license:renew:1" in _callbacks(keyboard)


def test_audit_view_does_not_render_metadata_or_secrets() -> None:
    text = audit_text([{
        "id": 1, "action": "tenant.create", "entity_type": "tenant", "entity_id": "3",
        "metadata": {"private": "do-not-render"},
    }])
    assert "do-not-render" not in text


def test_application_registers_global_access_gate(conn) -> None:
    settings = Settings(
        master_bot_token="123456789:" + "A" * 35,
        master_admin_id=9001,
        token_encryption_key=generate_key(),
        database_path="unused.db",
    )
    app, returned = build_application(settings, connection=conn)
    assert returned is conn
    assert -1 in app.handlers
    assert any(isinstance(handler, TypeHandler) for handler in app.handlers[-1])
    assert isinstance(app.bot_data["master_service"], MasterService)
    assert app.bot_data["license_job_runner"] is not None
    assert app.bot_data["tenant_runtime_gate"] is not None
    assert [job.name for job in app.job_queue.jobs()] == ["license-service"]


def test_access_gate_allows_normal_customer_messages(conn) -> None:
    service = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key())
    )
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12), callback_query=None, effective_message=message
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"master_service": service}))
    asyncio.run(access_gate(update, context))
    message.reply_text.assert_not_awaited()


def test_access_gate_stops_normal_user_forging_admin_callback(conn) -> None:
    service = MasterService(conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key()))
    query = SimpleNamespace(data="tenant:new", answer=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=12), callback_query=query, effective_message=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"master_service": service}))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(access_gate(update, context))
    query.answer.assert_awaited_once_with("Access denied", show_alert=True)


def test_access_gate_allows_only_owner(conn) -> None:
    service = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key())
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9001), callback_query=None, effective_message=None
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"master_service": service}))
    asyncio.run(access_gate(update, context))


def test_sensitive_tenant_status_requires_live_confirmation(conn) -> None:
    service = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key())
    )
    tenant = service.create_tenant(9001, name="Confirm", slug="confirm-one", owner_telegram_id=1)
    query = SimpleNamespace(
        data=f"tenant:status:{int(tenant['id'])}:suspended",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9001), callback_query=query, effective_message=None
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}), user_data={}
    )
    asyncio.run(on_callback(update, context))
    assert service.get_tenant(9001, int(tenant["id"]))["status"] == "active"
    assert context.user_data["confirm"]["kind"] == "tenant_status"

    query.data = "confirm:tenant_status"
    asyncio.run(on_callback(update, context))
    assert service.get_tenant(9001, int(tenant["id"]))["status"] == "suspended"
    assert "confirm" not in context.user_data


def test_forged_confirmation_without_state_changes_nothing(conn) -> None:
    service = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key())
    )
    tenant = service.create_tenant(9001, name="Forged", slug="forged-one", owner_telegram_id=1)
    query = SimpleNamespace(
        data="confirm:tenant_status", answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9001), callback_query=query, effective_message=None
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}), user_data={}
    )
    asyncio.run(on_callback(update, context))
    assert service.get_tenant(9001, int(tenant["id"]))["status"] == "active"


def test_token_message_is_deleted_and_never_saved_in_flow(conn) -> None:
    class Verifier:
        async def verify(self, token: str) -> BotIdentity:
            return BotIdentity(telegram_bot_id=88, username="safe_bot")

    service = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=Verifier(),
    )
    tenant = service.create_tenant(9001, name="Token", slug="token-one", owner_telegram_id=1)
    token = "123456789:" + "B" * 35
    message = SimpleNamespace(text=token, delete=AsyncMock(), reply_text=AsyncMock())
    chat = SimpleNamespace(send_message=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9001),
        effective_message=message,
        effective_chat=chat,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service}),
        user_data={"flow": {"kind": "bot_token", "tenant_id": int(tenant["id"]), "role": "admin"}},
    )
    asyncio.run(on_text(update, context))
    message.delete.assert_awaited_once()
    assert "flow" not in context.user_data
    assert token not in repr(context.user_data)
    stored = service.conn.execute("SELECT * FROM tenant_bots").fetchone()
    assert token not in str(dict(stored))


def test_customer_keyboard_exposes_only_storefront_actions() -> None:
    keyboard = customer_main_keyboard()
    labels = [button for row in keyboard.keyboard for button in row]
    assert set(labels) == set(CUSTOMER_BUTTONS)
    assert "⚙️ تنظیمات" not in labels
    assert "💳 پرداخت‌ها" not in labels
    assert "🔐 لایسنس‌ها" not in labels


def test_access_gate_blocks_customer_forging_settings_callback(conn) -> None:
    service = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key())
    )
    query = SimpleNamespace(data="menu:settings", answer=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12),
        callback_query=query,
        effective_message=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"master_service": service})
    )
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(access_gate(update, context))
    query.answer.assert_awaited_once_with("Access denied", show_alert=True)
