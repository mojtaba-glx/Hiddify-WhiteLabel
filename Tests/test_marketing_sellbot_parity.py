"""SellBot parity for the marketing settings section.

Covers the exact SellBot menu (buttons, texts, order), the new settings
keys, the edit wizards, and the discount-OR-increase coupon gate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

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


def test_marketing_menu_matches_sellbot_head(service) -> None:
    text, keyboard = _render_section(service, "marketing")
    assert text == "🎯تنظیمات بازاریابی"
    rows = keyboard.inline_keyboard
    labels = [[button.text for button in row] for row in rows]
    callbacks = [[button.callback_data for button in row] for row in rows]
    # Exact SellBot block first, in SellBot order.
    assert labels[:7] == [
        ["🛍 اعمال کد تخفیف | ✅"],
        ["🔼 اعمال کد افزایشی | ❌"],
        ["🎁 دکمه هدیه | ✅"],
        ["👤 نمایش وضعیت کاربر | ✅"],
        ["🚀 استفاده آنی هدیه کوپن | ❌"],
        ["⚙️تنظیم متن هدایای اتوماتیک"],
        ["⚙️حداقل شارژ هدیه اتوماتیک"],
    ]
    assert callbacks[:7] == [
        ["userbot:settings:marketing:toggle:enable_discount_code"],
        ["userbot:settings:marketing:toggle:enable_increase_code"],
        ["userbot:settings:marketing:toggle:show_gift_button"],
        ["userbot:settings:marketing:toggle:show_user_status"],
        ["userbot:settings:marketing:toggle:instant_gift_coupon"],
        ["userbot:settings:marketing:edit:auto_gift_text"],
        ["userbot:settings:marketing:edit:min_auto_gift_charge"],
    ]
    # WhiteLabel-only navigation stays after the SellBot block.
    assert labels[7:] == [
        ["🤝 رفرال | ❌"],
        ["🔥 تست رایگان | ❌"],
        ["🎟 مدیریت کوپن تخفیف"],
        ["🏷 مدیریت کدهای هدیه"],
        ["🔙بازگشت"],
    ]


def test_marketing_defaults_match_sellbot_values(service) -> None:
    settings = service.userbot_settings_admin(7001)
    assert settings["enable_increase_code"] is False
    assert settings["instant_gift_coupon"] is False
    assert (
        settings["auto_gift_text"] == "🎁 هدیه شما فعال شد. از همراهی‌تان متشکریم."
    )
    assert settings["min_auto_gift_charge"] == 100000


def test_marketing_toggles_flip(service) -> None:
    for key in (
        "enable_discount_code",
        "enable_increase_code",
        "show_gift_button",
        "show_user_status",
        "instant_gift_coupon",
    ):
        before = bool(service.userbot_settings_admin(7001)[key])
        after = service.toggle_userbot_setting_admin(7001, key=key)
        assert bool(after[key]) is (not before)


def test_min_auto_gift_charge_allows_zero_but_rejects_negative(service) -> None:
    service.set_userbot_setting_admin(7001, key="min_auto_gift_charge", value=0)
    assert service.userbot_settings_admin(7001)["min_auto_gift_charge"] == 0
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="min_auto_gift_charge", value=-1)
    with pytest.raises(PermissionError):
        service.set_userbot_setting_admin(101, key="min_auto_gift_charge", value=5)


def test_coupon_usable_is_discount_or_increase() -> None:
    assert user_handlers._coupon_usable({"enable_discount_code": True}) is True
    assert user_handlers._coupon_usable({"enable_increase_code": True}) is True
    assert user_handlers._coupon_usable(
        {"enable_discount_code": False, "enable_increase_code": False}
    ) is False
    assert user_handlers._coupon_usable({}) is False


def test_marketing_edit_callbacks_are_wired() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:marketing:toggle:enable_increase_code",
        "userbot:settings:marketing:toggle:instant_gift_coupon",
        "userbot:settings:marketing:edit:auto_gift_text",
        "userbot:settings:marketing:edit:min_auto_gift_charge",
        '"marketing_edit"',
        "متن هدایای اتوماتیک ذخیره شد",
        "حداقل شارژ هدیه اتوماتیک روی",
    ):
        assert callback in source
