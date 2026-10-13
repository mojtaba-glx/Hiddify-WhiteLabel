"""SellBot parity for the tx_plans (transaction & plans) admin section.

Covers the exact SellBot menu (buttons, texts, callbacks) and the UserBot
purchase behavior driven by these settings: random transaction marker toggle,
minimum transaction limit, category simple/categorized mode, and the
price/gb/days + asc/desc sort model.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from Shared.crypto import FernetTokenCipher, generate_key
from TenantRuntime.AdminBot import userbot_management as admin_userbot
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.UserBot import handlers as user_handlers


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


def test_tx_plans_menu_matches_sellbot_exactly(service) -> None:
    text, keyboard = _render_section(service, "tx_plans")
    assert text == "🧮تنظیمات تراکنشات و پلن ها"
    rows = keyboard.inline_keyboard
    assert _texts(rows) == [
        ["🔢مشخصه تصادفی تراکنش | ❌"],
        ["🧲محدودیت حداقل تراکنش"],
        ["🗂دسته بندی پلن‌ها"],
        ["🔢ترتیب پلن‌ها"],
        ["🔙بازگشت"],
    ]
    assert _callbacks(rows) == [
        ["userbot:settings:tx_plans:random_tx_spec"],
        ["userbot:settings:tx_plans:min_tx"],
        ["userbot:settings:tx_plans:plan_categories_mode:menu"],
        ["userbot:settings:tx_plans:plan_sort_mode:menu"],
        ["userbot:settings_menu"],
    ]


def test_tx_plans_random_toggle_icon_reflects_state(service) -> None:
    service.set_userbot_setting_admin(7001, key="random_tx_spec", value=True)
    _, keyboard = _render_section(service, "tx_plans")
    assert keyboard.inline_keyboard[0][0].text == "🔢مشخصه تصادفی تراکنش | ✅"


def test_category_mode_menu_matches_sellbot() -> None:
    for enabled, simple, categorized in (
        (True, "❌", "✅"),
        (False, "✅", "❌"),
    ):
        rows = admin_userbot._tx_plan_categories_mode_rows(
            {"plan_categories_enabled": enabled}
        )
        assert _texts(rows) == [
            [f"{simple} | ساده", f"{categorized} | دسته‌بندی"],
            ["🔙بازگشت"],
        ]
        assert rows[0][0].callback_data == (
            "userbot:settings:tx_plans:plan_categories_mode:set:simple"
        )
        assert rows[0][1].callback_data == (
            "userbot:settings:tx_plans:plan_categories_mode:set:categorized"
        )
        assert rows[1][0].callback_data == "userbot:settings:tx_plans"


def test_sort_mode_menu_matches_sellbot() -> None:
    rows = admin_userbot._tx_plan_sort_mode_rows(
        {"plan_sort_mode": "gb", "plan_sort_desc": True}
    )
    assert _texts(rows) == [
        ["❌ | مرتب سازی بر اساس قیمت"],
        ["✅ | مرتب سازی بر اساس حجم"],
        ["❌ | مرتب سازی بر اساس زمان"],
        ["✅ | نزولی", "❌ | صعودی"],
        ["🔙بازگشت"],
    ]
    callbacks = _callbacks(rows)
    assert callbacks[0] == ["userbot:settings:tx_plans:plan_sort_mode:set:price"]
    assert callbacks[3] == [
        "userbot:settings:tx_plans:plan_sort_dir:set:desc",
        "userbot:settings:tx_plans:plan_sort_dir:set:asc",
    ]


def test_min_transaction_default_and_validation(service) -> None:
    settings = service.userbot_settings_admin(7001)
    assert settings["min_transaction_toman"] == 10000

    service.set_userbot_setting_admin(7001, key="min_transaction_toman", value=25000)
    assert service.userbot_settings_admin(7001)["min_transaction_toman"] == 25000
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="min_transaction_toman", value=0)


def test_sort_defaults_and_validation(service) -> None:
    settings = service.userbot_settings_admin(7001)
    assert settings["plan_sort_mode"] == "price"
    assert settings["plan_sort_desc"] is False

    service.set_userbot_setting_admin(7001, key="plan_sort_mode", value="days")
    assert service.userbot_settings_admin(7001)["plan_sort_mode"] == "days"
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="plan_sort_mode", value="price_asc")
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="plan_sort_mode", value="id")


def test_legacy_sort_values_migrate_to_sellbot_model(service, conn) -> None:
    tenant_id = int(service.tenant_id)
    conn.execute(
        "INSERT INTO tenant_userbot_settings (tenant_id, key, value, updated_at)"
        " VALUES (?, 'plan_sort_mode', '\"traffic_desc\"', '2026-01-01T00:00:00Z')"
        " ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value",
        (tenant_id,),
    )
    conn.commit()
    settings = service.runtime_userbot_settings()
    assert settings["plan_sort_mode"] == "gb"
    assert settings["plan_sort_desc"] is True


def test_descending_mirrors_plan_rows_like_sellbot() -> None:
    buttons = [object(), object(), object(), object()]
    rows = user_handlers._column_rows(buttons, 2, rtl=False)
    assert rows == [[buttons[0], buttons[1]], [buttons[2], buttons[3]]]
    rows = user_handlers._column_rows(buttons, 2, rtl=True)
    assert rows == [[buttons[1], buttons[0]], [buttons[3], buttons[2]]]
    single = user_handlers._column_rows(buttons, 1, rtl=True)
    assert single == [[buttons[0]], [buttons[1]], [buttons[2]], [buttons[3]]]


def test_new_tx_plans_callbacks_are_wired() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:tx_plans:random_tx_spec",
        "userbot:settings:tx_plans:min_tx",
        "userbot:settings:tx_plans:plan_categories_mode:menu",
        "userbot:settings:tx_plans:plan_categories_mode:set:",
        "userbot:settings:tx_plans:plan_sort_mode:menu",
        "userbot:settings:tx_plans:plan_sort_mode:set:",
        "userbot:settings:tx_plans:plan_sort_dir:set:",
        '"tx_min_transaction"',
    ):
        assert callback in source
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert "حداقل مبلغ مجاز تراکنش" in runtime
    assert "min_transaction_toman" in runtime
