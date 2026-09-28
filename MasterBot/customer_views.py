"""Pure presentation helpers for the customer side of the PlatformBot."""

from __future__ import annotations

import json
from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from Shared.timeutils import format_tehran, parse_utc


BUY = "🛒 خرید ربات"
SERVICES = "📋 سرویس‌های من"
SETUP = "🔑 راه‌اندازی ربات"
WALLET = "👛 کیف پول"
GUIDE = "📝 راهنمای استفاده"
FEATURES = "⭐ ویژگی‌ها"
TRIAL = "🎁 لایسنس تست"

CUSTOMER_BUTTONS = frozenset({BUY, SERVICES, SETUP, WALLET, GUIDE, FEATURES, TRIAL})


def customer_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUY, SERVICES],
            [SETUP, WALLET],
            [GUIDE, FEATURES],
            [TRIAL],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="یک گزینه را انتخاب کنید",
    )


def customer_home_text(
    display_name: str,
    *,
    store_name: str = "فروش ربات اختصاصی",
    support_contact: str = "",
) -> str:
    lines = [
        f"👋 سلام {display_name}، خوش آمدید.",
        "",
        f"🤖 {store_name}",
        "ربات فروش اختصاصی خودتان را تهیه کنید؛ راه‌اندازی، میزبانی و بروزرسانی از سمت سیستم انجام می‌شود.",
        "",
        "از منوی پایین می‌توانید خرید، سرویس‌ها، کیف پول و راه‌اندازی ربات را مدیریت کنید.",
    ]
    if support_contact:
        lines.extend(["", f"☎️ پشتیبانی: {support_contact}"])
    return "\n".join(lines)


