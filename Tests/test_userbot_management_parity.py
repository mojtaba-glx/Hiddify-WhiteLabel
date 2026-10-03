"""SellBot-parity tests for tenant UserBot management."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from TenantRuntime.AdminBot import handlers as admin_handlers
from TenantRuntime.AdminBot import userbot_management as admin_userbot
from TenantRuntime.UserBot import handlers as user_handlers
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.button_styles import set_button_settings
from Shared.timeutils import parse_utc, utcnow


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


def _reply_labels(markup):
    return [[button.text for button in row] for row in markup.keyboard]


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
    assert defaults["show_renew_in_main_menu"] is True
    assert defaults["renew_policy"] == "advanced"
    assert defaults["renew_volume_mode"] == "reset"
    assert defaults["renew_time_mode"] == "reset"
    assert defaults["renew_max_days"] == 3
    assert defaults["renew_max_remaining_gb"] == 3
    assert defaults["renew_unlimited_volume"] is False
    assert defaults["renew_unlimited_time"] is False
    assert defaults["shuffle_server_layout"] is True
    assert defaults["shuffle_config_layout"] is True
    assert defaults["show_user_page_link"] is True
    assert defaults["show_username"] is True
    assert defaults["show_direct_config"] is True
    assert defaults["show_auto_sub_link"] is False
    assert defaults["show_sub_link"] is True
    assert defaults["show_sub_link_b64"] is False
    assert defaults["show_multi_server"] is False
    assert defaults["show_multi_server_b64"] is False
    assert defaults["enable_discount_code"] is True
    assert defaults["show_user_status"] is True
    assert defaults["plan_categories_enabled"] is True
    assert defaults["plan_sort_by_priority"] is True
    assert "راهنمای اندروید" in defaults["guide_android_text"]
    assert defaults["smart_base_url"] == ""

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


def test_legacy_smart_link_toggle_migrates_until_new_toggle_is_saved(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    legacy = service.set_userbot_setting_admin(
        7001, key="show_smart_link", value=True
    )
    assert legacy["show_multi_server"] is True

    explicit = service.set_userbot_setting_admin(
        7001, key="show_multi_server", value=False
    )
    assert explicit["show_multi_server"] is False
    assert service.runtime_userbot_settings()["show_multi_server"] is False


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
    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    assert "💳خرید اشتراک" not in labels
    assert "🔥تست رایگان" not in labels
    assert "💌دعوت دوستان" not in labels
    assert "🎁دریافت هدیه" not in labels

    service.set_userbot_setting_admin(7001, key="enable_buy", value=True)
    service.set_userbot_setting_admin(
        7001, key="show_gift_button", value=True
    )
    service.update_growth_settings(
        7001, referral_enabled=True, trial_enabled=True
    )
    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    assert "💳خرید اشتراک" in labels
    assert "🔥تست رایگان" in labels
    assert "💌دعوت دوستان" in labels
    assert "🎁دریافت هدیه" in labels


def test_userbot_customer_main_menu_matches_sellbot_navigation(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")
    service.update_growth_settings(
        7001, referral_enabled=True, trial_enabled=True
    )

    labels = _reply_labels(user_handlers._main_keyboard(spec, service))
    assert labels == [
        ["📊وضعیت اشتراک"],
        ["♾تمدید اشتراک", "💳خرید اشتراک"],
        ["🔗اتصال اشتراک"],
        ["🔥تست رایگان", "💰کیف پول"],
        ["📩پشتیبانی", "📚راهنما", "❗️سوالات متداول"],
        ["💌دعوت دوستان"],
        ["🎁دریافت هدیه"],
    ]


def test_userbot_main_navigation_callbacks_are_real() -> None:
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for callback in (
        'data == "runtime:status"',
        'data == "shop:renewmenu"',
        'data == "shop:buy"',
        'data == "shop:connect"',
        'data == "shop:trial"',
        'data == "shop:wallet"',
        'data == "shop:tickets"',
        'data == "shop:guide"',
        'data == "shop:faq"',
        'data == "shop:referral"',
        'data == "shop:gift"',
    ):
        assert callback in runtime


def test_status_screen_keeps_account_order_and_subscription_navigation() -> None:
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for callback in (
        'callback_data="shop:account"',
        'callback_data="shop:orders"',
        'callback_data="shop:subs"',
        'callback_data="shop:connect"',
    ):
        assert callback in runtime


def test_faq_navigation_is_always_available(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")
    service.set_userbot_setting_admin(7001, key="faq_text", value="")
    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    assert "❗️سوالات متداول" in labels


def test_renew_main_menu_setting_is_functional(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")

    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    assert "♾تمدید اشتراک" in labels

    service.set_userbot_setting_admin(
        7001, key="show_renew_in_main_menu", value=False
    )
    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    assert "♾تمدید اشتراک" not in labels

    service.set_userbot_setting_admin(7001, key="enable_renew", value=False)
    service.set_userbot_setting_admin(
        7001, key="show_renew_in_main_menu", value=True
    )
    labels = sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])
    # SellBot parity: visibility is independent from enable_renew; the
    # renewal handler itself rejects access while renewal is disabled.
    assert "♾تمدید اشتراک" in labels
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert 'if not bool(settings.get("enable_renew", True)):' in runtime


def test_user_status_visibility_is_functional(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")
    assert "📊وضعیت اشتراک" in sum(_reply_labels(user_handlers._main_keyboard(spec, service)), [])

    service.set_userbot_setting_admin(
        7001, key="show_user_status", value=False
    )
    assert "📊وضعیت اشتراک" not in sum(
        _reply_labels(user_handlers._main_keyboard(spec, service)), []
    )


def _serialized_style(button) -> str | None:
    payload = button.to_dict()
    return payload.get("style")


def test_colored_button_themes_apply_to_all_userbot_buttons() -> None:
    smart = user_handlers.InlineKeyboardButton(
        "💳 خرید اشتراک",
        callback_data="shop:buy",
        settings={"colored_buttons": True, "button_theme": "smart"},
    )
    assert getattr(smart, "style", None) == "success"
    assert _serialized_style(smart) == "success"

    shop = user_handlers.InlineKeyboardButton(
        "💰 کیف پول",
        callback_data="shop:wallet",
        settings={"colored_buttons": True, "button_theme": "shop"},
    )
    assert getattr(shop, "style", None) == "success"
    assert _serialized_style(shop) == "success"

    pro = user_handlers.InlineKeyboardButton(
        "📊 وضعیت اشتراک",
        callback_data="runtime:status",
        settings={"colored_buttons": True, "button_theme": "pro"},
    )
    assert getattr(pro, "style", None) == "primary"
    assert _serialized_style(pro) == "primary"

    minimal = user_handlers.InlineKeyboardButton(
        "💳 خرید اشتراک",
        callback_data="shop:buy",
        settings={"colored_buttons": True, "button_theme": "minimal"},
    )
    assert getattr(minimal, "style", None) is None
    assert _serialized_style(minimal) is None

    danger = user_handlers.InlineKeyboardButton(
        "❌ لغو",
        callback_data="shop:cancel",
        settings={"colored_buttons": True, "button_theme": "minimal"},
    )
    assert getattr(danger, "style", None) == "danger"
    assert _serialized_style(danger) == "danger"

    plain = user_handlers.InlineKeyboardButton(
        "💳 خرید اشتراک",
        callback_data="shop:buy",
        settings={"colored_buttons": False, "button_theme": "shop"},
    )
    assert getattr(plain, "style", None) is None
    assert _serialized_style(plain) is None


def test_userbot_button_context_styles_direct_buttons() -> None:
    user_handlers._set_button_settings(
        {"colored_buttons": True, "button_theme": "shop"}
    )
    button = user_handlers.InlineKeyboardButton(
        "🎁 دریافت هدیه",
        callback_data="shop:gift",
    )
    assert getattr(button, "style", None) == "success"
    assert _serialized_style(button) == "success"

    user_handlers._set_button_settings(
        {"colored_buttons": False, "button_theme": "shop"}
    )
    plain = user_handlers.InlineKeyboardButton(
        "🎁 دریافت هدیه",
        callback_data="shop:gift",
    )
    assert getattr(plain, "style", None) is None
    assert _serialized_style(plain) is None


def test_colored_style_is_inside_final_inline_keyboard_payload() -> None:
    button = user_handlers.InlineKeyboardButton(
        "✅ تایید",
        callback_data="confirm",
        settings={"colored_buttons": True, "button_theme": "smart"},
    )
    markup = user_handlers.InlineKeyboardMarkup([[button]])
    payload = markup.to_dict()
    assert payload["inline_keyboard"][0][0]["style"] == "success"


def test_adminbot_inline_and_reply_buttons_serialize_native_styles() -> None:
    set_button_settings(
        {"colored_buttons": True, "button_theme": "smart"}
    )
    inline = admin_userbot.build_userbot_main_menu().to_dict()
    assert inline["inline_keyboard"][0][0]["style"] == "primary"

    reply = admin_handlers.admin_main_keyboard().to_dict()
    first = reply["keyboard"][0][0]
    assert first["style"] == "primary"

    set_button_settings(
        {"colored_buttons": True, "button_theme": "shop"}
    )
    cancel = admin_handlers.cancel_keyboard().to_dict()
    assert cancel["keyboard"][0][0]["style"] == "danger"


def test_userbot_main_menu_is_persistent_bottom_reply_keyboard(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    spec = SimpleNamespace(tenant_name="Speed Test")
    service.update_growth_settings(
        7001, referral_enabled=True, trial_enabled=True
    )

    markup = user_handlers._main_keyboard(spec, service)
    payload = markup.to_dict()

    assert payload["is_persistent"] is True
    assert payload["resize_keyboard"] is True
    assert "inline_keyboard" not in payload
    assert payload["keyboard"][0][0]["text"] == "📊وضعیت اشتراک"
    assert payload["keyboard"][0][0]["style"] == "primary"
    assert payload["keyboard"][1][0]["style"] == "success"


def test_userbot_main_reply_buttons_are_routed_to_real_actions() -> None:
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert "async def _handle_main_reply_action" in runtime
    assert 'if text == BTN_BUY:' in runtime
    assert 'if text == BTN_RENEW:' in runtime
    assert 'if text == BTN_CONNECT:' in runtime
    assert 'if text == BTN_TRIAL:' in runtime
    assert 'if text == BTN_WALLET:' in runtime
    assert 'if text == BTN_SUPPORT:' in runtime
    assert 'if text == BTN_GUIDE:' in runtime
    assert 'if text == BTN_FAQ:' in runtime
    assert 'if text == BTN_REFERRAL:' in runtime
    assert 'if text == BTN_GIFT:' in runtime
    assert 'if text in main_labels:' in runtime


def test_layout_helpers_make_columns_and_safe_config_items(monkeypatch) -> None:
    buttons = [
        user_handlers.TelegramInlineKeyboardButton(str(i), callback_data=f"x:{i}")
        for i in range(5)
    ]
    rows = user_handlers._column_rows(buttons, 3)
    assert [len(row) for row in rows] == [3, 2]

    calls = []

    def reverse(items):
        calls.append(len(items))
        items.reverse()

    monkeypatch.setattr(user_handlers.random, "shuffle", reverse)
    ordered = user_handlers._ordered_indexed(
        ["a", "b", "c"], shuffle_enabled=True
    )
    assert [index for index, _item in ordered] == [2, 1, 0]
    assert calls == [3]

    configs = user_handlers._extract_config_items(
        "vless://one\nvmess://two\ntrojan://three"
    )
    assert configs == ["vless://one", "vmess://two", "trojan://three"]

    opaque = user_handlers._extract_config_items("YWJjZGVmZw==\nmetadata")
    assert opaque == ["YWJjZGVmZw==\nmetadata"]


def test_plan_server_and_config_layout_settings_are_runtime_wired() -> None:
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert 'settings.get("plan_columns")' in runtime
    assert 'settings.get("server_columns")' in runtime
    assert 'settings.get("shuffle_server_layout", True)' in runtime
    assert 'settings.get("shuffle_configs", True)' in runtime
    assert 'settings.get("shuffle_config_layout", True)' in runtime
    assert 'data.startswith("shop:configserver:")' in runtime
    assert 'data.startswith("shop:configitem:")' in runtime


def test_layout_column_settings_reject_out_of_range(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(7001, key="plan_columns", value=3)
    service.set_userbot_setting_admin(7001, key="server_columns", value=3)
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="plan_columns", value=4)
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(7001, key="server_columns", value=4)


def test_admin_theme_screen_has_all_four_themes() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for label in (
        "✨ هوشمند",
        "🛒 فروشگاهی",
        "💼 حرفه‌ای",
        "🕊 مینیمال",
        "رنگی بودن دکمه‌ها",
    ):
        assert label in source


def test_admin_renewal_policy_controls_match_reference_structure() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for label in (
        "تنظیم شیوه تمدید",
        "پیشفرض",
        "پیشرفته",
        "منصفانه",
        "حجم افزایشی",
        "حجم ریست",
        "زمان افزایشی",
        "زمان ریست",
        "حداکثر زمان مجاز برای تمدید",
        "حداکثر حجم باقی‌مانده",
        "حجم نامحدود∞",
        "زمان نامحدود∞",
    ):
        assert label in source
    for callback in (
        "userbot:settings:buy_renew:renew_mode_info",
        "userbot:settings:buy_renew:renew_policy:default",
        "userbot:settings:buy_renew:renew_policy:advanced",
        "userbot:settings:buy_renew:renew_policy:fair",
        "userbot:settings:buy_renew:renew_rollover:volume:add",
        "userbot:settings:buy_renew:renew_rollover:volume:reset",
        "userbot:settings:buy_renew:renew_rollover:time:add",
        "userbot:settings:buy_renew:renew_rollover:time:reset",
        "userbot:settings:buy_renew:renew_limit:days",
        "userbot:settings:buy_renew:renew_limit:usage",
    ):
        assert callback in source


def test_userbot_renewal_policy_is_enforced_in_all_entry_points() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert "def _renewable_subscriptions" in source
    assert source.count("renewal_eligibility(") >= 3
    assert "renewal_not_allowed_text()" in source
    assert "renew_unlimited_volume_from_gb" in source
    assert "renew_unlimited_time_from_days" in source


def test_purchase_catalog_category_and_selected_server_are_tenant_scoped(
    conn, factories, cipher
) -> None:
    tenant, service = _service(conn, factories, cipher)
    category = service.add_plan_category_admin(
        7001, title="یک ماهه", priority=10
    )
    plan = service.add_plan(
        7001,
        name="50 گیگ یک ماهه",
        traffic_gb=50,
        duration_days=30,
        price=250000,
        currency="IRR",
        category_id=int(category["id"]),
        priority=5,
    )
    server = service.add_server(
        7001,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example",
    )
    service.set_panel_credential(
        7001,
        server_id=int(server["id"]),
        secret="tr-api-key",
    )
    service.register_customer(
        7101, display_name="Buyer", username="buyer"
    )

    purchase_servers = service.list_purchase_servers()
    assert [int(item["id"]) for item in purchase_servers] == [int(server["id"])]

    order = service.create_order(
        7101,
        int(plan["id"]),
        server_id=int(server["id"]),
    )
    assert int(order["selected_server_id"]) == int(server["id"])
    assert order["selected_server_label"] == "Turkey"
    assert order["category_title"] == "یک ماهه"

    with pytest.raises(TenantBusinessError, match="unfinished purchase orders"):
        service.delete_server(7001, server_id=int(server["id"]))

    other_tenant = factories.tenant(owner_telegram_id=8001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(other_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
    )
    with pytest.raises(TenantBusinessError):
        other.plan_category(int(category["id"]), public=False)
    with pytest.raises(TenantBusinessError):
        other._purchase_server(int(server["id"]))


def test_purchase_plan_sorting_honors_priority_then_selected_mode() -> None:
    plans = [
        {"id": 1, "priority": 20, "price": 100, "traffic_gb": 10},
        {"id": 2, "priority": 10, "price": 300, "traffic_gb": 30},
        {"id": 3, "priority": 10, "price": 200, "traffic_gb": 20},
    ]
    ordered = user_handlers._sorted_purchase_plans(
        plans,
        {
            "plan_sort_by_priority": True,
            "plan_sort_mode": "price_asc",
        },
    )
    assert [item["id"] for item in ordered] == [3, 2, 1]

    ordered = user_handlers._sorted_purchase_plans(
        plans,
        {
            "plan_sort_by_priority": False,
            "plan_sort_mode": "price_desc",
        },
    )
    assert [item["id"] for item in ordered] == [2, 3, 1]


def test_purchase_navigation_is_plan_then_server_then_order() -> None:
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for callback in (
        'data == "shop:buy"',
        'data.startswith("shop:buycat:")',
        'data.startswith("shop:plan:")',
        'data.startswith("shop:server:")',
        'data.startswith("shop:changeserver:")',
        'data.startswith("shop:orderserver:")',
    ):
        assert callback in runtime
    assert 'settings.get("plans_list_text")' in runtime
    assert 'settings.get("servers_list_text")' in runtime
    assert 'settings.get("plan_columns")' in runtime
    assert 'settings.get("server_columns")' in runtime
    assert 'settings.get("enable_buy", True)' in runtime
    assert "server_id=server_id" in runtime


def test_purchase_catalog_admin_controls_are_wired() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:tx_plans:plan_categories_enabled",
        "userbot:settings:tx_plans:plan_sort_by_priority",
        "userbot:settings:tx_plans:categories",
        "userbot:settings:tx_plans:category:add",
        "userbot:settings:tx_plans:category:plans:",
        "userbot:settings:tx_plans:category:assign:",
    ):
        assert callback in source


def test_deep_settings_callbacks_are_wired_to_real_runtime() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:subscription:show_user_page_link",
        "userbot:settings:subscription:shuffle_server_layout",
        "userbot:settings:subscription:sub_status_reminder",
        "userbot:settings:subscription:trial_spec",
        "userbot:settings:subscription:reset_free_trial",
        "userbot:settings:sub_link_status:set_base_url",
        "userbot:settings:buy_renew:show_renew_in_main_menu",
        "userbot:settings:buy_renew:plan_columns:menu",
        "userbot:settings:tx_plans:plan_sort_mode:menu",
        "userbot:settings:texts:guide_menu",
        "userbot:settings:marketing:toggle:enable_discount_code",
        "userbot:settings:force_join:toggle",
    ):
        assert callback in source

    assert 'data == "shop:renewmenu"' in runtime
    assert 'settings.get("show_renew_in_main_menu", True)' in runtime
    assert 'settings.get("enable_discount_code", True)' in runtime
    for callback in (
        "shop:guide:android",
        "shop:guide:ios",
        "shop:guide:windows",
        "shop:guide:mac",
        "shop:guide:linux",
    ):
        assert callback in runtime


def test_payment_root_callbacks_match_sellbot() -> None:
    callbacks = [
        button.callback_data
        for row in admin_userbot.build_payments_menu_keyboard().inline_keyboard
        for button in row
    ]
    assert "userbot:payments:list:approved" in callbacks
    assert "userbot:payments:list:rejected" in callbacks
    assert "userbot:payments:list:pending" in callbacks
    assert "userbot:payments:list:card" in callbacks
    assert "userbot:payments:list:approved:1" not in callbacks


def test_reset_all_free_trials_is_tenant_scoped(
    conn, factories, cipher
) -> None:
    tenant, service = _service(conn, factories, cipher)
    service.register_customer(7101, display_name="Trial A", username=None)
    service.register_customer(7102, display_name="Trial B", username=None)
    conn.execute(
        "UPDATE tenant_customers SET trial_used_at='2026-10-01T00:00:00+00:00' "
        "WHERE tenant_id=?",
        (int(tenant["id"]),),
    )

    other_tenant = factories.tenant(owner_telegram_id=8001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(other_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
    )
    other.register_customer(8101, display_name="Other Trial", username=None)
    conn.execute(
        "UPDATE tenant_customers SET trial_used_at='2026-10-01T00:00:00+00:00' "
        "WHERE tenant_id=?",
        (int(other_tenant["id"]),),
    )
    conn.commit()

    assert service.reset_all_customer_trials_admin(7001) == 2
    rows = conn.execute(
        "SELECT trial_used_at FROM tenant_customers WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchall()
    assert all(row[0] is None for row in rows)

    foreign = conn.execute(
        "SELECT trial_used_at FROM tenant_customers WHERE tenant_id=?",
        (int(other_tenant["id"]),),
    ).fetchone()
    assert foreign[0] is not None


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


def test_gift_voucher_edits_change_real_redemption_rules(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    first = service.register_customer(
        7101, display_name="Gift A", username="gift_a"
    )
    service.register_customer(
        7102, display_name="Gift B", username="gift_b"
    )
    voucher = service.add_gift_voucher_admin(
        7001,
        code="EDITME",
        amount=1000,
        currency="IRR",
        max_uses=2,
    )

    renamed = service.rename_gift_voucher_admin(
        7001, voucher_id=int(voucher["id"]), code="EDITED-GIFT"
    )
    assert renamed["code"] == "EDITED-GIFT"
    changed = service.set_gift_voucher_amount_admin(
        7001, voucher_id=int(voucher["id"]), amount=2500
    )
    assert int(changed["amount"]) == 2500
    expiring = service.set_gift_voucher_expiry_hours_admin(
        7001, voucher_id=int(voucher["id"]), hours=24
    )
    assert expiring["expires_at"] is not None
    assert parse_utc(str(expiring["expires_at"])) > utcnow()

    redeemed = service.redeem_gift_voucher(7101, code="edited-gift")
    assert int(redeemed["amount"]) == 2500
    assert int(redeemed["resulting_balance"]) == 2500

    full = service.set_gift_voucher_max_uses_admin(
        7001, voucher_id=int(voucher["id"]), max_uses=1
    )
    assert full["status"] == "disabled"
    with pytest.raises(TenantBusinessError):
        service.redeem_gift_voucher(7102, code="EDITED-GIFT")

    reopened = service.set_gift_voucher_max_uses_admin(
        7001, voucher_id=int(voucher["id"]), max_uses=3
    )
    assert reopened["status"] == "active"
    second = service.redeem_gift_voucher(7102, code="EDITED-GIFT")
    assert int(second["resulting_balance"]) == 2500

    unlimited = service.set_gift_voucher_expiry_hours_admin(
        7001, voucher_id=int(voucher["id"]), hours=0
    )
    assert unlimited["expires_at"] is None
    assert int(first["tenant_id"]) == service.tenant_id


def test_bulk_gift_codes_are_unique_one_time_and_expiring(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    items = service.create_gift_vouchers_bulk_admin(
        7001,
        prefix="FEST",
        count=12,
        amount=40000,
        currency="IRR",
        expiry_hours=48,
    )
    assert len(items) == 12
    assert len({str(item["code"]) for item in items}) == 12
    assert all(str(item["code"]).startswith("FEST-") for item in items)
    assert all(int(item["max_uses"]) == 1 for item in items)
    assert all(int(item["amount"]) == 40000 for item in items)
    assert all(
        item["expires_at"] is not None
        and parse_utc(str(item["expires_at"])) > utcnow()
        for item in items
    )


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


def test_stage9_marketing_controls_map_to_real_user_actions() -> None:
    enabled = user_handlers._checkout_markup(
        55,
        {"enable_discount_code": True},
    )
    disabled = user_handlers._checkout_markup(
        55,
        {"enable_discount_code": False},
    )
    enabled_callbacks = [
        button.callback_data
        for row in enabled.inline_keyboard
        for button in row
    ]
    disabled_callbacks = [
        button.callback_data
        for row in disabled.inline_keyboard
        for button in row
    ]
    assert "shop:coupon:55" in enabled_callbacks
    assert "shop:coupon:55" not in disabled_callbacks

    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert '"🎁 اعمال کد هدیه"' in runtime
    assert 'callback_data="shop:gift"' in runtime
    assert 'settings.get("show_gift_button", True)' in runtime


def test_stage9_admin_actions_are_wired_and_no_dead_marketing_options() -> None:
    gifts = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    sales = open(
        "TenantRuntime/AdminBot/handlers.py",
        encoding="utf-8",
    ).read()

    for callback in (
        "userbot:gifts:coupon:set_code:",
        "userbot:gifts:coupon:set_amount:",
        "userbot:gifts:coupon:set_limit:",
        "userbot:gifts:coupon:set_exp:",
        "userbot:gifts:auto_off",
    ):
        assert callback in gifts
    assert "gift_bulk_expiry" in gifts
    assert "GIFT_CAMPAIGN_PRESETS" in gifts
    assert '"winback"' in gifts

    for callback in (
        "biz:couponedit:",
        "biz:couponredemptions:",
        "biz:coupondelete:",
    ):
        assert callback in sales
    assert 'kind == "coupon_edit"' in sales

    # These SellBot keys have no meaningful customer execution path, so phase
    # 9 intentionally does not expose them as WhiteLabel admin controls.
    assert "instant_gift_coupon" not in gifts
    assert "enable_increase_code" not in gifts


def test_admin_forms_use_bottom_cancel_and_not_pipe_for_core_new_flows() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    assert '[[KeyboardButton("❌لغو")]]' in source
    assert "payment_add_title" in source
    assert '"kind":"gift_add_code"' in source
    assert '"kind":"referral_manual_customer"' in source
    assert '"kind":"wallet_set_currency"' in source
    assert "payment_add_instructions" in source
    assert "gift_add_expiry" in source


def test_broadcast_and_channel_media_flows_are_real() -> None:
    source = open(
        "TenantRuntime/AdminBot/broadcast_channel.py",
        encoding="utf-8",
    ).read()
    router = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    assert "_send_broadcast" in source
    assert "await bot.send_photo" in source
    assert "await bot.send_video" in source
    # Active SellBot Admin broadcast deliberately has no preview/publish
    # draft stage; it sends directly after optional photo/skip.
    assert 'callback_data="userbot:broadcast:preview"' not in source
    assert 'callback_data="userbot:broadcast:publish"' not in source
    assert "channelpost:preview" in source
    assert "channelpost:publish" in source
    assert "_download_admin_media" in source
    assert "MAX_BUTTONS = 8" in source
    assert "broadcast_channel.handle_callback" in router
