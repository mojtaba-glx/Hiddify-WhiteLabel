"""SellBot-compatible management UI for one tenant's UserBot.

This module intentionally mirrors the proven Hiddify-SellBot AdminBot/UserBot
management navigation while translating every operation to the tenant-scoped
WhiteLabel business service. No callback can escape the current tenant.
"""

from __future__ import annotations

import asyncio
import json
import math
from io import BytesIO
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from Database.repositories import BotRepository
from Shared.crypto import fingerprint_token
from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError

PAGE_SIZE = 21
FLOW_KEY = "userbot_admin_flow"

SEGMENT_LABELS = {
    "all": "تمام کاربران",
    "expired_all": "تمام کاربران منقضی شده",
    "no_order": "کاربران بدون سفارش",
    "expired_1w": "کاربران منقضی شده بیش از یک هفته",
    "expired_2w": "کاربران منقضی شده بیش از دو هفته",
    "expired_4w": "کاربران منقضی شده بیش از چهار هفته",
    "expired_8w": "کاربران منقضی شده بیش از هشت هفته",
}


def userbot_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌لغو")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


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
        [InlineKeyboardButton("✅لیست تراکنشات تایید شده", callback_data="userbot:payments:list:approved:1")],
        [InlineKeyboardButton("🚫لیست تراکنشات رد شده", callback_data="userbot:payments:list:rejected:1")],
        [InlineKeyboardButton("⏳لیست تراکنشات در انتظار", callback_data="userbot:payments:list:pending:1")],
        [InlineKeyboardButton("💳لیست تراکنشات کارت به کارت", callback_data="userbot:payments:list:card:1")],
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


def _display_name(item: dict[str, Any]) -> str:
    username = str(item.get("username") or "").strip()
    if username:
        return f"@{username}"
    return str(item.get("display_name") or item.get("telegram_user_id") or item.get("id") or "کاربر")


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
        f"💰 قیمت: {int(order.get('amount') or 0):,} {order.get('currency') or ''}\n"
        f"📊 وضعیت: {order.get('status') or '-'}\n"
        f"🔁 نوع: {order.get('operation') or 'purchase'}",
        InlineKeyboardMarkup(rows),
    )


async def _send_payments_page(update: Update, business: Any, actor: int, filter_type: str, page: int = 1) -> None:
    status = filter_type if filter_type in ("approved", "rejected", "pending") else None
    kind = "card" if filter_type == "card" else None
    items = business.list_receipts_history_admin(actor, status=status, kind=kind)
    selected, page, pages = _page(items, page)
    title = {
        "approved": "لیست تراکنشات تایید شده ✅",
        "rejected": "لیست تراکنشات رد شده 🚫",
        "pending": "لیست تراکنشات در انتظار ⏳",
        "card": "لیست تراکنشات کارت به کارت 💳",
    }.get(filter_type, "لیست تراکنشات")
    money: dict[str, int] = {}
    for item in items:
        currency = str(item.get("currency") or "")
        money[currency] = money.get(currency, 0) + int(item.get("amount") or 0)
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for item in selected:
        current.append(InlineKeyboardButton(str(item["id"]), callback_data=f"userbot:pay:detail:{int(item['id'])}"))
        if len(current) == 3:
            rows.append(current); current = []
    if current:
        rows.append(current)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"userbot:payments:list:{filter_type}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="userbot:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"userbot:payments:list:{filter_type}:{page+1}"))
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


async def _send_payment_detail(update: Update, business: Any, actor: int, receipt_id: int) -> None:
    pay = business.receipt_admin(actor, receipt_id=receipt_id)
    status_title = {"approved": "✅ تایید شده", "rejected": "❌ رد شده", "pending": "⏳ در انتظار"}.get(str(pay.get("status")), str(pay.get("status")))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("👤 پروفایل کاربر", callback_data=f"userbot:user:{int(pay['customer_id'])}")]
    ]
    if pay["status"] == "pending":
        rows.extend([
            [
                InlineKeyboardButton("✅ تایید", callback_data=f"userbot:pay:review:{receipt_id}:yes"),
                InlineKeyboardButton("❌ رد", callback_data=f"userbot:pay:review:{receipt_id}:no"),
            ]
        ])
    rows.append([InlineKeyboardButton("🔙بازگشت", callback_data="userbot:payments_menu")])
    await _edit_or_send(
        update,
        f"◈ شناسه تراکنش: {receipt_id}\n"
        f"👤 کاربر: {pay.get('display_name') or '-'}\n"
        f"◈ نام کاربری: {'@' + str(pay.get('username')).lstrip('@') if pay.get('username') else '-'}\n"
        f"◈ شناسه کاربر: {pay.get('telegram_user_id') or '-'}\n"
        f"◈ تاریخ تراکنش: {pay.get('created_at') or '-'}\n"
        f"◈ مبلغ تراکنش: {int(pay.get('amount') or 0):,} {pay.get('currency') or ''}\n"
        "❖ • -------------------------- • ❖\n"
        f"◈ وضعیت: {status_title}\n"
        f"◈ روش تراکنش: {pay.get('payment_kind') or '-'}\n"
        f"◈ پیگیری: {pay.get('reference') or ('تصویر رسید' if pay.get('telegram_file_id') else '-')}",
        InlineKeyboardMarkup(rows),
    )


