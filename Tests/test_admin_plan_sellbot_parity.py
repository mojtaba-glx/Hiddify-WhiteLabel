"""Regression coverage for AdminBot plan menus ported from Hiddify-SellBot."""

import asyncio

from TenantRuntime.AdminBot import server_actions
from Tests.test_server_management_complete import setup, ui, click, send


def keyboard_rows(message):
    markup = message.sent[-1][1]["reply_markup"]
    return [[button.text for button in row] for row in markup.inline_keyboard]


def test_plan_root_settings_and_mode_match_sellbot(monkeypatch, conn, factories, cipher):
    business, _, service, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, business)
    sid = source["id"]

    async def run():
        await click(update, context, f"srv:plans:{sid}")
        assert message.sent[-1][0] == (
            f"مدیریت پلن‌ها برای سرور 🖥 {source['label']}\n"
            "━━━━━━━━━━━━━━\n"
            "حالت نمایش فعلی در ربات کاربران: فقط پلن‌های ثابت\n\n"
            "یکی از گزینه‌های زیر را انتخاب کنید:"
        )
        assert keyboard_rows(message) == [
            ["📂 لیست دسته‌های پلن"],
            ["⚙️تنظیمات پلن‌ها"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:settings:{sid}")
        assert message.sent[-1][0] == (
            "⚙️تنظیمات پلن‌ها\n\nیکی از گزینه‌های زیر را انتخاب کنید:"
        )
        assert keyboard_rows(message) == [
            ["نوع نمایش پلن‌ها📋"],
            ["تنظیم پلن پویا📈"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:mode:{sid}")
        assert message.sent[-1][0] == (
            "⚙️تنظیمات پلن‌ها\n\nحالت نمایش پلن‌ها را انتخاب کنید:"
        )
        assert keyboard_rows(message) == [
            ["✅ ثابت", "❌ پویا", "❌ ترکیبی"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:mode:{sid}:dynamic")
        assert keyboard_rows(message)[0] == ["❌ ثابت", "✅ پویا", "❌ ترکیبی"]
        await click(update, context, f"srv:plans:{sid}")
        assert keyboard_rows(message) == [
            ["⚙️تنظیمات پلن‌ها"],
            ["🎛 مدیریت حرفه‌ای تخفیف‌ها"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:mode:{sid}:mixed")
        await click(update, context, f"srv:plans:{sid}")
        assert keyboard_rows(message) == [
            ["📂 لیست دسته‌های پلن"],
            ["⚙️تنظیمات پلن‌ها"],
            ["🎛 مدیریت حرفه‌ای تخفیف‌ها"],
            ["بازگشت🔙"],
        ]

    asyncio.run(run())


def test_dynamic_plan_settings_use_sellbot_month_fields(monkeypatch, conn, factories, cipher):
    business, _, service, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, business)
    sid = source["id"]
    service.set_sales(
        7001,
        sid,
        {
            "mode": "dynamic",
            "pricing_model": "sellbot_month",
            "price_gb": 11000,
            "price_month": 20000,
            "min_gb": 10,
            "max_gb": 200,
            "step_gb": 5,
            "min_month": 1,
            "max_month": 3,
            "step_month": 1,
        },
    )

    async def run():
        await click(update, context, f"srv:settings:{sid}:dynamic")
        text = message.sent[-1][0]
        assert "📈 تنظیم مقادیر پلن پویا" in text
        assert "💰 قیمت هر گیگ: 11,000 تومان" in text
        assert "💰 قیمت هر ماه: 20,000 تومان" in text
        assert "📊 حجم قابل فروش: از 10 تا 200 گیگ (گام: 5)" in text
        assert "⌛ زمان اشتراک: از 1 تا 3 ماه (گام: 1)" in text
        assert keyboard_rows(message) == [
            ["💰 قیمت هر گیگ"],
            ["💰 قیمت هر ماه"],
            ["📊 حداقل/حداکثر حجم و گام"],
            ["⌛ حداقل/حداکثر زمان و گام"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:salesfield:{sid}:price_month")
        assert context.user_data[server_actions.FLOW]["kind"] == "sales_field"
        assert message.sent[-1][0] == "💰 قیمت هر ماه اشتراک را (تومان) ارسال کنید:"
        await send(update, context, "۲۵۰۰۰")
        assert service.sales(sid)["price_month"] == 25000
        assert "💰 قیمت هر ماه: 25,000 تومان" in message.sent[-1][0]

        await click(update, context, f"srv:salesfield:{sid}:time_range")
        await send(update, context, "۱-۶-۱")
        sales = service.sales(sid)
        assert (sales["min_month"], sales["max_month"], sales["step_month"]) == (1, 6, 1)

        # 10GB + one month: 10*11000 + 25000.
        assert service.quote(sid, 10, 30) == (135000, "IRR")

    asyncio.run(run())


def test_discount_menu_and_edit_flow_match_sellbot(monkeypatch, conn, factories, cipher):
    business, _, service, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, business)
    sid = source["id"]
    service.set_sales(
        7001,
        sid,
        {
            "mode": "dynamic",
            "pricing_model": "sellbot_month",
            "price_gb": 1000,
            "discount_step_gb": 10,
            "discount_percent_step": 20,
            "discount_percent_max": 20,
        },
    )

    async def run():
        await click(update, context, f"srv:discounts:{sid}")
        assert "🎛 مدیریت حرفه‌ای تخفیف‌ها" in message.sent[-1][0]
        assert "🎁 تخفیف حجمی ساده: غیرفعال ❌" in message.sent[-1][0]
        assert keyboard_rows(message) == [
            ["روشن کن تخفیف حجمی ساده"],
            ["روشن کن تخفیف پلاکانی"],
            ["✏️ ویرایش تخفیف حجمی ساده"],
            ["✏️ ویرایش تخفیف پله‌ای"],
            ["⏱ تنظیم تایمر تخفیف حجمی ساده"],
            ["⏱ تنظیم تایمر تخفیف پلاکانی"],
            ["بازگشت🔙"],
        ]

        await click(update, context, f"srv:discounttoggle:{sid}:simple:on")
        assert "🎁 تخفیف حجمی ساده: فعال ✅" in message.sent[-1][0]
        assert keyboard_rows(message)[0] == ["خاموش کن تخفیف حجمی ساده"]

        await click(update, context, f"srv:discountedit:{sid}:simple")
        assert context.user_data[server_actions.FLOW]["kind"] == "discount_simple_threshold"
        await send(update, context, "۳۰")
        assert context.user_data[server_actions.FLOW]["kind"] == "discount_simple_percent"
        await send(update, context, "۲۵٪")
        sales = service.sales(sid)
        assert sales["discount_simple_enabled"] is True
        assert sales["discount_step_gb"] == 30
        assert sales["discount_percent_step"] == 25
        assert sales["discount_percent_max"] == 25

        await click(update, context, f"srv:discountedit:{sid}:tiered")
        await send(update, context, "۱۰:۲۰،۳۰:۲۵,۵۰:۳۰")
        sales = service.sales(sid)
        assert sales["discount_tiers"] == [
            {"gb": 10, "percent": 20},
            {"gb": 30, "percent": 25},
            {"gb": 50, "percent": 30},
        ]
        assert sales["discount_tiered_enabled"] is True

        await click(update, context, f"srv:discountedit:{sid}:tiered_timer")
        await send(update, context, "12")
        sales = service.sales(sid)
        assert sales["discount_tiered_enabled"] is True
        assert sales["discount_tiered_until"]

    asyncio.run(run())
