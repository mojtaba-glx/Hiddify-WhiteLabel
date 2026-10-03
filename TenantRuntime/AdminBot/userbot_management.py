"""SellBot-compatible management UI for one tenant's UserBot.

This module intentionally mirrors the proven Hiddify-SellBot AdminBot/UserBot
management navigation while translating every operation to the tenant-scoped
WhiteLabel business service. No callback can escape the current tenant.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from telegram import Bot, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from Database.repositories import BotRepository
from Shared.crypto import fingerprint_token
from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.button_styles import (
    inline_button as InlineKeyboardButton,
    keyboard_button as KeyboardButton,
    set_button_settings,
)

PAGE_SIZE = 21
FLOW_KEY = "userbot_admin_flow"
CHANNEL_DRAFT_KEY = "tenant_channel_post_draft"
MAX_CHANNEL_BUTTONS = 8
BROADCAST_SKIP_TEXT = "⏩رد کردن"


SEGMENT_LABELS = {
    "all": "تمام کاربران",
    "expired_all": "تمام کاربران منقضی شده",
    "no_order": "کاربران بدون سفارش",
    "expired_1w": "کاربران منقضی شده بیش از یک هفته",
    "expired_2w": "کاربران منقضی شده بیش از دو هفته",
    "expired_4w": "کاربران منقضی شده بیش از چهار هفته",
    "expired_8w": "کاربران منقضی شده بیش از هشت هفته",
}


GIFT_CAMPAIGN_PRESETS: dict[str, dict[str, Any]] = {
    "welcome": {
        "title": "🎁 خوش‌آمدگویی",
        "prefix": "WELCOME",
        "amount": 30000,
        "max_uses": 100,
        "hours": 168,
    },
    "festival": {
        "title": "🔥 جشنواره فروش",
        "prefix": "FEST",
        "amount": 50000,
        "max_uses": 300,
        "hours": 48,
    },
    "vip": {
        "title": "💎 مشتری وفادار",
        "prefix": "VIP",
        "amount": 100000,
        "max_uses": 50,
        "hours": 72,
    },
    "winback": {
        "title": "🕒 بازگشت کاربر",
        "prefix": "BACK",
        "amount": 40000,
        "max_uses": 150,
        "hours": 120,
    },
}


def userbot_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌لغو")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def broadcast_skip_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BROADCAST_SKIP_TEXT)],
            [KeyboardButton("❌لغو")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _is_broadcast_skip(value: str) -> bool:
    normalized = str(value or "").replace("\u200c", "").replace(" ", "").strip()
    return normalized in {
        "⏩ردکردن", "⏩️ردکردن", "ردکردن", "⏭️ردکردن", "▶️ردکردن"
    }


def _normalize_button_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        username = raw[1:].strip()
        return (
            f"https://t.me/{username}"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else ""
        )
    if raw.lower().startswith("t.me/"):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    scheme = str(parsed.scheme or "").lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return raw
    if scheme == "tg" and (parsed.netloc or parsed.path):
        return raw
    return ""


def build_userbot_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤مدیریت کاربران ربات", callback_data="userbot:users_menu")],
        [
            InlineKeyboardButton("💵مدیریت تراکنشات", callback_data="userbot:payments_menu"),
            InlineKeyboardButton("📗مدیریت سفارشات", callback_data="userbot:orders_menu"),
        ],
        [InlineKeyboardButton("🎁مدیریت هدایا", callback_data="userbot:gifts_menu")],
        [InlineKeyboardButton("🤝مدیریت رفرال", callback_data="userbot:referral_menu")],
        [
            InlineKeyboardButton("📑مدیریت تیکت‌ها", callback_data="userbot:tickets_menu"),
            InlineKeyboardButton("📧ارسال پیام همگانی", callback_data="userbot:broadcast_menu"),
        ],
        [InlineKeyboardButton("📢 مدیریت کانال", callback_data="channelpost:menu")],
        [InlineKeyboardButton("⚙️تنظیمات", callback_data="userbot:settings_menu")],
    ])


def build_users_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥لیست کاربران ربات", callback_data="userbot:users:1")],
        [InlineKeyboardButton("🔍جستجوی کاربران", callback_data="userbot:users_search_menu")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def build_users_search_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤جستجو با نام", callback_data="userbot:search:name")],
        [InlineKeyboardButton("✝️جستجو با Telegram ID", callback_data="userbot:search:id")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:users_menu")],
    ])


def build_payments_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳لیست تراکنشات در انتظار", callback_data="userbot:payments:list:pending")],
        [
            InlineKeyboardButton("✅لیست تراکنشات تایید شده", callback_data="userbot:payments:list:approved"),
            InlineKeyboardButton("🚫لیست تراکنشات رد شده", callback_data="userbot:payments:list:rejected"),
        ],
        [
            InlineKeyboardButton("💳لیست تراکنشات کارت به کارت", callback_data="userbot:payments:list:card"),
            InlineKeyboardButton("🪙لیست تراکنشات Crypto", callback_data="userbot:payments:list:crypto"),
        ],
        [
            InlineKeyboardButton("➕شارژهای کیف پول", callback_data="userbot:payments:list:wallet_topup"),
            InlineKeyboardButton("💰پرداخت‌های کیف پول", callback_data="userbot:payments:list:wallet_order"),
        ],
        [InlineKeyboardButton("🔍جستجوی تراکنش", callback_data="userbot:payments:search")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def build_orders_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📗لیست سفارشات", callback_data="userbot:orders:list:1")],
        [InlineKeyboardButton("🔍جستجوی سفارشات", callback_data="userbot:orders:search")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def build_gifts_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 داشبورد هدایا", callback_data="userbot:gifts:dashboard")],
        [InlineKeyboardButton("🏷 کوپن‌ها و کدهای هدیه", callback_data="userbot:gifts:coupons")],
        [
            InlineKeyboardButton("🎯 قالب‌های آماده کمپین", callback_data="userbot:gifts:presets"),
            InlineKeyboardButton("🧩 ساخت گروهی کد هدیه", callback_data="userbot:gifts:bulk"),
        ],
        [InlineKeyboardButton("📜 گزارش مصرف هدایا", callback_data="userbot:gifts:redemptions")],
        [
            InlineKeyboardButton("📣 متن آماده کمپین", callback_data="userbot:gifts:campaign"),
            InlineKeyboardButton("🛡 کنترل سوءاستفاده", callback_data="userbot:gifts:security"),
        ],
        [InlineKeyboardButton("📘 راهنمای هدایا", callback_data="userbot:gifts:help")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def build_tickets_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨تیکت‌های در انتظار", callback_data="userbot:tickets:list:pending:1")],
        [InlineKeyboardButton("📬تیکت‌های باز", callback_data="userbot:tickets:list:open:1")],
        [InlineKeyboardButton("📩تیکت‌های بسته", callback_data="userbot:tickets:list:closed:1")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def build_broadcast_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تمام کاربران", callback_data="userbot:broadcast:segment:all")],
        [InlineKeyboardButton("تمام کاربران منقضی شده", callback_data="userbot:broadcast:segment:expired_all")],
        [InlineKeyboardButton("کاربران بدون سفارش", callback_data="userbot:broadcast:segment:no_order")],
        [InlineKeyboardButton("کاربران منقضی شده بیش از یک هفته", callback_data="userbot:broadcast:segment:expired_1w")],
        [InlineKeyboardButton("کاربران منقضی شده بیش از دو هفته", callback_data="userbot:broadcast:segment:expired_2w")],
        [InlineKeyboardButton("کاربران منقضی شده بیش از چهار هفته", callback_data="userbot:broadcast:segment:expired_4w")],
        [InlineKeyboardButton("کاربران منقضی شده بیش از هشت هفته", callback_data="userbot:broadcast:segment:expired_8w")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def _settings_menu(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    theme = str(settings.get("button_theme") or "smart")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍تنظیمات اشتراک", callback_data="userbot:settings:subscription")],
        [InlineKeyboardButton("📁وضعیت نمایش لینک اشتراک", callback_data="userbot:settings:sub_link_status")],
        [InlineKeyboardButton(f"🎨 دکمه‌های رنگی | {theme}", callback_data="userbot:settings:ui")],
        [InlineKeyboardButton("🛒تنظیمات خرید و تمدید", callback_data="userbot:settings:buy_renew")],
        [InlineKeyboardButton("🧮تنظیمات تراکنشات و پلن ها", callback_data="userbot:settings:tx_plans")],
        [InlineKeyboardButton("🧾تنظیمات متون", callback_data="userbot:settings:texts")],
        [InlineKeyboardButton("🎯تنظیمات بازاریابی", callback_data="userbot:settings:marketing")],
        [InlineKeyboardButton("🔒تنظیمات عضویت اجباری", callback_data="userbot:settings:force_join")],
        [InlineKeyboardButton("💳تنظیمات پرداخت", callback_data="userbot:settings:payment")],
        [InlineKeyboardButton("🗂️تنظیمات بکاپ و بازیابی", callback_data="userbot:settings:backup_restore")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])


def _bool_icon(value: Any) -> str:
    return "✅" if bool(value) else "❌"


def _trial_settings_text(growth: dict[str, Any]) -> str:
    return (
        "🎊 مشخصات اشتراک تستی\n"
        f"وضعیت: {_bool_icon(growth.get('trial_enabled'))}\n"
        f"اعلان موفقیت تست: {_bool_icon(growth.get('trial_announce_enabled', True))}\n"
        f"حجم: {growth.get('trial_traffic_gb')}GB\n"
        f"مدت: {growth.get('trial_duration_days')} روز"
    )


def _trial_settings_keyboard(growth: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔥 وضعیت اشتراک تستی | {_bool_icon(growth.get('trial_enabled'))}",
            callback_data="userbot:settings:trial_spec:enabled",
        )],
        [InlineKeyboardButton(
            f"📣 اعلان تست | {_bool_icon(growth.get('trial_announce_enabled', True))}",
            callback_data="userbot:settings:trial_spec:announce",
        )],
        [InlineKeyboardButton(
            "📊 حجم اشتراک تستی",
            callback_data="userbot:settings:trial_spec:usage",
        )],
        [InlineKeyboardButton(
            "📆 مدت اشتراک تستی",
            callback_data="userbot:settings:trial_spec:days",
        )],
        [InlineKeyboardButton(
            "🔙بازگشت",
            callback_data="userbot:settings:subscription",
        )],
    ])


def _reminder_settings_text(settings: dict[str, Any]) -> str:
    return (
        "🔔 یادآور وضعیت اشتراک\n"
        f"وضعیت: {_bool_icon(settings.get('reminder_enabled'))}\n"
        f"یادآوری زمانی: {int(settings.get('reminder_days') or 3)} روز مانده\n"
        f"یادآوری حجمی: {int(settings.get('reminder_remaining_gb') or 3)} گیگ مانده\n"
        "ℹ️ این تنظیمات مستقیماً توسط Lifecycle اجرا می‌شوند."
    )


def _reminder_settings_keyboard(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔔 یادآور وضعیت اشتراک | {_bool_icon(settings.get('reminder_enabled'))}",
            callback_data="userbot:settings:sub_status_reminder:enabled",
        )],
        [InlineKeyboardButton(
            "📊 یادآور وضعیت مصرف",
            callback_data="userbot:settings:sub_status_reminder:usage",
        )],
        [InlineKeyboardButton(
            "📆 یادآور وضعیت زمان",
            callback_data="userbot:settings:sub_status_reminder:days",
        )],
        [InlineKeyboardButton(
            "🔙بازگشت",
            callback_data="userbot:settings:subscription",
        )],
    ])



def _plan_category_detail_text(category: dict[str, Any]) -> str:
    return (
        f"📂 دسته: {category['title']}\n"
        "━━━━━━━━━━━━━━\n"
        f"🔢 اولویت: {int(category.get('priority') or 0)}\n"
        f"📋 تعداد پلن: {int(category.get('plan_count') or 0)}\n"
        f"وضعیت: {'فعال ✅' if str(category.get('status')) == 'active' else 'غیرفعال ⛔'}"
    )


def _plan_category_detail_rows(
    category: dict[str, Any],
) -> list[list[InlineKeyboardButton]]:
    category_id = int(category["id"])
    active = str(category.get("status")) == "active"
    return [
        [InlineKeyboardButton(
            "📋 اتصال/جداسازی پلن‌ها",
            callback_data=f"userbot:settings:tx_plans:category:plans:{category_id}",
        )],
        [InlineKeyboardButton(
            "📝 ویرایش عنوان",
            callback_data=f"userbot:settings:tx_plans:category:edit_title:{category_id}",
        )],
        [InlineKeyboardButton(
            "🔢 ویرایش اولویت",
            callback_data=f"userbot:settings:tx_plans:category:edit_priority:{category_id}",
        )],
        [InlineKeyboardButton(
            "⛔ غیرفعال کردن" if active else "✅ فعال کردن",
            callback_data=f"userbot:settings:tx_plans:category:toggle:{category_id}",
        )],
        [InlineKeyboardButton(
            "🔙بازگشت",
            callback_data="userbot:settings:tx_plans:categories",
        )],
    ]


def _display_name(item: dict[str, Any]) -> str:
    username = str(item.get("username") or "").strip()
    if username:
        return f"@{username}"
    return str(item.get("display_name") or item.get("telegram_user_id") or item.get("id") or "کاربر")


async def _refresh_admin_reply_keyboard(update: Update) -> None:
    """Refresh the persistent AdminBot ReplyKeyboard after theme changes."""
    chat = update.effective_chat
    if chat is None:
        return
    try:
        from TenantRuntime.AdminBot.handlers import admin_main_keyboard

        message = await chat.send_message(
            "\u2063",
            reply_markup=admin_main_keyboard(),
        )
        try:
            await message.delete()
        except Exception:
            pass
    except Exception:
        return


async def _edit_or_send(update: Update, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs: Any) -> None:
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            await query.message.edit_text(text, reply_markup=markup, **kwargs)
            return
        except BadRequest:
            pass
    if update.effective_chat is not None:
        await update.effective_chat.send_message(text, reply_markup=markup, **kwargs)


async def send_userbot_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(FLOW_KEY, None)
    await _edit_or_send(
        update,
        "🤖 مدیریت ربات کاربران\n"
        "از این بخش می‌توانید کاربران ربات، سفارشات، تراکنش‌ها و سایر بخش‌ها را مدیریت کنید.",
        build_userbot_main_menu(),
    )


def _user_profile_text(business: Any, actor: int, customer_id: int) -> str:
    profile = business.customer_profile_admin(actor, customer_id=customer_id)
    wallet = business.customer_wallet_admin(actor, customer_id=customer_id)
    balances = list(wallet.get("accounts") or [])
    wallet_text = "0"
    if balances:
        wallet_text = " | ".join(
            f"{int(x.get('balance') or 0):,} {x.get('currency') or ''}"
            for x in balances
        )
    got_trial = bool(profile.get("trial_used_at"))
    return (
        f"👤 کاربر: {_display_name(profile)}\n"
        f"🔹 نام کاربری: {'@' + str(profile.get('username')).lstrip('@') if profile.get('username') else '-'}\n"
        f"🔸 شناسه کاربر: {profile.get('telegram_user_id')}\n"
        f"🔸 وضعیت دریافت تست رایگان: {'✅' if got_trial else '❌ (نگرفته)'}\n"
        f"🔸 موجودی کیف پول: {wallet_text}\n"
        f"🔸 وضعیت اکانت: {'🟢 فعال' if profile.get('status') == 'active' else '🔴 مسدود'}\n"
        "❖ ⬩----------------------------------⬩ ❖\n"
        f"🔸 تعداد اشتراک‌های خریداری شده: {int(profile.get('subscriptions_total') or 0)}\n"
        f"🔸 تعداد اشتراک‌های فعال: {int(profile.get('subscriptions_active') or 0)}\n"
        f"🔸 تعداد سفارشات: {len(business.customer_orders_admin(actor, customer_id=customer_id))}\n"
        f"🔸 تعداد تراکنشات: {len(business.customer_receipts_admin(actor, customer_id=customer_id))}\n"
        "❖ ⬩----------------------------------⬩ ❖\n"
        f"🔸 تیکت باز: {int(profile.get('tickets_open') or 0)}"
    )


def _user_profile_keyboard(customer_id: int, *, back: str = "userbot:users_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست سرویس‌ها", callback_data=f"userbot:user:{customer_id}:services")],
        [InlineKeyboardButton("📗 لیست سفارشات", callback_data=f"userbot:user:{customer_id}:orders")],
        [InlineKeyboardButton("💵 لیست تراکنشات", callback_data=f"userbot:user:{customer_id}:payments")],
        [InlineKeyboardButton("💳 ویرایش کیف پول", callback_data=f"userbot:user:{customer_id}:wallet")],
        [InlineKeyboardButton("🔄 بازنشانی اشتراک تستی", callback_data=f"userbot:user:{customer_id}:reset_trial")],
        [InlineKeyboardButton("🚫 مسدود/آزاد سازی کاربر", callback_data=f"userbot:user:{customer_id}:ban")],
        [
            InlineKeyboardButton("📨 ارسال پیام", callback_data=f"userbot:user:{customer_id}:message"),
            InlineKeyboardButton("📑 لیست تیکت‌ها", callback_data=f"userbot:user:{customer_id}:tickets"),
        ],
        [InlineKeyboardButton("🔙بازگشت", callback_data=back)],
    ])


def _page(items: list[Any], page: int) -> tuple[list[Any], int, int]:
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(1, min(int(page), pages))
    start = (page - 1) * PAGE_SIZE
    return items[start:start + PAGE_SIZE], page, pages


async def _send_users_page(update: Update, business: Any, actor: int, page: int = 1, query: str = "") -> None:
    items = business.list_customers_admin(actor, query=query)
    selected, page, pages = _page(items, page)
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for item in selected:
        current.append(
            InlineKeyboardButton(
                f"🔵 {_display_name(item)[:18]}",
                callback_data=f"userbot:user:{int(item['id'])}",
            )
        )
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"userbot:users:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="userbot:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"userbot:users:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:users_menu")])
    await _edit_or_send(
        update,
        "👥 لیست کاربران ربات\n"
        f"تعداد کل: {len(items)}\n"
        f"صفحه: {page}/{pages}\n",
        InlineKeyboardMarkup(rows),
    )


async def _send_user_profile(update: Update, business: Any, actor: int, customer_id: int, back: str = "userbot:users_menu") -> None:
    await _edit_or_send(
        update,
        _user_profile_text(business, actor, customer_id),
        _user_profile_keyboard(customer_id, back=back),
    )


async def _send_service_detail(
    update: Update,
    business: Any,
    actor: int,
    subscription_id: int,
) -> None:
    item = business.subscription_admin(actor, subscription_id=int(subscription_id))
    usage = int(item.get("usage_bytes") or 0) / (1024 ** 3)
    limit = int(item.get("traffic_bytes") or 0) / (1024 ** 3)
    status = str(item.get("status") or "")
    status_text = {
        "active": "🟢وضعیت حساب: فعال",
        "disabled": "⚫وضعیت حساب: غیرفعال",
        "expired": "🔴وضعیت حساب: منقضی",
        "pending_provisioning": "🟡وضعیت حساب: در انتظار ساخت",
    }.get(status, f"وضعیت: {status}")
    text = (
        f"👤 کاربر:  {item.get('display_name') or 'کاربر'}\n"
        "❖⬩╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍⬩❖\n"
        f"⬖ سرور:  {item.get('server_label') or 'ثبت نشده'}\n"
        f"📊مصرف: {usage:.2f} از {limit:.2f} گیگابایت\n"
        f"📆انقضا: {item.get('expires_at') or 'نامشخص'}\n"
        f"{status_text}\n"
        f"📶آخرین اتصال: {item.get('last_online') or 'ثبت نشده'}\n"
        f"📝یادداشت: —\n"
        f"🔑 شناسه سرویس: {int(item['id'])}"
    )
    customer_id = int(item["customer_id"])
    if status == "pending_provisioning":
        rows = [
            [InlineKeyboardButton(
                "🔁 تلاش ساخت/تحویل",
                callback_data=f"biz:fulfill:{int(item['order_id'])}",
            )],
            [InlineKeyboardButton(
                "🗑 پاک‌سازی رکورد شروع‌نشده",
                callback_data=f"search:drop:{subscription_id}",
            )],
            [InlineKeyboardButton(
                "📋بازگشت به سرویس‌ها",
                callback_data=f"userbot:user:{customer_id}:services",
            )],
        ]
    else:
        rows = [
            [InlineKeyboardButton(
                "📄کانفیگ ها",
                callback_data=f"userbot:svc:{subscription_id}:configs",
            )],
            [InlineKeyboardButton(
                "✏️ویرایش کاربر",
                callback_data=f"search:edit:{subscription_id}",
            )],
            [InlineKeyboardButton(
                "∞تمدید اشتراک",
                callback_data=f"search:renew:{subscription_id}",
            )],
            [InlineKeyboardButton(
                "🗑حذف کاربر",
                callback_data=f"search:delete:{subscription_id}",
            )],
            [InlineKeyboardButton(
                "👤پروفایل کاربر",
                callback_data=f"userbot:user:{customer_id}",
            )],
            [InlineKeyboardButton(
                "📋بازگشت به سرویس‌ها",
                callback_data=f"userbot:user:{customer_id}:services",
            )],
        ]
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


def _orders_stats(items: list[dict[str, Any]]) -> tuple[int, int, dict[str, int]]:
    total_gb = sum(int(x.get("traffic_gb") or 0) for x in items)
    money: dict[str, int] = {}
    for item in items:
        currency = str(item.get("currency") or "")
        money[currency] = money.get(currency, 0) + int(item.get("amount") or 0)
    return len(items), total_gb, money


async def _send_orders_page(update: Update, business: Any, actor: int, page: int = 1, items: list[dict[str, Any]] | None = None) -> None:
    all_items = list(items if items is not None else business.list_orders_admin(actor))
    selected, page, pages = _page(all_items, page)
    total_count, total_gb, money = _orders_stats(all_items)
    money_text = " | ".join(f"{v:,} {k}" for k, v in money.items()) or "0"
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for item in selected:
        current.append(InlineKeyboardButton(str(item["id"]), callback_data=f"userbot:order:{int(item['id'])}"))
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"userbot:orders:list:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="userbot:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"userbot:orders:list:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:orders_menu")])
    await _edit_or_send(
        update,
        "🔹 لیست سفارشات\n"
        f"🔸 تعداد سفارشات: {total_count}\n"
        f"🔸 مجموع حجم سفارشات(GB): {total_gb:,}\n"
        f"🔸 مجموع ارزش سفارشات: {money_text}\n"
        "❖ ⬩----------------------------------⬩ ❖\n"
        f"صفحه: {page}/{pages}",
        InlineKeyboardMarkup(rows),
    )


async def _send_order_detail(update: Update, business: Any, actor: int, order_id: int) -> None:
    order = business.order_admin(actor, order_id=order_id)
    rows = [[InlineKeyboardButton("👤 پروفایل کاربر", callback_data=f"userbot:user:{int(order['customer_id'])}")]]
    if order["status"] == "paid":
        rows.append([InlineKeyboardButton("🚀 تحویل/تلاش مجدد", callback_data=f"biz:fulfill:{order_id}")])
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:orders_menu")])
    await _edit_or_send(
        update,
        f"📄 سفارش #{order_id}\n"
        f"👤 خریدار: {order.get('display_name') or '-'}\n"
        f"📅 تاریخ: {order.get('created_at') or '-'}\n"
        f"📦 پلن: {order.get('plan_name') or '-'}\n"
        f"📂 دسته: {order.get('category_title') or 'بدون دسته'}\n"
        f"🛰 سرور خرید: {order.get('selected_server_label') or 'پیش‌فرض/قدیمی'}\n"
        f"💰 قیمت: {int(order.get('amount') or 0):,} {order.get('currency') or ''}\n"
        f"📊 وضعیت: {order.get('status') or '-'}\n"
        f"🔁 نوع: {order.get('operation') or 'purchase'}",
        InlineKeyboardMarkup(rows),
    )


async def _send_payments_page(
    update: Update,
    business: Any,
    actor: int,
    filter_type: str,
    page: int = 1,
) -> None:
    status = filter_type if filter_type in ("approved", "rejected", "pending") else None
    kind = filter_type if filter_type in ("card", "crypto") else None
    source = filter_type if filter_type in ("wallet_topup", "wallet_order") else None
    items = business.list_payments_admin(
        actor,
        status=status,
        kind=kind,
        source=source,
    )
    selected, page, pages = _page(items, page)
    title = {
        "approved": "تراکنشات تایید شده ✅",
        "rejected": "تراکنشات رد شده 🚫",
        "pending": "تراکنشات در انتظار ⏳",
        "card": "تراکنشات کارت به کارت 💳",
        "crypto": "تراکنشات Crypto 🪙",
        "wallet_topup": "شارژهای کیف پول ➕",
        "wallet_order": "پرداخت‌های مستقیم کیف پول 💰",
    }.get(filter_type, "همه تراکنشات")
    money: dict[str, int] = {}
    for item in items:
        currency = str(item.get("currency") or "")
        money[currency] = money.get(currency, 0) + int(item.get("amount") or 0)
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for item in selected:
        icon = str(item.get("provider_icon") or "💳")
        current.append(InlineKeyboardButton(
            f"{icon} {int(item['id'])}",
            callback_data=f"userbot:pay:detail:{item['payment_key']}",
        ))
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(
            "➡️",
            callback_data=f"userbot:payments:list:{filter_type}:{page-1}",
        ))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="userbot:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton(
            "⬅️",
            callback_data=f"userbot:payments:list:{filter_type}:{page+1}",
        ))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:payments_menu")])
    await _edit_or_send(
        update,
        f"🔹 {title}\n"
        f"🔸 تعداد تراکنشات: {len(items)}\n"
        f"🔸 مبلغ تراکنشات: {' | '.join(f'{v:,} {k}' for k,v in money.items()) or '0'}\n"
        "❖ ⬩----------------------------------⬩ ❖\n"
        f"صفحه: {page}/{pages}",
        InlineKeyboardMarkup(rows),
    )


async def _send_payment_detail(
    update: Update,
    business: Any,
    actor: int,
    payment_key: str | int,
) -> None:
    pay = business.payment_admin(actor, payment_key=payment_key)
    status_title = {
        "approved": "✅ تایید شده",
        "rejected": "❌ رد شده",
        "pending": "⏳ در انتظار",
    }.get(str(pay.get("status")), str(pay.get("status")))
    source_title = {
        "order": "سفارش",
        "wallet_topup": "شارژ کیف پول",
        "wallet_order": "پرداخت مستقیم کیف پول",
    }.get(str(pay.get("source") or ""), str(pay.get("source") or "-"))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "👤 پروفایل کاربر",
            callback_data=f"userbot:user:{int(pay['customer_id'])}",
        )]
    ]
    if pay["status"] == "pending" and pay.get("source") in ("order", "wallet_topup"):
        rows.append([
            InlineKeyboardButton(
                "✅ تایید",
                callback_data=f"userbot:pay:review:{pay['payment_key']}:yes",
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"userbot:pay:review:{pay['payment_key']}:no",
            ),
        ])
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:payments_menu")])
    await _edit_or_send(
        update,
        f"◈ شناسه تراکنش: {pay['payment_key']}\n"
        f"👤 کاربر: {pay.get('display_name') or '-'}\n"
        f"◈ نام کاربری: {'@' + str(pay.get('username')).lstrip('@') if pay.get('username') else '-'}\n"
        f"◈ شناسه کاربر: {pay.get('telegram_user_id') or '-'}\n"
        f"◈ تاریخ تراکنش: {pay.get('created_at') or '-'}\n"
        f"◈ مبلغ تراکنش: {int(pay.get('amount') or 0):,} {pay.get('currency') or ''}\n"
        "❖ • -------------------------- • ❖\n"
        f"◈ وضعیت: {status_title}\n"
        f"◈ نوع: {source_title}\n"
        f"◈ Provider: {pay.get('provider_icon') or ''} {pay.get('provider_title') or pay.get('payment_title') or '-'}\n"
        f"◈ پیگیری: {pay.get('reference') or ('تصویر رسید' if pay.get('telegram_file_id') else '-')}\n"
        f"◈ یادداشت بررسی: {pay.get('review_note') or '-'}",
        InlineKeyboardMarkup(rows),
    )
    media = business.payment_receipt_media_admin(
        actor, payment_key=pay["payment_key"]
    )
    if media is not None and update.effective_message is not None:
        receipt_file = BytesIO(bytes(media["media"]))
        receipt_file.name = "receipt.jpg"
        try:
            await update.effective_message.reply_photo(
                photo=receipt_file,
                caption=f"🧾 تصویر رسید {pay['payment_key']}",
            )
        except (BadRequest, Forbidden, NetworkError, TimedOut):
            pass


def _gift_stats(business: Any, actor: int) -> dict[str, int]:
    vouchers = business.list_gift_vouchers_admin(actor)
    redemptions = business.gift_redemptions_admin(actor)
    now = utcnow()
    active = expired = full = 0
    for item in vouchers:
        is_expired = False
        if item.get("expires_at"):
            try:
                from Shared.timeutils import parse_utc
                is_expired = parse_utc(str(item["expires_at"])) <= now
            except Exception:
                pass
        is_full = int(item.get("used_count") or 0) >= int(item.get("max_uses") or 1)
        if is_expired:
            expired += 1
        elif is_full:
            full += 1
        elif item.get("status") == "active":
            active += 1
    return {
        "total": len(vouchers),
        "active": active,
        "expired": expired,
        "full": full,
        "redemptions": len(redemptions),
    }


async def _send_gifts_menu(update: Update, business: Any, actor: int) -> None:
    stats = _gift_stats(business, actor)
    total_amount = sum(
        int(x.get("amount") or 0)
        for x in business.gift_redemptions_admin(actor)
    )
    await _edit_or_send(
        update,
        "🎁 مدیریت هدایا\n"
        "❖ ◈━━━━━━━━━━━━━━━━━━━━◈ ❖\n"
        f"🟢 کوپن‌های فعال: {stats['active']}\n"
        f"📦 کل کوپن‌ها: {stats['total']}\n"
        f"🎯 مصرف‌شده: {stats['redemptions']} بار\n"
        f"💰 مجموع هدیه مصرف‌شده: {total_amount:,}\n\n"
        "از دکمه‌های زیر برای ساخت، گزارش و مدیریت کمپین هدیه استفاده کنید.",
        build_gifts_menu_keyboard(),
    )


async def _send_coupons(update: Update, business: Any, actor: int) -> None:
    items = business.list_gift_vouchers_admin(actor)
    rows = [
        [InlineKeyboardButton(
            f"{'🟢' if x['status']=='active' else '⚫'} {x['code']} | {x['used_count']}/{x['max_uses']}",
            callback_data=f"userbot:gifts:coupon:{int(x['id'])}",
        )]
        for x in items
    ]
    rows.extend([
        [InlineKeyboardButton("افزودن کوپن جدید➕", callback_data="userbot:gifts:coupons:add")],
        [InlineKeyboardButton("🧩 ساخت گروهی کد هدیه", callback_data="userbot:gifts:bulk")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:gifts_menu")],
    ])
    await _edit_or_send(
        update,
        "💼 کوپن شارژ کیف پول\n"
        f"◈ تعداد کل: {len(items)}\n"
        f"◈ فعال: {sum(1 for x in items if x['status']=='active')}\n"
        f"◈ غیرفعال: {sum(1 for x in items if x['status']!='active')}",
        InlineKeyboardMarkup(rows),
    )


async def _send_coupon_detail(update: Update, business: Any, actor: int, coupon_id: int) -> None:
    item = business.gift_voucher_admin(actor, voucher_id=coupon_id)
    redemptions = business.gift_redemptions_admin(actor, voucher_id=coupon_id)
    username_row = BotRepository(business.conn).get_by_tenant_role(
        business.tenant_id, "user"
    ) or {}
    username = str(username_row.get("telegram_username") or "").strip().lstrip("@")
    deep_link = (
        f"https://t.me/{username}?start=gift_{item['code']}"
        if username else "یوزرنیم UserBot ثبت نشده است"
    )
    rows = [
        [InlineKeyboardButton(
            "⏸ خاموش کردن کوپن" if item["status"] == "active" else "▶️ روشن کردن کوپن",
            callback_data=f"userbot:gifts:coupon:toggle:{coupon_id}",
        )],
        [
            InlineKeyboardButton(
                "✏️ ویرایش کد",
                callback_data=f"userbot:gifts:coupon:set_code:{coupon_id}",
            ),
            InlineKeyboardButton(
                "🎁 ویرایش مبلغ",
                callback_data=f"userbot:gifts:coupon:set_amount:{coupon_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 ویرایش سقف مصرف",
                callback_data=f"userbot:gifts:coupon:set_limit:{coupon_id}",
            ),
            InlineKeyboardButton(
                "🕒 ویرایش انقضا",
                callback_data=f"userbot:gifts:coupon:set_exp:{coupon_id}",
            ),
        ],
        [InlineKeyboardButton("📜 گزارش مصرف این کوپن", callback_data=f"userbot:gifts:redemptions:{coupon_id}")],
        [InlineKeyboardButton("📣 متن تبلیغ همین کوپن", callback_data=f"userbot:gifts:coupon:campaign:{coupon_id}")],
        [InlineKeyboardButton("🗑 حذف کوپن", callback_data=f"userbot:gifts:coupon:delete:{coupon_id}")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:gifts:coupons")],
    ]
    await _edit_or_send(
        update,
        f"🏷 کد: {item['code']}\n"
        "❖ ◈━━━━━━━━━━━━━━━━━━━━◈ ❖\n"
        f"◈ وضعیت: {item['status']}\n"
        f"◈ هدیه کیف پول: {int(item['amount']):,} {item['currency']}\n"
        f"◈ استفاده: {int(item['used_count'] or 0)} از {int(item['max_uses'])}\n"
        f"◈ مصرف ثبت‌شده: {len(redemptions)}\n"
        f"◈ انقضا: {item.get('expires_at') or 'نامحدود'}\n"
        f"◈ دیپ‌لینک: {deep_link}",
        InlineKeyboardMarkup(rows),
        disable_web_page_preview=True,
    )


async def _send_referral_menu(update: Update, business: Any, actor: int) -> None:
    settings = business.growth_settings(actor)
    stats = business.referral_admin_stats(actor)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 داشبورد رفرال", callback_data="userbot:referral:dashboard")],
        [
            InlineKeyboardButton("⚙️ تنظیمات", callback_data="userbot:referral:settings"),
            InlineKeyboardButton(
                f"🎁 فعال/غیرفعال | {_bool_icon(settings.get('referral_enabled'))}",
                callback_data="userbot:referral:toggle",
            ),
        ],
        [
            InlineKeyboardButton("👥 لیست دعوت‌ها", callback_data="userbot:referral:list:1"),
            InlineKeyboardButton("💰 لیست پاداش‌ها", callback_data="userbot:referral:rewards:1"),
        ],
        [InlineKeyboardButton("🧾 پاداش دستی", callback_data="userbot:referral:manual")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])
    await _edit_or_send(
        update,
        "🤝 مدیریت رفرال (دعوت دوستان)\n"
        f"وضعیت: {'فعال' if settings.get('referral_enabled') else 'خاموش'}\n"
        f"🧪 پاداش تست: {_bool_icon(settings.get('referral_trial_reward_enabled'))} "
        f"{int(settings.get('referral_trial_reward') or 0):,} "
        f"{settings.get('referral_currency') or ''}\n"
        f"🛒 پاداش خرید: {_bool_icon(settings.get('referral_purchase_reward_enabled'))} "
        f"{int(settings.get('referral_purchase_reward') or 0):,} "
        f"{settings.get('referral_currency') or ''}\n"
        f"👥 دعوت‌ها: {int(stats.get('total_referrals') or 0)}\n"
        f"💰 مجموع پاداش: {int(stats.get('total_reward_cost') or 0):,} "
        f"{settings.get('referral_currency') or ''}",
        kb,
    )


def _ticket_bucket(status: str) -> set[str]:
    if status == "pending":
        return {"open"}
    if status == "open":
        return {"answered"}
    return {"closed"}


def _ticket_status_label(status: object) -> str:
    return {
        "open": "📨 در انتظار",
        "answered": "📬 باز / پاسخ‌داده",
        "closed": "📩 بسته",
    }.get(str(status or ""), str(status or "-"))


def _ticket_thread_admin_text(
    ticket: dict[str, Any],
    messages: list[dict[str, Any]],
) -> str:
    lines = [
        f"🎫 تیکت #{int(ticket['id'])}",
        f"👤 {ticket.get('display_name') or '-'}",
        f"🔹 @{str(ticket.get('username') or '-').lstrip('@')}",
        f"🔸 Telegram ID: {ticket.get('telegram_user_id') or '-'}",
        f"وضعیت: {_ticket_status_label(ticket.get('status'))}",
        f"موضوع: {ticket.get('subject') or '-'}",
        "",
    ]
    for item in messages[-40:]:
        who = "👤 کاربر" if item.get("sender_type") == "user" else "🛟 پشتیبانی"
        sender = str(item.get("sender_name") or "").strip()
        lines.append(f"{who}{' · ' + sender if sender else ''}:")
        body = str(item.get("message_text") or "").strip()
        if body:
            lines.append(body)
        if item.get("has_media"):
            lines.append("🖼 تصویر پیوست دارد")
        lines.append("")
    if not messages:
        lines.append("پیامی ثبت نشده است.")
    return "\n".join(lines).strip()


def ticket_reply_skip_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩رد کردن", callback_data="userbot:ticketreply:skip")],
        [InlineKeyboardButton("❌لغو", callback_data="userbot:ticketreply:cancel")],
    ])


def ticket_reply_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ارسال", callback_data="userbot:ticketreply:send"),
            InlineKeyboardButton("✏️ویرایش", callback_data="userbot:ticketreply:edit"),
        ],
        [InlineKeyboardButton("❌لغو", callback_data="userbot:ticketreply:cancel")],
    ])


def _ticket_reply_preview_text(flow: dict[str, Any]) -> str:
    return (
        "👁 پیش‌نمایش پاسخ تیکت\n\n"
        f"{str(flow.get('reply_text') or '').strip()}\n\n"
        f"🖼 تصویر: {'دارد' if flow.get('photo_file_id') else 'ندارد'}"
    )


async def _send_tickets(update: Update, business: Any, actor: int, status: str | None = None, page: int = 1, customer_id: int = 0) -> None:
    items = business.list_tickets_admin(actor)
    if customer_id > 0:
        items = [x for x in items if int(x["customer_id"]) == customer_id]
    if status:
        allowed = _ticket_bucket(status)
        items = [x for x in items if str(x["status"]) in allowed]
    selected, page, pages = _page(items, page)
    if status is None:
        pending = sum(1 for x in items if x["status"] == "open")
        answered = sum(1 for x in items if x["status"] == "answered")
        closed = sum(1 for x in items if x["status"] == "closed")
        text = (
            "📑 مدیریت تیکت‌ها\n"
            f"📨 در انتظار: {pending}\n"
            f"📬 باز/پاسخ‌داده: {answered}\n"
            f"📩 بسته: {closed}"
        )
        await _edit_or_send(update, text, build_tickets_menu_keyboard())
        return
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for item in selected:
        cur.append(InlineKeyboardButton(
            f"#{item['id']}",
            callback_data=f"userbot:ticket:detail:{int(item['id'])}:{status}:{page}",
        ))
        if len(cur) == 3:
            rows.append(cur); cur = []
    if cur:
        rows.append(cur)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"userbot:tickets:list:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="userbot:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"userbot:tickets:list:{status}:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:tickets_menu")])
    await _edit_or_send(update, f"📑 تیکت‌ها\nتعداد: {len(items)}", InlineKeyboardMarkup(rows))


async def _send_ticket_admin_detail(
    update: Update,
    business: Any,
    actor: int,
    *,
    ticket_id: int,
    list_status: str,
    page: int,
) -> None:
    item = business.ticket_admin(actor, ticket_id=int(ticket_id))
    messages = business.ticket_messages_admin(actor, ticket_id=int(ticket_id))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "👤 پروفایل کاربر",
            callback_data=f"userbot:user:{int(item['customer_id'])}",
        )]
    ]
    media_buttons = [
        InlineKeyboardButton(
            f"🖼 تصویر پیام #{int(message['id'])}",
            callback_data=(
                f"userbot:ticket:media:{int(ticket_id)}:"
                f"{int(message['id'])}:{list_status}:{int(page)}"
            ),
        )
        for message in messages
        if message.get("has_media")
    ]
    for index in range(0, len(media_buttons), 2):
        rows.append(media_buttons[index:index+2])
    if str(item.get("status") or "") != "closed":
        rows.append([InlineKeyboardButton(
            "📩پاسخ",
            callback_data=(
                f"userbot:ticket:reply:{int(ticket_id)}:"
                f"{list_status}:{int(page)}"
            ),
        )])
        rows.append([InlineKeyboardButton(
            "📪 بستن تیکت",
            callback_data=(
                f"userbot:ticket:status:{int(ticket_id)}:closed:"
                f"{list_status}:{int(page)}"
            ),
        )])
    else:
        rows.append([InlineKeyboardButton(
            "📬 باز کردن تیکت",
            callback_data=(
                f"userbot:ticket:status:{int(ticket_id)}:open:"
                f"{list_status}:{int(page)}"
            ),
        )])
    rows.append([InlineKeyboardButton(
        "🔙بازگشت",
        callback_data=f"userbot:tickets:list:{list_status}:{int(page)}",
    )])
    await _edit_or_send(
        update,
        _ticket_thread_admin_text(item, messages),
        InlineKeyboardMarkup(rows),
    )


def _sibling_user_bot_token(business: Any) -> str:
    if business.secret_cipher is None:
        raise TenantBusinessError("UserBot encryption is unavailable")
    row = BotRepository(business.conn).get_by_tenant_role(business.tenant_id, "user")
    if row is None or row.get("status") != "active":
        raise TenantBusinessError("UserBot is not active")
    plain = business.secret_cipher.decrypt(str(row["encrypted_token"]))
    if fingerprint_token(plain) != str(row["token_fingerprint"]):
        raise TenantBusinessError("UserBot credential integrity failed")
    return plain


async def _send_via_userbot(
    business: Any,
    chat_id: int | str,
    *,
    text: str,
    photo_bytes: bytes | None = None,
) -> None:
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=chat_id, text=text)
            payload = bytes(photo_bytes or b"")
            if payload:
                stream = BytesIO(payload)
                stream.name = "ticket-reply.jpg"
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=stream,
                    caption="🖼 تصویر پاسخ پشتیبانی",
                )
    finally:
        token = ""


async def _download_admin_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> BytesIO:
    tg_file = await context.bot.get_file(file_id)
    raw = bytes(await tg_file.download_as_bytearray())
    stream = BytesIO(raw)
    stream.name = "telegram-media"
    stream.seek(0)
    return stream


async def _send_broadcast_to_targets(
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
    segment: str,
    body_text: str,
    photo_file_id: str = "",
) -> dict[str, int]:
    targets = business.broadcast_targets_admin(actor, segment=segment)
    now = iso_utc(utcnow())
    cursor = business.conn.execute(
        "INSERT INTO tenant_broadcast_runs "
        "(tenant_id, segment, message_kind, target_count, sent_count, failed_count, created_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?)",
        (
            business.tenant_id,
            segment,
            "photo" if photo_file_id else "text",
            len(targets),
            now,
        ),
    )
    run_id = int(cursor.lastrowid or 0)
    business.conn.commit()

    media_bytes: bytes | None = None
    if photo_file_id:
        stream = await _download_admin_file(context, photo_file_id)
        media_bytes = stream.getvalue()

    sent = failed = recovered = unreachable = temporary = other = 0
    reusable_userbot_file_id = ""
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            for target in targets:
                chat_id = int(target["telegram_user_id"])
                for attempt in range(3):
                    try:
                        if photo_file_id:
                            if reusable_userbot_file_id:
                                result = await bot.send_photo(
                                    chat_id=chat_id,
                                    photo=reusable_userbot_file_id,
                                    caption=body_text,
                                )
                            else:
                                assert media_bytes is not None
                                stream = BytesIO(media_bytes)
                                stream.name = "broadcast-image.jpg"
                                result = await bot.send_photo(
                                    chat_id=chat_id,
                                    photo=stream,
                                    caption=body_text,
                                )
                                photos = list(getattr(result, "photo", None) or [])
                                if photos:
                                    reusable_userbot_file_id = str(
                                        photos[-1].file_id
                                    )
                        else:
                            await bot.send_message(chat_id=chat_id, text=body_text)
                        sent += 1
                        if attempt:
                            recovered += 1
                        break
                    except RetryAfter as exc:
                        if attempt >= 2:
                            failed += 1
                            temporary += 1
                            break
                        wait = getattr(exc, "retry_after", 1)
                        if hasattr(wait, "total_seconds"):
                            wait = wait.total_seconds()
                        await asyncio.sleep(min(max(float(wait), 0.5), 30.0))
                    except (TimedOut, NetworkError):
                        if attempt >= 2:
                            failed += 1
                            temporary += 1
                            break
                        await asyncio.sleep(float(attempt + 1))
                    except (Forbidden, BadRequest):
                        failed += 1
                        unreachable += 1
                        break
                    except Exception:
                        failed += 1
                        other += 1
                        break
    finally:
        token = ""

    business.conn.execute(
        "UPDATE tenant_broadcast_runs SET sent_count=?, failed_count=?, "
        "finished_at=? WHERE id=? AND tenant_id=?",
        (
            sent,
            failed,
            iso_utc(utcnow()),
            run_id,
            business.tenant_id,
        ),
    )
    business.conn.commit()
    return {
        "target": len(targets),
        "sent": sent,
        "failed": failed,
        "recovered": recovered,
        "unreachable": unreachable,
        "temporary": temporary,
        "other": other,
    }


def _broadcast_result_text(result: dict[str, int]) -> str:
    return (
        "✅ ارسال همگانی تمام شد.\n"
        f"🎯 کل هدف: {int(result.get('target') or 0)}\n"
        f"✅ موفق: {int(result.get('sent') or 0)}\n"
        f"❌ ناموفق: {int(result.get('failed') or 0)}\n"
        f"♻️ بازیابی‌شده با تلاش مجدد: {int(result.get('recovered') or 0)}\n"
        f"🚫 غیرقابل دسترس/مسدود: {int(result.get('unreachable') or 0)}\n"
        f"⏳ خطای موقت: {int(result.get('temporary') or 0)}\n"
        f"⚠️ سایر خطاها: {int(result.get('other') or 0)}"
    )




def _channel_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    value = context.user_data.get(CHANNEL_DRAFT_KEY)
    if not isinstance(value, dict):
        value = {"kind": "", "text": "", "file_id": "", "buttons": []}
        context.user_data[CHANNEL_DRAFT_KEY] = value
    return value


def _channel_markup(draft: dict[str, Any]) -> InlineKeyboardMarkup | None:
    buttons = list(draft.get("buttons") or [])
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(item["text"]), url=str(item["url"]))]
        for item in buttons[:MAX_CHANNEL_BUTTONS]
    ])


def _channel_admin_menu(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ ساخت پست جدید", callback_data="channelpost:new")]]
    if draft.get("kind"):
        rows.extend([
            [InlineKeyboardButton("✏️ ویرایش پست", callback_data="channelpost:edit")],
            [InlineKeyboardButton("🔘 افزودن دکمه", callback_data="channelpost:button")],
            [InlineKeyboardButton("👁 پیش‌نمایش", callback_data="channelpost:preview")],
            [InlineKeyboardButton("🚀 انتشار در کانال", callback_data="channelpost:publish")],
            [InlineKeyboardButton("🧹 پاک کردن دکمه‌ها", callback_data="channelpost:clear_buttons")],
        ])
    rows.extend([
        [InlineKeyboardButton("⚙️ تنظیم کانال مقصد", callback_data="channelpost:set")],
        [InlineKeyboardButton("🔙 بازگشت به مدیریت ربات کاربران", callback_data="userbot:menu")],
    ])
    return InlineKeyboardMarkup(rows)


async def _show_channel_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
) -> None:
    settings = business.userbot_settings_admin(actor)
    draft = _channel_draft(context)
    channel = str(settings.get("channel_id") or "").strip() or "تنظیم نشده"
    kind = {"text": "متنی", "photo": "عکس + کپشن", "video": "ویدئو + کپشن"}.get(
        str(draft.get("kind") or ""), "هنوز ساخته نشده"
    )
    await _edit_or_send(
        update,
        "📢 مدیریت پست کانال\n\n"
        f"📝 نوع پست: {kind}\n"
        f"🔘 تعداد دکمه‌ها: {len(draft.get('buttons') or [])}\n"
        f"🎯 مقصد: {channel}\n\n"
        "انتشار با توکن ربات کاربران انجام می‌شود.",
        _channel_admin_menu(draft),
    )


async def _channel_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    draft = _channel_draft(context)
    kind = str(draft.get("kind") or "")
    text = str(draft.get("text") or "")
    markup = _channel_markup(draft)
    message = update.callback_query.message if update.callback_query else update.effective_message
    if not kind:
        await message.reply_text("❌ هنوز محتوای پست ساخته نشده است.")
    elif kind == "text":
        await message.reply_text(text or " ", parse_mode="HTML", reply_markup=markup)
    elif kind == "photo":
        await message.reply_photo(
            photo=str(draft["file_id"]),
            caption=text or None,
            parse_mode="HTML",
            reply_markup=markup,
        )
    elif kind == "video":
        await message.reply_video(
            video=str(draft["file_id"]),
            caption=text or None,
            parse_mode="HTML",
            reply_markup=markup,
        )


async def _publish_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
) -> None:
    draft = _channel_draft(context)
    settings = business.userbot_settings_admin(actor)
    target = str(settings.get("channel_id") or "").strip()
    if not target:
        raise TenantBusinessError("channel is not configured")
    kind = str(draft.get("kind") or "")
    if not kind:
        raise TenantBusinessError("channel post is empty")
    text = str(draft.get("text") or "")
    markup = _channel_markup(draft)
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            if kind == "text":
                sent = await bot.send_message(
                    chat_id=target,
                    text=text or " ",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                media = await _download_admin_file(
                    context, str(draft.get("file_id") or "")
                )
                if kind == "photo":
                    sent = await bot.send_photo(
                        chat_id=target,
                        photo=media,
                        caption=text or None,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                else:
                    sent = await bot.send_video(
                        chat_id=target,
                        video=media,
                        caption=text or None,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
    finally:
        token = ""
    context.user_data.pop(CHANNEL_DRAFT_KEY, None)
    context.user_data.pop(FLOW_KEY, None)
    await update.effective_chat.send_message(
        "✅ پست با موفقیت توسط ربات کاربران در کانال منتشر شد.\n"
        f"🆔 Message ID: {int(sent.message_id)}"
    )




async def _settings_root(update: Update, business: Any, actor: int) -> None:
    settings = business.userbot_settings_admin(actor)
    await _edit_or_send(
        update,
        "⚙️ تنظیمات ربات کاربران\nیکی از بخش‌های زیر را انتخاب کنید:",
        _settings_menu(settings),
    )


def _setting_toggle_keyboard(title: str, settings: dict[str, Any], keys: list[tuple[str, str]], back: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{label} | {_bool_icon(settings.get(key))}", callback_data=f"userbot:settings:toggle:{key}:{back}")]
        for key, label in keys
    ]
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data=back)])
    return InlineKeyboardMarkup(rows)


async def _settings_section(update: Update, business: Any, actor: int, section: str) -> None:
    """Render the tenant-safe subset of SellBot UserBot settings.

    Button labels and callback namespaces intentionally follow Hiddify-SellBot.
    Provider-specific features that do not exist in WhiteLabel (for example
    ZarinPal/PerfectMoney/SMS webhook credentials) are not exposed as dead
    buttons; card and crypto are managed through the generic tenant payment
    model below.
    """
    s = business.userbot_settings_admin(actor)

    if section == "subscription":
        rows = [
            [InlineKeyboardButton(
                f"نمایش لینک صفحه یوزر هیدیفای | {_bool_icon(s.get('show_user_page_link'))}",
                callback_data="userbot:settings:subscription:show_user_page_link",
            )],
            [InlineKeyboardButton(
                f"نمایش نام کاربری | {_bool_icon(s.get('show_username'))}",
                callback_data="userbot:settings:subscription:show_username",
            )],
            [InlineKeyboardButton(
                f"تصادفی کردن کانفیگ‌ها | {_bool_icon(s.get('shuffle_configs'))}",
                callback_data="userbot:settings:subscription:shuffle_configs",
            )],
            [InlineKeyboardButton(
                f"تصادفی کردن چینش سرورها | {_bool_icon(s.get('shuffle_server_layout'))}",
                callback_data="userbot:settings:subscription:shuffle_server_layout",
            )],
            [InlineKeyboardButton(
                f"تصادفی کردن چینش کانفیگ‌ها | {_bool_icon(s.get('shuffle_config_layout'))}",
                callback_data="userbot:settings:subscription:shuffle_config_layout",
            )],
            [InlineKeyboardButton(
                "🔔یادآور وضعیت اشتراک",
                callback_data="userbot:settings:subscription:sub_status_reminder",
            )],
            [InlineKeyboardButton(
                "🎊مشخصات اشتراک تستی",
                callback_data="userbot:settings:subscription:trial_spec",
            )],
            [InlineKeyboardButton(
                "🔄بازنشانی تست رایگان",
                callback_data="userbot:settings:subscription:reset_free_trial",
            )],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, "🛍 تنظیمات اشتراک", InlineKeyboardMarkup(rows))
        return

    if section == "sub_link_status":
        rows = [
            [InlineKeyboardButton(
                f"کانفیگ مستقیم | {_bool_icon(s.get('show_direct_config'))}",
                callback_data="userbot:settings:sub_link_status:show_direct_config",
            )],
            [InlineKeyboardButton(
                f"اشتراک خودکار | {_bool_icon(s.get('show_auto_sub_link'))}",
                callback_data="userbot:settings:sub_link_status:show_auto_sub_link",
            )],
            [InlineKeyboardButton(
                f"لینک اشتراک | {_bool_icon(s.get('show_sub_link'))}",
                callback_data="userbot:settings:sub_link_status:show_sub_link",
            )],
            [InlineKeyboardButton(
                f"لینک اشتراک b64 | {_bool_icon(s.get('show_sub_link_b64'))}",
                callback_data="userbot:settings:sub_link_status:show_sub_link_b64",
            )],
            [InlineKeyboardButton(
                f"لینک اشتراک هوشمند | {_bool_icon(s.get('show_multi_server'))}",
                callback_data="userbot:settings:sub_link_status:show_multi_server",
            )],
            [InlineKeyboardButton(
                f"لینک اشتراک هوشمند b64 | {_bool_icon(s.get('show_multi_server_b64'))}",
                callback_data="userbot:settings:sub_link_status:show_multi_server_b64",
            )],
            [InlineKeyboardButton(
                "🌐 تنظیم دامنه لینک اشتراک هوشمند",
                callback_data="userbot:settings:sub_link_status:set_base_url",
            )],
            [InlineKeyboardButton(
                "🔐 راهنمای SSL دامنه",
                callback_data="userbot:settings:sub_link_status:ssl_help",
            )],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        base_url = str(s.get("smart_base_url") or "").strip() or "پیش‌فرض سرور"
        await _edit_or_send(
            update,
            "📁 وضعیت نمایش لینک اشتراک\n"
            f"🌐 دامنه Smart Link: {base_url}\n"
            "ℹ️ دامنه بالا فقط Smart Link را تغییر می‌دهد؛ "
            "لینک معمولی Subscription مستقیماً از پنل ساخته می‌شود.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "buy_renew":
        policy = str(s.get("renew_policy") or "advanced")
        policy_title = {
            "advanced": "پیشرفته",
            "default": "پیشفرض",
            "fair": "منصفانه",
        }.get(policy, "پیشرفته")
        rows = [
            [InlineKeyboardButton(
                f"امکان خرید اشتراک | {_bool_icon(s.get('enable_buy'))}",
                callback_data="userbot:settings:buy_renew:enable_buy",
            )],
            [InlineKeyboardButton(
                f"امکان تمدید اشتراک | {_bool_icon(s.get('enable_renew'))}",
                callback_data="userbot:settings:buy_renew:enable_renew",
            )],
            [InlineKeyboardButton(
                f"دکمه تمدید اشتراک در منوی اصلی | {_bool_icon(s.get('show_renew_in_main_menu'))}",
                callback_data="userbot:settings:buy_renew:show_renew_in_main_menu",
            )],
            [InlineKeyboardButton(
                f"تنظیم شیوه تمدید | {policy_title}",
                callback_data="userbot:settings:buy_renew:renew_mode_info",
            )],
            [
                InlineKeyboardButton(
                    "ستون‌های پلن‌ها",
                    callback_data="userbot:settings:buy_renew:plan_columns:menu",
                ),
                InlineKeyboardButton(
                    "ستون‌های سرورها",
                    callback_data="userbot:settings:buy_renew:server_columns:menu",
                ),
            ],
            [InlineKeyboardButton(
                f"حجم نامحدود∞ | {_bool_icon(s.get('renew_unlimited_volume'))}",
                callback_data="userbot:settings:buy_renew:renew_unlimited_volume",
            )],
            [InlineKeyboardButton(
                f"زمان نامحدود∞ | {_bool_icon(s.get('renew_unlimited_time'))}",
                callback_data="userbot:settings:buy_renew:renew_unlimited_time",
            )],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(
            update,
            "🛒 تنظیمات خرید و تمدید\n"
            f"⚙️ سیاست تمدید فعلی: {policy_title}",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "renew_policy":
        policy = str(s.get("renew_policy") or "advanced")
        volume_mode = str(s.get("renew_volume_mode") or "reset")
        time_mode = str(s.get("renew_time_mode") or "reset")
        policy_title = {
            "advanced": "پیشرفته",
            "default": "پیشفرض",
            "fair": "منصفانه",
        }.get(policy, "پیشرفته")
        volume_text = (
            "افزایشی (باقیمانده + پلن جدید)"
            if volume_mode == "add"
            else "ریست (فقط پلن جدید)"
        )
        time_text = (
            "افزایشی (باقیمانده + پلن جدید)"
            if time_mode == "add"
            else "ریست (فقط پلن جدید)"
        )
        rows = [
            [
                InlineKeyboardButton(
                    f"پیشفرض | {'✅' if policy == 'default' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_policy:default",
                ),
                InlineKeyboardButton(
                    f"پیشرفته | {'✅' if policy == 'advanced' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_policy:advanced",
                ),
                InlineKeyboardButton(
                    f"منصفانه | {'✅' if policy == 'fair' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_policy:fair",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"حجم افزایشی | {'✅' if volume_mode == 'add' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_rollover:volume:add",
                ),
                InlineKeyboardButton(
                    f"حجم ریست | {'✅' if volume_mode == 'reset' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_rollover:volume:reset",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"زمان افزایشی | {'✅' if time_mode == 'add' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_rollover:time:add",
                ),
                InlineKeyboardButton(
                    f"زمان ریست | {'✅' if time_mode == 'reset' else '❌'}",
                    callback_data="userbot:settings:buy_renew:renew_rollover:time:reset",
                ),
            ],
            [InlineKeyboardButton(
                f"حداکثر زمان مجاز برای تمدید📊 | {int(s.get('renew_max_days') or 3)} روز",
                callback_data="userbot:settings:buy_renew:renew_limit:days",
            )],
            [InlineKeyboardButton(
                f"حداکثر حجم باقی‌مانده📆 | {int(s.get('renew_max_remaining_gb') or 3)} GB",
                callback_data="userbot:settings:buy_renew:renew_limit:usage",
            )],
            [InlineKeyboardButton(
                "🔙بازگشت",
                callback_data="userbot:settings:buy_renew",
            )],
        ]
        await _edit_or_send(
            update,
            "تنظیم شیوه تمدید\n"
            f"⚙️ پروفایل فعلی: {policy_title}\n"
            f"📦 حالت حجم در تمدید: {volume_text}\n"
            f"⏳ حالت زمان در تمدید: {time_text}\n"
            f"📊 مقدار فعلی زمان: {int(s.get('renew_max_days') or 3)} روز\n"
            f"📆 مقدار فعلی حجم: {int(s.get('renew_max_remaining_gb') or 3)} گیگابایت",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "ui":
        current_theme = str(s.get("button_theme") or "smart")
        theme_meta = {
            "smart": (
                "✨ هوشمند",
                "خرید و تایید سبز، هشدار قرمز و مسیرهای اصلی آبی",
            ),
            "shop": (
                "🛒 فروشگاهی",
                "خرید، کیف پول، هدیه و پرداخت پررنگ‌تر نمایش داده می‌شوند",
            ),
            "pro": (
                "💼 حرفه‌ای",
                "رنگ فقط برای اکشن‌های مهم و مسیرهای مدیریتی استفاده می‌شود",
            ),
            "minimal": (
                "🕊 مینیمال",
                "فقط تاییدهای مهم و عملیات خطرناک رنگ می‌گیرند",
            ),
        }
        rows = [
            [InlineKeyboardButton(
                f"رنگی بودن دکمه‌ها | {_bool_icon(s.get('colored_buttons'))}",
                callback_data="userbot:settings:ui:colored_buttons",
            )],
        ]
        for key, (title, _description) in theme_meta.items():
            rows.append([
                InlineKeyboardButton(
                    f"{'✅ ' if current_theme == key else '▫️ '}{title}",
                    callback_data=f"userbot:settings:ui:theme:{key}",
                )
            ])
        rows.append([
            InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")
        ])
        current_title, current_description = theme_meta.get(
            current_theme, theme_meta["smart"]
        )
        await _edit_or_send(
            update,
            "🎨 تنظیم ظاهر و رنگ دکمه‌های UserBot\n"
            f"وضعیت رنگ‌ها: {'روشن ✅' if s.get('colored_buttons') else 'خاموش ❌'}\n"
            f"طرح فعلی: {current_title}\n"
            f"توضیح: {current_description}\n\n"
            "این تنظیم روی تمام دکمه‌های Inline ربات کاربران اعمال می‌شود.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "tx_plans":
        categories = business.list_plan_categories(public=False)
        rows = [
            [InlineKeyboardButton(
                f"📂 دسته‌بندی پلن‌ها | {_bool_icon(s.get('plan_categories_enabled'))}",
                callback_data="userbot:settings:tx_plans:plan_categories_enabled",
            )],
            [InlineKeyboardButton(
                f"🗂 مدیریت دسته‌ها | {len(categories)}",
                callback_data="userbot:settings:tx_plans:categories",
            )],
            [InlineKeyboardButton(
                f"🔢 اولویت دستی پلن‌ها | {_bool_icon(s.get('plan_sort_by_priority'))}",
                callback_data="userbot:settings:tx_plans:plan_sort_by_priority",
            )],
            [InlineKeyboardButton(
                f"↕️ ترتیب پلن‌ها | {s.get('plan_sort_mode')}",
                callback_data="userbot:settings:tx_plans:plan_sort_mode:menu",
            )],
            [InlineKeyboardButton(
                f"📋 ستون‌های پلن‌ها | {s.get('plan_columns')}",
                callback_data="userbot:settings:buy_renew:plan_columns:menu",
            )],
            [InlineKeyboardButton(
                f"🛰 ستون‌های سرورها | {s.get('server_columns')}",
                callback_data="userbot:settings:buy_renew:server_columns:menu",
            )],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(
            update,
            "🧮 تنظیمات تراکنشات و پلن ها\n\n"
            "دسته‌بندی، ترتیب و ستون‌های این بخش مستقیماً روی مسیر خرید UserBot اعمال می‌شوند.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "texts":
        rows = [
            [InlineKeyboardButton("🔔پیام خوش آمدگویی", callback_data="userbot:settings:texts:edit:welcome_message")],
            [InlineKeyboardButton("📕متن سوالات متداول", callback_data="userbot:settings:texts:edit:faq_text")],
            [InlineKeyboardButton("💡متن راهنما", callback_data="userbot:settings:texts:guide_menu")],
            [InlineKeyboardButton("🛰️متن لیست سرورها", callback_data="userbot:settings:texts:edit:servers_list_text")],
            [InlineKeyboardButton("📋متن لیست پلن‌ها", callback_data="userbot:settings:texts:edit:plans_list_text")],
            [InlineKeyboardButton("📬متن پنل تیکت", callback_data="userbot:settings:texts:edit:ticket_panel_text")],
            [InlineKeyboardButton("💌 متون دعوت و بنر", callback_data="userbot:settings:texts:invite_menu")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(
            update,
            "🧾 تنظیمات متون\n\n"
            "تمام متن‌های این بخش مستقیماً در UserBot استفاده می‌شوند. "
            "برای بازگشت یک متن به مقدار پیش‌فرض، 0 ارسال کنید.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "guide_texts":
        rows = [
            [InlineKeyboardButton("📝 متن ابتدای راهنما", callback_data="userbot:settings:texts:edit:guide_text")],
            [InlineKeyboardButton("📱 راهنمای اندروید", callback_data="userbot:settings:texts:edit:guide_android_text")],
            [InlineKeyboardButton("📱 راهنمای IOS", callback_data="userbot:settings:texts:edit:guide_ios_text")],
            [InlineKeyboardButton("🖥️ راهنمای ویندوز", callback_data="userbot:settings:texts:edit:guide_windows_text")],
            [InlineKeyboardButton("💻 راهنمای مک", callback_data="userbot:settings:texts:edit:guide_mac_text")],
            [InlineKeyboardButton("🖥️ راهنمای لینوکس", callback_data="userbot:settings:texts:edit:guide_linux_text")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:texts")],
        ]
        await _edit_or_send(update, "💡 تنظیم متن‌های راهنما", InlineKeyboardMarkup(rows))
        return

    if section == "invite_texts":
        photo_state = "تنظیم شده ✅" if str(s.get("invite_banner_photo_id") or "").strip() else "بدون عکس"
        rows = [
            [InlineKeyboardButton("💌 متن لینک دعوت", callback_data="userbot:settings:texts:edit:invite_text")],
            [InlineKeyboardButton("🎁 متن معرفی دعوت", callback_data="userbot:settings:texts:edit:invite_info_text")],
            [InlineKeyboardButton("📝 متن بنر دعوت", callback_data="userbot:settings:texts:edit:invite_banner_text")],
            [InlineKeyboardButton(f"🖼 عکس بنر دعوت | {photo_state}", callback_data="userbot:settings:texts:invite_photo")],
        ]
        if str(s.get("invite_banner_photo_id") or "").strip():
            rows.append([InlineKeyboardButton(
                "🗑 حذف عکس بنر",
                callback_data="userbot:settings:texts:invite_photo_remove",
            )])
        rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:texts")])
        await _edit_or_send(
            update,
            "💌 تنظیمات دعوت و بنر\n\n"
            "متغیرهای قابل استفاده: {invite_link}، {trial_reward} و {purchase_reward}.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "marketing":
        growth = business.growth_settings(actor)
        rows = [
            [InlineKeyboardButton(
                f"🎟 کد تخفیف | {_bool_icon(s.get('enable_discount_code'))}",
                callback_data="userbot:settings:marketing:toggle:enable_discount_code",
            )],
            [InlineKeyboardButton(
                f"🎁 نمایش دکمه هدیه | {_bool_icon(s.get('show_gift_button'))}",
                callback_data="userbot:settings:marketing:toggle:show_gift_button",
            )],
            [InlineKeyboardButton(
                f"📊 نمایش وضعیت | {_bool_icon(s.get('show_user_status'))}",
                callback_data="userbot:settings:marketing:toggle:show_user_status",
            )],
            [InlineKeyboardButton(
                f"🤝 رفرال | {_bool_icon(growth.get('referral_enabled'))}",
                callback_data="userbot:referral:toggle",
            )],
            [InlineKeyboardButton(
                f"🔥 تست رایگان | {_bool_icon(growth.get('trial_enabled'))}",
                callback_data="userbot:settings:subscription:trial_spec",
            )],
            [InlineKeyboardButton("🎟 مدیریت کوپن تخفیف", callback_data="biz:growth")],
            [InlineKeyboardButton("🏷 مدیریت کدهای هدیه", callback_data="userbot:gifts:coupons")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, "🎯 تنظیمات بازاریابی", InlineKeyboardMarkup(rows))
        return

    if section == "force_join":
        rows = [
            [InlineKeyboardButton(
                f"🔒 عضویت اجباری | {_bool_icon(s.get('force_join_enabled'))}",
                callback_data="userbot:settings:force_join:toggle",
            )],
            [InlineKeyboardButton("📢 تنظیم کانال", callback_data="userbot:settings:force_join:set_channel")],
            [InlineKeyboardButton("❓ راهنما", callback_data="userbot:settings:force_join:help")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(
            update,
            f"🔒 تنظیمات عضویت اجباری\nکانال: {s.get('force_join_channel') or 'تنظیم نشده'}",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "payment":
        methods = business.list_payment_methods_admin(actor)
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(
                f"{'✅' if x['status']=='active' else '❌'} "
                f"{x.get('provider_icon') or '💳'} {x['title']} · {x['currency']}",
                callback_data=f"userbot:settings:payment:method:{int(x['id'])}",
            )]
            for x in methods
        ]
        rows.append([
            InlineKeyboardButton(
                "➕ افزودن روش پرداخت",
                callback_data="userbot:settings:payment:add",
            )
        ])
        rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")])
        await _edit_or_send(
            update,
            "💳 تنظیمات پرداخت Tenant\n"
            "ترتیب نمایش با Priority است. Provider مستقل از UserBot ذخیره می‌شود؛ "
            "درگاه یا تأییدکننده جدید بعداً از همین لایه اضافه می‌شود.",
            InlineKeyboardMarkup(rows),
        )
        return

    if section == "backup_restore":
        await _edit_or_send(
            update,
            "🗂️ تنظیمات بکاپ و بازیابی\n"
            "بکاپ Tenant شامل تنظیمات ربات کاربران، پلن‌ها، روش‌های پرداخت و کوپن‌هاست.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📩دریافت فایل بکاپ", callback_data="userbot:settings:backup:download")],
                [InlineKeyboardButton("📤بازیابی فایل بکاپ", callback_data="userbot:settings:backup:restore")],
                [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
            ]),
        )
        return


def _tenant_backup_payload(business: Any) -> dict[str, Any]:
    tables = [
        "tenant_userbot_settings",
        "tenant_sale_plans",
        "tenant_payment_methods",
        "tenant_coupons",
        "tenant_sales_growth_settings",
        "tenant_gift_vouchers",
    ]
    payload: dict[str, Any] = {"format": "hiddify-whitelabel-tenant-userbot-v1", "tenant_id": business.tenant_id, "tables": {}}
    for table in tables:
        rows = business.conn.execute(
            f"SELECT * FROM {table} WHERE tenant_id=?", (business.tenant_id,)
        ).fetchall()
        payload["tables"][table] = [dict(row) for row in rows]
    return payload


async def _send_backup(update: Update, business: Any) -> None:
    raw = json.dumps(_tenant_backup_payload(business), ensure_ascii=False, indent=2).encode("utf-8")
    bio = BytesIO(raw)
    bio.name = f"tenant-{business.tenant_id}-userbot-backup.json"
    await update.effective_chat.send_document(
        document=bio,
        filename=bio.name,
        caption="📦 بکاپ Tenant UserBot",
    )


async def _restore_backup(business: Any, data: bytes) -> None:
    payload = json.loads(data.decode("utf-8"))
    if payload.get("format") != "hiddify-whitelabel-tenant-userbot-v1":
        raise ValueError("invalid tenant backup format")
    if int(payload.get("tenant_id") or 0) != int(business.tenant_id):
        raise ValueError("backup belongs to another tenant")
    allowed = {
        "tenant_userbot_settings",
        "tenant_sale_plans",
        "tenant_payment_methods",
        "tenant_coupons",
        "tenant_sales_growth_settings",
        "tenant_gift_vouchers",
    }
    tables = payload.get("tables")
    if not isinstance(tables, dict) or set(tables) - allowed:
        raise ValueError("invalid tenant backup tables")
    # Restore is intentionally conservative: replace only configuration tables
    # that do not own order/subscription history. IDs are preserved so existing
    # references remain valid.
    business.conn.execute("BEGIN IMMEDIATE")
    try:
        for table in allowed:
            rows = tables.get(table)
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise ValueError("invalid backup rows")
            if table in ("tenant_sale_plans", "tenant_payment_methods", "tenant_coupons", "tenant_gift_vouchers"):
                # IDs are global primary keys. Reject a crafted/foreign backup
                # before an UPSERT could ever overwrite another tenant's row.
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("invalid backup row")
                    row_id = int(row.get("id") or 0)
                    if row_id <= 0:
                        raise ValueError("invalid backup row id")
                    existing = business.conn.execute(
                        f"SELECT tenant_id FROM {table} WHERE id=?",
                        (row_id,),
                    ).fetchone()
                    if (
                        existing is not None
                        and int(existing["tenant_id"]) != int(business.tenant_id)
                    ):
                        raise ValueError("backup row id belongs to another tenant")
                # Do not delete rows referenced by financial/service history.
                # Existing same-tenant IDs are upserted; missing rows stay untouched.
                pass
            else:
                business.conn.execute(f"DELETE FROM {table} WHERE tenant_id=?", (business.tenant_id,))
            for row in rows:
                if not isinstance(row, dict) or int(row.get("tenant_id") or 0) != business.tenant_id:
                    raise ValueError("cross-tenant backup row")
                cols = list(row.keys())
                vals = [row[k] for k in cols]
                marks = ",".join("?" for _ in cols)
                names = ",".join(cols)
                if table in ("tenant_sale_plans", "tenant_payment_methods", "tenant_coupons", "tenant_gift_vouchers"):
                    updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("id", "tenant_id"))
                    business.conn.execute(
                        f"INSERT INTO {table} ({names}) VALUES ({marks}) "
                        f"ON CONFLICT(id) DO UPDATE SET {updates}",
                        tuple(vals),
                    )
                else:
                    business.conn.execute(
                        f"INSERT INTO {table} ({names}) VALUES ({marks})",
                        tuple(vals),
                    )
        business.conn.commit()
    except Exception:
        business.conn.rollback()
        raise


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
) -> bool:
    set_button_settings(business.runtime_userbot_settings())
    query = update.callback_query
    if query is None:
        return False
    data = str(query.data or "")
    if not (data.startswith("userbot:") or data.startswith("channelpost:")):
        return False

    if data == "userbot:noop":
        await query.answer()
        return True
    await query.answer()
    if data == "userbot:menu":
        await send_userbot_main_menu(update, context); return True

    if data == "userbot:users_menu":
        await _edit_or_send(update, "👤 مدیریت کاربران ربات\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:", build_users_menu_keyboard()); return True
    if data.startswith("userbot:users:"):
        await _send_users_page(update, business, actor, int(data.rsplit(":",1)[1])); return True
    if data == "userbot:users_search_menu":
        await _edit_or_send(update, "روش جستجو:", build_users_search_menu_keyboard()); return True
    if data.startswith("userbot:search:"):
        mode = data.rsplit(":",1)[1]
        context.user_data[FLOW_KEY] = {"kind":"user_search","mode":mode}
        await query.message.reply_text(
            f"لطفاً {'شناسه عددی' if mode=='id' else 'نام یا @یوزرنیم'} کاربر را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data.startswith("userbot:svc:"):
        parts = data.split(":")
        subscription_id = int(parts[2])
        if len(parts) == 3:
            await _send_service_detail(
                update, business, actor, subscription_id
            )
            return True
        action = parts[3]
        if action == "configs":
            item = business.subscription_admin(
                actor, subscription_id=subscription_id
            )
            link = business.admin_subscription_link(
                actor, subscription_id=subscription_id
            )
            await _edit_or_send(
                update,
                "📄کانفیگ ها\n\n"
                f"👤 {item.get('display_name') or 'کاربر'}\n"
                f"🔗 لینک اشتراک:\n{link}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔙بازگشت",
                        callback_data=f"userbot:svc:{subscription_id}",
                    )
                ]]),
                disable_web_page_preview=True,
            )
            return True

    if data.startswith("userbot:user:"):
        parts = data.split(":")
        customer_id = int(parts[2])
        if len(parts) == 3:
            await _send_user_profile(update,business,actor,customer_id); return True
        action = parts[3]
        if action == "services":
            items = business.subscriptions_for_customer_admin(
                actor, customer_id=customer_id
            )
            rows = [[InlineKeyboardButton(
                f"📦 #{x['id']} · {x['plan_name']} · {x['status']}",
                callback_data=f"userbot:svc:{int(x['id'])}",
            )] for x in items]
            rows.append([InlineKeyboardButton(
                "👤بازگشت به پروفایل",
                callback_data=f"userbot:user:{customer_id}",
            )])
            await _edit_or_send(
                update,
                f"📋 لیست سرویس‌ها\nتعداد: {len(items)}",
                InlineKeyboardMarkup(rows),
            )
            return True
        if action == "orders":
            items = business.customer_orders_admin(actor, customer_id=customer_id)
            rows = [[InlineKeyboardButton(f"#{x['id']} · {x['status']}", callback_data=f"userbot:order:{int(x['id'])}")] for x in items[:50]]
            rows.append([InlineKeyboardButton("👤بازگشت به پروفایل", callback_data=f"userbot:user:{customer_id}")])
            await _edit_or_send(update, f"📗 لیست سفارشات\nتعداد: {len(items)}", InlineKeyboardMarkup(rows)); return True
        if action == "payments":
            items = business.list_payments_admin(actor, customer_id=customer_id)
            rows = [[InlineKeyboardButton(
                f"{x.get('provider_icon') or '💳'} {x['payment_key']} · {x['status']}",
                callback_data=f"userbot:pay:detail:{x['payment_key']}",
            )] for x in items[:50]]
            rows.append([InlineKeyboardButton("👤بازگشت به پروفایل", callback_data=f"userbot:user:{customer_id}")])
            await _edit_or_send(update, f"💵 لیست تراکنشات\nتعداد: {len(items)}", InlineKeyboardMarkup(rows)); return True
        if action == "wallet":
            wallet = business.customer_wallet_admin(actor, customer_id=customer_id)
            current = list(wallet.get("accounts") or [])
            text = "💳 ویرایش کیف پول\n" + ("\n".join(f"• {x['currency']}: {int(x['balance']):,}" for x in current) or "• موجودی ثبت نشده") + "\n\n💱 ارز موردنظر را وارد کنید؛ مثال IRR:"
            context.user_data[FLOW_KEY]={"kind":"wallet_set_currency","customer_id":customer_id}
            await query.message.reply_text(text, reply_markup=userbot_cancel_keyboard()); return True
        if action == "reset_trial":
            await _edit_or_send(
                update,
                "⚠️ بازنشانی تست رایگان این کاربر\n"
                "بعد از تأیید، کاربر دوباره واجد شرایط دریافت تست می‌شود "
                "(اگر خرید واقعی قبلی نداشته باشد).",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ تأیید بازنشانی",
                        callback_data=f"userbot:user:{customer_id}:reset_trial_confirm",
                    )],
                    [InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=f"userbot:user:{customer_id}",
                    )],
                ]),
            )
            return True
        if action == "reset_trial_confirm":
            business.reset_customer_trial_admin(actor, customer_id=customer_id)
            await _send_user_profile(update,business,actor,customer_id)
            return True
        if action == "ban":
            profile=business.customer_profile_admin(actor,customer_id=customer_id)
            new_status="blocked" if profile["status"]=="active" else "active"
            business.set_customer_status_admin(actor,customer_id=customer_id,status=new_status)
            await _send_user_profile(update,business,actor,customer_id); return True
        if action == "message":
            context.user_data[FLOW_KEY]={"kind":"user_message","customer_id":customer_id}
            await query.message.reply_text("✍ لطفا متن پیامی که می خواهید برای کاربر ارسال شود را وارد کنید:", reply_markup=userbot_cancel_keyboard()); return True
        if action == "tickets":
            await _send_tickets(update,business,actor,status="open",page=1,customer_id=customer_id); return True

    if data == "userbot:orders_menu":
        await _edit_or_send(update,"📗 مدیریت سفارشات\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",build_orders_menu_keyboard()); return True
    if data.startswith("userbot:orders:list:"):
        await _send_orders_page(update,business,actor,int(data.rsplit(":",1)[1])); return True
    if data == "userbot:orders:search":
        context.user_data[FLOW_KEY]={"kind":"order_search"}
        await query.message.reply_text("🔎 شناسه سفارش، نام، یوزرنیم یا Telegram ID را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:order:"):
        await _send_order_detail(update,business,actor,int(data.rsplit(":",1)[1])); return True

    if data == "userbot:payments_menu":
        await _edit_or_send(update,"💵 مدیریت تراکنشات\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",build_payments_menu_keyboard()); return True
    if data.startswith("userbot:payments:list:"):
        parts=data.split(":"); await _send_payments_page(update,business,actor,parts[3],int(parts[4]) if len(parts)>4 else 1); return True
    if data == "userbot:payments:search":
        context.user_data[FLOW_KEY]={"kind":"payment_search"}
        await query.message.reply_text("🔎 شناسه تراکنش را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:pay:detail:"):
        payment_key = data[len("userbot:pay:detail:"):]
        await _send_payment_detail(update, business, actor, payment_key)
        return True
    if data.startswith("userbot:pay:review:"):
        raw = data[len("userbot:pay:review:"):]
        payment_key, decision = raw.rsplit(":", 1)
        approve = decision == "yes"
        result = business.review_payment_admin(
            actor, payment_key=payment_key, approve=approve
        )
        if approve and result.get("source") == "order" and result.get("status") == "paid":
            try:
                business.fulfill_paid_order(actor, order_id=int(result["order_id"]))
            except TenantBusinessError:
                pass
        await _send_payment_detail(update, business, actor, payment_key)
        return True

    if data == "userbot:gifts_menu":
        await _send_gifts_menu(update,business,actor); return True
    if data == "userbot:gifts:dashboard":
        await _send_gifts_menu(update,business,actor); return True
    if data == "userbot:gifts:coupons":
        await _send_coupons(update,business,actor); return True
    if data == "userbot:gifts:coupons:add":
        context.user_data[FLOW_KEY]={"kind":"gift_add_code"}
        await query.message.reply_text(
            "🏷 کد هدیه را وارد کنید؛ مثال: WELCOME50",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:toggle:"):
        cid=int(data.rsplit(":",1)[1]); item=business.gift_voucher_admin(actor,voucher_id=cid)
        business.set_gift_voucher_status_admin(actor,voucher_id=cid,enabled=item["status"]!="active")
        await _send_coupon_detail(update,business,actor,cid); return True
    if data.startswith("userbot:gifts:coupon:set_code:"):
        cid=int(data.rsplit(":",1)[1])
        business.gift_voucher_admin(actor,voucher_id=cid)
        context.user_data[FLOW_KEY]={"kind":"gift_edit_code","voucher_id":cid}
        await query.message.reply_text(
            "✏️ کد جدید را وارد کنید؛ فقط حروف انگلیسی، عدد، _ و -:",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:set_amount:"):
        cid=int(data.rsplit(":",1)[1])
        business.gift_voucher_admin(actor,voucher_id=cid)
        context.user_data[FLOW_KEY]={"kind":"gift_edit_amount","voucher_id":cid}
        await query.message.reply_text(
            "🎁 مبلغ جدید هدیه را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:set_limit:"):
        cid=int(data.rsplit(":",1)[1])
        business.gift_voucher_admin(actor,voucher_id=cid)
        context.user_data[FLOW_KEY]={"kind":"gift_edit_limit","voucher_id":cid}
        await query.message.reply_text(
            "👥 سقف کل مصرف را وارد کنید (عدد مثبت):",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:set_exp:"):
        cid=int(data.rsplit(":",1)[1])
        business.gift_voucher_admin(actor,voucher_id=cid)
        context.user_data[FLOW_KEY]={"kind":"gift_edit_expiry","voucher_id":cid}
        await query.message.reply_text(
            "🕒 مدت انقضا را به ساعت وارد کنید؛ 0 یعنی نامحدود:",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:delete:"):
        cid=int(data.rsplit(":",1)[1])
        await _edit_or_send(update,"❓ از حذف این کوپن مطمئن هستید؟",InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله",callback_data=f"userbot:gifts:coupon:deleteok:{cid}"),
            InlineKeyboardButton("لغو❌",callback_data=f"userbot:gifts:coupon:{cid}"),
        ]])); return True
    if data.startswith("userbot:gifts:coupon:deleteok:"):
        cid=int(data.rsplit(":",1)[1]); business.delete_gift_voucher_admin(actor,voucher_id=cid); await _send_coupons(update,business,actor); return True
    if data.startswith("userbot:gifts:coupon:campaign:"):
        cid=int(data.rsplit(":",1)[1]); item=business.gift_voucher_admin(actor,voucher_id=cid)
        username_row=BotRepository(business.conn).get_by_tenant_role(business.tenant_id,"user") or {}
        username=str(username_row.get("telegram_username") or "").strip().lstrip("@")
        link=f"https://t.me/{username}?start=gift_{item['code']}" if username else f"کد: {item['code']}"
        await _edit_or_send(update,
            f"📣 هدیه ویژه\n🎁 {int(item['amount']):,} {item['currency']} هدیه کیف پول\n"
            f"🏷 کد: {item['code']}\n🔗 {link}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data=f"userbot:gifts:coupon:{cid}")]]),
            disable_web_page_preview=True,
        ); return True
    if data.startswith("userbot:gifts:coupon:") and data.rsplit(":",1)[1].isdigit():
        await _send_coupon_detail(update,business,actor,int(data.rsplit(":",1)[1])); return True
    if data.startswith("userbot:gifts:redemptions"):
        coupon_id=None
        parts=data.split(":")
        if len(parts)>3 and parts[-1].isdigit(): coupon_id=int(parts[-1])
        rows=business.gift_redemptions_admin(actor,voucher_id=coupon_id)
        text="📜 گزارش مصرف هدایا\n" + ("\n".join(
            f"• {x['code']} · {x['display_name']} · {int(x['amount']):,} {x['currency']} · {x['redeemed_at']}"
            for x in rows[:50]
        ) or "هنوز مصرفی ثبت نشده است.")
        await _edit_or_send(update,text,InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])); return True
    if data == "userbot:gifts:security":
        stats=_gift_stats(business,actor)
        await _edit_or_send(update,
            "🛡 کنترل سوءاستفاده هدایا\n"
            "◈ هر کاربر هر کد هدیه را فقط یک‌بار می‌تواند مصرف کند.\n"
            "◈ سقف کل مصرف هر کد در دیتابیس enforce می‌شود.\n"
            "◈ انقضا و تکمیل ظرفیت قبل از شارژ کیف پول کنترل می‌شود.\n"
            f"🟢 فعال: {stats['active']} · ⏰ منقضی: {stats['expired']} · 🔒 تکمیل ظرفیت: {stats['full']}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🧹 خاموش‌سازی منقضی/تکمیل",callback_data="userbot:gifts:auto_off")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")],
            ])
        ); return True
    if data == "userbot:gifts:auto_off":
        changed=business.deactivate_unusable_gift_vouchers_admin(actor)
        await _edit_or_send(
            update,
            f"✅ {changed} کد منقضی یا تکمیل‌شده خاموش شد.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts:security")]])
        ); return True
    if data == "userbot:gifts:help":
        await _edit_or_send(update,
            "📘 راهنمای مدیریت هدایا\n"
            "1) کد هدیه کیف پول با مبلغ، سقف مصرف و انقضا بسازید.\n"
            "2) هر کاربر هر کد را فقط یک‌بار می‌تواند مصرف کند.\n"
            "3) گزارش مصرف نشان می‌دهد چه کسی از کدام کد استفاده کرده است.\n"
            "4) برای کمپین عمومی ظرفیت و تاریخ انقضای محدود انتخاب کنید.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])
        ); return True
    if data == "userbot:gifts:campaign":
        items=[x for x in business.list_gift_vouchers_admin(actor) if x["status"]=="active"]
        sample=items[0]["code"] if items else "GIFT-CODE"
        username_row=BotRepository(business.conn).get_by_tenant_role(business.tenant_id,"user") or {}
        username=str(username_row.get("telegram_username") or "").strip().lstrip("@")
        link=f"https://t.me/{username}?start=gift_{sample}" if username else f"کد هدیه: {sample}"
        await _edit_or_send(update,
            "📣 متن آماده کمپین هدیه\n\n"
            f"🎁 هدیه ویژه فعال شد!\nکد: {sample}\n🔗 {link}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])
        ); return True
    if data == "userbot:gifts:presets":
        rows=[
            [InlineKeyboardButton(
                f"{item['title']} · {int(item['amount']):,}",
                callback_data=f"userbot:gifts:preset:{key}",
            )]
            for key,item in GIFT_CAMPAIGN_PRESETS.items()
        ]
        rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")])
        await _edit_or_send(
            update,
            "🎯 قالب‌های آماده کمپین هدیه\n"
            "هر قالب یک کد واقعی کیف پول با مبلغ، ظرفیت و انقضای مشخص می‌سازد.",
            InlineKeyboardMarkup(rows),
        ); return True
    if data.startswith("userbot:gifts:preset:"):
        key=data.rsplit(":",1)[1]
        preset=GIFT_CAMPAIGN_PRESETS.get(key)
        if preset is None:
            raise ValueError("invalid gift preset")
        code=f"{preset['prefix']}-{secrets.token_hex(3).upper()}"
        item=business.add_gift_voucher_admin(
            actor,
            code=code,
            amount=int(preset["amount"]),
            currency="IRR",
            max_uses=int(preset["max_uses"]),
        )
        item=business.set_gift_voucher_expiry_hours_admin(
            actor,
            voucher_id=int(item["id"]),
            hours=int(preset["hours"]),
        )
        await _send_coupon_detail(update,business,actor,int(item["id"])); return True
    if data == "userbot:gifts:bulk":
        context.user_data[FLOW_KEY]={"kind":"gift_bulk_prefix"}
        await query.message.reply_text("🧩 پیشوند کدها را وارد کنید؛ مثال: FEST",reply_markup=userbot_cancel_keyboard()); return True

    if data == "userbot:referral_menu":
        await _send_referral_menu(update,business,actor); return True
    if data == "userbot:referral:dashboard":
        stats=business.referral_admin_stats(actor)
        settings=business.growth_settings(actor)
        currency=str(settings.get("referral_currency") or "")
        text=(
            "📊 داشبورد رفرال\n"
            "❖ ◈━━━━━━━━━━━━━━━━━━━━◈ ❖\n"
            f"👥 کل دعوت‌ها: {int(stats.get('total_referrals') or 0)}\n"
            f"🟢 فعال: {int(stats.get('active_referrals') or 0)}\n"
            f"✅ واجد شرایط: {int(stats.get('qualified_referrals') or 0)}\n"
            f"🛒 دعوت موفق خرید: {int(stats.get('successful_referrals') or 0)}\n"
            f"🧪 پاداش تست: {int(stats.get('trial_rewards_count') or 0)} "
            f"({int(stats.get('trial_rewards_amount') or 0):,} {currency})\n"
            f"🛒 پاداش خرید: {int(stats.get('purchase_rewards_count') or 0)} "
            f"({int(stats.get('purchase_rewards_amount') or 0):,} {currency})\n"
            f"🧾 پاداش دستی: {int(stats.get('manual_rewards_count') or 0)} "
            f"({int(stats.get('manual_rewards_amount') or 0):,} {currency})\n"
            f"💸 مجموع هزینه پاداش: {int(stats.get('total_reward_cost') or 0):,} {currency}\n"
            f"🚩 پرچم تقلب: {int(stats.get('fraud_flagged') or 0)}"
        )
        await _edit_or_send(
            update,text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")]])
        ); return True
    if data == "userbot:referral:toggle":
        cur=business.growth_settings(actor)
        business.update_growth_settings(
            actor,referral_enabled=not bool(cur["referral_enabled"])
        )
        await _send_referral_menu(update,business,actor); return True
    if data.startswith("userbot:referral:toggle:"):
        name=data.rsplit(":",1)[1]
        cur=business.growth_settings(actor)
        if name=="trial_reward_enabled":
            business.update_growth_settings(
                actor,
                referral_trial_reward_enabled=not bool(cur["referral_trial_reward_enabled"]),
            )
        elif name=="purchase_reward_enabled":
            business.update_growth_settings(
                actor,
                referral_purchase_reward_enabled=not bool(cur["referral_purchase_reward_enabled"]),
            )
        else:
            raise ValueError("invalid referral toggle")
        await _edit_or_send(
            update,"✅ وضعیت پاداش تغییر کرد.",
            InlineKeyboardMarkup([[InlineKeyboardButton("↩️ تنظیمات رفرال",callback_data="userbot:referral:settings")]])
        ); return True
    if data == "userbot:referral:settings":
        settings=business.growth_settings(actor)
        invite_text=str(settings.get("referral_invite_text") or "").strip()
        preview=(invite_text[:350] + ("…" if len(invite_text)>350 else "")) if invite_text else "متن‌های دعوت مرحله ۷"
        await _edit_or_send(update,
            "⚙️ تنظیمات رفرال\n"
            f"🧪 پاداش تست: {_bool_icon(settings.get('referral_trial_reward_enabled'))} "
            f"{int(settings['referral_trial_reward']):,} {settings['referral_currency']}\n"
            f"🛒 پاداش خرید: {_bool_icon(settings.get('referral_purchase_reward_enabled'))} "
            f"{int(settings['referral_purchase_reward']):,} {settings['referral_currency']}\n"
            f"💳 حداقل خرید: {int(settings['referral_min_purchase']):,}\n"
            f"👥 سقف دعوت موفق: {int(settings['referral_max_rewards']) or 'نامحدود'}\n\n"
            f"📝 متن دعوت:\n{preview}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🧪 پاداش تست | {_bool_icon(settings.get('referral_trial_reward_enabled'))}",
                    callback_data="userbot:referral:toggle:trial_reward_enabled",
                )],
                [InlineKeyboardButton(
                    f"🛒 پاداش خرید | {_bool_icon(settings.get('referral_purchase_reward_enabled'))}",
                    callback_data="userbot:referral:toggle:purchase_reward_enabled",
                )],
                [InlineKeyboardButton("✏️ مبلغ پاداش تست",callback_data="userbot:referral:edit:trial")],
                [InlineKeyboardButton("✏️ مبلغ پاداش خرید",callback_data="userbot:referral:edit:purchase")],
                [InlineKeyboardButton("✏️ سقف دعوت موفق",callback_data="userbot:referral:edit:max")],
                [InlineKeyboardButton("✏️ حداقل مبلغ خرید",callback_data="userbot:referral:edit:min")],
                [InlineKeyboardButton("✏️ متن صفحه دعوت",callback_data="userbot:referral:edit:invite_text")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")],
            ])
        ); return True
    if data.startswith("userbot:referral:edit:"):
        field=data.rsplit(":",1)[1]
        prompts={
            "trial":"💰 مبلغ پاداش تست را وارد کنید:",
            "purchase":"💰 مبلغ پاداش اولین خرید را وارد کنید:",
            "max":"🔢 سقف دعوت موفق را وارد کنید؛ 0 یعنی نامحدود:",
            "min":"💳 حداقل مبلغ خرید برای دریافت پاداش را وارد کنید:",
            "invite_text":(
                "📝 متن صفحه دعوت را ارسال کنید.\n"
                "متغیرهای مجاز: {invite_link}، {trial_reward}، "
                "{purchase_reward} و {referred_count}"
            ),
        }
        if field not in prompts:
            raise ValueError("invalid referral edit field")
        context.user_data[FLOW_KEY]={"kind":"referral_edit","field":field}
        await query.message.reply_text(prompts[field],reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:referral:list:"):
        items=business.referrals_admin(actor); selected,page,pages=_page(items,int(data.rsplit(":",1)[1]))
        lines=[]
        for item in selected:
            qualified="✅" if int(item.get("qualified") or 0)==1 else "⚪"
            fraud=" 🚩" if int(item.get("fraud_flag") or 0)==1 else ""
            lines.append(
                f"#{item['id']} · {item['inviter_name']} ← {item['invitee_name']} · "
                f"{item['status']} · {qualified}{fraud}"
            )
        text="👥 لیست دعوت‌ها\n"+("\n".join(lines) or "موردی نیست.")
        rows=[]; nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️",callback_data=f"userbot:referral:list:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{pages}",callback_data="userbot:noop"))
        if page<pages: nav.append(InlineKeyboardButton("▶️",callback_data=f"userbot:referral:list:{page+1}"))
        rows.append(nav); rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")])
        await _edit_or_send(update,text,InlineKeyboardMarkup(rows)); return True
    if data.startswith("userbot:referral:rewards:"):
        items=business.referral_rewards_admin(actor); selected,page,pages=_page(items,int(data.rsplit(":",1)[1]))
        labels={"trial":"پاداش تست","purchase":"پاداش خرید","manual":"پاداش دستی"}
        text="💰 لیست پاداش‌ها\n"+(
            "\n".join(
                f"#{item['id']} · {item['inviter_name']} · "
                f"{int(item['amount']):,} {item['currency']} · "
                f"{labels.get(str(item.get('reward_type') or ''), item.get('reward_type') or '-')}"
                for item in selected
            ) or "موردی نیست."
        )
        rows=[]; nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️",callback_data=f"userbot:referral:rewards:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{pages}",callback_data="userbot:noop"))
        if page<pages: nav.append(InlineKeyboardButton("▶️",callback_data=f"userbot:referral:rewards:{page+1}"))
        rows.append(nav); rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")])
        await _edit_or_send(update,text,InlineKeyboardMarkup(rows)); return True
    if data == "userbot:referral:manual":
        context.user_data[FLOW_KEY]={"kind":"referral_manual_customer"}
        await query.message.reply_text(
            "👤 Customer ID کاربر را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        ); return True

    if data == "userbot:tickets_menu":
        await _send_tickets(update,business,actor); return True
    if data.startswith("userbot:tickets:list:"):
        parts=data.split(":"); await _send_tickets(update,business,actor,status=parts[3],page=int(parts[4])); return True
    if data.startswith("userbot:ticket:detail:"):
        parts=data.split(":"); tid=int(parts[3]); status=parts[4]; page=int(parts[5]); item=business.ticket_admin(actor,ticket_id=tid)
        rows=[
            [InlineKeyboardButton("👤 پروفایل کاربر",callback_data=f"userbot:user:{int(item['customer_id'])}")],
            [InlineKeyboardButton("📩پاسخ",callback_data=f"userbot:ticket:reply:{tid}:{status}:{page}")],
        ]
        if item["status"]!="closed": rows.append([InlineKeyboardButton("📪 بستن تیکت",callback_data=f"userbot:ticket:close:{tid}:{status}:{page}")])
        rows.append([InlineKeyboardButton("🔙بازگشت",callback_data=f"userbot:tickets:list:{status}:{page}")])
        await _edit_or_send(update,
            f"🎫 تیکت #{tid}\n👤 {item['display_name']}\nوضعیت: {item['status']}\nموضوع: {item['subject']}\n\n{item['body']}\n\nپاسخ:\n{item.get('admin_reply') or '—'}",
            InlineKeyboardMarkup(rows)
        ); return True
    if data.startswith("userbot:ticket:reply:"):
        parts=data.split(":"); context.user_data[FLOW_KEY]={"kind":"ticket_reply","ticket_id":int(parts[3]),"status":parts[4],"page":int(parts[5])}
        await query.message.reply_text("📩 پاسخ تیکت را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:ticket:close:"):
        parts=data.split(":"); business.close_ticket_admin(actor,ticket_id=int(parts[3])); await _send_tickets(update,business,actor,status=parts[4],page=int(parts[5])); return True

    if data == "userbot:broadcast_menu":
        await _edit_or_send(update,
            "📧 ارسال پیام همگانی\nگروه هدف را انتخاب کنید:",
            build_broadcast_menu_keyboard()
        ); return True
    if data.startswith("userbot:broadcast:segment:"):
        segment=data.rsplit(":",1)[1]; targets=business.broadcast_targets_admin(actor,segment=segment)
        context.user_data[FLOW_KEY]={"kind":"broadcast","segment":segment,"step":"wait_text","text":""}
        await query.message.reply_text(
            f"✍ لطفا پیام خود را برای ارسال به «{SEGMENT_LABELS[segment]}» وارد کنید:\n"
            f"تعداد فعلی گیرنده‌ها: {len(targets)}",
            reply_markup=userbot_cancel_keyboard()
        ); return True

    if data == "channelpost:menu":
        await _show_channel_menu(update,context,business,actor); return True
    if data == "channelpost:set":
        context.user_data[FLOW_KEY]={"kind":"channel_set"}
        await query.message.reply_text(
            "📢 @channel یا -100... را ارسال کنید:",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data == "channelpost:new":
        context.user_data[CHANNEL_DRAFT_KEY]={"kind":"","text":"","file_id":"","buttons":[]}
        context.user_data[FLOW_KEY]={"kind":"channel_content"}
        await query.message.reply_text(
            "📝 متن پست را بفرست، یا عکس/ویدئو را همراه کپشن ارسال کن.",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data == "channelpost:edit":
        draft=_channel_draft(context)
        if not draft.get("kind"): raise TenantBusinessError("channel post is empty")
        await _edit_or_send(update,
            "✏️ ویرایش پست\nبخشی که می‌خواهید تغییر کند را انتخاب کنید.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 ویرایش متن / کپشن",callback_data="channelpost:edit_text")],
                [InlineKeyboardButton("🖼 ویرایش عکس / ویدئو",callback_data="channelpost:edit_media")],
                [InlineKeyboardButton("🔄 جایگزینی کامل پست",callback_data="channelpost:new")],
                [InlineKeyboardButton("🔙 بازگشت",callback_data="channelpost:menu")],
            ])
        ); return True
    if data == "channelpost:edit_text":
        context.user_data[FLOW_KEY]={"kind":"channel_edit_text"}
        await query.message.reply_text(
            "📝 متن یا کپشن جدید را بفرستید. برای حذف کامل متن، 0 بفرستید.",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data == "channelpost:edit_media":
        context.user_data[FLOW_KEY]={"kind":"channel_edit_media"}
        await query.message.reply_text(
            "🖼 عکس یا ویدئوی جدید را بفرستید.",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data == "channelpost:button":
        draft=_channel_draft(context)
        if not draft.get("kind"): raise TenantBusinessError("channel post is empty")
        if len(draft.get("buttons") or [])>=MAX_CHANNEL_BUTTONS:
            raise TenantBusinessError("channel button limit reached")
        context.user_data[FLOW_KEY]={"kind":"channel_button_text"}
        await query.message.reply_text(
            "🔘 عنوان دکمه را بفرستید؛ مثال: 🛒 خرید سرویس",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data == "channelpost:preview":
        await _channel_preview(update,context); return True
    if data == "channelpost:clear_buttons":
        _channel_draft(context)["buttons"]=[]
        await _show_channel_menu(update,context,business,actor); return True
    if data == "channelpost:publish":
        await _publish_channel(update,context,business,actor); return True

    if data == "userbot:settings_menu":
        await _settings_root(update, business, actor)
        return True

    # Exact Hiddify-SellBot callback names. Keeping these stable makes the
    # transferred keyboards testable and avoids menu buttons that only look
    # correct but are not wired to the tenant runtime.
    exact_toggles = {
        "userbot:settings:subscription:show_user_page_link": ("show_user_page_link", "subscription"),
        "userbot:settings:subscription:show_username": ("show_username", "subscription"),
        "userbot:settings:subscription:shuffle_configs": ("shuffle_configs", "subscription"),
        "userbot:settings:subscription:shuffle_server_layout": ("shuffle_server_layout", "subscription"),
        "userbot:settings:subscription:shuffle_config_layout": ("shuffle_config_layout", "subscription"),
        "userbot:settings:sub_link_status:show_direct_config": ("show_direct_config", "sub_link_status"),
        "userbot:settings:sub_link_status:show_auto_sub_link": ("show_auto_sub_link", "sub_link_status"),
        "userbot:settings:sub_link_status:show_sub_link": ("show_sub_link", "sub_link_status"),
        "userbot:settings:sub_link_status:show_sub_link_b64": ("show_sub_link_b64", "sub_link_status"),
        "userbot:settings:sub_link_status:show_multi_server": ("show_multi_server", "sub_link_status"),
        "userbot:settings:sub_link_status:show_multi_server_b64": ("show_multi_server_b64", "sub_link_status"),
        # Backward compatibility for stale pre-v0.15 inline keyboards.
        "userbot:settings:sub_link_status:show_smart_link": ("show_multi_server", "sub_link_status"),
        "userbot:settings:buy_renew:enable_buy": ("enable_buy", "buy_renew"),
        "userbot:settings:buy_renew:enable_renew": ("enable_renew", "buy_renew"),
        "userbot:settings:buy_renew:show_renew_in_main_menu": ("show_renew_in_main_menu", "buy_renew"),
        "userbot:settings:tx_plans:plan_categories_enabled": ("plan_categories_enabled", "tx_plans"),
        "userbot:settings:tx_plans:plan_sort_by_priority": ("plan_sort_by_priority", "tx_plans"),
        "userbot:settings:marketing:toggle:enable_discount_code": ("enable_discount_code", "marketing"),
        "userbot:settings:marketing:toggle:show_gift_button": ("show_gift_button", "marketing"),
        "userbot:settings:marketing:toggle:show_user_status": ("show_user_status", "marketing"),
        "userbot:settings:force_join:toggle": ("force_join_enabled", "force_join"),
        "userbot:settings:ui:colored_buttons": ("colored_buttons", "ui"),
    }
    if data in exact_toggles:
        key, section = exact_toggles[data]
        updated_settings = business.toggle_userbot_setting_admin(
            actor, key=key
        )
        set_button_settings(updated_settings)
        await _settings_section(update, business, actor, section)
        if key == "colored_buttons":
            await _refresh_admin_reply_keyboard(update)
        return True

    if data.startswith("userbot:settings:ui:theme:"):
        theme = data.rsplit(":", 1)[1]
        updated_settings = business.set_userbot_setting_admin(
            actor, key="button_theme", value=theme
        )
        set_button_settings(updated_settings)
        await _settings_section(update, business, actor, "ui")
        await _refresh_admin_reply_keyboard(update)
        return True

    if data == "userbot:settings:texts:guide_menu":
        await _settings_section(update, business, actor, "guide_texts")
        return True
    if data == "userbot:settings:texts:invite_menu":
        await _settings_section(update, business, actor, "invite_texts")
        return True
    if data == "userbot:settings:texts:invite_photo":
        current = str(
            business.userbot_settings_admin(actor).get("invite_banner_photo_id")
            or ""
        )
        context.user_data[FLOW_KEY] = {
            "kind": "invite_banner_photo",
            "return_section": "invite_texts",
        }
        await query.message.reply_text(
            "🖼 عکس جدید بنر دعوت را ارسال کنید.\n"
            f"وضعیت فعلی: {'تنظیم شده' if current else 'بدون عکس'}",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data == "userbot:settings:texts:invite_photo_remove":
        business.set_userbot_setting_admin(
            actor,
            key="invite_banner_photo_id",
            value="",
        )
        await _settings_section(update, business, actor, "invite_texts")
        return True
    if data.startswith("userbot:settings:texts:edit:"):
        key = data.rsplit(":", 1)[1]
        current = business.userbot_settings_admin(actor).get(key) or "—"
        if key.startswith("guide_") or key == "guide_text":
            return_section = "guide_texts"
        elif key.startswith("invite_"):
            return_section = "invite_texts"
        else:
            return_section = "texts"
        context.user_data[FLOW_KEY] = {
            "kind": "setting_text",
            "key": key,
            "return_section": return_section,
        }
        await query.message.reply_text(
            f"📝 مقدار فعلی:\n{current}\n\n"
            "متن جدید را ارسال کنید. برای بازگشت به متن پیش‌فرض، 0 بفرستید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data == "userbot:settings:sub_link_status:set_base_url":
        current = str(business.userbot_settings_admin(actor).get("smart_base_url") or "")
        context.user_data[FLOW_KEY] = {
            "kind": "setting_text",
            "key": "smart_base_url",
            "return_section": "sub_link_status",
        }
        await query.message.reply_text(
            "🌐 دامنه عمومی Smart Link را با http/https ارسال کنید.\n"
            f"مقدار فعلی: {current or 'پیش‌فرض سرور'}\n"
            "این مقدار فقط روی Smart Link اثر دارد و لینک Subscription پنل را تغییر نمی‌دهد.\n"
            "برای برگشت به مقدار پیش‌فرض، 0 ارسال کنید.",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data == "userbot:settings:sub_link_status:ssl_help":
        await _edit_or_send(
            update,
            "🔐 راهنمای SSL دامنه\n\n"
            "دامنه باید به IP سرور Smart Subscription اشاره کند و HTTPS معتبر داشته باشد. "
            "این دامنه فقط برای Smart Link استفاده می‌شود؛ لینک عادی Subscription از خود پنل می‌آید. "
            "آدرس ذخیره‌شده باید شامل scheme و host (و در صورت نیاز port/path پایه) باشد.\n"
            "نمونه: https://sub.example.com",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:sub_link_status")
            ]]),
        )
        return True

    if data == "userbot:settings:buy_renew:renew_mode_info":
        await _settings_section(update, business, actor, "renew_policy")
        return True

    if data.startswith("userbot:settings:buy_renew:renew_policy:"):
        policy = data.rsplit(":", 1)[1]
        business.set_renewal_policy_admin(actor, policy=policy)
        await _settings_section(update, business, actor, "renew_policy")
        return True

    if data.startswith("userbot:settings:buy_renew:renew_rollover:"):
        parts = data.split(":")
        if len(parts) != 6:
            raise ValueError("invalid renewal rollover callback")
        business.set_renewal_rollover_admin(
            actor,
            kind=parts[4],
            mode=parts[5],
        )
        await _settings_section(update, business, actor, "renew_policy")
        return True

    if data.startswith("userbot:settings:buy_renew:renew_limit:"):
        field = data.rsplit(":", 1)[1]
        settings = business.userbot_settings_admin(actor)
        if field == "days":
            key = "renew_max_days"
            current = int(settings.get(key) or 3)
            prompt = (
                f"📊 مقدار فعلی: {current} روز\n"
                "حداکثر چند روز مانده به پایان اشتراک، تمدید مجاز باشد؟"
            )
        elif field == "usage":
            key = "renew_max_remaining_gb"
            current = int(settings.get(key) or 3)
            prompt = (
                f"📆 مقدار فعلی: {current} گیگابایت\n"
                "حداکثر چند گیگابایتِ باقی‌مانده، تمدید مجاز باشد؟"
            )
        else:
            raise ValueError("invalid renewal limit")
        context.user_data[FLOW_KEY] = {
            "kind": "renew_setting_int",
            "key": key,
        }
        await query.message.reply_text(
            prompt,
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data == "userbot:settings:buy_renew:renew_unlimited_volume":
        settings = business.toggle_userbot_setting_admin(
            actor, key="renew_unlimited_volume"
        )
        if bool(settings.get("renew_unlimited_volume")):
            context.user_data[FLOW_KEY] = {
                "kind": "renew_setting_int",
                "key": "renew_unlimited_volume_from_gb",
            }
            await query.message.reply_text(
                "♾ حداقل حجم پلن برای نمایش «نامحدود» را به گیگابایت ارسال کنید.\n"
                f"مقدار فعلی: {int(settings.get('renew_unlimited_volume_from_gb') or 1000)}",
                reply_markup=userbot_cancel_keyboard(),
            )
            return True
        await _settings_section(update, business, actor, "buy_renew")
        return True

    if data == "userbot:settings:buy_renew:renew_unlimited_time":
        settings = business.toggle_userbot_setting_admin(
            actor, key="renew_unlimited_time"
        )
        if bool(settings.get("renew_unlimited_time")):
            context.user_data[FLOW_KEY] = {
                "kind": "renew_setting_int",
                "key": "renew_unlimited_time_from_days",
            }
            await query.message.reply_text(
                "♾ حداقل مدت برای نمایش «نامحدود» را به روز ارسال کنید.\n"
                f"مقدار فعلی: {int(settings.get('renew_unlimited_time_from_days') or 365)}",
                reply_markup=userbot_cancel_keyboard(),
            )
            return True
        await _settings_section(update, business, actor, "buy_renew")
        return True

    if data == "userbot:settings:buy_renew:plan_columns:menu":
        s = business.userbot_settings_admin(actor)
        rows = [
            [InlineKeyboardButton(
                f"{'✅ ' if int(s.get('plan_columns') or 1) == value else ''}{value} ستون",
                callback_data=f"userbot:settings:buy_renew:plan_columns:{value}",
            )]
            for value in (1, 2, 3)
        ]
        rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:buy_renew")])
        await _edit_or_send(update, "📋 تعداد ستون‌های نمایش پلن‌ها", InlineKeyboardMarkup(rows))
        return True
    if data.startswith("userbot:settings:buy_renew:plan_columns:"):
        value = int(data.rsplit(":", 1)[1])
        business.set_userbot_setting_admin(actor, key="plan_columns", value=value)
        await _settings_section(update, business, actor, "buy_renew")
        return True

    if data == "userbot:settings:buy_renew:server_columns:menu":
        s = business.userbot_settings_admin(actor)
        rows = [
            [InlineKeyboardButton(
                f"{'✅ ' if int(s.get('server_columns') or 1) == value else ''}{value} ستون",
                callback_data=f"userbot:settings:buy_renew:server_columns:{value}",
            )]
            for value in (1, 2, 3)
        ]
        rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:buy_renew")])
        await _edit_or_send(update, "🛰 تعداد ستون‌های نمایش سرورها", InlineKeyboardMarkup(rows))
        return True
    if data.startswith("userbot:settings:buy_renew:server_columns:"):
        value = int(data.rsplit(":", 1)[1])
        business.set_userbot_setting_admin(actor, key="server_columns", value=value)
        await _settings_section(update, business, actor, "buy_renew")
        return True

    if data == "userbot:settings:tx_plans:categories":
        categories = business.list_plan_categories(public=False)
        plans = [
            p for p in business.list_plans(public=False)
            if not str(p.get("name") or "").startswith("__WHITELABEL_")
        ]
        uncategorized = sum(1 for p in plans if p.get("category_id") is None)
        rows = [
            [InlineKeyboardButton(
                (
                    f"{'✅' if str(cat.get('status')) == 'active' else '⛔'} "
                    f"{cat['title']} · اولویت {int(cat.get('priority') or 0)} "
                    f"· {int(cat.get('plan_count') or 0)} پلن"
                )[:64],
                callback_data=f"userbot:settings:tx_plans:category:{int(cat['id'])}",
            )]
            for cat in categories
        ]
        rows.extend([
            [InlineKeyboardButton(
                "➕ افزودن دسته",
                callback_data="userbot:settings:tx_plans:category:add",
            )],
            [InlineKeyboardButton(
                f"📋 پلن‌های بدون دسته: {uncategorized}",
                callback_data="userbot:settings:tx_plans",
            )],
            [InlineKeyboardButton(
                "🔙بازگشت",
                callback_data="userbot:settings:tx_plans",
            )],
        ])
        await _edit_or_send(
            update,
            "🗂 مدیریت دسته‌بندی پلن‌ها\n"
            "اولویت کمتر، بالاتر نمایش داده می‌شود.",
            InlineKeyboardMarkup(rows),
        )
        return True

    if data == "userbot:settings:tx_plans:category:add":
        context.user_data[FLOW_KEY] = {"kind": "plan_category_add"}
        await query.message.reply_text(
            "➕ عنوان دسته | اولویت را ارسال کنید.\n"
            "مثال: یک ماهه | 10",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data.startswith("userbot:settings:tx_plans:category:toggle:"):
        category_id = int(data.rsplit(":", 1)[1])
        category = business.plan_category(category_id, public=False)
        business.update_plan_category_admin(
            actor,
            category_id=category_id,
            status=(
                "disabled"
                if str(category.get("status")) == "active"
                else "active"
            ),
        )
        category = business.plan_category(category_id, public=False)
        rows = _plan_category_detail_rows(category)
        await _edit_or_send(
            update,
            _plan_category_detail_text(category),
            InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("userbot:settings:tx_plans:category:edit_title:"):
        category_id = int(data.rsplit(":", 1)[1])
        category = business.plan_category(category_id, public=False)
        context.user_data[FLOW_KEY] = {
            "kind": "plan_category_edit_title",
            "category_id": category_id,
        }
        await query.message.reply_text(
            f"📝 عنوان فعلی: {category['title']}\nعنوان جدید را بفرستید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data.startswith("userbot:settings:tx_plans:category:edit_priority:"):
        category_id = int(data.rsplit(":", 1)[1])
        category = business.plan_category(category_id, public=False)
        context.user_data[FLOW_KEY] = {
            "kind": "plan_category_edit_priority",
            "category_id": category_id,
        }
        await query.message.reply_text(
            f"🔢 اولویت فعلی: {int(category.get('priority') or 0)}\n"
            "عدد اولویت جدید را بفرستید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data.startswith("userbot:settings:tx_plans:category:plans:"):
        category_id = int(data.rsplit(":", 1)[1])
        category = business.plan_category(category_id, public=False)
        plans = [
            p for p in business.list_plans(public=False)
            if not str(p.get("name") or "").startswith("__WHITELABEL_")
        ]
        rows = [
            [InlineKeyboardButton(
                (
                    f"{'✅ ' if int(p.get('category_id') or 0) == category_id else '▫️ '}"
                    f"{p['name']} · {int(p.get('traffic_gb') or 0)}GB"
                )[:64],
                callback_data=(
                    f"userbot:settings:tx_plans:category:assign:"
                    f"{category_id}:{int(p['id'])}"
                ),
            )]
            for p in plans
        ]
        rows.append([InlineKeyboardButton(
            "🔙بازگشت",
            callback_data=f"userbot:settings:tx_plans:category:{category_id}",
        )])
        await _edit_or_send(
            update,
            f"📋 اتصال پلن‌ها به دسته «{category['title']}»\n"
            "با لمس هر پلن، عضویت آن در این دسته تغییر می‌کند.",
            InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("userbot:settings:tx_plans:category:assign:"):
        parts = data.split(":")
        if len(parts) != 7:
            raise ValueError("invalid category assignment callback")
        category_id = int(parts[5])
        plan_id = int(parts[6])
        plan = business.plan(plan_id, public=False)
        target = (
            None
            if int(plan.get("category_id") or 0) == category_id
            else category_id
        )
        business.assign_plan_category_admin(
            actor,
            plan_id=plan_id,
            category_id=target,
        )
        category = business.plan_category(category_id, public=False)
        plans = [
            p for p in business.list_plans(public=False)
            if not str(p.get("name") or "").startswith("__WHITELABEL_")
        ]
        rows = [
            [InlineKeyboardButton(
                (
                    f"{'✅ ' if int(p.get('category_id') or 0) == category_id else '▫️ '}"
                    f"{p['name']} · {int(p.get('traffic_gb') or 0)}GB"
                )[:64],
                callback_data=(
                    f"userbot:settings:tx_plans:category:assign:"
                    f"{category_id}:{int(p['id'])}"
                ),
            )]
            for p in plans
        ]
        rows.append([InlineKeyboardButton(
            "🔙بازگشت",
            callback_data=f"userbot:settings:tx_plans:category:{category_id}",
        )])
        await _edit_or_send(
            update,
            f"📋 اتصال پلن‌ها به دسته «{category['title']}»",
            InlineKeyboardMarkup(rows),
        )
        return True

    if (
        data.startswith("userbot:settings:tx_plans:category:")
        and data.rsplit(":", 1)[1].isdigit()
    ):
        category_id = int(data.rsplit(":", 1)[1])
        category = business.plan_category(category_id, public=False)
        await _edit_or_send(
            update,
            _plan_category_detail_text(category),
            InlineKeyboardMarkup(_plan_category_detail_rows(category)),
        )
        return True

    if data == "userbot:settings:tx_plans:plan_sort_mode:menu":
        s = business.userbot_settings_admin(actor)
        current = str(s.get("plan_sort_mode") or "id")
        choices = [
            ("id", "پیش‌فرض"),
            ("price_asc", "قیمت کم به زیاد"),
            ("price_desc", "قیمت زیاد به کم"),
            ("traffic_asc", "حجم کم به زیاد"),
            ("traffic_desc", "حجم زیاد به کم"),
        ]
        rows = [[InlineKeyboardButton(
            f"{'✅ ' if current == value else ''}{title}",
            callback_data=f"userbot:settings:tx_plans:plan_sort_mode:{value}",
        )] for value, title in choices]
        rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:tx_plans")])
        await _edit_or_send(update, "🔢 ترتیب نمایش پلن‌ها", InlineKeyboardMarkup(rows))
        return True
    if data.startswith("userbot:settings:tx_plans:plan_sort_mode:"):
        value = data.rsplit(":", 1)[1]
        business.set_userbot_setting_admin(actor, key="plan_sort_mode", value=value)
        await _settings_section(update, business, actor, "tx_plans")
        return True

    if data == "userbot:settings:subscription:trial_spec":
        g = business.growth_settings(actor)
        await _edit_or_send(
            update,
            _trial_settings_text(g),
            _trial_settings_keyboard(g),
        )
        return True
    if data.startswith("userbot:settings:trial_spec:"):
        action = data.rsplit(":", 1)[1]
        g = business.growth_settings(actor)
        if action == "enabled":
            business.update_growth_settings(
                actor, trial_enabled=not bool(g.get("trial_enabled"))
            )
            g = business.growth_settings(actor)
            await _edit_or_send(
                update,
                _trial_settings_text(g),
                _trial_settings_keyboard(g),
            )
            return True
        if action == "announce":
            business.update_growth_settings(
                actor,
                trial_announce_enabled=not bool(
                    g.get("trial_announce_enabled", True)
                ),
            )
            g = business.growth_settings(actor)
            await _edit_or_send(
                update,
                _trial_settings_text(g),
                _trial_settings_keyboard(g),
            )
            return True
        if action not in {"usage", "days"}:
            raise ValueError("invalid trial setting")
        context.user_data[FLOW_KEY] = {
            "kind": "trial_edit",
            "field": "traffic" if action == "usage" else "days",
        }
        await query.message.reply_text(
            "مقدار عددی جدید را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data == "userbot:settings:subscription:sub_status_reminder":
        s = business.userbot_settings_admin(actor)
        await _edit_or_send(
            update,
            _reminder_settings_text(s),
            _reminder_settings_keyboard(s),
        )
        return True
    if data.startswith("userbot:settings:sub_status_reminder:"):
        action = data.rsplit(":", 1)[1]
        if action == "enabled":
            business.toggle_userbot_setting_admin(actor, key="reminder_enabled")
            s = business.userbot_settings_admin(actor)
            await _edit_or_send(
                update,
                _reminder_settings_text(s),
                _reminder_settings_keyboard(s),
            )
            return True
        if action not in {"usage", "days"}:
            raise ValueError("invalid reminder setting")
        context.user_data[FLOW_KEY] = {
            "kind": "reminder_edit",
            "field": "gb" if action == "usage" else "days",
        }
        await query.message.reply_text(
            "📊 حجم باقی‌مانده را به گیگ وارد کنید (1 تا 1000):"
            if action == "usage"
            else "📅 تعداد روز مانده را وارد کنید (1 تا 30):",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data == "userbot:settings:subscription:reset_free_trial":
        await _edit_or_send(
            update,
            "⚠️ بازنشانی تست رایگان همه کاربران\n"
            "این کار سابقه دریافت تست رایگان همه کاربران همین Tenant را پاک می‌کند.\n"
            "برای یک کاربر، از پروفایل همان کاربر استفاده کنید. ادامه می‌دهید؟",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، بازنشانی شود", callback_data="userbot:settings:subscription:reset_free_trial:yes")],
                [InlineKeyboardButton("❌ لغو", callback_data="userbot:settings:subscription")],
            ]),
        )
        return True
    if data == "userbot:settings:subscription:reset_free_trial:yes":
        count = business.reset_all_customer_trials_admin(actor)
        await _edit_or_send(
            update,
            f"✅ تست رایگان برای {count} کاربر بازنشانی شد.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:subscription")
            ]]),
        )
        return True

    if data == "userbot:settings:force_join:set_channel":
        context.user_data[FLOW_KEY] = {"kind": "force_join_channel"}
        await query.message.reply_text(
            "📢 @channel یا -100... را ارسال کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data == "userbot:settings:force_join:help":
        s = business.userbot_settings_admin(actor)
        await _edit_or_send(
            update,
            "❓ راهنمای عضویت اجباری\n\n"
            + str(s.get("force_join_help_text") or ""),
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:force_join")
            ]]),
        )
        return True

    if data == "userbot:settings:payment:add":
        providers = business.available_payment_providers_admin(actor)
        rows = [[InlineKeyboardButton(
            f"{item['icon']} {item['title']}",
            callback_data=f"userbot:settings:payment:addprovider:{item['key']}",
        )] for item in providers]
        rows.append([InlineKeyboardButton(
            "🔙بازگشت", callback_data="userbot:settings:payment"
        )])
        await _edit_or_send(
            update,
            "💳 Provider روش پرداخت را انتخاب کنید:",
            InlineKeyboardMarkup(rows),
        )
        return True
    if data.startswith("userbot:settings:payment:addprovider:"):
        provider_key = data.rsplit(":", 1)[1]
        provider = next(
            (
                item for item in business.available_payment_providers_admin(actor)
                if item["key"] == provider_key
            ),
            None,
        )
        if provider is None:
            raise TenantBusinessError("payment provider not found")
        context.user_data[FLOW_KEY] = {
            "kind": "payment_add_title",
            "payment_provider": provider_key,
            "payment_kind": provider["legacy_kind"],
            "requires_network": bool(provider.get("requires_network")),
        }
        await query.message.reply_text(
            "📝 عنوان روش پرداخت را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data.startswith("userbot:settings:payment:addkind:"):
        # Backward compatibility for old Telegram messages from <= v0.20.0.
        kind = data.rsplit(":", 1)[1]
        provider_key = "card_manual" if kind == "card" else "crypto_manual"
        provider = next(
            (
                item for item in business.available_payment_providers_admin(actor)
                if item["key"] == provider_key
            ),
            None,
        )
        if provider is None:
            raise TenantBusinessError("payment provider not found")
        context.user_data[FLOW_KEY] = {
            "kind": "payment_add_title",
            "payment_provider": provider_key,
            "payment_kind": provider["legacy_kind"],
            "requires_network": bool(provider.get("requires_network")),
        }
        await query.message.reply_text(
            "📝 عنوان روش پرداخت را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data.startswith("userbot:settings:payment:edit:"):
        parts = data.split(":")
        if len(parts) != 6:
            raise ValueError("invalid payment edit callback")
        mid = int(parts[4])
        field = parts[5]
        if field not in {"title","currency","destination","network","instructions","priority"}:
            raise ValueError("invalid payment edit field")
        item = business.payment_method_admin(actor, method_id=mid)
        current = item.get(field) if field != "priority" else int(item.get("priority") or 0)
        context.user_data[FLOW_KEY] = {
            "kind": "payment_edit",
            "method_id": mid,
            "field": field,
        }
        await query.message.reply_text(
            f"✏️ مقدار فعلی: {current or '-'}\nمقدار جدید را ارسال کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data.startswith("userbot:settings:payment:remove_confirm:"):
        mid = int(data.rsplit(":", 1)[1])
        result = business.remove_payment_method_admin(actor, method_id=mid)
        message = (
            "✅ روش پرداخت حذف شد."
            if result.get("removed")
            else "✅ روش دارای سابقه تراکنش بود؛ برای حفظ تاریخچه غیرفعال شد."
        )
        await _edit_or_send(
            update,
            message,
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔙 تنظیمات پرداخت",
                    callback_data="userbot:settings:payment",
                )
            ]]),
        )
        return True
    if data.startswith("userbot:settings:payment:remove:"):
        mid = int(data.rsplit(":", 1)[1])
        item = business.payment_method_admin(actor, method_id=mid)
        await _edit_or_send(
            update,
            f"⚠️ حذف روش پرداخت «{item['title']}»؟\n"
            "اگر سابقه مالی داشته باشد حذف نمی‌شود و فقط غیرفعال خواهد شد.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "✅ تأیید",
                    callback_data=f"userbot:settings:payment:remove_confirm:{mid}",
                )],
                [InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"userbot:settings:payment:method:{mid}",
                )],
            ]),
        )
        return True
    if data.startswith("userbot:settings:payment:method:"):
        mid = int(data.rsplit(":", 1)[1])
        item = business.payment_method_admin(actor, method_id=mid)
        await _edit_or_send(
            update,
            f"{item.get('provider_icon') or '💳'} {item['title']}\n"
            f"Provider: {item.get('provider_title') or item.get('provider_key')} "
            f"({item.get('provider_key')})\n"
            f"ارز: {item['currency']}\n"
            f"مقصد: {item['destination']}\n"
            f"شبکه: {item.get('network') or '-'}\n"
            f"Priority: {int(item.get('priority') or 0)}\n"
            f"توضیحات: {item.get('instructions') or '-'}\n"
            f"وضعیت: {item['status']}",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✏️ عنوان", callback_data=f"userbot:settings:payment:edit:{mid}:title"),
                    InlineKeyboardButton("💱 ارز", callback_data=f"userbot:settings:payment:edit:{mid}:currency"),
                ],
                [
                    InlineKeyboardButton("📍 مقصد", callback_data=f"userbot:settings:payment:edit:{mid}:destination"),
                    InlineKeyboardButton("🌐 شبکه", callback_data=f"userbot:settings:payment:edit:{mid}:network"),
                ],
                [
                    InlineKeyboardButton("📝 توضیحات", callback_data=f"userbot:settings:payment:edit:{mid}:instructions"),
                    InlineKeyboardButton("↕️ Priority", callback_data=f"userbot:settings:payment:edit:{mid}:priority"),
                ],
                [InlineKeyboardButton("⏸/▶️ تغییر وضعیت", callback_data=f"userbot:settings:payment:toggle:{mid}")],
                [InlineKeyboardButton("🗑 حذف / غیرفعال امن", callback_data=f"userbot:settings:payment:remove:{mid}")],
                [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings:payment")],
            ]),
        )
        return True
    if data.startswith("userbot:settings:payment:toggle:"):
        mid = int(data.rsplit(":", 1)[1])
        item = business.payment_method_admin(actor, method_id=mid)
        business.set_payment_method_status_admin(
            actor, method_id=mid, enabled=item["status"] != "active"
        )
        await _settings_section(update, business, actor, "payment")
        return True

    if data == "userbot:settings:backup:download":
        await _send_backup(update, business)
        return True
    if data == "userbot:settings:backup:restore":
        context.user_data[FLOW_KEY] = {"kind": "backup_restore"}
        await query.message.reply_text(
            "📤 فایل JSON بکاپ همین Tenant را ارسال کنید.",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    # Backward-compatible callbacks from v0.10.4. They remain supported so an
    # old Telegram message can still be clicked safely after an update.
    if data.startswith("userbot:settings:toggle:"):
        parts = data.split(":")
        key = parts[3]
        back = ":".join(parts[4:])
        business.toggle_userbot_setting_admin(actor, key=key)
        if back.startswith("userbot:settings:"):
            section = back.rsplit(":", 1)[1]
            if section == "reminders":
                section = "subscription"
            await _settings_section(update, business, actor, section)
        else:
            await _settings_root(update, business, actor)
        return True
    if data.startswith("userbot:settings:value:"):
        parts = data.split(":")
        key = parts[3]
        value = parts[4]
        business.set_userbot_setting_admin(actor, key=key, value=value)
        await _settings_section(update, business, actor, "ui")
        return True
    if data.startswith("userbot:settings:text:"):
        key = data.rsplit(":", 1)[1]
        current = business.userbot_settings_admin(actor).get(key) or "—"
        context.user_data[FLOW_KEY] = {
            "kind": "setting_text",
            "key": key,
            "return_section": "texts",
        }
        await query.message.reply_text(
            f"📝 مقدار فعلی:\n{current}\n\nمتن جدید را ارسال کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data == "userbot:settings:plansort":
        await _settings_section(update, business, actor, "tx_plans")
        return True
    if data == "userbot:settings:plancol":
        s = business.userbot_settings_admin(actor)
        value = (int(s.get("plan_columns") or 1) % 3) + 1
        business.set_userbot_setting_admin(actor, key="plan_columns", value=value)
        await _settings_section(update, business, actor, "tx_plans")
        return True
    if data == "userbot:settings:servercol":
        s = business.userbot_settings_admin(actor)
        value = (int(s.get("server_columns") or 1) % 3) + 1
        business.set_userbot_setting_admin(actor, key="server_columns", value=value)
        await _settings_section(update, business, actor, "tx_plans")
        return True
    if data in {"userbot:settings:trial", "userbot:settings:reminders"}:
        target = (
            "userbot:settings:subscription:trial_spec"
            if data.endswith(":trial")
            else "userbot:settings:subscription:sub_status_reminder"
        )
        # Old message callback: render the same screen directly.
        if target.endswith("trial_spec"):
            g = business.growth_settings(actor)
            await _edit_or_send(
                update,
                _trial_settings_text(g),
                _trial_settings_keyboard(g),
            )
        else:
            s = business.userbot_settings_admin(actor)
            await _edit_or_send(
                update,
                _reminder_settings_text(s),
                _reminder_settings_keyboard(s),
            )
        return True
    if data.startswith("userbot:settings:trial:"):
        action = data.rsplit(":", 1)[1]
        mapped = {
            "toggle": "enabled",
            "announce": "announce",
            "traffic": "usage",
            "days": "days",
        }.get(action)
        if mapped is None:
            raise ValueError("invalid trial setting")
        if mapped in {"enabled", "announce"}:
            g = business.growth_settings(actor)
            if mapped == "enabled":
                business.update_growth_settings(
                    actor, trial_enabled=not bool(g.get("trial_enabled"))
                )
            else:
                business.update_growth_settings(
                    actor,
                    trial_announce_enabled=not bool(
                        g.get("trial_announce_enabled", True)
                    ),
                )
            await _settings_section(update, business, actor, "subscription")
            return True
        context.user_data[FLOW_KEY] = {
            "kind": "trial_edit",
            "field": "traffic" if mapped == "usage" else "days",
        }
        await query.message.reply_text(
            "مقدار عددی جدید را وارد کنید:",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True
    if data.startswith("userbot:settings:reminder:"):
        field = data.rsplit(":", 1)[1]
        if field not in {"days", "gb"}:
            raise ValueError("invalid reminder setting")
        context.user_data[FLOW_KEY] = {"kind": "reminder_edit", "field": field}
        await query.message.reply_text(
            "📅 تعداد روز مانده را وارد کنید (1 تا 30):"
            if field == "days"
            else "📊 حجم باقی‌مانده را به گیگ وارد کنید (1 تا 1000):",
            reply_markup=userbot_cancel_keyboard(),
        )
        return True

    if data.startswith("userbot:settings:"):
        section = data.rsplit(":", 1)[1]
        if section in {
            "subscription",
            "sub_link_status",
            "ui",
            "buy_renew",
            "tx_plans",
            "texts",
            "marketing",
            "force_join",
            "payment",
            "backup_restore",
        }:
            await _settings_section(update, business, actor, section)
            return True

    return True


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    set_button_settings(business.runtime_userbot_settings())
    flow=context.user_data.get(FLOW_KEY)
    if not isinstance(flow,dict):
        return False
    text=str(update.effective_message.text or "").strip()
    if text in {"❌لغو","❌ لغو","لغو","/cancel"}:
        context.user_data.pop(FLOW_KEY,None)
        await update.effective_message.reply_text("❌ لغو شد.",reply_markup=admin_main_keyboard())
        return True
    kind=str(flow.get("kind") or "")
    try:
        if kind=="user_search":
            items=business.list_customers_admin(actor,query=text)
            context.user_data.pop(FLOW_KEY,None)
            if not items:
                await update.effective_message.reply_text("❌ کاربر یافت نشد.",reply_markup=admin_main_keyboard()); return True
            rows=[[InlineKeyboardButton(f"🔵 {_display_name(x)[:25]}",callback_data=f"userbot:user:{int(x['id'])}")] for x in items[:50]]
            rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:users_menu")])
            await update.effective_message.reply_text(f"✅ {len(items)} نتیجه پیدا شد.",reply_markup=admin_main_keyboard())
            await update.effective_message.reply_text("نتایج:",reply_markup=InlineKeyboardMarkup(rows)); return True
        if kind=="wallet_set_currency":
            currency=text.strip().upper()
            if not 3<=len(currency)<=8: raise ValueError("wallet currency")
            flow["currency"]=currency; flow["kind"]="wallet_set_amount"
            await update.effective_message.reply_text(
                "💰 موجودی نهایی کیف پول را وارد کنید:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="wallet_set_amount":
            target=int(text.replace(",",""))
            if target<0: raise ValueError("wallet amount")
            cid=int(flow["customer_id"]); currency=str(flow["currency"])
            wallet=business.customer_wallet_admin(actor,customer_id=cid)
            current=next((int(x["balance"]) for x in wallet["accounts"] if x["currency"]==currency),0)
            delta=target-current
            if delta:
                business.adjust_wallet_admin(
                    actor,customer_id=cid,currency=currency,amount=delta,
                    note="Admin set wallet balance"
                )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                f"✅ موجودی کیف پول روی {target:,} {currency} تنظیم شد.",
                reply_markup=admin_main_keyboard(),
            ); return True
        if kind=="user_message":
            cid=int(flow["customer_id"]); profile=business.customer_profile_admin(actor,customer_id=cid)
            await _send_via_userbot(business,int(profile["telegram_user_id"]),text=text)
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ پیام ارسال شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="order_search":
            items=business.search_orders_admin(actor,text); context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(f"✅ {len(items)} نتیجه پیدا شد.",reply_markup=admin_main_keyboard())
            rows=[[InlineKeyboardButton(f"#{x['id']} · {x['display_name']}",callback_data=f"userbot:order:{int(x['id'])}")] for x in items[:50]] or [[InlineKeyboardButton("نتیجه‌ای نیست",callback_data="userbot:noop")]]
            await update.effective_message.reply_text("نتایج سفارش:",reply_markup=InlineKeyboardMarkup(rows)); return True
        if kind=="payment_search":
            items=business.search_payments_admin(actor,text)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                f"✅ {len(items)} تراکنش پیدا شد.",
                reply_markup=admin_main_keyboard(),
            )
            rows=[[InlineKeyboardButton(
                f"{x.get('provider_icon') or '💳'} {x['payment_key']} · {x.get('display_name') or '-'}",
                callback_data=f"userbot:pay:detail:{x['payment_key']}",
            )] for x in items[:50]] or [[InlineKeyboardButton("نتیجه‌ای نیست",callback_data="userbot:noop")]]
            await update.effective_message.reply_text(
                "نتایج تراکنش:",reply_markup=InlineKeyboardMarkup(rows)
            ); return True
        if kind=="gift_add_code":
            code=text.strip().upper()
            if not re.fullmatch(r"[A-Z0-9_-]{4,48}",code):
                raise ValueError("gift code")
            flow["code"]=code; flow["kind"]="gift_add_amount"
            await update.effective_message.reply_text("💰 مبلغ هدیه را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_add_amount":
            amount=int(text.replace(",",""))
            if amount<=0: raise ValueError("gift amount")
            flow["amount"]=amount; flow["kind"]="gift_add_currency"
            await update.effective_message.reply_text("💱 ارز را وارد کنید؛ مثال IRR:",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_add_currency":
            currency=text.strip().upper()
            if not 3<=len(currency)<=8: raise ValueError("gift currency")
            flow["currency"]=currency; flow["kind"]="gift_add_uses"
            await update.effective_message.reply_text("👥 حداکثر تعداد مصرف را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_add_uses":
            uses=int(text)
            if uses<=0: raise ValueError("gift uses")
            flow["max_uses"]=uses; flow["kind"]="gift_add_expiry"
            await update.effective_message.reply_text(
                "🕒 مدت انقضا را به ساعت وارد کنید؛ 0 یعنی نامحدود:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="gift_add_expiry":
            hours=int(text.replace(",",""))
            if hours<0: raise ValueError("gift expiry")
            item=business.add_gift_voucher_admin(
                actor,code=str(flow["code"]),amount=int(flow["amount"]),
                currency=str(flow["currency"]),max_uses=int(flow["max_uses"]),
            )
            if hours>0:
                item=business.set_gift_voucher_expiry_hours_admin(
                    actor,voucher_id=int(item["id"]),hours=hours
                )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ کد هدیه ساخته شد.",reply_markup=admin_main_keyboard())
            await _send_coupon_detail(update,business,actor,int(item["id"])); return True
        if kind=="gift_edit_code":
            cid=int(flow["voucher_id"])
            code=text.strip().upper()
            item=business.rename_gift_voucher_admin(actor,voucher_id=cid,code=code)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ کد هدیه ویرایش شد.",reply_markup=admin_main_keyboard())
            await _send_coupon_detail(update,business,actor,int(item["id"])); return True
        if kind=="gift_edit_amount":
            cid=int(flow["voucher_id"])
            amount=int(text.replace(",",""))
            item=business.set_gift_voucher_amount_admin(actor,voucher_id=cid,amount=amount)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ مبلغ هدیه ویرایش شد.",reply_markup=admin_main_keyboard())
            await _send_coupon_detail(update,business,actor,int(item["id"])); return True
        if kind=="gift_edit_limit":
            cid=int(flow["voucher_id"])
            limit=int(text.replace(",",""))
            item=business.set_gift_voucher_max_uses_admin(actor,voucher_id=cid,max_uses=limit)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ سقف مصرف ویرایش شد.",reply_markup=admin_main_keyboard())
            await _send_coupon_detail(update,business,actor,int(item["id"])); return True
        if kind=="gift_edit_expiry":
            cid=int(flow["voucher_id"])
            hours=int(text.replace(",",""))
            item=business.set_gift_voucher_expiry_hours_admin(actor,voucher_id=cid,hours=hours)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ انقضای هدیه ویرایش شد.",reply_markup=admin_main_keyboard())
            await _send_coupon_detail(update,business,actor,int(item["id"])); return True
        if kind=="gift_bulk_prefix":
            prefix=text.strip().upper()
            if not re.fullmatch(r"[A-Z0-9_-]{2,24}",prefix): raise ValueError("gift prefix")
            flow["prefix"]=prefix; flow["kind"]="gift_bulk_count"
            await update.effective_message.reply_text("🔢 تعداد کدها را وارد کنید (1 تا 200):",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_bulk_count":
            count=int(text)
            if not 1<=count<=200: raise ValueError("gift count")
            flow["count"]=count; flow["kind"]="gift_bulk_amount"
            await update.effective_message.reply_text("💰 مبلغ هر کد هدیه را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_bulk_amount":
            amount=int(text.replace(",",""))
            if amount<=0: raise ValueError("gift amount")
            flow["amount"]=amount; flow["kind"]="gift_bulk_currency"
            await update.effective_message.reply_text("💱 ارز را وارد کنید؛ مثال IRR:",reply_markup=userbot_cancel_keyboard()); return True
        if kind=="gift_bulk_currency":
            currency=text.strip().upper()
            if not 3<=len(currency)<=8: raise ValueError("gift currency")
            flow["currency"]=currency; flow["kind"]="gift_bulk_expiry"
            await update.effective_message.reply_text(
                "🕒 انقضای همه کدها را به ساعت وارد کنید؛ 0 یعنی نامحدود:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="gift_bulk_expiry":
            hours=int(text.replace(",",""))
            if hours<0: raise ValueError("gift expiry")
            items=business.create_gift_vouchers_bulk_admin(
                actor,
                prefix=str(flow["prefix"]),
                count=int(flow["count"]),
                amount=int(flow["amount"]),
                currency=str(flow["currency"]),
                expiry_hours=hours,
            )
            username_row=BotRepository(business.conn).get_by_tenant_role(business.tenant_id,"user") or {}
            username=str(username_row.get("telegram_username") or "").strip().lstrip("@")
            lines=[
                "✅ کدهای یک‌بارمصرف ساخته شدند.",
                f"🧩 تعداد: {len(items)}",
                f"💰 مبلغ هر کد: {int(flow['amount']):,} {flow['currency']}",
                f"🕒 انقضا: {'نامحدود' if hours==0 else f'{hours} ساعت'}",
                "",
            ]
            for item in items:
                code=str(item["code"])
                lines.append(
                    f"{code} | https://t.me/{username}?start=gift_{code}"
                    if username else code
                )
            context.user_data.pop(FLOW_KEY,None)
            output="\n".join(lines)
            for pos in range(0,len(output),3500):
                await update.effective_message.reply_text(
                    output[pos:pos+3500],
                    disable_web_page_preview=True,
                    reply_markup=admin_main_keyboard() if pos==0 else None,
                )
            return True
        if kind=="referral_edit":
            field=str(flow["field"])
            if field=="invite_text":
                if not text.strip():
                    raise ValueError("referral invite text")
                business.update_growth_settings(
                    actor,referral_invite_text=text.strip()
                )
            else:
                value=int(text.replace(",","").replace("٬",""))
                if value<0:
                    raise ValueError("referral value")
                kwargs={}
                if field=="trial": kwargs["referral_trial_reward"]=value
                elif field=="purchase": kwargs["referral_purchase_reward"]=value
                elif field=="max": kwargs["referral_max_rewards"]=value
                elif field=="min": kwargs["referral_min_purchase"]=value
                else: raise ValueError("referral field")
                business.update_growth_settings(actor,**kwargs)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ تنظیمات رفرال ذخیره شد.",
                reply_markup=admin_main_keyboard(),
            )
            return True
        if kind=="referral_manual_customer":
            cid=int(text)
            business.customer_profile_admin(actor,customer_id=cid)
            flow["customer_id"]=cid; flow["kind"]="referral_manual_amount"
            await update.effective_message.reply_text(
                "💰 مبلغ پاداش را وارد کنید:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="referral_manual_amount":
            amount=int(text.replace(",","").replace("٬",""))
            if amount<=0: raise ValueError("referral amount")
            settings=business.growth_settings(actor)
            reward=business.grant_manual_referral_reward_admin(
                actor,
                customer_id=int(flow["customer_id"]),
                amount=amount,
                currency=str(settings.get("referral_currency") or "IRR"),
                note="Admin manual referral reward",
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ پاداش دستی ثبت شد.\n"
                f"🎁 شناسه پاداش: #{int(reward['id'])}\n"
                f"💰 مبلغ: {int(reward['amount']):,} {reward['currency']}",
                reply_markup=admin_main_keyboard(),
            ); return True
        if kind=="referral_manual_currency":
            currency=text.strip().upper()
            if not 3<=len(currency)<=8: raise ValueError("referral currency")
            reward=business.grant_manual_referral_reward_admin(
                actor,
                customer_id=int(flow["customer_id"]),
                amount=int(flow["amount"]),
                currency=currency,
                note="Admin manual referral reward",
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                f"✅ پاداش دستی #{int(reward['id'])} ثبت شد.",
                reply_markup=admin_main_keyboard(),
            ); return True
        if kind=="ticket_reply":
            item=business.reply_ticket_admin(actor,ticket_id=int(flow["ticket_id"]),reply=text)
            try: await _send_via_userbot(business,int(item["telegram_user_id"]),text=f"📩 پاسخ تیکت #{item['id']}\n\n{text}")
            except Exception: pass
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ پاسخ ثبت شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="broadcast":
            step=str(flow.get("step") or "wait_text")
            if step=="wait_text":
                if not text: raise ValueError("empty broadcast")
                flow["text"]=text; flow["step"]="wait_photo"
                await update.effective_message.reply_text(
                    "🖼️ لطفا عکس خود را برای ارسال به کاربران ارسال کنید "
                    "یا روی دکمه [⏩رد کردن] کلیک کنید:",
                    reply_markup=broadcast_skip_cancel_keyboard(),
                ); return True
            if step=="wait_photo" and _is_broadcast_skip(text):
                result=await _send_broadcast_to_targets(
                    context,business,actor,str(flow["segment"]),
                    str(flow.get("text") or ""),""
                )
                context.user_data.pop(FLOW_KEY,None)
                await update.effective_message.reply_text(
                    _broadcast_result_text(result),reply_markup=admin_main_keyboard()
                ); return True
            await update.effective_message.reply_text(
                "❌ لطفا عکس ارسال کنید یا روی دکمه [⏩رد کردن] بزنید.",
                reply_markup=broadcast_skip_cancel_keyboard(),
            ); return True
        if kind=="channel_set":
            business.set_userbot_setting_admin(actor,key="channel_id",value=text)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ کانال ذخیره شد.",reply_markup=admin_main_keyboard()
            ); return True
        if kind=="channel_content":
            draft=_channel_draft(context)
            draft.update(kind="text",text=text,file_id="")
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ محتوای پست ذخیره شد.",reply_markup=admin_main_keyboard())
            return True
        if kind=="channel_edit_text":
            draft=_channel_draft(context)
            draft["text"]="" if text in {"0","-","—"} else text
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ متن/کپشن ویرایش شد.",reply_markup=admin_main_keyboard())
            return True
        if kind=="channel_button_text":
            if not text or len(text)>64: raise ValueError("button text")
            flow["label"]=text; flow["kind"]="channel_button_url"
            await update.effective_message.reply_text(
                "🔗 حالا لینک دکمه را بفرستید؛ لینک کامل یا @username.",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="channel_button_url":
            url=_normalize_button_url(text)
            if not url: raise ValueError("button url")
            draft=_channel_draft(context); buttons=list(draft.get("buttons") or [])
            if len(buttons)>=MAX_CHANNEL_BUTTONS: raise TenantBusinessError("channel button limit reached")
            buttons.append({"text":str(flow["label"]),"url":url}); draft["buttons"]=buttons
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ دکمه اضافه شد.",reply_markup=admin_main_keyboard())
            return True
        if kind=="plan_category_add":
            fields=[part.strip() for part in text.split("|")]
            if not fields or not fields[0]:
                raise ValueError("category title")
            priority=int(fields[1]) if len(fields)>=2 and fields[1] else 0
            business.add_plan_category_admin(
                actor,
                title=fields[0],
                priority=priority,
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ دسته پلن ساخته شد.",
                reply_markup=admin_main_keyboard(),
            )
            await _settings_section(update,business,actor,"tx_plans")
            return True
        if kind=="plan_category_edit_title":
            category_id=int(flow["category_id"])
            business.update_plan_category_admin(
                actor,
                category_id=category_id,
                title=text,
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ عنوان دسته ذخیره شد.",
                reply_markup=admin_main_keyboard(),
            )
            return True
        if kind=="plan_category_edit_priority":
            category_id=int(flow["category_id"])
            business.update_plan_category_admin(
                actor,
                category_id=category_id,
                priority=int(text),
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ اولویت دسته ذخیره شد.",
                reply_markup=admin_main_keyboard(),
            )
            return True
        if kind=="renew_setting_int":
            key=str(flow["key"])
            business.set_userbot_setting_admin(
                actor,
                key=key,
                value=int(text),
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ تنظیم تمدید ذخیره شد.",
                reply_markup=admin_main_keyboard(),
            )
            section=(
                "renew_policy"
                if key in {"renew_max_days","renew_max_remaining_gb"}
                else "buy_renew"
            )
            await _settings_section(update,business,actor,section)
            return True
        if kind=="setting_text":
            key = str(flow["key"])
            value = text
            if key == "smart_base_url":
                if text in {"0", "-", "—"}:
                    value = ""
                else:
                    parsed = urlparse(text)
                    if (
                        str(parsed.scheme or "").lower() not in {"http", "https"}
                        or not parsed.netloc
                        or parsed.username
                        or parsed.password
                        or parsed.query
                        or parsed.fragment
                    ):
                        raise ValueError("invalid smart subscription base url")
                    value = text.rstrip("/")
            business.set_userbot_setting_admin(actor, key=key, value=value)
            return_section = str(flow.get("return_section") or "")
            context.user_data.pop(FLOW_KEY, None)
            await update.effective_message.reply_text(
                "✅ متن ذخیره شد.", reply_markup=admin_main_keyboard()
            )
            if return_section:
                await _settings_section(update, business, actor, return_section)
            return True
        if kind=="trial_edit":
            value=int(text); field=str(flow["field"]); kwargs={"trial_traffic_gb":value} if field=="traffic" else {"trial_duration_days":value}; business.update_growth_settings(actor,**kwargs); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ مشخصات تست ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="reminder_edit":
            value=int(text)
            field=str(flow["field"])
            key="reminder_days" if field=="days" else "reminder_remaining_gb"
            business.set_userbot_setting_admin(actor,key=key,value=value)
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ تنظیم یادآور ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="force_join_channel":
            business.set_userbot_setting_admin(actor,key="force_join_channel",value=text); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ کانال عضویت اجباری ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="payment_edit":
            field=str(flow["field"])
            mid=int(flow["method_id"])
            kwargs: dict[str, Any] = {}
            if field == "priority":
                value=int(text.replace(",",""))
                if value < 0:
                    raise ValueError("payment priority")
                kwargs[field]=value
            elif field == "currency":
                value=text.strip().upper()
                if not 3 <= len(value) <= 8:
                    raise ValueError("payment currency")
                kwargs[field]=value
            elif field == "network":
                kwargs[field]="" if text in {"0","-","—"} else text
            elif field == "instructions":
                kwargs[field]="" if text in {"0","-","—"} else text
            else:
                kwargs[field]=text
            business.update_payment_method_admin(
                actor, method_id=mid, **kwargs
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ روش پرداخت ویرایش شد.",
                reply_markup=admin_main_keyboard(),
            )
            return True
        if kind=="payment_add_title":
            if not text or len(text)>80: raise ValueError("payment title")
            flow["title"]=text; flow["kind"]="payment_add_currency"
            await update.effective_message.reply_text(
                "💱 ارز را وارد کنید؛ مثال IRR یا USDT:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="payment_add_currency":
            currency=text.strip().upper()
            if not 3<=len(currency)<=8: raise ValueError("payment currency")
            flow["currency"]=currency; flow["kind"]="payment_add_destination"
            await update.effective_message.reply_text(
                "📍 شماره کارت / آدرس کیف پول / مقصد Provider را وارد کنید:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="payment_add_destination":
            if not text or len(text)>180: raise ValueError("payment destination")
            flow["destination"]=text
            if bool(flow.get("requires_network")):
                flow["kind"]="payment_add_network"
                await update.effective_message.reply_text(
                    "🌐 نام شبکه را وارد کنید؛ مثال TRC20:",
                    reply_markup=userbot_cancel_keyboard(),
                ); return True
            flow["network"]=""; flow["kind"]="payment_add_instructions"
            await update.effective_message.reply_text(
                "📝 توضیحات پرداخت را وارد کنید یا 0 برای بدون توضیح:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="payment_add_network":
            network="" if text in {"0","-","—"} else text
            if bool(flow.get("requires_network")) and not network:
                raise ValueError("payment network")
            flow["network"]=network
            flow["kind"]="payment_add_instructions"
            await update.effective_message.reply_text(
                "📝 توضیحات پرداخت را وارد کنید یا 0 برای بدون توضیح:",
                reply_markup=userbot_cancel_keyboard(),
            ); return True
        if kind=="payment_add_instructions":
            instructions="" if text in {"0","-","—"} else text
            business.add_payment_method(
                actor,
                provider_key=str(flow["payment_provider"]),
                kind=str(flow["payment_kind"]),
                title=str(flow["title"]),
                currency=str(flow["currency"]),
                destination=str(flow["destination"]),
                network=str(flow.get("network") or ""),
                instructions=instructions,
            )
            context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text(
                "✅ روش پرداخت اضافه شد.",reply_markup=admin_main_keyboard()
            ); return True
    except (ValueError,TenantBusinessError):
        await update.effective_message.reply_text("❌ مقدار یا وضعیت معتبر نیست. دوباره تلاش کنید.",reply_markup=userbot_cancel_keyboard())
        return True
    return False


async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    set_button_settings(business.runtime_userbot_settings())
    flow=context.user_data.get(FLOW_KEY)
    if not isinstance(flow,dict):
        return False
    kind=str(flow.get("kind") or "")
    message=update.effective_message
    if message is None:
        return False

    if kind=="invite_banner_photo":
        file_id = ""
        if getattr(message, "photo", None):
            file_id = str(message.photo[-1].file_id)
        else:
            document = getattr(message, "document", None)
            mime = str(getattr(document, "mime_type", "") or "").lower()
            if document is not None and mime.startswith("image/"):
                file_id = str(document.file_id)
        if not file_id:
            await message.reply_text(
                "❌ فقط عکس معتبر ارسال کنید.",
                reply_markup=userbot_cancel_keyboard(),
            )
            return True
        business.set_userbot_setting_admin(
            actor,
            key="invite_banner_photo_id",
            value=file_id,
        )
        context.user_data.pop(FLOW_KEY, None)
        await message.reply_text(
            "✅ عکس بنر دعوت ذخیره شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _settings_section(update, business, actor, "invite_texts")
        return True

    if kind=="broadcast" and str(flow.get("step") or "")=="wait_photo":
        file_id=""
        if getattr(message,"photo",None):
            file_id=str(message.photo[-1].file_id)
        else:
            document=getattr(message,"document",None)
            mime=str(getattr(document,"mime_type","") or "").lower()
            if document is not None and mime.startswith("image/"):
                file_id=str(document.file_id)
        if not file_id:
            await message.reply_text(
                "❌ فقط عکس ارسال کنید یا «⏩رد کردن» را بزنید.",
                reply_markup=broadcast_skip_cancel_keyboard(),
            )
            return True
        result=await _send_broadcast_to_targets(
            context,business,actor,str(flow["segment"]),
            str(flow.get("text") or ""),file_id
        )
        context.user_data.pop(FLOW_KEY,None)
        await message.reply_text(
            _broadcast_result_text(result),reply_markup=admin_main_keyboard()
        )
        return True

    if kind in {"channel_content","channel_edit_media"}:
        draft=_channel_draft(context)
        if getattr(message,"photo",None):
            draft["kind"]="photo"; draft["file_id"]=str(message.photo[-1].file_id)
            if kind=="channel_content":
                draft["text"]=str(getattr(message,"caption_html",None) or message.caption or "")
        elif getattr(message,"video",None):
            draft["kind"]="video"; draft["file_id"]=str(message.video.file_id)
            if kind=="channel_content":
                draft["text"]=str(getattr(message,"caption_html",None) or message.caption or "")
        else:
            return False
        context.user_data.pop(FLOW_KEY,None)
        await message.reply_text(
            "✅ محتوای پست ذخیره/ویرایش شد.",reply_markup=admin_main_keyboard()
        )
        return True
    return False


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    set_button_settings(business.runtime_userbot_settings())
    flow=context.user_data.get(FLOW_KEY)
    if (
        isinstance(flow, dict)
        and (
            (
                flow.get("kind") == "broadcast"
                and str(flow.get("step") or "") == "wait_photo"
            )
            or flow.get("kind") == "invite_banner_photo"
        )
    ):
        return await handle_media(
            update,context,business=business,actor=actor,
            admin_main_keyboard=admin_main_keyboard,
        )
    if not isinstance(flow,dict) or flow.get("kind")!="backup_restore":
        return False
    document=update.effective_message.document
    if document is None or not str(document.file_name or "").lower().endswith(".json"):
        await update.effective_message.reply_text("❌ فقط فایل JSON بکاپ معتبر است.",reply_markup=userbot_cancel_keyboard()); return True
    file=await context.bot.get_file(document.file_id)
    data=bytes(await file.download_as_bytearray())
    try:
        await _restore_backup(business,data)
    except Exception:
        await update.effective_message.reply_text("❌ بازیابی انجام نشد؛ فایل نامعتبر یا ناسازگار است.",reply_markup=userbot_cancel_keyboard()); return True
    context.user_data.pop(FLOW_KEY,None)
    await update.effective_message.reply_text("✅ بکاپ Tenant بازیابی شد.",reply_markup=admin_main_keyboard())
    return True
