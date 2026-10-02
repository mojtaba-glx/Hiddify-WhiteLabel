"""Tenant AdminBot handlers.

All tenant-admin Telegram menus, callbacks and text flows live in this module.
User-shop handlers are intentionally kept out of this package.
"""

from __future__ import annotations

import sqlite3
from html import escape
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from Gateway.catalog import RuntimeBotSpec
from Shared.timeutils import parse_utc, utcnow
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.common import _deny_update, _services, runtime_access_gate, runtime_error

def _money_lines(items: list[dict]) -> list[str]:
    if not items:
        return ["• مبلغ تأییدشده: 0"]
    return [
        f"• {int(item.get('amount') or 0):,} {item.get('currency') or ''} "
        f"({int(item.get('count') or 0)} پرداخت)"
        for item in items
    ]


def _report_text(report: dict, *, dashboard: bool = False) -> str:
    current = dict(report.get("current") or {})
    lines = [
        "📊 داشبورد امروز" if dashboard else "📈 گزارش فروش",
        f"🗓 {report.get('period_label') or '-'}",
        "",
        "💰 دریافتی تأییدشده",
        *_money_lines(list(report.get("totals") or [])),
    ]
    operations = list(report.get("operations") or [])
    if operations:
        lines.extend(["", "🛒 خرید و تمدید"])
        for item in operations:
            currency = item.get("currency") or ""
            lines.append(
                f"• خرید: {int(item.get('purchase_count') or 0)} مورد — "
                f"{int(item.get('purchase_amount') or 0):,} {currency}"
            )
            lines.append(
                f"• تمدید: {int(item.get('renewal_count') or 0)} مورد — "
                f"{int(item.get('renewal_amount') or 0):,} {currency}"
            )
            lines.append(
                f"• حجم فروخته/تمدیدشده: {int(item.get('traffic_gb') or 0):,}GB"
            )
    lines.extend([
        "",
        "👥 کاربران",
        f"• خریداران یکتا: {int(report.get('unique_customers') or 0)}",
        f"• کاربران جدید: {int(report.get('new_customers') or 0)}",
        "",
        "🧾 رسیدها",
        f"• تأییدشده: {int(report.get('approved_receipts') or 0)}",
        f"• ردشده: {int(report.get('rejected_receipts') or 0)}",
        "",
        "📡 وضعیت فعلی سرویس‌ها",
        f"• فعال: {int(current.get('subs_active') or 0)}",
        f"• غیرفعال: {int(current.get('subs_disabled') or 0)}",
        f"• منقضی: {int(current.get('subs_expired') or 0)}",
        f"• در انتظار ساخت: {int(current.get('subs_pending') or 0)}",
        f"• انقضا تا ۲۴ ساعت: {int(current.get('expiring_24h') or 0)}",
        f"• مصرف تجمیعی: {int(current.get('usage_bytes') or 0)/(1024**3):.2f}/"
        f"{int(current.get('traffic_bytes') or 0)/(1024**3):.2f}GB",
        "",
        "🖥 زیرساخت",
        f"• سرور فعال: {int(current.get('servers_active') or 0)}",
        f"• نود فعال: {int(current.get('nodes_active') or 0)}",
        "",
        "⚠️ نیازمند توجه",
        f"• رسید در انتظار: {int(current.get('receipts_pending') or 0)}",
        f"• پرداخت شده / تحویل‌نشده: {int(current.get('fulfillment_pending') or 0)}",
        f"• Enforcement pending: {int(current.get('enforcement_pending') or 0)}",
        f"• نود خطادار/Frozen: {int(current.get('node_attention') or 0)}",
        f"• تیکت باز: {int(current.get('tickets_open') or 0)}",
        "",
        f"👤 مشتریان: {int(current.get('customers_active') or 0)} فعال از "
        f"{int(current.get('customers_total') or 0)} کل",
    ])
    return "\n".join(lines)


def _customer_profile_text(profile: dict) -> str:
    username = str(profile.get("username") or "").strip()
    paid = _money_lines(list(profile.get("paid_totals") or []))
    lines = [
        f"👤 {profile.get('display_name') or 'کاربر'}",
        f"🔹 یوزرنیم: {'@' + username.lstrip('@') if username else '-'}",
        f"🔢 Telegram ID: {profile.get('telegram_user_id')}",
        f"🆔 Customer ID: {profile.get('id')}",
        f"وضعیت: {profile.get('status')}",
        "",
        "📦 سرویس‌ها",
        f"• کل: {int(profile.get('subscriptions_total') or 0)}",
        f"• فعال: {int(profile.get('subscriptions_active') or 0)}",
        f"• غیرفعال: {int(profile.get('subscriptions_disabled') or 0)}",
        f"• منقضی: {int(profile.get('subscriptions_expired') or 0)}",
        f"• در انتظار ساخت: {int(profile.get('subscriptions_pending') or 0)}",
        "",
        "💰 مجموع پرداخت",
        *paid,
        "",
        f"🎫 تیکت باز: {int(profile.get('tickets_open') or 0)}",
    ]
    recent = list(profile.get("recent_orders") or [])
    if recent:
        lines.extend(["", "🧾 آخرین سفارش‌ها"])
        for order in recent[:5]:
            op = {
                "renewal": "تمدید",
                "trial": "تست رایگان",
            }.get(str(order.get("operation") or ""), "خرید")
            lines.append(
                f"• #{order['id']} · {op} · {order.get('plan_name') or '-'} · "
                f"{order.get('status')} · {int(order.get('amount') or 0):,} "
                f"{order.get('currency') or ''}"
            )
    return "\n".join(lines)



def _wallet_text(summary: dict) -> str:
    accounts = list(summary.get("accounts") or [])
    history = list(summary.get("history") or [])
    lines = ["💰 کیف پول", ""]
    if accounts:
        lines.extend(
            f"• {int(x.get('balance') or 0):,} {x.get('currency') or ''}"
            for x in accounts
        )
    else:
        lines.append("• موجودی: 0")
    if history:
        lines.extend(["", "🧾 آخرین تراکنش‌ها"])
        labels = {
            "topup": "شارژ کیف پول",
            "admin_credit": "شارژ ادمین",
            "admin_debit": "کسر ادمین",
            "referral_trial": "پاداش دعوت/تست",
            "referral_purchase": "پاداش دعوت/خرید",
            "purchase": "پرداخت سفارش",
            "refund": "برگشت",
        }
        for tx in history[:10]:
            amount = int(tx.get("amount") or 0)
            lines.append(
                f"• {labels.get(str(tx.get('kind')), str(tx.get('kind') or '-'))}: "
                f"{amount:+,} {tx.get('currency') or ''} → "
                f"{int(tx.get('resulting_balance') or 0):,}"
            )
    return "\n".join(lines)



def _growth_text(settings: dict, coupons: list[dict]) -> str:
    return "\n".join([
        "🎯 فروش پیشرفته",
        "",
        f"🤝 Referral: {'فعال' if int(settings.get('referral_enabled') or 0) else 'خاموش'}",
        f"• پاداش تست: {int(settings.get('referral_trial_reward') or 0):,} {settings.get('referral_currency') or ''}",
        f"• پاداش اولین خرید: {int(settings.get('referral_purchase_reward') or 0):,} {settings.get('referral_currency') or ''}",
        f"• حداقل خرید: {int(settings.get('referral_min_purchase') or 0):,}",
        f"• سقف پاداش هر دعوت‌کننده: {int(settings.get('referral_max_rewards') or 0) or 'نامحدود'}",
        "",
        f"🎁 تست رایگان: {'فعال' if int(settings.get('trial_enabled') or 0) else 'خاموش'}",
        f"• حجم: {int(settings.get('trial_traffic_gb') or 0)}GB",
        f"• مدت: {int(settings.get('trial_duration_days') or 0)} روز",
        "",
        f"🎟 کوپن‌ها: {len(coupons)}",
    ])



SEARCH_PAGE_SIZE = 21


def _search_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 جستجوی هوشمند کاربر", callback_data="searchmenu:smart")],
        [InlineKeyboardButton("📊پیگیری اشتراک", callback_data="searchmenu:tracking")],
        [InlineKeyboardButton("⚠️ لیست کاربران منقضی شده", callback_data="searchmenu:expired")],
        [InlineKeyboardButton("♻️ اشتراک‌های منقضی‌شده", callback_data="searchmenu:expired_profiles")],
        [InlineKeyboardButton("🧹 بررسی رکوردهای مشکوک/قدیمی", callback_data="searchmenu:review_old")],
        [InlineKeyboardButton("بازگشت🔙", callback_data="searchmenu:back_main")],
    ])


def _search_status_emoji(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "active":
        return "🔵"
    if value == "expired":
        return "🔴"
    return "🟡"


def _search_results_view(
    results: list[dict], *, page: int = 1, title: str = "[📥 نتیجه جستجو]"
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(results)
    pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    page = max(1, min(int(page), pages))
    start = (page - 1) * SEARCH_PAGE_SIZE
    selected = results[start:start + SEARCH_PAGE_SIZE]

    active = sum(1 for x in results if str(x.get("status")) == "active")
    expired = sum(1 for x in results if str(x.get("status")) == "expired")
    inactive = max(0, total - active - expired)
    text = (
        f"{title}\n"
        "#️⃣ لیست کاربران\n"
        "شما می‌توانید لیست کاربران و اطلاعات آن‌ها را اینجا مشاهده کنید\n"
        f"👤 تعداد کاربران: {total}\n"
        f"🔵 کاربران فعال: {active}\n"
        f"🟡 کاربران غیرفعال/درانتظار: {inactive}\n"
        f"🔴 کاربران منقضی: {expired}"
    )

    buttons = [
        InlineKeyboardButton(
            f"{str(item.get('display_name') or 'کاربر')[:20]}{_search_status_emoji(str(item.get('status') or ''))}",
            callback_data=f"search:sel:{int(item['id'])}",
        )
        for item in selected
    ]
    rows = [
        list(reversed(buttons[i:i + 3]))
        for i in range(0, len(buttons), 3)
    ]
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"search:page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="search:noop"))
        if page < pages:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"search:page:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("بازگشت🔙", callback_data="search:back")])
    return text, InlineKeyboardMarkup(rows)


def _subscription_detail_view(
    business, actor: int, subscription_id: int
) -> tuple[str, InlineKeyboardMarkup]:
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
    expiry = str(item.get("expires_at") or "")
    expire_text = "نامشخص"
    if expiry:
        try:
            delta = parse_utc(expiry) - utcnow()
            days = int(delta.total_seconds() // 86400)
            expire_text = (
                f"منقضی شده ({abs(days)} روز پیش)"
                if delta.total_seconds() < 0
                else f"{max(0, days)} روز دیگر"
            )
        except Exception:
            expire_text = expiry
    last_online = str(item.get("last_online") or "ثبت نشده")
    text = (
        f"👤 کاربر:  {item.get('display_name') or 'کاربر'}\n"
        "❖⬩╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍⬩❖\n"
        f"⬖ سرور:  {item.get('server_label') or 'ثبت نشده'}\n"
        f"📊مصرف: {usage:.2f} از {limit:.2f} گیگابایت\n"
        f"📆انقضا: {expire_text}\n"
        f"{status_text}\n"
        f"📶آخرین اتصال: {last_online}\n"
        f"📝یادداشت: —\n"
        f"🔑 شناسه سرویس: {int(item['id'])}"
    )
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
                "پروفایل کاربر👤",
                callback_data=f"biz:customer:{int(item['customer_id'])}",
            )],
            [InlineKeyboardButton("بازگشت به نتایج🔙", callback_data="search:results")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("کانفیگ ها📄", callback_data=f"search:cfg:{subscription_id}")],
            [InlineKeyboardButton("ویرایش کاربر✏️", callback_data=f"search:edit:{subscription_id}")],
            [InlineKeyboardButton("تمدید اشتراک♾️", callback_data=f"search:renew:{subscription_id}")],
            [InlineKeyboardButton("حذف کاربر🗑️", callback_data=f"search:delete:{subscription_id}")],
            [InlineKeyboardButton("پروفایل کاربر👤", callback_data=f"biz:customer:{int(item['customer_id'])}")],
            [InlineKeyboardButton("بازگشت به نتایج🔙", callback_data="search:results")],
        ]
    return text, InlineKeyboardMarkup(rows)