def plans_keyboard(plans: Iterable[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for plan in plans:
        rows.append([InlineKeyboardButton(
            f"🛒 {plan['name']} · {int(plan['price']):,} {plan['currency']} · {int(plan['duration_days'])} روز",
            callback_data=f"customer:plan:{int(plan['id'])}",
        )])
    rows.append([InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")])
    return InlineKeyboardMarkup(rows)


def plan_text(plan: dict[str, Any]) -> str:
    features = _features(plan)
    lines = [
        f"📦 {plan['name']}", "",
        f"⏳ اعتبار: {int(plan['duration_days'])} روز",
        f"💰 قیمت: {int(plan['price']):,} {plan['currency']}",
        f"🖥 حداکثر سرور: {int(plan['max_servers'])}",
        f"👥 حداکثر کاربر: {int(plan['max_users'])}",
    ]
    if features:
        lines.extend(["", "⭐ امکانات:", *[f"• {item}" for item in features]])
    return "\n".join(lines)


def plan_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ثبت سفارش", callback_data=f"customer:order:{int(plan_id)}")],
        [InlineKeyboardButton("↩️ پلن‌ها", callback_data="customer:plans")],
    ])


def payment_keyboard(
    order: dict[str, Any], methods: Iterable[dict[str, Any]], *, wallet_balance: int
) -> InlineKeyboardMarkup:
    rows = []
    for method in methods:
        icon = "💳" if method["kind"] == "card" else "💎"
        rows.append([InlineKeyboardButton(
            f"{icon} {method['title']}",
            callback_data=f"customer:pay:{int(order['id'])}:{int(method['id'])}",
        )])
    if wallet_balance >= int(order["amount"]):
        rows.append([InlineKeyboardButton(
            f"👛 پرداخت از کیف پول ({wallet_balance:,})",
            callback_data=f"customer:walletpay:{int(order['id'])}",
        )])
    rows.append([InlineKeyboardButton("📋 سفارش‌های من", callback_data="customer:orders")])
    return InlineKeyboardMarkup(rows)


def payment_instructions(method: dict[str, Any], order: dict[str, Any]) -> str:
    parts = [
        "💳 اطلاعات پرداخت", "",
        f"سفارش: {order['public_id']}",
        f"مبلغ: {int(order['amount']):,} {order['currency']}",
        f"روش: {method['title']}",
        f"مقصد: {method['destination']}",
    ]
    if method.get("recipient"):
        parts.append(f"به نام: {method['recipient']}")
    if method.get("network"):
        parts.append(f"شبکه: {method['network']}")
    if method.get("instructions"):
        parts.extend(["", str(method["instructions"])])
    parts.extend(["", "پس از پرداخت، کد پیگیری را بفرستید یا تصویر رسید را ارسال کنید."])
    return "\n".join(parts)


def services_text(services: Iterable[dict[str, Any]], *, timezone_name: str) -> str:
    rows = list(services)
    if not rows:
        return (
            "📋 سرویس‌های من\n\n"
            "❌ هنوز ربات فعالی ندارید.\n"
            "برای شروع از «🛒 خرید ربات» یک پلن تهیه کنید."
        )
    lines = ["📋 سرویس‌های من"]
    for item in rows:
        expiry = "نامشخص"
        if item.get("expires_at"):
            expiry = format_tehran(parse_utc(str(item["expires_at"])), timezone_name)
        lines.extend([
            "",
            f"🤖 {item['name']} · {item['slug']}",
            f"وضعیت سرویس: {item['status']}",
            f"لایسنس: {item.get('license_status') or 'ندارد'}",
            f"پلن: {item.get('plan_name') or 'نامشخص'}",
            f"انقضا: {expiry}",
        ])
    return "\n".join(lines)


def services_keyboard(services: Iterable[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for item in services:
        rows.append([InlineKeyboardButton(
            f"♻️ تمدید {item['name']}", callback_data=f"customer:renew:{int(item['id'])}"
        )])
    rows.append([InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")])
    return InlineKeyboardMarkup(rows)


def setup_keyboard(orders: Iterable[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"🔑 راه‌اندازی {item['public_id']}",
        callback_data=f"customer:setup:{int(item['id'])}",
    )] for item in orders]
    rows.append([InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")])
    return InlineKeyboardMarkup(rows)


def orders_text(orders: Iterable[dict[str, Any]]) -> str:
    rows = list(orders)
    if not rows:
        return "🧾 سفارش‌های من\n\nسفارشی ثبت نشده است."
    lines = ["🧾 سفارش‌های من"]
    for item in rows:
        lines.append(
            f"\n{item['public_id']} · {item.get('plan_name') or 'شارژ کیف پول'}\n"
            f"{int(item['amount']):,} {item['currency']} · {item['status']}"
        )
    return "\n".join(lines)


def features_text(plans: Iterable[dict[str, Any]]) -> str:
    rows = list(plans)
    lines = [
        "⭐ امکانات ربات اختصاصی", "",
        "🤖 ربات مدیریت و ربات کاربران با نام و توکن خودتان",
        "☁️ اجرا روی زیرساخت مرکزی؛ بدون نیاز به نصب ربات روی سرور شما",
        "🔐 نگهداری امن و جداگانه اطلاعات هر مشتری",
        "🛒 فروش و تمدید اشتراک",
        "👛 کیف پول و مدیریت پرداخت",
        "🖥 مدیریت سرور، نود و پلن‌های فروش",
        "🔗 لینک هوشمند اشتراک",
        "🎫 تیکت و پشتیبانی",
        "📊 گزارش و مدیریت سرویس‌ها",
        "♻️ بروزرسانی و نگهداری متمرکز",
    ]
    if rows:
        lines.extend(["", "📦 پلن‌های قابل خرید:"])
        for plan in rows:
            lines.append(
                f"• {plan['name']} · {int(plan['price']):,} {plan['currency']} · "
                f"{int(plan['duration_days'])} روز"
            )
    return "\n".join(lines)


def guide_text() -> str:
    return (
        "📝 راهنمای راه‌اندازی\n\n"
        "1️⃣ از «🛒 خرید ربات» پلن موردنظر را انتخاب کنید.\n"
        "2️⃣ هزینه را از کیف پول یا یکی از روش‌های پرداخت فعال پرداخت کنید.\n"
        "3️⃣ بعد از تأیید پرداخت، وارد «🔑 راه‌اندازی ربات» شوید.\n"
        "4️⃣ در BotFather دو ربات بسازید: یکی برای مدیریت و یکی برای کاربران.\n"
        "5️⃣ توکن‌ها را طبق مراحل ربات ارسال کنید. شناسه تلگرام شما به‌صورت خودکار تشخیص داده می‌شود.\n"
        "6️⃣ پس از تأیید توکن‌ها، ربات‌های اختصاصی شما روی سیستم راه‌اندازی می‌شوند.\n\n"
        "🔐 پیام حاوی توکن پس از دریافت حذف می‌شود و توکن فقط به‌صورت رمزنگاری‌شده نگهداری می‌شود."
    )


def _features(plan: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(str(plan.get("features") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [str(item)[:100] for item in value[:10]]
    if isinstance(value, dict):
        return [str(key)[:100] for key, enabled in value.items() if enabled][:10]
    return []
