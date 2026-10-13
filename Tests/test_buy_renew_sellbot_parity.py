"""SellBot parity for the buy_renew (purchase & renewal) admin section.

Covers the exact SellBot menu (buttons, texts, order, horizontal column
menus) and the matching wizard/confirm texts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from TenantRuntime.AdminBot import userbot_management as admin_userbot
from TenantRuntime.business import TenantBusinessService


@pytest.fixture()
def service(conn, factories, cipher) -> TenantBusinessService:
    tenant = factories.tenant(owner_telegram_id=7001)
    return TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
    )


def _texts(rows):
    return [[button.text for button in row] for row in rows]


def _callbacks(rows):
    return [[button.callback_data for button in row] for row in rows]


def _render_section(service, section: str):
    sent: list[tuple] = []

    class FakeMessage:
        async def edit_text(self, text, reply_markup=None, **kwargs):
            sent.append((text, reply_markup))

    update = SimpleNamespace(
        callback_query=SimpleNamespace(message=FakeMessage()),
        effective_chat=None,
    )
    asyncio.run(
        admin_userbot._settings_section(update, service, 7001, section)
    )
    assert sent, "settings section rendered nothing"
    return sent[-1]


def test_buy_renew_menu_matches_sellbot_exactly(service) -> None:
    text, keyboard = _render_section(service, "buy_renew")
    assert text == "🛒تنظیمات خرید و تمدید"
    rows = keyboard.inline_keyboard
    assert _texts(rows) == [
        ["امکان خرید اشتراک | ✅"],
        ["امکان تمدید اشتراک | ✅"],
        ["دکمه تمدید اشتراک در منوی اصلی | ✅"],
        ["تنظیم شیوه تمدید"],
        ["ستون‌های پلن‌ها", "ستون‌های سرورها"],
        ["حجم نامحدود∞ | ❌"],
        ["زمان نامحدود∞ | ❌"],
        ["❌", "تنظیم کانال رویداد📢"],
        ["🔙بازگشت"],
    ]
    callbacks = _callbacks(rows)
    assert callbacks[4] == [
        "userbot:settings:buy_renew:plan_columns:menu",
        "userbot:settings:buy_renew:server_columns:menu",
    ]
    assert callbacks[7] == [
        "userbot:settings:buy_renew:event_channel_enabled",
        "userbot:settings:buy_renew:event_channel_set",
    ]
    assert callbacks[8] == ["userbot:settings_menu"]


def test_plan_columns_menu_is_horizontal_like_sellbot() -> None:
    rows = admin_userbot._buy_plan_columns_rows({"plan_columns": 2})
    assert _texts(rows) == [
        ["❌ | یک", "✅ | دو"],
        ["🔙بازگشت"],
    ]
    assert _callbacks(rows)[0] == [
        "userbot:settings:buy_renew:plan_columns:set:1",
        "userbot:settings:buy_renew:plan_columns:set:2",
    ]


def test_server_columns_menu_is_horizontal_like_sellbot() -> None:
    rows = admin_userbot._buy_server_columns_rows({"server_columns": 1})
    assert _texts(rows) == [
        ["✅ | یک", "❌ | دو", "❌ | سه"],
        ["🔙بازگشت"],
    ]
    assert _callbacks(rows)[0] == [
        "userbot:settings:buy_renew:server_columns:set:1",
        "userbot:settings:buy_renew:server_columns:set:2",
        "userbot:settings:buy_renew:server_columns:set:3",
    ]


def test_renew_policy_texts_match_sellbot(service) -> None:
    text, keyboard = _render_section(service, "renew_policy")
    assert "📆 مقدار فعلی مصرف:" in text
    assert "مقدار فعلی حجم" not in text
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "حداکثر زمان مجاز برای تمدید📊" in labels
    assert "حداکثر مصرف مجاز برای تمدید📆" in labels
    assert not any("روز" in label and "حداکثر" in label for label in labels)


def test_column_set_callbacks_are_wired() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:buy_renew:plan_columns:set:",
        "userbot:settings:buy_renew:server_columns:set:",
    ):
        assert callback in source


def test_plan_columns_capped_at_two_like_sellbot(service) -> None:
    service.set_userbot_setting_admin(7001, key="plan_columns", value=3)
    rows = admin_userbot._buy_plan_columns_rows(
        service.userbot_settings_admin(7001)
    )
    # Out-of-range legacy value: no option is marked, like SellBot.
    assert _texts(rows)[0] == ["❌ | یک", "❌ | دو"]