def _daily_report_text(report: dict) -> str:
    cash = list(report.get("cash") or [])
    services = list(report.get("services") or [])
    cash_count = sum(int(x.get("count") or 0) for x in cash)
    buy_count = sum(int(x.get("buy_count") or 0) for x in services)
    renew_count = sum(int(x.get("renew_count") or 0) for x in services)
    lines = [
        "📊 <b>گزارش کامل روزانه فروش</b>",
        f"📅 تاریخ: <b>{report.get('report_day') or '-'}</b>",
        "",
        "💰 <b>دریافتی واقعی به سیستم</b>",
        f"• رسید خرید/تمدید تأییدشده: {int(report.get('approved_receipts') or 0)} مورد",
        f"• شارژ کیف پول تأییدشده: {int(report.get('approved_topups') or 0)} مورد",
    ]
    if cash:
        for item in cash:
            lines.append(
                f"• {item.get('currency') or ''}: {int(item.get('amount') or 0):,} "
                f"({int(item.get('count') or 0)} پرداخت)"
            )
    else:
        lines.append("• جمع ورودی نقدی: 0")
    lines.extend([
        f"• جمع پرداخت‌های خارجی: <b>{cash_count}</b>",
        f"• رد/ناموفق: {int(report.get('rejected_receipts') or 0) + int(report.get('rejected_topups') or 0)}",
        "",
        "🛒 <b>عملیات موفق سرویس</b>",
        f"• ساخت سرویس: <b>{buy_count}</b> مورد",
        f"• تمدید سرویس: <b>{renew_count}</b> مورد",
        "",
        "👤 <b>فروش مستقیم UserBot</b>",
    ])
    if services:
        for item in services:
            currency = item.get("currency") or ""
            lines.extend([
                f"• خرید: {int(item.get('buy_count') or 0)} مورد — {int(item.get('buy_amount') or 0):,} {currency}",
                f"• تمدید: {int(item.get('renew_count') or 0)} مورد — {int(item.get('renew_amount') or 0):,} {currency}",
                f"• پرداخت از کیف پول: {int(item.get('wallet_count') or 0)} مورد — {int(item.get('wallet_amount') or 0):,} {currency}",
            ])
    else:
        lines.append("• فروش ثبت‌شده: 0")
    lines.extend([
        "",
        f"👥 کاربران جدید: {int(report.get('new_customers') or 0)}",
        "",
        "ℹ️ پرداخت از کیف پول «فروش سرویس» است، اما ورودی نقدی جدید محسوب نمی‌شود.",
        "ℹ️ شارژ کیف پول در دریافتی واقعی ثبت می‌شود و مصرف همان اعتبار دوباره به ورودی نقدی اضافه نمی‌شود.",
        f"🕛 گزارش پایان روز ({report.get('timezone') or 'Asia/Tehran'})",
    ])
    return "\n".join(lines)


BTN_SERVERS = "🖥 مدیریت سرورها"
BTN_SEARCH_USER = "🔍 جستجوی کاربر"
BTN_USERBOT = "🤖 مدیریت ربات کاربران"
BTN_STATUS = "📊 وضعیت سرور"
BTN_BACKUP = "📫 دریافت بکاپ"
BTN_AGENCIES = "🏢 نمایندگی"
BTN_DAILY_REPORT = "📊 گزارش روزانه"

ADMIN_MAIN_BUTTONS = {
    BTN_SERVERS,
    BTN_SEARCH_USER,
    BTN_USERBOT,
    BTN_STATUS,
    BTN_BACKUP,
    BTN_AGENCIES,
    BTN_DAILY_REPORT,
}