def _gift_stats(business: Any, actor: int) -> dict[str, int]:
    coupons = business.list_coupons_admin(actor)
    redemptions = business.list_coupon_redemptions_admin(actor)
    now = utcnow()
    active = expired = full = 0
    for item in coupons:
        is_expired = False
        if item.get("expires_at"):
            try:
                from Shared.timeutils import parse_utc
                is_expired = parse_utc(str(item["expires_at"])) <= now
            except Exception:
                pass
        is_full = int(item.get("max_uses") or 0) > 0 and int(item.get("used_count") or 0) >= int(item.get("max_uses") or 0)
        if is_expired:
            expired += 1
        elif is_full:
            full += 1
        elif item.get("status") == "active":
            active += 1
    return {"total": len(coupons), "active": active, "expired": expired, "full": full, "redemptions": len(redemptions)}


async def _send_gifts_menu(update: Update, business: Any, actor: int) -> None:
    stats = _gift_stats(business, actor)
    await _edit_or_send(
        update,
        "🎁 مدیریت هدایا\n"
        "❖ ◈━━━━━━━━━━━━━━━━━━━━◈ ❖\n"
        f"🟢 کوپن‌های فعال: {stats['active']}\n"
        f"📦 کل کوپن‌ها: {stats['total']}\n"
        f"🎯 مصرف‌شده: {stats['redemptions']} بار\n\n"
        "از دکمه‌های زیر برای ساخت، گزارش و مدیریت کمپین هدیه استفاده کنید.",
        build_gifts_menu_keyboard(),
    )


