"""SellBot-parity tests for tenant UserBot management."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from TenantRuntime.AdminBot import userbot_management as admin_userbot
from TenantRuntime.UserBot import handlers as user_handlers
from TenantRuntime.business import TenantBusinessError, TenantBusinessService


def _service(conn, factories, cipher):
    tenant = factories.tenant(owner_telegram_id=7001)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
    )
    return tenant, service


def _labels(markup):
    return [[button.text for button in row] for row in markup.inline_keyboard]


def test_userbot_admin_main_menu_matches_sellbot() -> None:
    labels = _labels(admin_userbot.build_userbot_main_menu())
    assert labels == [
        ["👤مدیریت کاربران ربات"],
        ["💵مدیریت تراکنشات", "📗مدیریت سفارشات"],
        ["🎁مدیریت هدایا"],
        ["🤝مدیریت رفرال"],
        ["📑مدیریت تیکت‌ها", "📧ارسال پیام همگانی"],
        ["📢 مدیریت کانال"],
        ["⚙️تنظیمات"],
    ]


def test_userbot_admin_submenus_match_sellbot_labels() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for label in (
        "👥لیست کاربران ربات",
        "🔍جستجوی کاربران",
        "✅لیست تراکنشات تایید شده",
        "🚫لیست تراکنشات رد شده",
        "⏳لیست تراکنشات در انتظار",
        "💳لیست تراکنشات کارت به کارت",
        "📗لیست سفارشات",
        "🔍جستجوی سفارشات",
        "📊 داشبورد هدایا",
        "🏷 کوپن‌ها و کدهای هدیه",
        "👥 لیست دعوت‌ها",
        "💰 لیست پاداش‌ها",
        "📨تیکت‌های در انتظار",
        "📬تیکت‌های باز",
        "📩تیکت‌های بسته",
        "تمام کاربران منقضی شده",
        "کاربران بدون سفارش",
        "🛍تنظیمات اشتراک",
        "📁وضعیت نمایش لینک اشتراک",
        "🛒تنظیمات خرید و تمدید",
        "🧾تنظیمات متون",
        "🔒تنظیمات عضویت اجباری",
        "💳تنظیمات پرداخت",
        "🗂️تنظیمات بکاپ و بازیابی",
    ):
        assert label in source


def test_tenant_userbot_settings_roundtrip(conn, factories, cipher) -> None:
    _tenant, service = _service(conn, factories, cipher)
    defaults = service.userbot_settings_admin(7001)
    assert defaults["enable_buy"] is True
    assert defaults["button_theme"] == "smart"
    assert defaults["reminder_days"] == 3

    changed = service.set_userbot_setting_admin(
        7001, key="enable_buy", value=False
    )
    assert changed["enable_buy"] is False
    changed = service.set_userbot_setting_admin(
        7001, key="button_theme", value="shop"
    )
    assert changed["button_theme"] == "shop"
    changed = service.set_userbot_setting_admin(
        7001, key="reminder_days", value=5
    )
    assert changed["reminder_days"] == 5

    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(
            7001, key="button_theme", value="unknown"
        )
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(
            7001, key="reminder_days", value=31
        )


def test_user_menu_reacts_to_admin_settings(conn, factories, cipher) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")
    service.update_growth_settings(
        7001, referral_enabled=False, trial_enabled=False
    )
    service.set_userbot_setting_admin(7001, key="enable_buy", value=False)
    service.set_userbot_setting_admin(
        7001, key="show_gift_button", value=False
    )
    labels = sum(_labels(user_handlers._menu(spec, service)), [])
    assert "💳 خرید اشتراک" not in labels
    assert "🎁 تست رایگان" not in labels
    assert "🤝 دعوت دوستان" not in labels
    assert "🎁 دریافت هدیه" not in labels

    service.set_userbot_setting_admin(7001, key="enable_buy", value=True)
    service.set_userbot_setting_admin(
        7001, key="show_gift_button", value=True
    )
    service.update_growth_settings(
        7001, referral_enabled=True, trial_enabled=True
    )
    labels = sum(_labels(user_handlers._menu(spec, service)), [])
    assert "💳 خرید اشتراک" in labels
    assert "🎁 تست رایگان" in labels
    assert "🤝 دعوت دوستان" in labels
    assert "🎁 دریافت هدیه" in labels


def test_real_wallet_gift_is_single_use_per_customer(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    service.register_customer(
        7101, display_name="Gift User", username="gift_user"
    )
    voucher = service.add_gift_voucher_admin(
        7001,
        code="WELCOME50",
        amount=50000,
        currency="IRR",
        max_uses=10,
    )
    redeemed = service.redeem_gift_voucher(7101, code="welcome50")
    assert redeemed["amount"] == 50000
    assert redeemed["resulting_balance"] == 50000

    wallet = service.wallet_summary(7101)
    assert wallet["accounts"][0]["balance"] == 50000
    assert any(row.get("kind") == "gift" for row in wallet["history"])

    with pytest.raises(TenantBusinessError):
        service.redeem_gift_voucher(7101, code="WELCOME50")

    stored = service.gift_voucher_admin(
        7001, voucher_id=int(voucher["id"])
    )
    assert int(stored["used_count"]) == 1


def test_gift_voucher_is_tenant_scoped(conn, factories, cipher) -> None:
    _tenant, service = _service(conn, factories, cipher)
    other_tenant = factories.tenant(owner_telegram_id=8001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(other_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
    )
    service.add_gift_voucher_admin(
        7001, code="PRIVATE", amount=1000, max_uses=1
    )
    other.register_customer(
        8101, display_name="Other", username="other"
    )
    with pytest.raises(TenantBusinessError):
        other.redeem_gift_voucher(8101, code="PRIVATE")


def test_broadcast_segments_are_tenant_scoped(conn, factories, cipher) -> None:
    tenant, service = _service(conn, factories, cipher)
    service.register_customer(7101, display_name="A", username=None)
    service.register_customer(7102, display_name="B", username=None)
    other_tenant = factories.tenant(owner_telegram_id=8001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(other_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
    )
    other.register_customer(8101, display_name="Foreign", username=None)

    targets = service.broadcast_targets_admin(7001, segment="all")
    assert {int(row["telegram_user_id"]) for row in targets} == {
        7101, 7102
    }
    assert 8101 not in {
        int(row["telegram_user_id"]) for row in targets
    }


def test_admin_forms_use_bottom_cancel_and_not_pipe_for_core_new_flows() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    assert '[[KeyboardButton("❌لغو")]]' in source
    assert '"kind":"payment_add_title"' in source
    assert '"kind":"gift_add_code"' in source
    assert '"kind":"referral_manual_customer"' in source
    assert '"kind":"wallet_set_currency"' in source
    assert "payment_add_instructions" in source
    assert "gift_add_expiry" in source


def test_broadcast_and_channel_media_flows_are_real() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    assert "broadcast_skip_cancel_keyboard" in source
    assert "_send_broadcast_to_targets" in source
    assert "await bot.send_photo" in source
    assert "channelpost:preview" in source
    assert "channelpost:publish" in source
    assert "_download_admin_file" in source
    assert "MAX_CHANNEL_BUTTONS = 8" in source