def admin_main_keyboard() -> ReplyKeyboardMarkup:
    """Main tenant-admin keyboard kept visually compatible with Hiddify-SellBot."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_SERVERS)],
            [KeyboardButton(BTN_SEARCH_USER), KeyboardButton(BTN_DAILY_REPORT)],
            [KeyboardButton(BTN_USERBOT)],
            [
                KeyboardButton(BTN_STATUS),
                KeyboardButton(BTN_AGENCIES),
                KeyboardButton(BTN_BACKUP),
            ],
        ],
        resize_keyboard=True,
        selective=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Exact bottom cancel keyboard used by Hiddify-SellBot text flows."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ لغو")]],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )


def _server_cancel_keyboard() -> ReplyKeyboardMarkup:
    # Backward-compatible alias for existing server wizards.
    return cancel_keyboard()


def _server_provider_title(server: dict) -> str:
    kind = str(server.get("panel_kind") or "").lower()
    if kind == "xui":
        flavor = str(server.get("xui_flavor") or "").lower()
        return "X-UI سنایی" if flavor == "sanaei" else "X-UI علیرضا"
    if kind == "xnet":
        return "X-NET"
    if kind == "hiddify":
        return "Hiddify"
    return kind or "نامشخص"


def _server_list_view(business) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [InlineKeyboardButton(
            str(item.get("label") or f"سرور #{item['id']}"),
            callback_data=f"srv:view:{int(item['id'])}",
        )]
        for item in business.list_servers()
    ]
    rows.append([
        InlineKeyboardButton("افزودن سرور➕", callback_data="srv:add")
    ])
    return (
        "‏🖥 مدیریت سرورها\n⬇️ لیست سرور های شما",
        InlineKeyboardMarkup(rows),
    )


def _server_detail_view(
    business, actor: int, server_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    server = business.server_admin_summary(actor, server_id=int(server_id))
    title = escape(str(server.get("label") or f"سرور #{server_id}"))
    endpoint = str(server.get("endpoint") or "").strip()
    if endpoint.startswith(("http://", "https://")):
        title_line = f'<a href="{escape(endpoint, quote=True)}">🖥 سرور: {title}</a>'
    else:
        title_line = f"🖥 سرور: {title}"
    limit = int(server.get("users_limit") or 0)
    limit_text = str(limit) if limit > 0 else "نامحدود"
    provider = escape(_server_provider_title(server))
    credential = "✅" if server.get("credential_configured") else "❌"
    text = (
        f"{title_line}\n"
        "❖ • -------------------------- • ❖\n"
        f"👤 تعداد کاربران: {int(server.get('users_count') or 0)} از {limit_text}\n"
        f"📋 تعداد پلن ها: {int(server.get('plans_count') or 0)}\n"
        f"🟩 اولویت: {int(server.get('priority') or 0)}\n"
        f"📦 پنل: {provider}\n"
        f"🔐 دسترسی پنل: {credential}"
    )
    rows = [
        [InlineKeyboardButton("👤لیست کاربران", callback_data=f"srv:users:{server_id}")],
        [InlineKeyboardButton("🛡️عملیات کاربری", callback_data=f"srv:userops:{server_id}")],
        [InlineKeyboardButton("📋پلن ها", callback_data=f"srv:plans:{server_id}")],
        [InlineKeyboardButton("🔗لیست دامنه‌ها", callback_data=f"srv:domains:{server_id}")],
        [InlineKeyboardButton("✏️ویرایش سرور", callback_data=f"srv:edit:{server_id}")],
        [InlineKeyboardButton("🗑️حذف سرور", callback_data=f"srv:delete:{server_id}")],
        [InlineKeyboardButton("⚙️لیست نودها", callback_data=f"srv:nodes:{server_id}")],
        [InlineKeyboardButton("🔄همگام سازی نودها", callback_data=f"srv:sync:{server_id}")],
        [InlineKeyboardButton("❄️ کاربران یخ‌زده این سرور", callback_data=f"srv:frozen:{server_id}")],
        [InlineKeyboardButton("↩️بازگشت", callback_data="biz:servers")],
    ]
    return text, InlineKeyboardMarkup(rows)


def _server_edit_view(server: dict) -> InlineKeyboardMarkup:
    sid = int(server["id"])
    kind = str(server.get("panel_kind") or "")
    rows = [
        [InlineKeyboardButton("📌ویرایش عنوان", callback_data=f"srv:editf:{sid}:label")],
        [InlineKeyboardButton("🌐ویرایش آدرس پنل", callback_data=f"srv:editf:{sid}:endpoint")],
        [InlineKeyboardButton("🗿ویرایش محدودیت کاربر", callback_data=f"srv:editf:{sid}:users_limit")],
        [InlineKeyboardButton("🔢ویرایش اولویت ترتیب", callback_data=f"srv:editf:{sid}:priority")],
    ]
    if kind == "hiddify":
        rows.extend([
            [InlineKeyboardButton("🔐ویرایش کد مسیر ادمین", callback_data=f"srv:editf:{sid}:admin_path")],
            [InlineKeyboardButton("🔐ویرایش کد مسیر کاربران", callback_data=f"srv:editf:{sid}:user_path")],
            [InlineKeyboardButton("🔑ویرایش کلید ادمین (UUID/API)", callback_data=f"srv:editf:{sid}:credential")],
        ])
    elif kind == "xui":
        rows.extend([
            [InlineKeyboardButton("🔑ویرایش دسترسی پنل", callback_data=f"srv:editf:{sid}:credential")],
            [InlineKeyboardButton("🔗ویرایش دامنه ساب", callback_data=f"srv:editf:{sid}:xui_public_origin")],
            [InlineKeyboardButton("🧩ویرایش اینباند", callback_data=f"srv:editf:{sid}:xui_inbound_ids")],
        ])
    elif kind == "xnet":
        rows.extend([
            [InlineKeyboardButton("🔑ویرایش توکن API X-NET", callback_data=f"srv:editf:{sid}:credential")],
            [InlineKeyboardButton("🔗ویرایش دامنه ساب", callback_data=f"srv:editf:{sid}:xnet_public_origin")],
            [InlineKeyboardButton("🧩ویرایش اینباند", callback_data=f"srv:editf:{sid}:xnet_inbound_ids")],
        ])
    rows.extend([
        [InlineKeyboardButton("🗑️حذف سرور", callback_data=f"srv:delete:{sid}")],
        [InlineKeyboardButton("🔙بازگشت", callback_data=f"srv:view:{sid}")],
    ])
    return InlineKeyboardMarkup(rows)


async def _reply_server_prompt(update: Update, text: str) -> None:
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text, reply_markup=_server_cancel_keyboard()
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=_server_cancel_keyboard()
        )


def _menu(spec: RuntimeBotSpec) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 داشبورد", callback_data="biz:dashboard"), InlineKeyboardButton("📈 گزارش فروش", callback_data="biz:reports")],
        [InlineKeyboardButton("🖥 سرورها", callback_data="biz:servers"), InlineKeyboardButton("🔗 نودها", callback_data="biz:nodes")],
        [InlineKeyboardButton("📦 پلن‌های فروش", callback_data="biz:plans"), InlineKeyboardButton("💳 پرداخت", callback_data="biz:payments")],
        [InlineKeyboardButton("🧾 سفارش‌ها", callback_data="biz:orders"), InlineKeyboardButton("📡 سرویس‌ها", callback_data="biz:subs")],
        [InlineKeyboardButton("👥 مشتریان", callback_data="biz:customers"), InlineKeyboardButton("⚠️ نیازمند بررسی", callback_data="biz:attention")],
        [InlineKeyboardButton("🎯 فروش پیشرفته", callback_data="biz:growth"), InlineKeyboardButton("🔗 لینک هوشمند", callback_data="biz:links")],
        [InlineKeyboardButton("🎫 تیکت‌ها", callback_data="biz:tickets")],
        [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
        [InlineKeyboardButton("📊 وضعیت", callback_data="runtime:status")],
    ]
    return InlineKeyboardMarkup(rows)


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, state_store, _ = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot handler registered for non-admin role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
    state = state_store.load(user_id)
    visits = int(state.get("visits") or 0) + 1
    state_store.save(user_id, {**state, "visits": visits, "screen": "home"})
    context.user_data.pop("biz_flow", None)
    text = (
        f"به ربات مدیریت {spec.tenant_name} خوش آمدید 👑\n"
        "از منوی زیر یکی از گزینه‌ها را انتخاب کنید."
    )
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        if update.effective_chat:
            await update.effective_chat.send_message(
                text, reply_markup=admin_main_keyboard()
            )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=admin_main_keyboard()
        )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, policy, state_store, business = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot handler registered for non-admin role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
    decision = policy.check(spec, telegram_user_id=user_id)
    if not decision.allowed:
        await _deny_update(update, decision.reason)
        raise ApplicationHandlerStop
    state = state_store.load(user_id)
    state_store.save(user_id, {**state, "screen": "status"})
    base = (
        f"📊 وضعیت ربات\n\n"
        f"مجموعه: {spec.tenant_name}\n"
        "نوع: AdminBot\n"
        f"لایسنس: {decision.license_status}\n"
        "Runtime: ready"
    )
    try:
        report = business.dashboard_summary(user_id)
        current = dict(report.get("current") or {})
        text = (
            base
            + "\n\n📡 وضعیت کسب‌وکار"
            + f"\nسرویس فعال: {int(current.get('subs_active') or 0)}"
            + f"\nمنقضی: {int(current.get('subs_expired') or 0)}"
            + f"\nدر انتظار تحویل: {int(current.get('fulfillment_pending') or 0)}"
            + f"\nرسید در انتظار: {int(current.get('receipts_pending') or 0)}"
            + f"\nنیازمند Enforcer: {int(current.get('enforcement_pending') or 0)}"
        )
    except (TenantBusinessError, ValueError, sqlite3.Error):
        text = base
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_menu(spec))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_menu(spec))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None:
        return
    data = str(update.callback_query.data or "")
    if data == "runtime:home":
        await show_home(update, context)
        return
    if data == "runtime:status":
        await show_status(update, context)
        return
    spec, _, _, business = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot callback registered for non-admin role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    try:
        if data.startswith(("userbot:", "channelpost:")):
            from TenantRuntime.AdminBot import userbot_management
            if await userbot_management.handle_callback(
                update, context, business=business, actor=actor
            ):
                return

        if data.startswith("searchmenu:"):
            await update.callback_query.answer()
            action = data.split(":", 1)[1]
            if action == "smart":
                msg = update.callback_query.message
                try:
                    await msg.delete()
                except Exception:
                    try:
                        await msg.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                context.user_data["biz_flow"] = {"kind": "search_smart"}
                await msg.reply_text(
                    "🔍 جستجوی هوشمند کاربر در کل ربات\n"
                    "نام کاربر، UUID یا لینک کانفیگ را ارسال کنید.",
                    reply_markup=cancel_keyboard(),
                )
                return
            if action == "tracking":
                context.user_data["biz_flow"] = {"kind": "search_tracking"}
                if update.callback_query and update.callback_query.message:
                    await update.callback_query.message.reply_text(
                        " 🀄️لطفا شناسه اشتراک را وارد کنید:",
                        reply_markup=cancel_keyboard(),
                    )
                return
            if action in ("expired", "expired_profiles"):
                items = business.list_subscriptions_tracking_admin(
                    actor, status="expired"
                )
                context.user_data["smart_search_results"] = items
                title = (
                    "[⚠️لیست کاربران منقضی شده]"
                    if action == "expired"
                    else "♻️ اشتراک‌های منقضی‌شده"
                )
                context.user_data["search_results_title"] = title
                text, kb = _search_results_view(items, title=title)
                await update.callback_query.edit_message_text(text, reply_markup=kb)
                return
            if action == "review_old":
                await update.callback_query.edit_message_text(
                    "🧹 بررسی رکوردهای مشکوک/قدیمی\n\n"
                    "این بخش فقط برای بررسی دستی است و چیزی را خودکار حذف نمی‌کند.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "🟠 روز صفر / وضعیت مشکوک",
                            callback_data="search:old:stale_zero",
                        )],
                        [InlineKeyboardButton(
                            "🟡 اشتراک‌های قدیمیِ شروع‌نشده",
                            callback_data="search:old:unstarted",
                        )],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data="search:back")],
                    ]),
                )
                return
            if action == "back_main":
                try:
                    await update.callback_query.message.delete()
                except Exception:
                    pass
                await update.effective_chat.send_message(
                    "به منوی اصلی برگشتید.",
                    reply_markup=admin_main_keyboard(),
                )
                return

        if data.startswith("search:"):
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action == "noop":
                await update.callback_query.answer()
                return
            if action == "back":
                context.user_data.pop("smart_search_results", None)
                await update.callback_query.edit_message_text(
                    "🔍 جستجوی کاربر", reply_markup=_search_menu_keyboard()
                )
                return
            if action == "results":
                items = list(context.user_data.get("smart_search_results") or [])
                title = str(context.user_data.get("search_results_title") or "[📥 نتیجه جستجو]")
                text, kb = _search_results_view(items, title=title)
                await update.callback_query.edit_message_text(text, reply_markup=kb)
                return
            if action == "page" and len(parts) == 3:
                items = list(context.user_data.get("smart_search_results") or [])
                title = str(context.user_data.get("search_results_title") or "[📥 نتیجه جستجو]")
                text, kb = _search_results_view(items, page=int(parts[2]), title=title)
                await update.callback_query.edit_message_text(text, reply_markup=kb)
                return
            if action == "old" and len(parts) == 3:
                items = business.review_old_subscriptions_admin(
                    actor, kind=parts[2]
                )
                context.user_data["smart_search_results"] = items
                title = (
                    "🟠 روز صفر / وضعیت مشکوک"
                    if parts[2] == "stale_zero"
                    else "🟡 اشتراک‌های قدیمیِ شروع‌نشده"
                )
                context.user_data["search_results_title"] = title
                text, kb = _search_results_view(items, title=title)
                await update.callback_query.edit_message_text(text, reply_markup=kb)
                return
            if action == "sel" and len(parts) == 3:
                text, kb = _subscription_detail_view(
                    business, actor, int(parts[2])
                )
                await update.callback_query.edit_message_text(text, reply_markup=kb)
                return
            if action == "cfg" and len(parts) == 3:
                sid = int(parts[2])
                link = business.admin_subscription_link(
                    actor, subscription_id=sid
                )
                await update.callback_query.edit_message_text(
                    f"کانفیگ ها📄\n\n🔗 لینک اشتراک:\n{link}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "بازگشت🔙", callback_data=f"search:sel:{sid}"
                        )
                    ]]),
                    disable_web_page_preview=True,
                )
                return
            if action == "edit" and len(parts) == 3:
                sid = int(parts[2])
                item = business.subscription_admin(actor, subscription_id=sid)
                edit_rows = []
                if item["status"] in ("active", "disabled"):
                    toggle = "✅ فعال‌سازی" if item["status"] == "disabled" else "⛔ غیرفعال‌سازی"
                    edit_rows.append([
                        InlineKeyboardButton(
                            toggle,
                            callback_data=f"search:editact:{sid}:toggle",
                        )
                    ])
                    edit_rows.append([
                        InlineKeyboardButton("بازنشانی حجم🔄", callback_data=f"search:editact:{sid}:reset_usage"),
                        InlineKeyboardButton("ویرایش حجم📊", callback_data=f"search:editact:{sid}:volume"),
                    ])
                edit_rows.append([
                    InlineKeyboardButton("بازنشانی مدت🔄", callback_data=f"search:editact:{sid}:reset_days"),
                    InlineKeyboardButton("ویرایش مدت📅", callback_data=f"search:editact:{sid}:days"),
                ])
                edit_rows.append([
                    InlineKeyboardButton("بازگشت🔙", callback_data=f"search:sel:{sid}")
                ])
                await update.callback_query.edit_message_text(
                    _subscription_detail_view(business, actor, sid)[0],
                    reply_markup=InlineKeyboardMarkup(edit_rows),
                )
                return
            if action == "editact" and len(parts) == 4:
                sid = int(parts[2])
                edit_action = parts[3]
                item = business.subscription_admin(actor, subscription_id=sid)
                if edit_action == "toggle":
                    enabled = item["status"] != "active"
                    result = business.set_subscription_enabled(
                        actor, subscription_id=sid, enabled=enabled
                    )
                    await update.callback_query.edit_message_text(
                        f"✅ وضعیت کاربر: {result['status']}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("بازگشت🔙", callback_data=f"search:edit:{sid}")
                        ]]),
                    )
                    return
                if edit_action == "reset_usage":
                    business.edit_subscription_terms_admin(
                        actor, subscription_id=sid, reset_usage=True
                    )
                    await update.callback_query.edit_message_text(
                        "✅ حجم مصرف‌شده بازنشانی شد.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("بازگشت🔙", callback_data=f"search:edit:{sid}")
                        ]]),
                    )
                    return
                if edit_action == "reset_days":
                    business.edit_subscription_terms_admin(
                        actor, subscription_id=sid, reset_days=True
                    )
                    await update.callback_query.edit_message_text(
                        "✅ مدت اشتراک بر اساس پلن بازنشانی شد.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("بازگشت🔙", callback_data=f"search:edit:{sid}")
                        ]]),
                    )
                    return
                if edit_action in ("volume", "days"):
                    context.user_data["biz_flow"] = {
                        "kind": f"search_edit_{edit_action}",
                        "subscription_id": sid,
                    }
                    await _reply_server_prompt(
                        update,
                        "📊 حجم جدید را به گیگابایت وارد کنید:"
                        if edit_action == "volume"
                        else "📅 مدت جدید را به روز وارد کنید:",
                    )
                    return
            if action == "renew" and len(parts) == 3:
                sid = int(parts[2])
                context.user_data["biz_flow"] = {
                    "kind": "search_renew_traffic",
                    "subscription_id": sid,
                }
                await _reply_server_prompt(
                    update, "📊 حجم تمدید را به گیگابایت وارد کنید:"
                )
                return
            if action == "drop" and len(parts) == 3:
                sid = int(parts[2])
                item = business.subscription_admin(actor, subscription_id=sid)
                if item["status"] != "pending_provisioning":
                    raise TenantBusinessError("subscription is not unstarted")
                await update.callback_query.edit_message_text(
                    "⚠️ این سرویس هنوز روی پنل ساخته نشده است.\n"
                    "رکورد سرویس پاک و سفارش مربوطه لغو می‌شود؛ سابقه مالی/رسید باقی می‌ماند.\n\n"
                    "ادامه می‌دهید؟",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✅ بله، پاک شود",
                            callback_data=f"search:dropok:{sid}",
                        ),
                        InlineKeyboardButton(
                            "لغو ❌",
                            callback_data=f"search:sel:{sid}",
                        ),
                    ]]),
                )
                return
            if action == "dropok" and len(parts) == 3:
                sid = int(parts[2])
                business.cleanup_unstarted_subscription_admin(
                    actor, subscription_id=sid
                )
                await update.callback_query.edit_message_text(
                    "✅ رکورد شروع‌نشده پاک شد؛ سابقه مالی و رسید حفظ شد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "بازگشت🔙", callback_data="search:results"
                        )
                    ]]),
                )
                return
            if action == "delete" and len(parts) == 3:
                sid = int(parts[2])
                await update.callback_query.edit_message_text(
                    "❓ از حذف کاربر از پنل و تمام نودهای سرویس مطمئن هستید؟",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"search:deleteok:{sid}"),
                        InlineKeyboardButton("لغو❌", callback_data=f"search:sel:{sid}"),
                    ]]),
                )
                return
            if action == "deleteok" and len(parts) == 3:
                sid = int(parts[2])
                business.delete_subscription_from_panel(
                    actor, subscription_id=sid
                )
                await update.callback_query.edit_message_text(
                    "✅ کاربر از پنل‌ها حذف شد و رکورد سرویس برای تاریخچه حفظ شد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("بازگشت🔙", callback_data="search:results")
                    ]]),
                )
                return

        if data == "biz:dashboard":
            report = business.dashboard_summary(actor)
            rows = [
                [
                    InlineKeyboardButton("📈 گزارش فروش", callback_data="biz:reports"),
                    InlineKeyboardButton("⚠️ نیازمند بررسی", callback_data="biz:attention"),
                ],
                [
                    InlineKeyboardButton("👥 مشتریان", callback_data="biz:customers"),
                    InlineKeyboardButton("📡 سرویس‌ها", callback_data="biz:subs"),
                ],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ]
            await update.callback_query.edit_message_text(
                _report_text(report, dashboard=True),
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data == "biz:growth":
            settings = business.growth_settings(actor)
            coupons = business.list_coupons_admin(actor)
            await update.callback_query.edit_message_text(
                _growth_text(settings, coupons),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🤝 روشن/خاموش Referral",
                            callback_data="biz:growthtoggle:referral",
                        ),
                        InlineKeyboardButton(
                            "🎁 روشن/خاموش تست",
                            callback_data="biz:growthtoggle:trial",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ تنظیم مقادیر",
                            callback_data="biz:growthconfig",
                        ),
                        InlineKeyboardButton(
                            "🎟 کوپن‌ها",
                            callback_data="biz:coupons",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "💰 شارژهای کیف پول",
                            callback_data="biz:wallettopups",
                        )
                    ],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data.startswith("biz:growthtoggle:"):
            target = data.rsplit(":", 1)[1]
            settings = business.growth_settings(actor)
            if target == "referral":
                settings = business.update_growth_settings(
                    actor,
                    referral_enabled=not bool(settings["referral_enabled"]),
                )
            elif target == "trial":
                settings = business.update_growth_settings(
                    actor,
                    trial_enabled=not bool(settings["trial_enabled"]),
                )
            else:
                raise ValueError("invalid growth toggle")
            await update.callback_query.edit_message_text(
                _growth_text(settings, business.list_coupons_admin(actor)),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ فروش پیشرفته", callback_data="biz:growth")]
                ]),
            ); return
        if data == "biz:growthconfig":
            context.user_data["biz_flow"] = {"kind": "growth_config"}
            await update.callback_query.edit_message_text(
                "تنظیمات را با این فرمت بفرستید:\n"
                "پاداش تست | پاداش اولین خرید | حداقل خرید | سقف پاداش | ارز | حجم تست GB | روز تست\n"
                "مثال: 10000 | 20000 | 50000 | 0 | IRR | 1 | 1",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ فروش پیشرفته", callback_data="biz:growth")]
                ]),
            ); return
        if data == "biz:coupons":
            items = business.list_coupons_admin(actor)
            lines = ["🎟 کوپن‌ها"]
            rows = []
            for item in items[:30]:
                kind = "%" if item["discount_kind"] == "percent" else str(item.get("currency") or "")
                lines.append(
                    f"• {item['code']} · {item['value']}{kind} · "
                    f"{item['used_count']}/{item['max_uses'] or '∞'} · {item['status']}"
                )
                rows.append([
                    InlineKeyboardButton(
                        f"{'⛔' if item['status']=='active' else '✅'} {item['code']}",
                        callback_data=f"biz:couponstatus:{item['id']}:{'off' if item['status']=='active' else 'on'}",
                    )
                ])
            if not items:
                lines.append("موردی نیست.")
            rows.extend([
                [InlineKeyboardButton("➕ کوپن جدید", callback_data="biz:addcoupon")],
                [InlineKeyboardButton("↩️ فروش پیشرفته", callback_data="biz:growth")],
            ])
            await update.callback_query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data == "biz:addcoupon":
            context.user_data["biz_flow"] = {"kind": "coupon_add"}
            await update.callback_query.edit_message_text(
                "فرمت کوپن:\n"
                "CODE | percent/fixed | مقدار | ارز(برای fixed) | حداقل سفارش | سقف تخفیف | حداکثر استفاده | سقف هر کاربر | انقضا اختیاری\n"
                "مثال درصدی: OFF15 | percent | 15 | | 0 | 0 | 100 | 1 |\n"
                "مثال ثابت: GIFT20 | fixed | 20000 | IRR | 50000 | 0 | 20 | 1 |",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ کوپن‌ها", callback_data="biz:coupons")]
                ]),
            ); return
        if data.startswith("biz:couponstatus:"):
            _, _, coupon_id, state = data.split(":", 3)
            business.set_coupon_status_admin(
                actor,
                coupon_id=int(coupon_id),
                enabled=state == "on",
            )
            await update.callback_query.edit_message_text(
                "✅ وضعیت کوپن تغییر کرد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ کوپن‌ها", callback_data="biz:coupons")]
                ]),
            ); return
        if data == "biz:wallettopups":
            items = business.list_wallet_topups_admin(actor)
            receipts = business.list_wallet_topup_receipts_admin(actor)
            text = "💰 شارژهای کیف پول\n" + (
                "\n".join(
                    f"• #{x['id']} · {x['display_name']} · "
                    f"{int(x['amount']):,} {x['currency']} · {x['status']}"
                    for x in items[:30]
                )
                or "موردی نیست."
            )
            rows = [[InlineKeyboardButton(
                f"✅/❌ رسید شارژ #{x['id']} · {x['display_name']}",
                callback_data=f"biz:wallettopupreceipt:{x['id']}",
            )] for x in receipts]
            rows.append([InlineKeyboardButton("↩️ فروش پیشرفته", callback_data="biz:growth")])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            ); return
        if data.startswith("biz:wallettopupreceipt:"):
            receipt_id = int(data.rsplit(":", 1)[1])
            receipt = next(
                (
                    x for x in business.list_wallet_topup_receipts_admin(actor)
                    if int(x["id"]) == receipt_id
                ),
                None,
            )
            if receipt is None:
                raise TenantBusinessError("wallet topup receipt not found")
            context.user_data["wallet_topup_confirm"] = {"receipt_id": receipt_id}
            await update.callback_query.edit_message_text(
                f"💰 رسید شارژ کیف پول #{receipt_id}\n"
                f"مشتری: {receipt['display_name']}\n"
                f"مبلغ: {int(receipt['amount']):,} {receipt['currency']}\n"
                f"پیگیری: {receipt.get('reference') or 'تصویر'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تأیید", callback_data="biz:wallettopupconfirm:yes")],
                    [InlineKeyboardButton("❌ رد", callback_data="biz:wallettopupconfirm:no")],
                    [InlineKeyboardButton("↩️ شارژها", callback_data="biz:wallettopups")],
                ]),
            ); return
        if data.startswith("biz:wallettopupconfirm:"):
            pending = context.user_data.pop("wallet_topup_confirm", None)
            if not isinstance(pending, dict):
                raise TenantBusinessError("confirmation expired")
            approve = data.rsplit(":", 1)[1] == "yes"
            reviewed = business.review_wallet_topup_receipt(
                actor,
                receipt_id=int(pending["receipt_id"]),
                approve=approve,
            )
            await update.callback_query.edit_message_text(
                (
                    f"✅ کیف پول {int(reviewed['amount']):,} {reviewed['currency']} شارژ شد."
                    if approve
                    else "❌ رسید شارژ کیف پول رد شد."
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ شارژها", callback_data="biz:wallettopups")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "biz:reports":
            rows = [
                [
                    InlineKeyboardButton("امروز", callback_data="biz:report:1"),
                    InlineKeyboardButton("۷ روز", callback_data="biz:report:7"),
                ],
                [
                    InlineKeyboardButton("۳۰ روز", callback_data="biz:report:30"),
                    InlineKeyboardButton("۹۰ روز", callback_data="biz:report:90"),
                ],
                [InlineKeyboardButton("همه زمان‌ها", callback_data="biz:report:0")],
                [InlineKeyboardButton("↩️ داشبورد", callback_data="biz:dashboard")],
            ]
            await update.callback_query.edit_message_text(
                "📈 بازه گزارش فروش را انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("biz:report:"):
            days = int(data.rsplit(":", 1)[1])
            if days not in (0, 1, 7, 30, 90):
                raise ValueError("invalid report range")
            report = business.sales_report(actor, days=days)
            await update.callback_query.edit_message_text(
                _report_text(report),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 انتخاب بازه", callback_data="biz:reports")],
                    [InlineKeyboardButton("↩️ داشبورد", callback_data="biz:dashboard")],
                ]),
            ); return
        if data == "biz:customers":
            report = business.dashboard_summary(actor)
            current = dict(report.get("current") or {})
            await update.callback_query.edit_message_text(
                "👥 مدیریت مشتریان\n"
                f"کل: {int(current.get('customers_total') or 0)}\n"
                f"فعال: {int(current.get('customers_active') or 0)}\n\n"
                "با نام، @username، Telegram ID یا Customer ID جستجو کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 جستجوی مشتری", callback_data="biz:customersearch")],
                    [InlineKeyboardButton("↩️ داشبورد", callback_data="biz:dashboard")],
                ]),
            ); return
        if data == "biz:customersearch":
            context.user_data["biz_flow"] = {"kind": "customer_search"}
            await update.callback_query.edit_message_text(
                "🔎 نام، @username، Telegram ID یا Customer ID را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")]
                ]),
            ); return
        if data.startswith("biz:customer:"):
            customer_id = int(data.rsplit(":", 1)[1])
            profile = business.customer_profile_admin(
                actor, customer_id=customer_id
            )
            next_status = (
                "active" if profile["status"] == "blocked" else "blocked"
            )
            toggle_title = (
                "✅ آزادسازی مشتری"
                if next_status == "active"
                else "🚫 مسدود کردن مشتری"
            )
            await update.callback_query.edit_message_text(
                _customer_profile_text(profile),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📦 سرویس‌های مشتری",
                            callback_data=f"biz:customersubs:{customer_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "💰 کیف پول مشتری",
                            callback_data=f"biz:wallet:{customer_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            toggle_title,
                            callback_data=f"biz:customerstatus:{customer_id}:{next_status}",
                        )
                    ],
                    [InlineKeyboardButton("🔎 جستجوی جدید", callback_data="biz:customersearch")],
                    [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")],
                ]),
            ); return
        if data.startswith("biz:wallet:"):
            customer_id = int(data.rsplit(":", 1)[1])
            profile = business.customer_profile_admin(
                actor, customer_id=customer_id
            )
            customer_actor = int(profile["telegram_user_id"])
            wallet = business.wallet_summary(customer_actor)
            await update.callback_query.edit_message_text(
                f"👤 {profile['display_name']}\n\n" + _wallet_text(wallet),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "➕/➖ تغییر موجودی",
                        callback_data=f"biz:walletadjust:{customer_id}",
                    )],
                    [InlineKeyboardButton(
                        "👤 پروفایل مشتری",
                        callback_data=f"biz:customer:{customer_id}",
                    )],
                ]),
            ); return
        if data.startswith("biz:walletadjust:"):
            customer_id = int(data.rsplit(":", 1)[1])
            business.customer_profile_admin(actor, customer_id=customer_id)
            context.user_data["biz_flow"] = {
                "kind": "wallet_adjust",
                "customer_id": customer_id,
            }
            await update.callback_query.edit_message_text(
                "ارز | مبلغ مثبت/منفی | یادداشت اختیاری\n"
                "مثال شارژ: IRR | 50000 | هدیه\n"
                "مثال کسر: IRR | -20000 | اصلاح حساب",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "↩️ کیف پول مشتری",
                        callback_data=f"biz:wallet:{customer_id}",
                    )]
                ]),
            ); return
        if data.startswith("biz:customerstatus:"):
            _, _, customer_id, status = data.split(":", 3)
            profile = business.set_customer_status_admin(
                actor,
                customer_id=int(customer_id),
                status=status,
            )
            await update.callback_query.edit_message_text(
                "✅ وضعیت مشتری تغییر کرد.\n\n" + _customer_profile_text(profile),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "👤 بازگشت به پروفایل",
                        callback_data=f"biz:customer:{int(customer_id)}",
                    )],
                    [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")],
                ]),
            ); return
        if data.startswith("biz:customersubs:"):
            customer_id = int(data.rsplit(":", 1)[1])
            profile = business.customer_profile_admin(
                actor, customer_id=customer_id
            )
            items = business.subscriptions_for_customer_admin(
                actor, customer_id=customer_id
            )
            text = (
                f"📦 سرویس‌های {profile.get('display_name') or 'مشتری'}\n"
                + (
                    "\n".join(
                        f"• #{x['id']} · {x['plan_name']} · {x['status']}"
                        f"{' ⚠️ enforcement' if int(x.get('enforcement_pending') or 0) else ''}\n"
                        f"  {int(x.get('usage_bytes') or 0)/(1024**3):.2f}/"
                        f"{int(x.get('traffic_bytes') or 0)/(1024**3):.0f}GB · "
                        f"{x.get('server_label') or '-'} · انقضا: {x.get('expires_at') or '-'}"
                        for x in items[:20]
                    )
                    or "سرویسی ثبت نشده است."
                )
            )
            rows = []
            for item in items[:10]:
                if item["status"] in ("active", "disabled"):
                    rows.append([
                        InlineKeyboardButton(
                            f"🔄 سینک #{item['id']}",
                            callback_data=f"biz:syncsub:{item['id']}",
                        ),
                        InlineKeyboardButton(
                            f"🧩 نودها #{item['id']}",
                            callback_data=f"biz:subnodes:{item['id']}",
                        ),
                    ])
            rows.extend([
                [InlineKeyboardButton(
                    "👤 پروفایل مشتری",
                    callback_data=f"biz:customer:{customer_id}",
                )],
                [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")],
            ])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            ); return
        if data == "biz:attention":
            items = business.service_attention_admin(actor)
            lines = ["⚠️ سرویس‌های نیازمند بررسی"]
            rows = []
            if not items:
                lines.append("✅ موردی برای بررسی وجود ندارد.")
            for item in items[:20]:
                reasons = []
                if item["status"] == "pending_provisioning":
                    reasons.append("در انتظار ساخت")
                elif item["status"] == "expired":
                    reasons.append("منقضی")
                elif item["status"] == "disabled":
                    reasons.append("غیرفعال")
                if int(item.get("enforcement_pending") or 0):
                    reasons.append("Enforcement pending")
                if int(item.get("node_errors") or 0):
                    reasons.append(f"{int(item['node_errors'])} نود خطادار")
                lines.append(
                    f"• #{item['id']} · {item['display_name']} · "
                    f"{item['plan_name']} · {' / '.join(reasons) or item['status']}"
                )
                if item["status"] == "pending_provisioning":
                    rows.append([InlineKeyboardButton(
                        f"🔁 تلاش تحویل سفارش #{item['order_id']}",
                        callback_data=f"biz:fulfill:{item['order_id']}",
                    )])
                elif int(item.get("enforcement_pending") or 0):
                    rows.append([InlineKeyboardButton(
                        f"🔄 اجرای مجدد #{item['id']}",
                        callback_data=f"biz:syncsub:{item['id']}",
                    )])
                elif int(item.get("node_errors") or 0):
                    rows.append([InlineKeyboardButton(
                        f"🧩 بررسی نودهای #{item['id']}",
                        callback_data=f"biz:subnodes:{item['id']}",
                    )])
            rows.extend([
                [InlineKeyboardButton("🔄 همگام‌سازی همه", callback_data="biz:syncall")],
                [InlineKeyboardButton("↩️ داشبورد", callback_data="biz:dashboard")],
            ])
            await update.callback_query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data == "biz:servers":
            text, keyboard = _server_list_view(business)
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard
            )
            return

        if data == "srv:add":
            context.user_data.pop("biz_flow", None)
            await update.callback_query.edit_message_text(
                "نوع پنل سرور را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "هیدیفای (Hiddify)",
                        callback_data="srv:addtype:hiddify",
                    )],
                    [
                        InlineKeyboardButton(
                            "🔵 X-UI علیرضا (alireza0)",
                            callback_data="srv:addtype:xui_alireza",
                        ),
                        InlineKeyboardButton(
                            "🟢 X-UI سنایی (3x-ui)",
                            callback_data="srv:addtype:xui_sanaei",
                        ),
                    ],
                    [InlineKeyboardButton(
                        "🟣 X-NET (Sing-box)",
                        callback_data="srv:addtype:xnet",
                    )],
                    [InlineKeyboardButton("🔙بازگشت", callback_data="biz:servers")],
                ]),
            )
            return

        if data.startswith("srv:addtype:"):
            selected = data.split(":", 2)[2]
            provider = "hiddify"
            flavor = ""
            if selected == "xui_sanaei":
                provider, flavor = "xui", "sanaei"
            elif selected == "xui_alireza":
                provider, flavor = "xui", "alireza"
            elif selected == "xnet":
                provider = "xnet"
            elif selected != "hiddify":
                raise ValueError("invalid server type")
            context.user_data["biz_flow"] = {
                "kind": "server_add_title",
                "provider": provider,
                "flavor": flavor,
            }
            await _reply_server_prompt(
                update, "لطفاً عنوان سرور را وارد کنید:"
            )
            return

        if data.startswith("srv:view:"):
            server_id = int(data.rsplit(":", 1)[1])
            text, keyboard = _server_detail_view(business, actor, server_id)
            await update.callback_query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        if data.startswith("srv:users:"):
            server_id = int(data.rsplit(":", 1)[1])
            items = business.server_subscriptions(actor, server_id=server_id)
            lines = ["👤 لیست کاربران"]
            rows = []
            if not items:
                lines.append("\nکاربری روی این سرور ثبت نشده است.")
            for item in items[:40]:
                used = int(item.get("usage_bytes") or 0) / (1024 ** 3)
                total = int(item.get("traffic_bytes") or 0) / (1024 ** 3)
                lines.append(
                    f"\n#{item['id']} · {item['display_name']} · {item['plan_name']}\n"
                    f"{used:.2f}/{total:.0f}GB · {item['status']}"
                )
                rows.append([InlineKeyboardButton(
                    f"👤 #{item['id']} · {str(item['display_name'])[:25]}",
                    callback_data=f"srv:user:{server_id}:{int(item['id'])}",
                )])
            rows.append([InlineKeyboardButton(
                "بازگشت🔙", callback_data=f"srv:view:{server_id}"
            )])
            await update.callback_query.edit_message_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if data.startswith("srv:user:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid server user callback")
            server_id, subscription_id = int(parts[2]), int(parts[3])
            matches = [
                item for item in business.server_subscriptions(
                    actor, server_id=server_id
                )
                if int(item["id"]) == subscription_id
            ]
            if not matches:
                raise TenantBusinessError("subscription not found on server")
            item = matches[0]
            used = int(item.get("usage_bytes") or 0) / (1024 ** 3)
            total = int(item.get("traffic_bytes") or 0) / (1024 ** 3)
            text = (
                f"👤 کاربر #{subscription_id}\n"
                f"نام: {item['display_name']}\n"
                f"پلن: {item['plan_name']}\n"
                f"وضعیت: {item['status']}\n"
                f"مصرف: {used:.2f} از {total:.0f} گیگ\n"
                f"انقضا: {item.get('expires_at') or '-'}"
            )
            rows = [[InlineKeyboardButton(
                "🔄 همگام‌سازی",
                callback_data=f"srv:useract:{server_id}:{subscription_id}:sync",
            )]]
            if item["status"] in ("active", "disabled"):
                enabled = item["status"] != "active"
                rows.append([InlineKeyboardButton(
                    "✅ فعال‌سازی" if enabled else "⛔ غیرفعال‌سازی",
                    callback_data=(
                        f"srv:useract:{server_id}:{subscription_id}:"
                        + ("enable" if enabled else "disable")
                    ),
                )])
            rows.extend([
                [InlineKeyboardButton(
                    "🗑 حذف از پنل",
                    callback_data=f"srv:useract:{server_id}:{subscription_id}:delete",
                )],
                [InlineKeyboardButton(
                    "بازگشت🔙", callback_data=f"srv:users:{server_id}"
                )],
            ])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if data.startswith("srv:useract:"):
            parts = data.split(":")
            if len(parts) != 5:
                raise ValueError("invalid server user action")
            server_id = int(parts[2])
            subscription_id = int(parts[3])
            action = parts[4]
            if action == "sync":
                result = business.sync_subscription_usage(
                    actor, subscription_id=subscription_id
                )
                msg = (
                    f"✅ همگام شد.\nوضعیت: {result['status']}\n"
                    f"مصرف: {result['usage_bytes']/(1024**3):.2f}GB"
                )
            elif action in ("enable", "disable"):
                result = business.set_subscription_enabled(
                    actor,
                    subscription_id=subscription_id,
                    enabled=action == "enable",
                )
                msg = f"✅ وضعیت کاربر: {result['status']}"
            elif action == "delete":
                result = business.delete_subscription_from_panel(
                    actor, subscription_id=subscription_id
                )
                msg = f"✅ کاربر از پنل حذف/غیرفعال شد. وضعیت: {result['status']}"
            else:
                raise ValueError("invalid server user action")
            await update.callback_query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:users:{server_id}"
                    )
                ]]),
            )
            return

        if data.startswith("srv:userops:"):
            server_id = int(data.rsplit(":", 1)[1])
            await update.callback_query.edit_message_text(
                "🛡️ عملیات کاربری",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "جستجوی کاربر🔍",
                        callback_data=f"srv:usersearch:{server_id}",
                    )],
                    [InlineKeyboardButton(
                        "🔄 همگام‌سازی کاربران این سرور",
                        callback_data=f"srv:sync:{server_id}",
                    )],
                    [InlineKeyboardButton(
                        "👤لیست کاربران",
                        callback_data=f"srv:users:{server_id}",
                    )],
                    [InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                    )],
                ]),
            )
            return

        if data.startswith("srv:usersearch:"):
            server_id = int(data.rsplit(":", 1)[1])
            business.server(server_id)
            context.user_data["biz_flow"] = {
                "kind": "server_user_search",
                "server_id": server_id,
            }
            await _reply_server_prompt(
                update,
                "🔍 نام، یوزرنیم، Telegram ID یا شناسه سرویس را بفرستید:",
            )
            return

        if data.startswith("srv:plans:"):
            server_id = int(data.rsplit(":", 1)[1])
            items = [
                item for item in business.list_plans(public=False)
                if not str(item.get("name") or "").startswith("__WHITELABEL_")
            ]
            text = "📋 پلن ها\n" + (
                "\n".join(
                    f"• {x['name']} · {x['traffic_gb']}GB · "
                    f"{x['duration_days']} روز · {x['price']:,} {x['currency']}"
                    for x in items
                )
                or "موردی نیست."
            )
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                    )
                ]]),
            )
            return

        if data.startswith("srv:domains:"):
            server_id = int(data.rsplit(":", 1)[1])
            server = business.server(server_id)
            kind = str(server.get("panel_kind") or "")
            if kind == "xui":
                domain = server.get("xui_public_origin") or server.get("endpoint") or "—"
                path = server.get("xui_sub_path") or "/sub/"
                field = "xui_public_origin"
            elif kind == "xnet":
                domain = server.get("xnet_public_origin") or server.get("endpoint") or "—"
                path = server.get("xnet_sub_path") or "sub"
                field = "xnet_public_origin"
            else:
                domain = server.get("endpoint") or "—"
                path = server.get("user_path") or "—"
                field = "endpoint"
            await update.callback_query.edit_message_text(
                "🔗 مدیریت دامنه‌ها\n"
                "❖ • -------------------------- • ❖\n"
                f"🌐 دامنه/آدرس: {domain}\n"
                f"🔗 مسیر اشتراک: {path}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✏️ ویرایش دامنه",
                        callback_data=f"srv:editf:{server_id}:{field}",
                    )],
                    [InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                    )],
                ]),
            )
            return

        if data.startswith("srv:edit:"):
            server_id = int(data.rsplit(":", 1)[1])
            server = business.server(server_id)
            text, _ = _server_detail_view(business, actor, server_id)
            await update.callback_query.edit_message_text(
                text,
                reply_markup=_server_edit_view(server),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        if data.startswith("srv:editf:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                raise ValueError("invalid server edit callback")
            server_id = int(parts[2])
            field = parts[3]
            server = business.server(server_id)
            allowed_fields = {
                "label", "endpoint", "users_limit", "priority",
                "admin_path", "user_path", "credential",
                "xui_public_origin", "xui_inbound_ids",
                "xnet_public_origin", "xnet_inbound_ids",
            }
            if field not in allowed_fields:
                raise ValueError("invalid server edit field")
            context.user_data["biz_flow"] = {
                "kind": "server_edit_field",
                "server_id": server_id,
                "field": field,
                "provider": str(server.get("panel_kind") or ""),
                "flavor": str(server.get("xui_flavor") or ""),
            }
            prompts = {
                "label": "📌 عنوان جدید سرور را وارد کنید:",
                "endpoint": "🌐 آدرس کامل پنل را با http/https وارد کنید:",
                "users_limit": "🗿 محدودیت تعداد کاربر را وارد کنید. 0 = نامحدود",
                "priority": "🔢 اولویت ترتیب را وارد کنید. عدد بزرگ‌تر اولویت بیشتر دارد.",
                "admin_path": "🔐 کد مسیر ادمین پنل را وارد کنید:",
                "user_path": "🔐 کد مسیر کاربران را وارد کنید:",
                "credential": "🔑 دسترسی جدید پنل را ارسال کنید.",
                "xui_public_origin": "🔗 دامنه عمومی اشتراک را وارد کنید. برای استفاده از آدرس پنل «-» بفرستید.",
                "xui_inbound_ids": "🧩 شناسه اینباندها را وارد کنید؛ 0 = همه.",
                "xnet_public_origin": "🔗 دامنه عمومی اشتراک را وارد کنید. برای استفاده از آدرس پنل «-» بفرستید.",
                "xnet_inbound_ids": "🧩 شناسه اینباندهای X-NET را وارد کنید؛ 0 = همه.",
            }
            if field == "credential" and server.get("panel_kind") == "xui":
                if str(server.get("xui_flavor") or "") == "sanaei":
                    prompts[field] = "🔑 API Token پنل Sanaei را ارسال کنید."
                else:
                    prompts[field] = "🔑 نام کاربری | رمز عبور | Secret Header اختیاری"
            elif field == "credential" and server.get("panel_kind") == "xnet":
                prompts[field] = (
                    "🔑 API Token X-NET را بفرستید؛ یا "
                    "API Token | نام کاربری | رمز عبور"
                )
            await _reply_server_prompt(update, prompts[field])
            return

        if data.startswith("srv:delete:"):
            server_id = int(data.rsplit(":", 1)[1])
            business.server(server_id)
            await update.callback_query.edit_message_text(
                "❓ آیا از حذف کامل این سرور مطمئن هستید؟\n"
                "اگر سرویس فعالی به سرور متصل باشد، حذف برای امنیت متوقف می‌شود.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ بله، حذف شود",
                        callback_data=f"srv:deleteok:{server_id}",
                    ),
                    InlineKeyboardButton(
                        "لغو❌", callback_data=f"srv:view:{server_id}"
                    ),
                ]]),
            )
            return

        if data.startswith("srv:deleteok:"):
            server_id = int(data.rsplit(":", 1)[1])
            deleted = business.delete_server(actor, server_id=server_id)
            text, keyboard = _server_list_view(business)
            await update.callback_query.edit_message_text(
                f"✅ سرور «{deleted['label']}» حذف شد.\n\n{text}",
                reply_markup=keyboard,
            )
            return

        if data.startswith("srv:nodes:"):
            server_id = int(data.rsplit(":", 1)[1])
            server = business.server(server_id)
            nodes = business.list_nodes(parent_server_id=server_id)
            lines = [f"⚙️ لیست نودها — {server['label']}"]
            rows = []
            if not nodes:
                lines.append("\nنودی برای این سرور ثبت نشده است.")
            for node in nodes:
                lines.append(
                    f"\n• {node['label']} · {node.get('server_label') or '-'} "
                    f"· {node.get('location') or '-'} · {node['status']}"
                )
                rows.append([InlineKeyboardButton(
                    f"🗑 حذف نود {str(node['label'])[:25]}",
                    callback_data=f"srv:nodedel:{server_id}:{int(node['id'])}",
                )])
            rows.extend([
                [InlineKeyboardButton(
                    "➕ افزودن نود", callback_data=f"srv:nodeadd:{server_id}"
                )],
                [InlineKeyboardButton(
                    "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                )],
            ])
            await update.callback_query.edit_message_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if data.startswith("srv:nodeadd:"):
            parent_id = int(data.rsplit(":", 1)[1])
            parent = business.server(parent_id)
            candidates = [
                item for item in business.list_servers()
                if int(item["id"]) != parent_id and item["status"] == "active"
            ]
            rows = [[InlineKeyboardButton(
                str(item["label"]),
                callback_data=f"srv:nodepick:{parent_id}:{int(item['id'])}",
            )] for item in candidates]
            rows.append([InlineKeyboardButton(
                "بازگشت🔙", callback_data=f"srv:nodes:{parent_id}"
            )])
            text = (
                f"➕ افزودن نود به «{parent['label']}»\n"
                "سروری که باید به عنوان نود متصل شود را انتخاب کنید."
            )
            if not candidates:
                text += "\n\nابتدا یک سرور دیگر اضافه کنید."
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if data.startswith("srv:nodepick:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid node selection")
            parent_id, target_id = int(parts[2]), int(parts[3])
            target = business.server(target_id)
            business.add_node(
                actor,
                label=str(target["label"]),
                server_id=target_id,
                parent_server_id=parent_id,
                location="",
            )
            await update.callback_query.edit_message_text(
                "✅ نود اضافه شد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⚙️ لیست نودها",
                        callback_data=f"srv:nodes:{parent_id}",
                    )
                ]]),
            )
            return

        if data.startswith("srv:nodedel:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid node delete")
            parent_id, node_id = int(parts[2]), int(parts[3])
            business.delete_node(
                actor, node_id=node_id, parent_server_id=parent_id
            )
            await update.callback_query.edit_message_text(
                "✅ نود از این سرور حذف شد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⚙️ لیست نودها",
                        callback_data=f"srv:nodes:{parent_id}",
                    )
                ]]),
            )
            return

        if data.startswith("srv:sync:"):
            server_id = int(data.rsplit(":", 1)[1])
            result = business.sync_server_subscriptions(
                actor, server_id=server_id
            )
            await update.callback_query.edit_message_text(
                "✅ همگام‌سازی انجام شد.\n"
                f"موفق: {result['synced']} · "
                f"منقضی: {result['expired']} · خطا: {result['errors']}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                    )
                ]]),
            )
            return

        if data.startswith("srv:frozen:"):
            server_id = int(data.rsplit(":", 1)[1])
            rows_data = business.server_frozen_subscriptions(
                actor, server_id=server_id
            )
            lines = ["❄️ کاربران یخ‌زده این سرور"]
            if not rows_data:
                lines.append("\n✅ رکورد یخ‌زده‌ای برای این سرور وجود ندارد.")
            for item in rows_data[:50]:
                lines.append(
                    f"\n• سرویس #{item['subscription_id']} · "
                    f"{item['display_name']} · {item['plan_name']}\n"
                    f"خطا: {item.get('last_error') or '-'} · "
                    f"تلاش ناموفق: {int(item.get('fail_count') or 0)}"
                )
            await update.callback_query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "بازگشت🔙", callback_data=f"srv:view:{server_id}"
                    )
                ]]),
            )
            return

        if data.startswith("biz:secret:"):
            server_id = int(data.rsplit(":", 1)[1])
            server = business.server(server_id)
            if server["panel_kind"] == "xui":
                flavor = str(server.get("xui_flavor") or "")
                context.user_data["biz_flow"] = {
                    "kind": "xui_secret",
                    "server_id": server_id,
                    "flavor": flavor,
                }
                prompt = (
                    "API Token پنل Sanaei را بفرستید."
                    if flavor == "sanaei"
                    else "نام کاربری | رمز عبور | Secret Header اختیاری\n"
                         "اگر Secret Header ندارید فقط نام کاربری | رمز عبور را بفرستید."
                )
            elif server["panel_kind"] == "xnet":
                context.user_data["biz_flow"] = {
                    "kind": "xnet_secret",
                    "server_id": server_id,
                }
                prompt = (
                    "دسترسی X-Net را بفرستید:\n"
                    "فقط API Token\n"
                    "یا API Token | نام کاربری | رمز عبور\n"
                    "اگر API Token ندارید: | admin | رمز عبور\n"
                    "اطلاعات ورود fallback فقط وقتی Token رد شود استفاده می‌شود."
                )
            else:
                context.user_data["biz_flow"] = {
                    "kind": "panel_secret",
                    "server_id": server_id,
                }
                prompt = "API Key پنل Hiddify را بفرستید."
            await update.callback_query.edit_message_text(
                prompt + "\nپیام شما پس از ثبت حذف می‌شود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ سرورها", callback_data="biz:servers")]]),
            ); return
        if data.startswith("biz:defaultserver:"):
            server_id = int(data.rsplit(":", 1)[1])
            business.set_default_server(actor, server_id=server_id)
            await update.callback_query.edit_message_text(
                "⭐ این سرور به عنوان سرور پیش‌فرض فروش انتخاب شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ سرورها", callback_data="biz:servers")]]),
            ); return
        if data == "biz:nodes":
            items = business.list_nodes()
            text = "🔗 نودهای Multi-node\n" + ("\n".join(
                f"• {x['label']} · server #{x.get('server_id') or '-'} · "
                f"{x.get('server_label') or 'بدون سرور'} · {x.get('provider_kind') or '-'} · "
                f"{x.get('location') or '-'} · {x['status']}"
                for x in items
            ) or "موردی نیست.")
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ نود", callback_data="biz:addnode")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "biz:addnode":
            context.user_data["biz_flow"] = {"kind": "node"}
            await update.callback_query.edit_message_text(
                "نام نود | Server ID | لوکیشن اختیاری\n"
                "Server ID را از بخش «سرورها» بردارید. هر نود فعال به‌صورت خودکار وارد سرویس‌های Multi-node می‌شود.",
                reply_markup=_menu(spec),
            ); return
        if data == "biz:plans":
            items = [
                x for x in business.list_plans(public=False)
                if not str(x.get("name") or "").startswith("__WHITELABEL_")
            ]; text = "📦 پلن‌های فروش\n" + ("\n".join(f"• {x['name']} · {x['traffic_gb']}GB · {x['duration_days']} روز · {x['price']:,} {x['currency']}" for x in items) or "موردی نیست.")
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ پلن", callback_data="biz:addplan")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]])); return
        if data == "biz:addplan":
            context.user_data["biz_flow"] = {"kind": "plan"}; await update.callback_query.edit_message_text("نام | حجم گیگ | روز | قیمت | ارز", reply_markup=_menu(spec)); return
        if data == "biz:payments":
            items = business.list_methods(); text = "💳 روش‌های پرداخت\n" + ("\n".join(f"• {x['kind']} · {x['title']} · {x['currency']}" for x in items) or "موردی نیست.")
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ کارت/رمزارز", callback_data="biz:addpayment")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]])); return
        if data == "biz:addpayment":
            context.user_data["biz_flow"] = {"kind": "payment"}; await update.callback_query.edit_message_text("نوع card/crypto | عنوان | ارز | مقصد | شبکه/نام | توضیح", reply_markup=_menu(spec)); return
        if data == "biz:orders":
            items = business.list_orders_admin(actor); receipts = business.list_receipts_admin(actor)
            pending = business.list_fulfillment_pending_admin(actor)
            text = "🧾 سفارش‌ها\n" + ("\n".join(
                f"#{x['id']} · {x['display_name']} · {x['plan_name']} · {x['operation']} · {x['status']}"
                for x in items
            ) or "موردی نیست.")
            rows = [[InlineKeyboardButton(f"✅/❌ بررسی رسید #{x['id']} · {x['display_name']}", callback_data=f"biz:receipt:{x['id']}")] for x in receipts]
            rows += [[InlineKeyboardButton(
                f"🔁 {'تمدید' if x['operation'] == 'renewal' else 'فعال‌سازی'} سفارش #{x['id']} · {x['display_name']}",
                callback_data=f"biz:fulfill:{x['id']}"
            )] for x in pending]
            rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")])
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("biz:fulfill:"):
            order_id = int(data.rsplit(":", 1)[1])
            try:
                result = business.fulfill_paid_order(actor, order_id=order_id)
                action = "تمدید" if result["operation"] == "renewal" else "فعال"
                await update.callback_query.edit_message_text(
                    f"✅ سفارش #{order_id} انجام شد و سرویس {action} شد.\n🔗 {result.get('subscription_url') or '-'}",
                    reply_markup=_menu(spec),
                )
            except TenantBusinessError:
                await update.callback_query.edit_message_text(
                    "⚠️ عملیات پنل انجام نشد. پرداخت محفوظ است و سفارش برای تلاش مجدد باقی ماند.",
                    reply_markup=_menu(spec),
                )
            return
        if data == "biz:subs":
            items = business.list_subscriptions_admin(actor)
            text = "📡 سرویس‌ها\n" + ("\n".join(
                f"#{x['id']} · {x['display_name']} · {x['plan_name']} · {x['status']}"
                f"{' ⚠️ enforcement-pending' if int(x.get('enforcement_pending') or 0) else ''} · "
                f"{int(x['usage_bytes']) / (1024**3):.2f}/{int(x['traffic_bytes']) / (1024**3):.0f}GB · "
                f"آخرین اتصال: {x.get('last_online') or '-'}"
                for x in items
            ) or "موردی نیست.")
            rows = []
            for x in items[:12]:
                if x["status"] in ("active", "disabled"):
                    rows.append([
                        InlineKeyboardButton(
                            f"🔄 سینک #{x['id']}",
                            callback_data=f"biz:syncsub:{x['id']}",
                        ),
                        InlineKeyboardButton(
                            f"🧩 نودها #{x['id']}",
                            callback_data=f"biz:subnodes:{x['id']}",
                        ),
                    ])
            rows += [
                [InlineKeyboardButton("🔄 همگام‌سازی همه", callback_data="biz:syncall")],
                [InlineKeyboardButton("⏱ اعمال انقضا", callback_data="biz:expireall")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ]
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("biz:subnodes:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            nodes = business.subscription_nodes_admin(
                actor, subscription_id=subscription_id
            )
            text = f"🧩 نودهای سرویس #{subscription_id}\n" + (
                "\n".join(
                    f"• {'⭐ ' if int(x.get('is_primary') or 0) else ''}"
                    f"{x.get('server_label') or x['server_id']} · "
                    f"{x.get('provider_kind') or '-'} · {x['status']} · "
                    f"{int(x.get('usage_bytes') or 0)/(1024**3):.2f}GB"
                    f"{' · خطا: '+str(x.get('last_error')) if x.get('last_error') else ''}"
                    for x in nodes
                )
                or "هنوز mapping ثبت نشده است."
            )
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔁 Retry / همگام‌سازی نودها",
                        callback_data=f"biz:retrynodes:{subscription_id}",
                    )],
                    [InlineKeyboardButton("↩️ سرویس‌ها", callback_data="biz:subs")],
                ]),
            ); return
        if data.startswith("biz:retrynodes:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            result = business.repair_subscription_nodes(
                actor, subscription_id=subscription_id
            )
            await update.callback_query.edit_message_text(
                f"✅ نودهای سرویس #{subscription_id} بررسی شدند.\n"
                f"ساخته: {result['created']} · بازیابی: {result['restored']} · "
                f"غیرفعال: {result['disabled']} · خطا: {result['errors']}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🧩 مشاهده نودها",
                        callback_data=f"biz:subnodes:{subscription_id}",
                    )],
                    [InlineKeyboardButton("↩️ سرویس‌ها", callback_data="biz:subs")],
                ]),
            ); return
        if data.startswith("biz:syncsub:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            result = business.sync_subscription_usage(actor, subscription_id=subscription_id)
            await update.callback_query.edit_message_text(
                f"✅ سرویس #{subscription_id} همگام شد.\nوضعیت: {result['status']}\n"
                f"مصرف: {result['usage_bytes'] / (1024**3):.2f}GB\n"
                f"آخرین اتصال: {result.get('last_online') or '-'}",
                reply_markup=_menu(spec),
            ); return
        if data == "biz:syncall":
            result = business.sync_all_subscriptions(actor)
            await update.callback_query.edit_message_text(
                f"✅ همگام‌سازی انجام شد.\nموفق: {result['synced']} · منقضی: {result['expired']} · خطا: {result['errors']}",
                reply_markup=_menu(spec),
            ); return
        if data == "biz:expireall":
            result = business.expire_due_subscriptions(actor)
            await update.callback_query.edit_message_text(
                f"✅ بررسی انقضا انجام شد.\nمنقضی: {result['expired']} · خطا: {result['errors']}",
                reply_markup=_menu(spec),
            ); return
        if data.startswith("biz:receipt:"):
            receipt_id = int(data.rsplit(":", 1)[1]); receipt = next((x for x in business.list_receipts_admin(actor) if int(x['id']) == receipt_id), None)
            if receipt is None: raise TenantBusinessError("receipt not found")
            context.user_data["biz_confirm"] = {"receipt_id": receipt_id}
            await update.callback_query.edit_message_text(f"رسید #{receipt_id}\nمشتری: {receipt['display_name']}\nمبلغ: {receipt['amount']:,} {receipt['currency']}\nپیگیری: {receipt.get('reference') or 'تصویر'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید", callback_data="biz:receiptconfirm:yes")], [InlineKeyboardButton("❌ رد", callback_data="biz:receiptconfirm:no")], [InlineKeyboardButton("↩️ سفارش‌ها", callback_data="biz:orders")]])); return
        if data.startswith("biz:receiptconfirm:"):
            pending = context.user_data.pop("biz_confirm", None)
            if not isinstance(pending, dict): raise TenantBusinessError("confirmation expired")
            approve = data.rsplit(":", 1)[1] == "yes"
            reviewed = business.review_receipt(actor, int(pending['receipt_id']), approve=approve)
            if not approve:
                await update.callback_query.edit_message_text("❌ رسید رد شد.", reply_markup=_menu(spec)); return
            try:
                result = business.fulfill_paid_order(actor, order_id=int(reviewed["order_id"]))
                action = "تمدید" if result["operation"] == "renewal" else "فعال"
                await update.callback_query.edit_message_text(
                    f"✅ پرداخت تأیید شد و سرویس #{result['id']} {action} شد.\n🔗 {result.get('subscription_url') or '-'}",
                    reply_markup=_menu(spec),
                )
            except TenantBusinessError:
                await update.callback_query.edit_message_text(
                    f"✅ پرداخت تأیید شد.\n⚠️ اجرای پنل انجام نشد؛ سفارش #{reviewed['order_id']} در بخش سفارش‌ها برای تلاش مجدد باقی ماند.",
                    reply_markup=_menu(spec),
                )
            return
        if data == "biz:tickets":
            items = business.list_tickets_admin(actor)
            text = "🎫 تیکت‌ها\n" + (
                "\n".join(
                    f"#{x['id']} · {x['display_name']} · {x['subject']} · {x['status']}"
                    for x in items[:30]
                )
                or "موردی نیست."
            )
            rows = [
                [InlineKeyboardButton(
                    f"{'🟠' if x['status'] == 'open' else '🟢' if x['status'] == 'answered' else '⚪'} "
                    f"#{x['id']} · {x['display_name']}"[:60],
                    callback_data=f"biz:ticket:{x['id']}",
                )]
                for x in items[:20]
            ]
            rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            ); return
        if data.startswith("biz:ticket:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            ticket = business.ticket_admin(actor, ticket_id=ticket_id)
            username = str(ticket.get("username") or "").strip()
            text = (
                f"🎫 تیکت #{ticket_id}\n"
                f"👤 {ticket['display_name']} · "
                f"{'@' + username.lstrip('@') if username else ticket['telegram_user_id']}\n"
                f"وضعیت: {ticket['status']}\n"
                f"موضوع: {ticket['subject']}\n\n"
                f"پیام:\n{ticket['body']}\n\n"
                f"پاسخ ادمین:\n{ticket.get('admin_reply') or '—'}"
            )
            rows = []
            if ticket["status"] != "closed":
                rows.append([
                    InlineKeyboardButton(
                        "✍️ پاسخ",
                        callback_data=f"biz:ticketreply:{ticket_id}",
                    ),
                    InlineKeyboardButton(
                        "✅ بستن",
                        callback_data=f"biz:ticketclose:{ticket_id}",
                    ),
                ])
            rows.extend([
                [InlineKeyboardButton(
                    "👤 پروفایل مشتری",
                    callback_data=f"biz:customer:{ticket['customer_id']}",
                )],
                [InlineKeyboardButton("↩️ تیکت‌ها", callback_data="biz:tickets")],
            ])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            ); return
        if data.startswith("biz:ticketreply:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            business.ticket_admin(actor, ticket_id=ticket_id)
            context.user_data["biz_flow"] = {
                "kind": "ticket_reply",
                "ticket_id": ticket_id,
            }
            await update.callback_query.edit_message_text(
                f"✍️ پاسخ تیکت #{ticket_id} را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "↩️ بازگشت به تیکت",
                        callback_data=f"biz:ticket:{ticket_id}",
                    )]
                ]),
            ); return
        if data.startswith("biz:ticketclose:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            business.close_ticket_admin(actor, ticket_id=ticket_id)
            await update.callback_query.edit_message_text(
                f"✅ تیکت #{ticket_id} بسته شد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ تیکت‌ها", callback_data="biz:tickets")]
                ]),
            ); return
        if data == "biz:links":
            items = business.list_smart_links(actor)
            managed = [x for x in items if str(x.get("target") or "").startswith("subscription:")]
            text = "🔗 لینک‌های هوشمند سرویس‌ها\n" + (
                "\n".join(
                    f"• {x['label']} · {x['target']}\n  {x.get('public_url') or 'SMART_SUB_PUBLIC_BASE_URL تنظیم نشده'}"
                    for x in managed
                )
                or "بعد از فعال‌شدن اولین سرویس، لینک هوشمند خودکار ساخته می‌شود."
            )
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]
                ]),
            ); return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.callback_query.answer("درخواست قابل انجام نیست.", show_alert=True)
        return
    await update.callback_query.answer("این دکمه معتبر نیست.", show_alert=True)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, _, business = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot text handler registered for non-admin role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    flow = context.user_data.get("biz_flow")
    text = str(update.effective_message.text or "").strip() if update.effective_message else ""
    try:
        if text in ADMIN_MAIN_BUTTONS:
            # Main-menu navigation always leaves any unfinished text wizard,
            # matching the proven SellBot behavior.
            context.user_data.pop("biz_flow", None)
            context.user_data.pop("userbot_admin_flow", None)

            if text == BTN_SERVERS:
                server_text, server_keyboard = _server_list_view(business)
                await update.effective_message.reply_text(
                    server_text, reply_markup=server_keyboard
                )
                return

            if text == BTN_SEARCH_USER:
                context.user_data.pop("smart_search_results", None)
                await update.effective_message.reply_text(
                    "🔍 جستجوی کاربر",
                    reply_markup=_search_menu_keyboard(),
                )
                return

            if text == BTN_DAILY_REPORT:
                report = business.daily_admin_report(actor)
                await update.effective_message.reply_text(
                    _daily_report_text(report), parse_mode="HTML"
                )
                return

            if text == BTN_STATUS:
                await show_status(update, context)
                return

            if text == BTN_USERBOT:
                from TenantRuntime.AdminBot import userbot_management
                await userbot_management.send_userbot_main_menu(update, context)
                return

            if text == BTN_AGENCIES:
                await update.effective_message.reply_text(
                    "🏢 بخش نمایندگی هنوز از Hiddify-SellBot به Tenant AdminBot "
                    "منتقل نشده است. این بخش در مرحله مستقل منتقل می‌شود."
                )
                return

            if text == BTN_BACKUP:
                await update.effective_message.reply_text(
                    "📫 بکاپ Tenant هنوز به‌صورت مستقل و امن منتقل نشده است. "
                    "بکاپ سراسری Master به ادمین Tenant نمایش داده نمی‌شود."
                )
                return

        from TenantRuntime.AdminBot import userbot_management
        if await userbot_management.handle_text(
            update,
            context,
            business=business,
            actor=actor,
            admin_main_keyboard=admin_main_keyboard,
        ):
            return

        if isinstance(flow, dict) and str(flow.get("kind") or "").startswith("search_"):
            kind = str(flow.get("kind") or "")
            if text == "❌ لغو":
                context.user_data.pop("biz_flow", None)
                context.user_data.pop("smart_search_results", None)
                context.user_data.pop("search_results_title", None)
                cancel_text = (
                    "❌ جستجو لغو شد."
                    if kind == "search_smart"
                    else "❌ لغو شد."
                )
                await update.effective_message.reply_text(
                    cancel_text, reply_markup=admin_main_keyboard()
                )
                return
            if kind == "search_tracking":
                items = business.search_subscriptions_admin(actor, text)
                exact = None
                plain = text.strip().lstrip("#")
                if plain.isdigit():
                    exact = next(
                        (x for x in items if int(x["id"]) == int(plain)),
                        None,
                    )
                if exact is None and len(items) == 1:
                    exact = items[0]
                if exact is None:
                    await update.effective_message.reply_text(
                        "❌اشتراکی با این شناسه یافت نشد"
                        if not items
                        else "⚠️ چند نتیجه پیدا شد؛ شناسه دقیق سرویس را وارد کنید.",
                        reply_markup=_server_cancel_keyboard(),
                    )
                    return
                context.user_data.pop("biz_flow", None)
                detail, kb = _subscription_detail_view(
                    business, actor, int(exact["id"])
                )
                await update.effective_message.reply_text(
                    "✅اشتراک یافت شد", reply_markup=admin_main_keyboard()
                )
                await update.effective_message.reply_text(
                    detail, reply_markup=kb
                )
                return
            if kind == "search_smart":
                items = business.search_subscriptions_admin(actor, text)
                context.user_data.pop("biz_flow", None)
                if not items:
                    context.user_data.pop("smart_search_results", None)
                    await update.effective_message.reply_text(
                        "❌ کاربر یافت نشد.",
                        reply_markup=admin_main_keyboard(),
                    )
                    return
                context.user_data["smart_search_results"] = items
                context.user_data["search_results_title"] = "[📥 نتیجه جستجو]"
                result_text, kb = _search_results_view(items)
                await update.effective_message.reply_text(
                    "✅ کاربر یافت شد", reply_markup=admin_main_keyboard()
                )
                await update.effective_message.reply_text(
                    result_text, reply_markup=kb
                )
                return
            if kind == "search_renew_traffic":
                value = int(text.replace(",", "").strip())
                if value <= 0:
                    raise ValueError("invalid traffic")
                flow["traffic_gb"] = value
                flow["kind"] = "search_renew_days"
                await update.effective_message.reply_text(
                    "📅 مدت تمدید را به روز وارد کنید:",
                    reply_markup=_server_cancel_keyboard(),
                )
                return
            if kind == "search_renew_days":
                days = int(text.replace(",", "").strip())
                if days <= 0:
                    raise ValueError("invalid days")
                sid = int(flow["subscription_id"])
                result = business.renew_subscription(
                    actor,
                    subscription_id=sid,
                    traffic_gb=int(flow["traffic_gb"]),
                    duration_days=days,
                    idempotency_key=f"admin-search-renew:{sid}",
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    f"✅ اشتراک تمدید شد.\nوضعیت: {result['status']}",
                    reply_markup=admin_main_keyboard(),
                )
                return
            if kind in ("search_edit_volume", "search_edit_days"):
                value = int(text.replace(",", "").strip())
                if value <= 0:
                    raise ValueError("invalid edit value")
                sid = int(flow["subscription_id"])
                kwargs = (
                    {"traffic_gb": value}
                    if kind == "search_edit_volume"
                    else {"duration_days": value}
                )
                business.edit_subscription_terms_admin(
                    actor, subscription_id=sid, **kwargs
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ اطلاعات اشتراک بروزرسانی شد.",
                    reply_markup=admin_main_keyboard(),
                )
                return

        if isinstance(flow, dict) and str(flow.get("kind") or "").startswith("server_"):
            kind = str(flow.get("kind") or "")
            if text == "❌ لغو":
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "❌ عملیات سرور لغو شد.",
                    reply_markup=admin_main_keyboard(),
                )
                return

            def optional(value: str) -> str:
                return "" if value.strip() in ("-", "—") else value.strip()

            if kind == "server_user_search":
                server_id = int(flow["server_id"])
                items = business.server_subscriptions(
                    actor, server_id=server_id, query=text
                )
                context.user_data.pop("biz_flow", None)
                rows = [[InlineKeyboardButton(
                    f"👤 #{item['id']} · {str(item['display_name'])[:25]}",
                    callback_data=f"srv:user:{server_id}:{int(item['id'])}",
                )] for item in items]
                rows.append([InlineKeyboardButton(
                    "بازگشت🔙", callback_data=f"srv:userops:{server_id}"
                )])
                await update.effective_message.reply_text(
                    (
                        f"✅ {len(items)} نتیجه پیدا شد."
                        if items else "❌ کاربری پیدا نشد."
                    ),
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return

            if kind == "server_edit_field":
                server_id = int(flow["server_id"])
                field = str(flow["field"])
                provider = str(flow.get("provider") or "")
                flavor = str(flow.get("flavor") or "")
                if field == "credential":
                    try:
                        await update.effective_message.delete()
                    except Exception:
                        pass
                    if provider == "xui":
                        if flavor == "sanaei":
                            business.set_xui_credential(
                                actor, server_id=server_id, api_token=text
                            )
                        else:
                            parts = [part.strip() for part in text.split("|")]
                            if len(parts) not in (2, 3):
                                raise ValueError("invalid X-UI credential")
                            business.set_xui_credential(
                                actor,
                                server_id=server_id,
                                username=parts[0],
                                password=parts[1],
                                secret_header=parts[2] if len(parts) == 3 else "",
                            )
                    elif provider == "xnet":
                        parts = [part.strip() for part in text.split("|")]
                        if len(parts) == 1:
                            business.set_xnet_credential(
                                actor, server_id=server_id, api_token=parts[0]
                            )
                        elif len(parts) == 3:
                            business.set_xnet_credential(
                                actor,
                                server_id=server_id,
                                api_token=parts[0],
                                username=parts[1],
                                password=parts[2],
                            )
                        else:
                            raise ValueError("invalid X-NET credential")
                    else:
                        business.set_panel_credential(
                            actor, server_id=server_id, secret=text
                        )
                else:
                    value: object = optional(text)
                    if field in ("users_limit", "priority"):
                        value = int(text.replace(",", "").strip())
                    if field == "endpoint" and value and not str(value).startswith(("http://", "https://")):
                        raise ValueError("invalid endpoint")
                    business.update_server(
                        actor, server_id=server_id, **{field: value}
                    )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ اطلاعات سرور ذخیره شد.",
                    reply_markup=admin_main_keyboard(),
                )
                return

            # New-server wizard. Secrets are requested only at the final step
            # and never stored in user_data.
            if kind == "server_add_title":
                flow["label"] = text[:80]
                flow["kind"] = "server_add_endpoint"
                await update.effective_message.reply_text(
                    "🌐 آدرس کامل پنل را وارد کنید:\nمثال: https://panel.example.com",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_endpoint":
                if not text.startswith(("http://", "https://")):
                    raise ValueError("invalid panel URL")
                flow["endpoint"] = text
                provider = str(flow["provider"])
                if provider == "hiddify":
                    flow["kind"] = "server_add_admin_path"
                    prompt = "🔑 کد مسیر ادمین پنل را وارد کنید. اگر نیاز نیست «-» بفرستید."
                elif provider == "xui":
                    flow["kind"] = "server_add_inbounds"
                    prompt = "🧩 شناسه اینباندها را وارد کنید؛ 0 = همه یا مثل 1,2,3"
                else:
                    flow["kind"] = "server_add_inbounds"
                    prompt = "🧩 شناسه اینباندهای X-NET را وارد کنید؛ 0 = همه یا مثل in-xxxx"
                await update.effective_message.reply_text(
                    prompt, reply_markup=_server_cancel_keyboard()
                )
                return

            if kind == "server_add_admin_path":
                flow["admin_path"] = optional(text)
                flow["kind"] = "server_add_user_path"
                await update.effective_message.reply_text(
                    "🔐 کد مسیر کاربران را وارد کنید. اگر نیاز نیست «-» بفرستید.",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_user_path":
                flow["user_path"] = optional(text)
                flow["kind"] = "server_add_limit"
                await update.effective_message.reply_text(
                    "🗿 محدودیت تعداد کاربر این سرور را وارد کنید. 0 = نامحدود",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_inbounds":
                flow["inbound_ids"] = optional(text)
                flow["kind"] = "server_add_public_origin"
                await update.effective_message.reply_text(
                    "🔗 دامنه عمومی اشتراک را وارد کنید. برای استفاده از آدرس پنل «-» بفرستید.",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_public_origin":
                flow["public_origin"] = optional(text)
                provider = str(flow["provider"])
                flow["kind"] = (
                    "server_add_xnet_port"
                    if provider == "xnet"
                    else "server_add_sub_path"
                )
                prompt = (
                    "🔌 پورت اشتراک X-NET را وارد کنید. 0 = پیش‌فرض 2096"
                    if provider == "xnet"
                    else "🔗 مسیر اشتراک را وارد کنید. برای پیش‌فرض «-» بفرستید."
                )
                await update.effective_message.reply_text(
                    prompt, reply_markup=_server_cancel_keyboard()
                )
                return

            if kind == "server_add_xnet_port":
                port = int(text.strip())
                if port < 0 or port > 65535:
                    raise ValueError("invalid port")
                flow["xnet_sub_port"] = port
                flow["kind"] = "server_add_sub_path"
                await update.effective_message.reply_text(
                    "🔗 مسیر اشتراک X-NET را وارد کنید. برای پیش‌فرض «-» بفرستید.",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_sub_path":
                flow["sub_path"] = optional(text)
                flow["kind"] = "server_add_limit"
                await update.effective_message.reply_text(
                    "🗿 محدودیت تعداد کاربر این سرور را وارد کنید. 0 = نامحدود",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_limit":
                limit = int(text.replace(",", "").strip())
                if limit < 0:
                    raise ValueError("invalid limit")
                flow["users_limit"] = limit
                flow["kind"] = "server_add_priority"
                await update.effective_message.reply_text(
                    "🔢 اولویت ترتیب سرور را وارد کنید. عدد بزرگ‌تر اولویت بیشتر دارد.",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_priority":
                priority = int(text.replace(",", "").strip())
                if priority < 0:
                    raise ValueError("invalid priority")
                flow["priority"] = priority
                provider = str(flow["provider"])
                if provider == "xui" and str(flow.get("flavor") or "") == "alireza":
                    flow["kind"] = "server_add_xui_username"
                    prompt = "👤 نام کاربری پنل X-UI علیرضا را وارد کنید:"
                else:
                    flow["kind"] = "server_add_secret"
                    prompt = (
                        "🔑 API Key پنل Hiddify را ارسال کنید."
                        if provider == "hiddify"
                        else (
                            "🔑 API Token پنل Sanaei را ارسال کنید."
                            if provider == "xui"
                            else "🔑 Bearer Token مدیریت X-NET را ارسال کنید."
                        )
                    )
                await update.effective_message.reply_text(
                    prompt, reply_markup=_server_cancel_keyboard()
                )
                return

            if kind == "server_add_xui_username":
                flow["xui_username"] = text.strip()
                flow["kind"] = "server_add_secret"
                await update.effective_message.reply_text(
                    "🔑 رمز عبور پنل X-UI علیرضا را ارسال کنید.",
                    reply_markup=_server_cancel_keyboard(),
                )
                return

            if kind == "server_add_secret":
                provider = str(flow["provider"])
                flavor = str(flow.get("flavor") or "")
                try:
                    await update.effective_message.delete()
                except Exception:
                    pass
                kwargs: dict[str, Any] = {
                    "label": str(flow["label"]),
                    "panel_kind": provider,
                    "endpoint": str(flow["endpoint"]),
                    "users_limit": int(flow.get("users_limit") or 0),
                    "priority": int(flow.get("priority") or 0),
                }
                if provider == "hiddify":
                    kwargs.update({
                        "admin_path": str(flow.get("admin_path") or ""),
                        "user_path": str(flow.get("user_path") or ""),
                    })
                elif provider == "xui":
                    kwargs.update({
                        "xui_flavor": flavor,
                        "xui_inbound_ids": str(flow.get("inbound_ids") or ""),
                        "xui_public_origin": str(flow.get("public_origin") or ""),
                        "xui_sub_path": str(flow.get("sub_path") or ""),
                    })
                elif provider == "xnet":
                    kwargs.update({
                        "xnet_inbound_ids": str(flow.get("inbound_ids") or ""),
                        "xnet_public_origin": str(flow.get("public_origin") or ""),
                        "xnet_sub_port": int(flow.get("xnet_sub_port") or 0),
                        "xnet_sub_path": str(flow.get("sub_path") or ""),
                    })
                server = business.add_server(actor, **kwargs)
                try:
                    if provider == "hiddify":
                        business.set_panel_credential(
                            actor, server_id=int(server["id"]), secret=text
                        )
                    elif provider == "xui" and flavor == "sanaei":
                        business.set_xui_credential(
                            actor, server_id=int(server["id"]), api_token=text
                        )
                    elif provider == "xui":
                        business.set_xui_credential(
                            actor,
                            server_id=int(server["id"]),
                            username=str(flow["xui_username"]),
                            password=text,
                        )
                    else:
                        business.set_xnet_credential(
                            actor, server_id=int(server["id"]), api_token=text
                        )
                except Exception:
                    business.delete_server(actor, server_id=int(server["id"]))
                    raise
                context.user_data.pop("biz_flow", None)
                await update.effective_chat.send_message(
                    "✅ سرور با موفقیت اضافه شد.",
                    reply_markup=admin_main_keyboard(),
                )
                return

        if isinstance(flow, dict):
            fields = [part.strip() for part in text.split("|")]
            kind = flow.get("kind")
            if kind == "ticket_reply":
                ticket_id = int(flow["ticket_id"])
                ticket = business.reply_ticket_admin(
                    actor,
                    ticket_id=ticket_id,
                    reply=text,
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    f"✅ پاسخ تیکت #{ticket_id} ثبت شد.\n"
                    f"وضعیت: {ticket['status']}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "🎫 مشاهده تیکت",
                            callback_data=f"biz:ticket:{ticket_id}",
                        )],
                        [InlineKeyboardButton("↩️ تیکت‌ها", callback_data="biz:tickets")],
                    ]),
                )
                return
            if kind == "customer_search":
                results = business.search_customers_admin(actor, text)
                context.user_data.pop("biz_flow", None)
                if not results:
                    await update.effective_message.reply_text(
                        "❌ مشتری پیدا نشد.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔎 جستجوی دوباره", callback_data="biz:customersearch")],
                            [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")],
                        ]),
                    )
                    return
                rows = [
                    [InlineKeyboardButton(
                        (
                            f"👤 {item.get('display_name') or item.get('telegram_user_id')} · "
                            f"فعال {int(item.get('active_subscriptions') or 0)} · "
                            f"منقضی {int(item.get('expired_subscriptions') or 0)}"
                        )[:60],
                        callback_data=f"biz:customer:{item['id']}",
                    )]
                    for item in results
                ]
                rows.extend([
                    [InlineKeyboardButton("🔎 جستجوی جدید", callback_data="biz:customersearch")],
                    [InlineKeyboardButton("↩️ مشتریان", callback_data="biz:customers")],
                ])
                await update.effective_message.reply_text(
                    f"✅ {len(results)} نتیجه پیدا شد.",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
            if kind == "growth_config":
                if len(fields) != 7:
                    raise ValueError("invalid growth config")
                business.update_growth_settings(
                    actor,
                    referral_trial_reward=int(fields[0]),
                    referral_purchase_reward=int(fields[1]),
                    referral_min_purchase=int(fields[2]),
                    referral_max_rewards=int(fields[3]),
                    referral_currency=fields[4],
                    trial_traffic_gb=int(fields[5]),
                    trial_duration_days=int(fields[6]),
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ تنظیمات فروش پیشرفته ذخیره شد.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎯 فروش پیشرفته", callback_data="biz:growth")]
                    ]),
                )
                return
            if kind == "coupon_add":
                if len(fields) not in (8, 9):
                    raise ValueError("invalid coupon")
                business.add_coupon(
                    actor,
                    code=fields[0],
                    discount_kind=fields[1],
                    value=int(fields[2]),
                    currency=fields[3],
                    min_amount=int(fields[4] or 0),
                    max_discount=int(fields[5] or 0),
                    max_uses=int(fields[6] or 0),
                    per_customer_limit=int(fields[7] or 0),
                    expires_at=fields[8] if len(fields) == 9 else "",
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ کوپن ساخته شد.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎟 کوپن‌ها", callback_data="biz:coupons")]
                    ]),
                )
                return
            if kind == "wallet_adjust":
                if len(fields) not in (2, 3):
                    raise ValueError("invalid wallet adjustment")
                customer_id = int(flow["customer_id"])
                business.adjust_wallet_admin(
                    actor,
                    customer_id=customer_id,
                    currency=fields[0],
                    amount=int(fields[1]),
                    note=fields[2] if len(fields) == 3 else "",
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ موجودی کیف پول تغییر کرد.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "💰 کیف پول مشتری",
                            callback_data=f"biz:wallet:{customer_id}",
                        )]
                    ]),
                )
                return
            if kind == "panel_secret":
                if not text:
                    raise ValueError("empty secret")
                business.set_panel_credential(
                    actor,
                    server_id=int(flow["server_id"]),
                    secret=text,
                )
                try:
                    await update.effective_message.delete()
                except Exception:
                    pass
            elif kind == "xui_secret":
                flavor = str(flow.get("flavor") or "")
                if flavor == "sanaei":
                    if not text:
                        raise ValueError("empty token")
                    business.set_xui_credential(
                        actor,
                        server_id=int(flow["server_id"]),
                        api_token=text,
                    )
                elif flavor == "alireza" and 2 <= len(fields) <= 3:
                    business.set_xui_credential(
                        actor,
                        server_id=int(flow["server_id"]),
                        username=fields[0],
                        password=fields[1],
                        secret_header=fields[2] if len(fields) == 3 else "",
                    )
                else:
                    raise ValueError("invalid X-UI credential")
                try:
                    await update.effective_message.delete()
                except Exception:
                    pass
            elif kind == "xnet_secret":
                if len(fields) == 1 and fields[0]:
                    business.set_xnet_credential(
                        actor,
                        server_id=int(flow["server_id"]),
                        api_token=fields[0],
                    )
                elif len(fields) == 3:
                    business.set_xnet_credential(
                        actor,
                        server_id=int(flow["server_id"]),
                        api_token=fields[0],
                        username=fields[1] or "admin",
                        password=fields[2],
                    )
                else:
                    raise ValueError("invalid X-NET credential")
                try:
                    await update.effective_message.delete()
                except Exception:
                    pass
            elif kind == "server" and 2 <= len(fields) <= 7:
                panel_kind = fields[1].lower()
                if panel_kind == "xui":
                    if len(fields) < 4:
                        raise ValueError("X-UI flavor is required")
                    business.add_server(
                        actor,
                        label=fields[0],
                        panel_kind=panel_kind,
                        endpoint=fields[2],
                        xui_flavor=fields[3],
                        xui_inbound_ids=fields[4] if len(fields) >= 5 else "",
                        xui_public_origin=fields[5] if len(fields) >= 6 else "",
                        xui_sub_path=fields[6] if len(fields) >= 7 else "",
                    )
                elif panel_kind == "xnet":
                    if len(fields) < 3:
                        raise ValueError("X-NET endpoint is required")
                    business.add_server(
                        actor,
                        label=fields[0],
                        panel_kind=panel_kind,
                        endpoint=fields[2],
                        xnet_inbound_ids=fields[3] if len(fields) >= 4 else "",
                        xnet_public_origin=fields[4] if len(fields) >= 5 else "",
                        xnet_sub_port=int(fields[5] or 0) if len(fields) >= 6 else 0,
                        xnet_sub_path=fields[6] if len(fields) >= 7 else "",
                    )
                else:
                    business.add_server(
                        actor,
                        label=fields[0],
                        panel_kind=panel_kind,
                        endpoint=fields[2] if len(fields) >= 3 else "",
                        admin_path=fields[3] if len(fields) >= 4 else "",
                        user_path=fields[4] if len(fields) >= 5 else "",
                    )
            elif kind == "node" and 2 <= len(fields) <= 3:
                business.add_node(
                    actor,
                    label=fields[0],
                    server_id=int(fields[1]),
                    location=fields[2] if len(fields) == 3 else "",
                )
            elif kind == "plan" and len(fields) == 5:
                business.add_plan(actor, name=fields[0], traffic_gb=int(fields[1]), duration_days=int(fields[2]), price=int(fields[3]), currency=fields[4])
            elif kind == "payment" and len(fields) == 6:
                business.add_payment_method(actor, kind=fields[0], title=fields[1], currency=fields[2], destination=fields[3], network=fields[4], instructions=fields[5])
            elif kind == "link" and len(fields) == 2:
                business.create_smart_link(actor, label=fields[0], target=fields[1])
            else:
                raise ValueError("invalid input")
            context.user_data.pop("biz_flow", None)
            await update.effective_message.reply_text("✅ ذخیره شد.", reply_markup=_menu(spec))
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ قالب یا وضعیت معتبر نیست.", reply_markup=_menu(spec))
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.",
            reply_markup=admin_main_keyboard(),
        )


async def userbot_admin_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    spec, _, _, business = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot media handler registered for non-admin role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    from TenantRuntime.AdminBot import userbot_management
    handled = await userbot_management.handle_media(
        update,
        context,
        business=business,
        actor=actor,
        admin_main_keyboard=admin_main_keyboard,
    )
    if not handled and update.effective_message:
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.",
            reply_markup=admin_main_keyboard(),
        )


async def userbot_admin_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    spec, _, _, business = _services(context)
    if spec.role != "admin":
        raise RuntimeError("AdminBot media handler registered for non-admin role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    from TenantRuntime.AdminBot import userbot_management
    handled = await userbot_management.handle_document(
        update,
        context,
        business=business,
        actor=actor,
        admin_main_keyboard=admin_main_keyboard,
    )
    if not handled and update.effective_message:
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.",
            reply_markup=admin_main_keyboard(),
        )


def register_admin_handlers(application: Application) -> None:
    spec = application.bot_data.get("runtime_spec")
    if not isinstance(spec, RuntimeBotSpec) or spec.role != "admin":
        raise RuntimeError("AdminBot handlers require an admin runtime spec")
    application.add_handler(TypeHandler(Update, runtime_access_gate), group=-1)
    application.add_handler(CommandHandler("start", show_home), group=0)
    application.add_handler(CommandHandler("menu", show_home), group=0)
    application.add_handler(CommandHandler("status", show_status), group=0)
    application.add_handler(CallbackQueryHandler(on_callback), group=0)
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO, userbot_admin_media), group=0
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL, userbot_admin_document), group=0
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text), group=0
    )
    application.add_error_handler(runtime_error)