async def _send_coupons(update: Update, business: Any, actor: int) -> None:
    items = business.list_coupons_admin(actor)
    rows = [
        [InlineKeyboardButton(
            f"{'🟢' if x['status']=='active' else '⚫'} {x['code']} | {x['used_count']}/{x['max_uses'] or '∞'}",
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
        "💼 کوپن‌ها و کدهای هدیه\n"
        f"◈ تعداد کل: {len(items)}\n"
        f"◈ فعال: {sum(1 for x in items if x['status']=='active')}\n"
        f"◈ غیرفعال: {sum(1 for x in items if x['status']!='active')}",
        InlineKeyboardMarkup(rows),
    )


async def _send_coupon_detail(update: Update, business: Any, actor: int, coupon_id: int) -> None:
    item = business.coupon(coupon_id)
    redemptions = business.list_coupon_redemptions_admin(actor, coupon_id=coupon_id)
    kind = "درصدی" if item["discount_kind"] == "percent" else "مبلغ ثابت"
    value = f"{item['value']}%" if item["discount_kind"] == "percent" else f"{int(item['value']):,} {item.get('currency') or ''}"
    rows = [
        [InlineKeyboardButton(
            "⏸ خاموش کردن کوپن" if item["status"] == "active" else "▶️ روشن کردن کوپن",
            callback_data=f"userbot:gifts:coupon:toggle:{coupon_id}",
        )],
        [InlineKeyboardButton("📜 گزارش مصرف این کوپن", callback_data=f"userbot:gifts:redemptions:{coupon_id}")],
        [InlineKeyboardButton("🗑 حذف کوپن", callback_data=f"userbot:gifts:coupon:delete:{coupon_id}")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:gifts:coupons")],
    ]
    await _edit_or_send(
        update,
        f"🏷 کد: {item['code']}\n"
        "❖ ◈━━━━━━━━━━━━━━━━━━━━◈ ❖\n"
        f"◈ وضعیت: {item['status']}\n"
        f"◈ نوع: {kind}\n"
        f"◈ مقدار: {value}\n"
        f"◈ استفاده: {int(item['used_count'] or 0)} از {int(item['max_uses'] or 0) or 'نامحدود'}\n"
        f"◈ مصرف ثبت‌شده: {len(redemptions)}\n"
        f"◈ انقضا: {item.get('expires_at') or 'نامحدود'}",
        InlineKeyboardMarkup(rows),
    )


async def _send_referral_menu(update: Update, business: Any, actor: int) -> None:
    settings = business.growth_settings(actor)
    referrals = business.referrals_admin(actor)
    rewards = business.referral_rewards_admin(actor)
    reward_total: dict[str, int] = {}
    for item in rewards:
        cur = str(item.get("currency") or "")
        reward_total[cur] = reward_total.get(cur, 0) + int(item.get("amount") or 0)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 داشبورد رفرال", callback_data="userbot:referral:dashboard")],
        [
            InlineKeyboardButton("⚙️ تنظیمات", callback_data="userbot:referral:settings"),
            InlineKeyboardButton(f"🎁 فعال/غیرفعال | {_bool_icon(settings.get('referral_enabled'))}", callback_data="userbot:referral:toggle"),
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
        f"👥 دعوت‌ها: {len(referrals)}\n"
        f"💰 پاداش‌ها: {' | '.join(f'{v:,} {k}' for k,v in reward_total.items()) or '0'}",
        kb,
    )


def _ticket_bucket(status: str) -> set[str]:
    if status == "pending":
        return {"open"}
    if status == "open":
        return {"open", "answered"}
    return {"closed"}


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


async def _send_via_userbot(business: Any, chat_id: int | str, *, text: str) -> None:
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=chat_id, text=text)
    finally:
        token = ""


async def _broadcast_text(business: Any, actor: int, segment: str, text: str) -> dict[str, int]:
    targets = business.broadcast_targets_admin(actor, segment=segment)
    now = iso_utc(utcnow())
    cursor = business.conn.execute(
        "INSERT INTO tenant_broadcast_runs "
        "(tenant_id, segment, message_kind, target_count, sent_count, failed_count, created_at) "
        "VALUES (?, ?, 'text', ?, 0, 0, ?)",
        (business.tenant_id, segment, len(targets), now),
    )
    run_id = int(cursor.lastrowid or 0)
    sent = failed = 0
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            for target in targets:
                try:
                    await bot.send_message(chat_id=int(target["telegram_user_id"]), text=text)
                    sent += 1
                except RetryAfter as exc:
                    wait = getattr(exc, "retry_after", 1)
                    if hasattr(wait, "total_seconds"):
                        wait = wait.total_seconds()
                    await asyncio.sleep(min(max(float(wait), 0.5), 30.0))
                    try:
                        await bot.send_message(chat_id=int(target["telegram_user_id"]), text=text)
                        sent += 1
                    except Exception:
                        failed += 1
                except (Forbidden, BadRequest, NetworkError, TimedOut):
                    failed += 1
    finally:
        token = ""
    business.conn.execute(
        "UPDATE tenant_broadcast_runs SET sent_count=?, failed_count=?, finished_at=? "
        "WHERE id=? AND tenant_id=?",
        (sent, failed, iso_utc(utcnow()), run_id, business.tenant_id),
    )
    business.conn.commit()
    return {"target": len(targets), "sent": sent, "failed": failed}


async def _channel_menu(update: Update, business: Any, actor: int) -> None:
    settings = business.userbot_settings_admin(actor)
    channel = str(settings.get("channel_id") or "").strip() or "تنظیم نشده"
    await _edit_or_send(
        update,
        "📢 مدیریت کانال\n"
        f"کانال فعلی: {channel}\n\n"
        "ربات کاربران باید در کانال مقصد دسترسی ارسال پیام داشته باشد.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ تنظیم کانال", callback_data="channelpost:set")],
            [InlineKeyboardButton("🚀 انتشار متن در کانال", callback_data="channelpost:publish")],
            [InlineKeyboardButton("🔙 بازگشت به مدیریت ربات کاربران", callback_data="userbot:menu")],
        ]),
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
    s = business.userbot_settings_admin(actor)
    if section == "subscription":
        kb = _setting_toggle_keyboard(
            "اشتراک", s,
            [
                ("show_user_page_link", "نمایش لینک صفحه یوزر هیدیفای"),
                ("show_username", "نمایش نام کاربری"),
                ("shuffle_configs", "تصادفی کردن کانفیگ‌ها"),
            ],
            "userbot:settings_menu",
        )
        kb.inline_keyboard.insert(-1, [InlineKeyboardButton("🔔یادآور وضعیت اشتراک", callback_data="userbot:settings:reminders")])
        kb.inline_keyboard.insert(-1, [InlineKeyboardButton("🎊مشخصات اشتراک تستی", callback_data="userbot:settings:trial")])
        await _edit_or_send(update, "🛍 تنظیمات اشتراک", kb)
        return
    if section == "sub_link_status":
        await _edit_or_send(
            update,
            "📁 وضعیت نمایش لینک اشتراک",
            _setting_toggle_keyboard(
                "لینک", s,
                [
                    ("show_direct_config", "کانفیگ مستقیم"),
                    ("show_sub_link", "لینک اشتراک"),
                    ("show_smart_link", "لینک اشتراک هوشمند"),
                ],
                "userbot:settings_menu",
            ),
        )
        return
    if section == "buy_renew":
        await _edit_or_send(
            update,
            "🛒 تنظیمات خرید و تمدید",
            _setting_toggle_keyboard(
                "خرید", s,
                [
                    ("enable_buy", "امکان خرید اشتراک"),
                    ("enable_renew", "امکان تمدید اشتراک"),
                    ("show_renew_in_main_menu", "دکمه تمدید اشتراک در منوی اصلی"),
                ],
                "userbot:settings_menu",
            ),
        )
        return
    if section == "ui":
        rows = [
            [InlineKeyboardButton(f"رنگی بودن دکمه‌ها | {_bool_icon(s.get('colored_buttons'))}", callback_data="userbot:settings:toggle:colored_buttons:userbot:settings:ui")],
            [InlineKeyboardButton("✨ هوشمند", callback_data="userbot:settings:value:button_theme:smart:userbot:settings:ui")],
            [InlineKeyboardButton("🛒 فروشگاهی", callback_data="userbot:settings:value:button_theme:shop:userbot:settings:ui")],
            [InlineKeyboardButton("💼 حرفه‌ای", callback_data="userbot:settings:value:button_theme:pro:userbot:settings:ui")],
            [InlineKeyboardButton("🕊 مینیمال", callback_data="userbot:settings:value:button_theme:minimal:userbot:settings:ui")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, f"🎨 تنظیم ظاهر دکمه‌ها\nطرح فعلی: {s.get('button_theme')}", InlineKeyboardMarkup(rows))
        return
    if section == "tx_plans":
        rows = [
            [InlineKeyboardButton(f"🔢 ترتیب پلن‌ها | {s.get('plan_sort_mode')}", callback_data="userbot:settings:plansort")],
            [InlineKeyboardButton(f"📋 ستون‌های پلن‌ها | {s.get('plan_columns')}", callback_data="userbot:settings:plancol")],
            [InlineKeyboardButton(f"🛰 ستون‌های سرورها | {s.get('server_columns')}", callback_data="userbot:settings:servercol")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, "🧮 تنظیمات تراکنشات و پلن ها", InlineKeyboardMarkup(rows))
        return
    if section == "texts":
        rows = [
            [InlineKeyboardButton("🔔پیام خوش آمدگویی", callback_data="userbot:settings:text:welcome_message")],
            [InlineKeyboardButton("📕متن سوالات متداول", callback_data="userbot:settings:text:faq_text")],
            [InlineKeyboardButton("💡متن راهنما", callback_data="userbot:settings:text:guide_text")],
            [InlineKeyboardButton("🛰️متن لیست سرورها", callback_data="userbot:settings:text:servers_list_text")],
            [InlineKeyboardButton("📋متن لیست پلن‌ها", callback_data="userbot:settings:text:plans_list_text")],
            [InlineKeyboardButton("📬متن پنل تیکت", callback_data="userbot:settings:text:ticket_panel_text")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, "🧾 تنظیمات متون", InlineKeyboardMarkup(rows))
        return
    if section == "marketing":
        growth = business.growth_settings(actor)
        rows = [
            [InlineKeyboardButton(f"🤝 رفرال | {_bool_icon(growth.get('referral_enabled'))}", callback_data="userbot:referral:toggle")],
            [InlineKeyboardButton(f"🎁 تست رایگان | {_bool_icon(growth.get('trial_enabled'))}", callback_data="userbot:settings:trial")],
            [InlineKeyboardButton("🏷 مدیریت کوپن‌ها", callback_data="userbot:gifts:coupons")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ]
        await _edit_or_send(update, "🎯 تنظیمات بازاریابی", InlineKeyboardMarkup(rows))
        return
    if section == "force_join":
        rows = [
            [InlineKeyboardButton(f"🔒 عضویت اجباری | {_bool_icon(s.get('force_join_enabled'))}", callback_data="userbot:settings:toggle:force_join_enabled:userbot:settings:force_join")],
            [InlineKeyboardButton("📢 تنظیم کانال", callback_data="userbot:settings:force_join:set_channel")],
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
        rows = [
            [InlineKeyboardButton(
                f"{'✅' if x['status']=='active' else '❌'} {x['title']} · {x['currency']}",
                callback_data=f"userbot:settings:payment:method:{int(x['id'])}",
            )]
            for x in methods
        ]
        rows.extend([
            [InlineKeyboardButton("➕ افزودن روش پرداخت", callback_data="userbot:settings:payment:add")],
            [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:settings_menu")],
        ])
        await _edit_or_send(update, "💳 تنظیمات پرداخت", InlineKeyboardMarkup(rows))
        return
    if section == "backup_restore":
        await _edit_or_send(
            update,
            "🗂️ تنظیمات بکاپ و بازیابی\n"
            "بکاپ Tenant شامل تنظیمات ربات کاربران، پلن‌ها، روش‌های پرداخت و کوپن‌هاست.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 دریافت بکاپ Tenant", callback_data="userbot:settings:backup:download")],
                [InlineKeyboardButton("📤 بازیابی بکاپ Tenant", callback_data="userbot:settings:backup:restore")],
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
            if table in ("tenant_sale_plans", "tenant_payment_methods", "tenant_coupons"):
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
                if table in ("tenant_sale_plans", "tenant_payment_methods", "tenant_coupons"):
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

    if data.startswith("userbot:user:"):
        parts = data.split(":")
        customer_id = int(parts[2])
        if len(parts) == 3:
            await _send_user_profile(update,business,actor,customer_id); return True
        action = parts[3]
        if action == "services":
            items = business.subscriptions_for_customer_admin(actor, customer_id=customer_id)
            rows = [[InlineKeyboardButton(
                f"📦 #{x['id']} · {x['plan_name']} · {x['status']}",
                callback_data=f"search:sel:{int(x['id'])}",
            )] for x in items]
            rows.append([InlineKeyboardButton("👤بازگشت به پروفایل", callback_data=f"userbot:user:{customer_id}")])
            await _edit_or_send(update, f"📋 لیست سرویس‌ها\nتعداد: {len(items)}", InlineKeyboardMarkup(rows)); return True
        if action == "orders":
            items = business.customer_orders_admin(actor, customer_id=customer_id)
            rows = [[InlineKeyboardButton(f"#{x['id']} · {x['status']}", callback_data=f"userbot:order:{int(x['id'])}")] for x in items[:50]]
            rows.append([InlineKeyboardButton("👤بازگشت به پروفایل", callback_data=f"userbot:user:{customer_id}")])
            await _edit_or_send(update, f"📗 لیست سفارشات\nتعداد: {len(items)}", InlineKeyboardMarkup(rows)); return True
        if action == "payments":
            items = business.customer_receipts_admin(actor, customer_id=customer_id)
            rows = [[InlineKeyboardButton(f"#{x['id']} · {x['status']}", callback_data=f"userbot:pay:detail:{int(x['id'])}")] for x in items[:50]]
            rows.append([InlineKeyboardButton("👤بازگشت به پروفایل", callback_data=f"userbot:user:{customer_id}")])
            await _edit_or_send(update, f"💵 لیست تراکنشات\nتعداد: {len(items)}", InlineKeyboardMarkup(rows)); return True
        if action == "wallet":
            wallet = business.customer_wallet_admin(actor, customer_id=customer_id)
            current = list(wallet.get("accounts") or [])
            text = "💳 ویرایش کیف پول\n" + ("\n".join(f"• {x['currency']}: {int(x['balance']):,}" for x in current) or "• موجودی ثبت نشده") + "\n\nمقدار جدید را به شکل «IRR | 50000» وارد کنید."
            context.user_data[FLOW_KEY]={"kind":"wallet_set","customer_id":customer_id}
            await query.message.reply_text(text, reply_markup=userbot_cancel_keyboard()); return True
        if action == "reset_trial":
            business.reset_customer_trial_admin(actor, customer_id=customer_id)
            await _send_user_profile(update,business,actor,customer_id); return True
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
        await _send_payment_detail(update,business,actor,int(data.rsplit(":",1)[1])); return True
    if data.startswith("userbot:pay:review:"):
        parts=data.split(":"); rid=int(parts[3]); approve=parts[4]=="yes"
        result=business.review_receipt(actor,rid,approve=approve)
        if approve and result.get("status")=="paid":
            try: business.fulfill_paid_order(actor,order_id=int(result["order_id"]))
            except TenantBusinessError: pass
        await _send_payment_detail(update,business,actor,rid); return True

    if data == "userbot:gifts_menu":
        await _send_gifts_menu(update,business,actor); return True
    if data == "userbot:gifts:dashboard":
        await _send_gifts_menu(update,business,actor); return True
    if data == "userbot:gifts:coupons":
        await _send_coupons(update,business,actor); return True
    if data == "userbot:gifts:coupons:add":
        context.user_data[FLOW_KEY]={"kind":"coupon_add"}
        await query.message.reply_text(
            "🏷 کوپن جدید را وارد کنید:\n"
            "CODE | percent/fixed | VALUE | CURRENCY | MAX_USES | EXPIRES_AT(optional)\n"
            "مثال: OFF20 | percent | 20 | IRR | 100",
            reply_markup=userbot_cancel_keyboard(),
        ); return True
    if data.startswith("userbot:gifts:coupon:toggle:"):
        cid=int(data.rsplit(":",1)[1]); item=business.coupon(cid)
        business.set_coupon_status_admin(actor,coupon_id=cid,enabled=item["status"]!="active")
        await _send_coupon_detail(update,business,actor,cid); return True
    if data.startswith("userbot:gifts:coupon:delete:"):
        cid=int(data.rsplit(":",1)[1])
        await _edit_or_send(update,"❓ از حذف این کوپن مطمئن هستید؟",InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله",callback_data=f"userbot:gifts:coupon:deleteok:{cid}"),
            InlineKeyboardButton("لغو❌",callback_data=f"userbot:gifts:coupon:{cid}"),
        ]])); return True
    if data.startswith("userbot:gifts:coupon:deleteok:"):
        cid=int(data.rsplit(":",1)[1]); business.delete_coupon_admin(actor,coupon_id=cid); await _send_coupons(update,business,actor); return True
    if data.startswith("userbot:gifts:coupon:") and data.rsplit(":",1)[1].isdigit():
        await _send_coupon_detail(update,business,actor,int(data.rsplit(":",1)[1])); return True
    if data.startswith("userbot:gifts:redemptions"):
        coupon_id=None
        parts=data.split(":")
        if len(parts)>3 and parts[-1].isdigit(): coupon_id=int(parts[-1])
        rows=business.list_coupon_redemptions_admin(actor,coupon_id=coupon_id)
        text="📜 گزارش مصرف هدایا\n" + ("\n".join(
            f"• {x['code']} · {x['display_name']} · {int(x['discount_amount']):,} · {x['created_at']}"
            for x in rows[:50]
        ) or "هنوز مصرفی ثبت نشده است.")
        await _edit_or_send(update,text,InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])); return True
    if data == "userbot:gifts:security":
        stats=_gift_stats(business,actor)
        await _edit_or_send(update,
            "🛡 کنترل سوءاستفاده هدایا\n"
            "◈ هر سفارش فقط یک redemption ثبت می‌کند.\n"
            "◈ سقف کل مصرف و سقف هر مشتری در دیتابیس enforce می‌شود.\n"
            "◈ تاریخ شروع/انقضا قبل از اعمال کوپن کنترل می‌شود.\n"
            f"🟢 فعال: {stats['active']} · ⏰ منقضی: {stats['expired']} · 🔒 تکمیل ظرفیت: {stats['full']}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])
        ); return True
    if data == "userbot:gifts:help":
        await _edit_or_send(update,
            "📘 راهنمای مدیریت هدایا\n"
            "1) کوپن درصدی یا مبلغ ثابت بسازید.\n"
            "2) max uses و محدودیت هر مشتری در سطح دیتابیس کنترل می‌شود.\n"
            "3) گزارش مصرف نشان می‌دهد چه کسی از کدام کوپن استفاده کرده است.\n"
            "4) برای کمپین عمومی ظرفیت و تاریخ انقضای محدود انتخاب کنید.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])
        ); return True
    if data == "userbot:gifts:campaign":
        items=[x for x in business.list_coupons_admin(actor) if x["status"]=="active"]
        sample=items[0]["code"] if items else "GIFT-CODE"
        await _edit_or_send(update,
            "📣 متن آماده کمپین هدیه\n\n"
            f"🎁 هدیه ویژه فعال شد!\nکد: {sample}\n"
            "کد را هنگام خرید در ربات وارد کنید.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")]])
        ); return True
    if data == "userbot:gifts:presets":
        await _edit_or_send(update,
            "🎯 قالب‌های آماده کمپین هدیه\n"
            "یک قالب را انتخاب کنید؛ کد به‌صورت درصدی ساخته می‌شود.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 خوش‌آمدگویی 10٪",callback_data="userbot:gifts:preset:WELCOME:10")],
                [InlineKeyboardButton("🔥 جشنواره 20٪",callback_data="userbot:gifts:preset:FEST:20")],
                [InlineKeyboardButton("💎 VIP 30٪",callback_data="userbot:gifts:preset:VIP:30")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:gifts_menu")],
            ])
        ); return True
    if data.startswith("userbot:gifts:preset:"):
        parts=data.split(":"); prefix=parts[3]; value=int(parts[4]); code=f"{prefix}-{str(int(utcnow().timestamp()))[-6:]}"
        business.add_coupon(actor,code=code,discount_kind="percent",value=value,max_uses=100,per_customer_limit=1)
        await _send_coupons(update,business,actor); return True
    if data == "userbot:gifts:bulk":
        context.user_data[FLOW_KEY]={"kind":"coupon_bulk"}
        await query.message.reply_text("🧩 PREFIX | COUNT | PERCENT | MAX_USES_PER_CODE\nمثال: FEST | 10 | 20 | 1",reply_markup=userbot_cancel_keyboard()); return True

    if data == "userbot:referral_menu":
        await _send_referral_menu(update,business,actor); return True
    if data in ("userbot:referral:dashboard","userbot:referral:toggle"):
        if data.endswith(":toggle"):
            cur=business.growth_settings(actor); business.update_growth_settings(actor,referral_enabled=not bool(cur["referral_enabled"]))
        await _send_referral_menu(update,business,actor); return True
    if data == "userbot:referral:settings":
        s=business.growth_settings(actor)
        await _edit_or_send(update,
            "⚙️ تنظیمات رفرال\n"
            f"پاداش تست: {int(s['referral_trial_reward']):,} {s['referral_currency']}\n"
            f"پاداش خرید: {int(s['referral_purchase_reward']):,} {s['referral_currency']}\n"
            f"حداقل خرید: {int(s['referral_min_purchase']):,}\n"
            f"سقف پاداش: {int(s['referral_max_rewards']) or 'نامحدود'}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ مبلغ پاداش تست",callback_data="userbot:referral:edit:trial")],
                [InlineKeyboardButton("✏️ مبلغ پاداش خرید",callback_data="userbot:referral:edit:purchase")],
                [InlineKeyboardButton("✏️ سقف دعوت موفق",callback_data="userbot:referral:edit:max")],
                [InlineKeyboardButton("✏️ حداقل مبلغ خرید",callback_data="userbot:referral:edit:min")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")],
            ])
        ); return True
    if data.startswith("userbot:referral:edit:"):
        field=data.rsplit(":",1)[1]; context.user_data[FLOW_KEY]={"kind":"referral_edit","field":field}
        await query.message.reply_text("مقدار عددی جدید را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:referral:list:"):
        items=business.referrals_admin(actor); selected,page,pages=_page(items,int(data.rsplit(":",1)[1]))
        text="👥 لیست دعوت‌ها\n"+("\n".join(f"#{x['id']} · {x['inviter_name']} ← {x['invitee_name']} · {x['status']}" for x in selected) or "موردی نیست.")
        rows=[]; nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️",callback_data=f"userbot:referral:list:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{pages}",callback_data="userbot:noop"))
        if page<pages: nav.append(InlineKeyboardButton("▶️",callback_data=f"userbot:referral:list:{page+1}"))
        rows.append(nav); rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")])
        await _edit_or_send(update,text,InlineKeyboardMarkup(rows)); return True
    if data.startswith("userbot:referral:rewards:"):
        items=business.referral_rewards_admin(actor); selected,page,pages=_page(items,int(data.rsplit(":",1)[1]))
        text="💰 لیست پاداش‌ها\n"+("\n".join(f"#{x['id']} · {x['inviter_name']} · {int(x['amount']):,} {x['currency']} · {x['reward_type']}" for x in selected) or "موردی نیست.")
        rows=[]; nav=[]
        if page>1: nav.append(InlineKeyboardButton("◀️",callback_data=f"userbot:referral:rewards:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{pages}",callback_data="userbot:noop"))
        if page<pages: nav.append(InlineKeyboardButton("▶️",callback_data=f"userbot:referral:rewards:{page+1}"))
        rows.append(nav); rows.append([InlineKeyboardButton("🔙بازگشت",callback_data="userbot:referral_menu")])
        await _edit_or_send(update,text,InlineKeyboardMarkup(rows)); return True
    if data == "userbot:referral:manual":
        context.user_data[FLOW_KEY]={"kind":"referral_manual"}
        await query.message.reply_text("🧾 Customer ID | AMOUNT | CURRENCY\nمثال: 12 | 50000 | IRR",reply_markup=userbot_cancel_keyboard()); return True

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
        context.user_data[FLOW_KEY]={"kind":"broadcast","segment":segment}
        await query.message.reply_text(
            f"✍ لطفا پیام خود را برای ارسال به «{SEGMENT_LABELS[segment]}» وارد کنید:\n"
            f"تعداد فعلی گیرنده‌ها: {len(targets)}",
            reply_markup=userbot_cancel_keyboard()
        ); return True

    if data == "channelpost:menu":
        await _channel_menu(update,business,actor); return True
    if data == "channelpost:set":
        context.user_data[FLOW_KEY]={"kind":"channel_set"}
        await query.message.reply_text("📢 @channel یا -100... را ارسال کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data == "channelpost:publish":
        context.user_data[FLOW_KEY]={"kind":"channel_publish"}
        await query.message.reply_text("✍ متن پست کانال را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True

    if data == "userbot:settings_menu":
        await _settings_root(update,business,actor); return True
    if data.startswith("userbot:settings:toggle:"):
        parts=data.split(":"); key=parts[3]; back=":".join(parts[4:])
        business.toggle_userbot_setting_admin(actor,key=key)
        if back.startswith("userbot:settings:"):
            await _settings_section(update,business,actor,back.rsplit(":",1)[1])
        else:
            await _settings_root(update,business,actor)
        return True
    if data.startswith("userbot:settings:value:"):
        parts=data.split(":"); key=parts[3]; value=parts[4]; business.set_userbot_setting_admin(actor,key=key,value=value)
        await _settings_section(update,business,actor,"ui"); return True
    if data.startswith("userbot:settings:text:"):
        key=data.rsplit(":",1)[1]; current=business.userbot_settings_admin(actor).get(key) or "—"
        context.user_data[FLOW_KEY]={"kind":"setting_text","key":key}
        await query.message.reply_text(f"📝 مقدار فعلی:\n{current}\n\nمتن جدید را ارسال کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data == "userbot:settings:plansort":
        s=business.userbot_settings_admin(actor); order=["id","price_asc","price_desc","traffic_asc","traffic_desc"]; current=str(s.get("plan_sort_mode")); value=order[(order.index(current)+1)%len(order)] if current in order else "id"; business.set_userbot_setting_admin(actor,key="plan_sort_mode",value=value); await _settings_section(update,business,actor,"tx_plans"); return True
    if data == "userbot:settings:plancol":
        s=business.userbot_settings_admin(actor); value=(int(s.get("plan_columns") or 1)%3)+1; business.set_userbot_setting_admin(actor,key="plan_columns",value=value); await _settings_section(update,business,actor,"tx_plans"); return True
    if data == "userbot:settings:servercol":
        s=business.userbot_settings_admin(actor); value=(int(s.get("server_columns") or 1)%3)+1; business.set_userbot_setting_admin(actor,key="server_columns",value=value); await _settings_section(update,business,actor,"tx_plans"); return True
    if data == "userbot:settings:trial":
        g=business.growth_settings(actor)
        await _edit_or_send(update,
            f"🎊 مشخصات اشتراک تستی\nوضعیت: {_bool_icon(g.get('trial_enabled'))}\nحجم: {g.get('trial_traffic_gb')}GB\nمدت: {g.get('trial_duration_days')} روز",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 روشن/خاموش تست",callback_data="userbot:settings:trial:toggle")],
                [InlineKeyboardButton("📊 حجم تست",callback_data="userbot:settings:trial:traffic")],
                [InlineKeyboardButton("📆 مدت تست",callback_data="userbot:settings:trial:days")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:settings:subscription")],
            ])
        ); return True
    if data.startswith("userbot:settings:trial:"):
        action=data.rsplit(":",1)[1]; g=business.growth_settings(actor)
        if action=="toggle":
            g=business.update_growth_settings(
                actor,trial_enabled=not bool(g["trial_enabled"])
            )
            await _edit_or_send(
                update,
                f"🎊 مشخصات اشتراک تستی\nوضعیت: {_bool_icon(g.get('trial_enabled'))}\n"
                f"حجم: {g.get('trial_traffic_gb')}GB\nمدت: {g.get('trial_duration_days')} روز",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔥 روشن/خاموش تست",callback_data="userbot:settings:trial:toggle")],
                    [InlineKeyboardButton("📊 حجم تست",callback_data="userbot:settings:trial:traffic")],
                    [InlineKeyboardButton("📆 مدت تست",callback_data="userbot:settings:trial:days")],
                    [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:settings:subscription")],
                ]),
            )
            return True
        context.user_data[FLOW_KEY]={"kind":"trial_edit","field":action}
        await query.message.reply_text("مقدار عددی جدید را وارد کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data == "userbot:settings:reminders":
        await _edit_or_send(update,
            "🔔 یادآور وضعیت اشتراک\nیادآورها توسط Runtime مشترک WhiteLabel اجرا می‌شوند و برای هر Tenant ایزوله هستند.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙بازگشت",callback_data="userbot:settings:subscription")]])
        ); return True
    if data == "userbot:settings:force_join:set_channel":
        context.user_data[FLOW_KEY]={"kind":"force_join_channel"}
        await query.message.reply_text("📢 @channel یا -100... را ارسال کنید:",reply_markup=userbot_cancel_keyboard()); return True
    if data == "userbot:settings:payment:add":
        context.user_data[FLOW_KEY]={"kind":"payment_add"}
        await query.message.reply_text("💳 kind(card/crypto) | title | currency | destination | network(optional) | instructions(optional)",reply_markup=userbot_cancel_keyboard()); return True
    if data.startswith("userbot:settings:payment:method:"):
        mid=int(data.rsplit(":",1)[1]); item=next((x for x in business.list_payment_methods_admin(actor) if int(x["id"])==mid),None)
        if item is None: raise TenantBusinessError("payment method not found")
        await _edit_or_send(update,
            f"💳 {item['title']}\nنوع: {item['kind']}\nارز: {item['currency']}\nمقصد: {item['destination']}\nوضعیت: {item['status']}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⏸/▶️ تغییر وضعیت",callback_data=f"userbot:settings:payment:toggle:{mid}")],
                [InlineKeyboardButton("🔙بازگشت",callback_data="userbot:settings:payment")],
            ])
        ); return True
    if data.startswith("userbot:settings:payment:toggle:"):
        mid=int(data.rsplit(":",1)[1]); item=next(x for x in business.list_payment_methods_admin(actor) if int(x["id"])==mid); business.set_payment_method_status_admin(actor,method_id=mid,enabled=item["status"]!="active"); await _settings_section(update,business,actor,"payment"); return True
    if data == "userbot:settings:backup:download":
        await _send_backup(update,business); return True
    if data == "userbot:settings:backup:restore":
        context.user_data[FLOW_KEY]={"kind":"backup_restore"}
        await query.message.reply_text("📤 فایل JSON بکاپ همین Tenant را ارسال کنید.",reply_markup=userbot_cancel_keyboard()); return True

    if data.startswith("userbot:settings:"):
        section=data.rsplit(":",1)[1]
        if section in {"subscription","sub_link_status","ui","buy_renew","tx_plans","texts","marketing","force_join","payment","backup_restore"}:
            await _settings_section(update,business,actor,section); return True

    return True


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
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
        if kind=="wallet_set":
            parts=[x.strip() for x in text.split("|")]
            if len(parts)!=2: raise ValueError("wallet format")
            currency=parts[0].upper(); target=int(parts[1].replace(",","")); cid=int(flow["customer_id"])
            wallet=business.customer_wallet_admin(actor,customer_id=cid); current=next((int(x["balance"]) for x in wallet["accounts"] if x["currency"]==currency),0); delta=target-current
            if delta: business.adjust_wallet_admin(actor,customer_id=cid,currency=currency,amount=delta,note="Admin set wallet balance")
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ موجودی کیف پول بروزرسانی شد.",reply_markup=admin_main_keyboard()); return True
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
            rid=int(text.lstrip("#")); context.user_data.pop(FLOW_KEY,None)
            await update.effective_message.reply_text("✅ تراکنش یافت شد.",reply_markup=admin_main_keyboard())
            # send a synthetic callback-like detail as a new message
            pay=business.receipt_admin(actor,receipt_id=rid)
            await update.effective_message.reply_text(
                f"◈ شناسه تراکنش: {rid}\n👤 {pay['display_name']}\n💰 {int(pay['amount']):,} {pay['currency']}\nوضعیت: {pay['status']}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("جزئیات",callback_data=f"userbot:pay:detail:{rid}")]])
            ); return True
        if kind=="coupon_add":
            parts=[x.strip() for x in text.split("|")]
            if len(parts)<3: raise ValueError("coupon format")
            code,discount_kind,value=parts[:3]; currency=parts[3] if len(parts)>3 else ""; max_uses=int(parts[4]) if len(parts)>4 and parts[4] else 0; expires=parts[5] if len(parts)>5 else ""
            business.add_coupon(actor,code=code,discount_kind=discount_kind,value=int(value),currency=currency,max_uses=max_uses,per_customer_limit=1,expires_at=expires)
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ کوپن ساخته شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="coupon_bulk":
            parts=[x.strip() for x in text.split("|")]
            if len(parts)!=4: raise ValueError("bulk format")
            prefix,count,percent,max_uses=parts[0],int(parts[1]),int(parts[2]),int(parts[3])
            if not 1<=count<=100: raise ValueError("bulk count")
            codes=[]
            stamp=str(int(utcnow().timestamp()))[-6:]
            for i in range(count):
                code=f"{prefix.upper()}-{stamp}-{i+1:02d}"
                business.add_coupon(actor,code=code,discount_kind="percent",value=percent,max_uses=max_uses,per_customer_limit=1)
                codes.append(code)
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ کدها ساخته شدند:\n"+"\n".join(codes),reply_markup=admin_main_keyboard()); return True
        if kind=="referral_edit":
            value=int(text.replace(",","")); field=str(flow["field"]); kwargs={}
            if field=="trial": kwargs["referral_trial_reward"]=value
            elif field=="purchase": kwargs["referral_purchase_reward"]=value
            elif field=="max": kwargs["referral_max_rewards"]=value
            elif field=="min": kwargs["referral_min_purchase"]=value
            else: raise ValueError("referral field")
            business.update_growth_settings(actor,**kwargs); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ تنظیمات رفرال ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="referral_manual":
            parts=[x.strip() for x in text.split("|")]
            if len(parts)!=3: raise ValueError("manual referral")
            cid,amount,currency=int(parts[0]),int(parts[1].replace(",","")),parts[2].upper()
            business.adjust_wallet_admin(actor,customer_id=cid,currency=currency,amount=amount,note="manual referral reward")
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ پاداش دستی به کیف پول اضافه شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="ticket_reply":
            item=business.reply_ticket_admin(actor,ticket_id=int(flow["ticket_id"]),reply=text)
            try: await _send_via_userbot(business,int(item["telegram_user_id"]),text=f"📩 پاسخ تیکت #{item['id']}\n\n{text}")
            except Exception: pass
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ پاسخ ثبت شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="broadcast":
            result=await _broadcast_text(business,actor,str(flow["segment"]),text)
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text(
                f"✅ ارسال همگانی تمام شد.\nموفق: {result['sent']}\nناموفق: {result['failed']}\nکل هدف: {result['target']}",
                reply_markup=admin_main_keyboard()
            ); return True
        if kind=="channel_set":
            business.set_userbot_setting_admin(actor,key="channel_id",value=text); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ کانال ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="channel_publish":
            channel=str(business.userbot_settings_admin(actor).get("channel_id") or "").strip()
            if not channel: raise TenantBusinessError("channel is not configured")
            await _send_via_userbot(business,channel,text=text); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ در کانال منتشر شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="setting_text":
            business.set_userbot_setting_admin(actor,key=str(flow["key"]),value=text); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ متن ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="trial_edit":
            value=int(text); field=str(flow["field"]); kwargs={"trial_traffic_gb":value} if field=="traffic" else {"trial_duration_days":value}; business.update_growth_settings(actor,**kwargs); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ مشخصات تست ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="force_join_channel":
            business.set_userbot_setting_admin(actor,key="force_join_channel",value=text); context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ کانال عضویت اجباری ذخیره شد.",reply_markup=admin_main_keyboard()); return True
        if kind=="payment_add":
            parts=[x.strip() for x in text.split("|")]
            if len(parts)<4: raise ValueError("payment format")
            business.add_payment_method(actor,kind=parts[0],title=parts[1],currency=parts[2],destination=parts[3],network=parts[4] if len(parts)>4 else "",instructions=parts[5] if len(parts)>5 else "")
            context.user_data.pop(FLOW_KEY,None); await update.effective_message.reply_text("✅ روش پرداخت اضافه شد.",reply_markup=admin_main_keyboard()); return True
    except (ValueError,TenantBusinessError):
        await update.effective_message.reply_text("❌ مقدار یا وضعیت معتبر نیست. دوباره تلاش کنید.",reply_markup=userbot_cancel_keyboard())
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
    flow=context.user_data.get(FLOW_KEY)
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
