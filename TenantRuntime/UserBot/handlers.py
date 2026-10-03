"""Tenant UserBot handlers.

All end-user shop menus, callbacks, receipts and text flows live in this module.
Tenant-admin management handlers are intentionally kept out of this package.
"""

from __future__ import annotations

import random
import sqlite3
from contextvars import ContextVar
from typing import Any

from telegram import (
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    InlineKeyboardMarkup,
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
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.button_styles import keyboard_button as KeyboardButton
from Shared.timeutils import format_tehran, parse_utc, utcnow
from TenantRuntime.common import _deny_update, _services, runtime_access_gate, runtime_error

def _money_lines(items: list[dict]) -> list[str]:
    if not items:
        return ["• مبلغ تأییدشده: 0"]
    return [
        f"• {int(item.get('amount') or 0):,} {item.get('currency') or ''} "
        f"({int(item.get('count') or 0)} پرداخت)"
        for item in items
    ]


def _account_status_label(value: object) -> str:
    return {
        "active": "🟢 فعال",
        "blocked": "🔴 مسدود",
    }.get(str(value or "").strip().lower(), str(value or "نامشخص"))


def _subscription_status_label(item: dict[str, Any]) -> str:
    if int(item.get("enforcement_pending") or 0):
        return "🟠 در حال قطع خودکار"
    return {
        "active": "🟢 فعال",
        "disabled": "🟡 غیرفعال",
        "expired": "🔴 منقضی",
        "pending_provisioning": "🟠 در انتظار فعال‌سازی",
    }.get(
        str(item.get("status") or "").strip().lower(),
        str(item.get("status") or "نامشخص"),
    )


def _order_status_label(value: object) -> str:
    return {
        "pending_payment": "🟠 در انتظار پرداخت",
        "payment_review": "🟠 در انتظار بررسی",
        "paid": "🔵 پرداخت‌شده / در انتظار فعال‌سازی",
        "fulfilled": "🟢 تکمیل‌شده",
        "cancelled": "⚪ لغوشده",
        "rejected": "🔴 ردشده",
    }.get(str(value or "").strip().lower(), str(value or "نامشخص"))


def _local_time_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "-"
    try:
        return format_tehran(parse_utc(text))
    except Exception:
        return text


def _days_left(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        seconds = (parse_utc(text) - utcnow()).total_seconds()
    except Exception:
        return None
    if seconds <= 0:
        return 0
    days = int(seconds // 86400)
    return days + (1 if seconds % 86400 else 0)


def _last_online_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "هنوز ثبت نشده"
    try:
        delta = (utcnow() - parse_utc(text)).total_seconds()
    except Exception:
        return text
    if delta < 0:
        return _local_time_text(text)
    if delta < 60:
        return "چند ثانیه پیش"
    if delta < 3600:
        return f"{max(1, int(delta // 60))} دقیقه پیش"
    if delta < 86400:
        return f"{max(1, int(delta // 3600))} ساعت پیش"
    if delta < 86400 * 30:
        return f"{max(1, int(delta // 86400))} روز پیش"
    return _local_time_text(text)


def _subscription_metrics(
    item: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    usage_gb = max(0.0, float(item.get("usage_bytes") or 0) / (1024 ** 3))
    limit_gb = max(0.0, float(item.get("traffic_bytes") or 0) / (1024 ** 3))
    remaining_gb = max(0.0, limit_gb - usage_gb)
    days_left = _days_left(item.get("expires_at"))

    unlimited_volume = (
        bool(settings.get("renew_unlimited_volume", False))
        and limit_gb >= float(
            settings.get("renew_unlimited_volume_from_gb") or 1000
        )
    )
    unlimited_time = (
        bool(settings.get("renew_unlimited_time", False))
        and days_left is not None
        and days_left >= int(
            settings.get("renew_unlimited_time_from_days") or 365
        )
    )
    return {
        "usage_gb": usage_gb,
        "limit_gb": limit_gb,
        "remaining_gb": remaining_gb,
        "days_left": days_left,
        "unlimited_volume": unlimited_volume,
        "unlimited_time": unlimited_time,
    }


def _subscription_detail_text(
    item: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    metrics = _subscription_metrics(item, settings)
    username = str(item.get("customer_username") or "").strip().lstrip("@")
    display_name = str(item.get("customer_display_name") or "").strip()
    days = metrics["days_left"]

    usage_line = (
        f"{metrics['usage_gb']:.2f} گیگ از نامحدود"
        if metrics["unlimited_volume"]
        else f"{metrics['usage_gb']:.2f} از {metrics['limit_gb']:.2f} گیگ"
    )
    remaining_line = (
        "نامحدود"
        if metrics["unlimited_volume"]
        else f"{metrics['remaining_gb']:.2f} گیگ"
    )
    if metrics["unlimited_time"]:
        remaining_time = "نامحدود"
    elif days is None:
        remaining_time = "نامشخص"
    elif days <= 0:
        remaining_time = "منقضی شده"
    else:
        remaining_time = f"{days} روز"

    lines = ["📄 اطلاعات اشتراک شما", ""]
    if bool(settings.get("show_username", True)):
        identity = f"@{username}" if username else (display_name or "-")
        lines.append(f"👤 کاربر: {identity}")
    lines.extend([
        f"📦 پلن: {item.get('plan_name') or '-'}",
        f"📡 سرور: {item.get('server_label') or 'نامشخص'}",
        f"📶 وضعیت: {_subscription_status_label(item)}",
        f"📊 میزان استفاده: {usage_line}",
        f"📥 حجم باقی‌مانده: {remaining_line}",
        f"📅 تاریخ انقضا: {'نامحدود' if metrics['unlimited_time'] else _local_time_text(item.get('expires_at'))}",
        f"⏳ زمان باقی‌مانده: {remaining_time}",
        f"🕓 آخرین اتصال: {_last_online_text(item.get('last_online'))}",
        f"🔑 شناسه: {item.get('id')}",
    ])
    return "\n".join(lines)


def _user_account_text(
    summary: dict, settings: dict | None = None
) -> str:
    settings = settings or {}
    customer = dict(summary.get("customer") or {})
    subs = dict(summary.get("subscriptions") or {})
    username = str(customer.get("username") or "").strip()
    lines = [
        "👤 پروفایل کاربر",
        f"نام: {customer.get('display_name') or 'کاربر'}",
    ]
    if bool(settings.get("show_username", True)):
        lines.append(
            f"یوزرنیم: {'@' + username.lstrip('@') if username else '-'}"
        )
    lines.extend([
        f"Telegram ID: {customer.get('telegram_user_id')}",
        f"وضعیت کاربر: {_account_status_label(customer.get('status'))}",
        f"تاریخ عضویت: {_local_time_text(customer.get('created_at'))}",
        "",
        "📦 وضعیت سرویس‌ها",
        f"• فعال: {int(subs.get('active') or 0)}",
        f"• غیرفعال: {int(subs.get('disabled') or 0)}",
        f"• منقضی: {int(subs.get('expired') or 0)}",
        f"• در انتظار فعال‌سازی: {int(subs.get('pending') or 0)}",
        "",
        "🧾 سفارش‌ها",
        f"• کل سفارش‌ها: {int(summary.get('orders_total') or 0)}",
        f"• تکمیل‌شده: {int(summary.get('fulfilled_orders') or 0)}",
        f"• باز / در انتظار: {int(summary.get('pending_orders') or 0)}",
        f"• لغوشده: {int(summary.get('cancelled_orders') or 0)}",
        "",
        "💰 پرداخت‌های تأییدشده",
        *_money_lines(list(summary.get("paid_totals") or [])),
    ])
    return "\n".join(lines)


def _order_detail_text(order: dict[str, Any]) -> str:
    operation = {
        "purchase": "خرید",
        "renewal": "تمدید",
        "trial": "تست رایگان",
    }.get(str(order.get("operation") or ""), "خرید")
    amount = int(order.get("amount") or 0)
    original = int(order.get("original_amount") or amount)
    discount = int(order.get("discount_amount") or 0)
    wallet = int(order.get("wallet_amount") or 0)
    lines = [
        f"🧾 سفارش #{order.get('id')}",
        "",
        f"نوع: {operation}",
        f"پلن: {order.get('plan_name') or '-'}",
        f"حجم پلن: {int(order.get('plan_traffic_gb') or 0)} گیگ",
        f"مدت: {int(order.get('plan_duration_days') or 0)} روز",
        f"وضعیت: {_order_status_label(order.get('status'))}",
        f"مبلغ نهایی: {amount:,} {order.get('currency') or ''}",
    ]
    if original != amount or discount:
        lines.append(f"مبلغ اولیه: {original:,} {order.get('currency') or ''}")
    if discount:
        lines.append(f"تخفیف: {discount:,} {order.get('currency') or ''}")
    if wallet:
        lines.append(f"پرداخت از کیف پول: {wallet:,} {order.get('currency') or ''}")
    lines.extend([
        f"ثبت سفارش: {_local_time_text(order.get('created_at'))}",
        f"زمان پرداخت: {_local_time_text(order.get('paid_at')) if order.get('paid_at') else '-'}",
    ])
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
            "gift": "🎁 هدیه",
        }
        for tx in history[:10]:
            amount = int(tx.get("amount") or 0)
            lines.append(
                f"• {labels.get(str(tx.get('kind')), str(tx.get('kind') or '-'))}: "
                f"{amount:+,} {tx.get('currency') or ''} → "
                f"{int(tx.get('resulting_balance') or 0):,}"
            )
    return "\n".join(lines)


def _checkout_text(order: dict, wallet: dict) -> str:
    balance = 0
    for account in list(wallet.get("accounts") or []):
        if str(account.get("currency")) == str(order.get("currency")):
            balance = int(account.get("balance") or 0)
            break
    original = int(order.get("original_amount") or order.get("amount") or 0)
    discount = int(order.get("discount_amount") or 0)
    final = int(order.get("amount") or 0)
    operation = str(order.get("operation") or "purchase")
    op_title = "تمدید" if operation == "renewal" else "خرید"
    lines = [
        f"🧾 {op_title} سفارش #{order['id']}",
        f"پلن: {order.get('plan_name') or '-'}",
    ]
    if str(order.get("category_title") or "").strip():
        lines.append(f"دسته‌بندی: {order.get('category_title')}")
    if str(order.get("selected_server_label") or "").strip():
        lines.append(f"سرور: {order.get('selected_server_label')}")
    lines.extend([
        f"مبلغ اصلی: {original:,} {order.get('currency') or ''}",
        f"تخفیف: {discount:,} {order.get('currency') or ''}",
        f"مبلغ نهایی: {final:,} {order.get('currency') or ''}",
        f"کیف پول: {balance:,} {order.get('currency') or ''}",
    ])
    return "\n".join(lines)



def _checkout_markup(
    order_id: int,
    settings: dict,
    *,
    back_callback: str = "runtime:home",
    back_label: str = "↩️ منو",
) -> InlineKeyboardMarkup:
    """Build checkout actions while honoring admin-side marketing settings."""
    pay_row: list[InlineKeyboardButton] = []
    if bool(settings.get("enable_discount_code", True)):
        pay_row.append(
            _button(
                "🎟 کد تخفیف",
                callback_data=f"shop:coupon:{int(order_id)}",
                settings=settings,
            )
        )
    pay_row.append(
        _button(
            "💰 پرداخت کیف پول",
            callback_data=f"shop:walletpay:{int(order_id)}",
            settings=settings,
        )
    )
    rows = [
        pay_row,
        [_button(
            "💳 روش‌های پرداخت",
            callback_data=f"shop:paymethods:{int(order_id)}",
            settings=settings,
        )],
        [_button(
            back_label,
            callback_data=back_callback,
            settings=settings,
        )],
    ]
    return InlineKeyboardMarkup(rows)


_BUTTON_SETTINGS: ContextVar[dict[str, Any]] = ContextVar(
    "tenant_userbot_button_settings", default={}
)

BUTTON_THEME_META = {
    "smart": {
        "title": "✨ هوشمند",
        "description": "خرید و تایید سبز، هشدار قرمز و مسیرهای اصلی آبی",
    },
    "shop": {
        "title": "🛒 فروشگاهی",
        "description": "اکشن‌های خرید، کیف پول، هدیه و پرداخت پررنگ‌تر",
    },
    "pro": {
        "title": "💼 حرفه‌ای",
        "description": "رنگ محدود به اکشن‌های مهم و مسیرهای مدیریتی",
    },
    "minimal": {
        "title": "🕊 مینیمال",
        "description": "فقط تاییدهای مهم و عملیات خطرناک رنگ می‌گیرند",
    },
}

_DANGER_TOKENS = (
    "❌", "🗑", "🚫", "لغو", "حذف", "بستن", "غیرفعال",
    "disable", "delete", "remove", "reject", "cancel", "close",
)
_SUCCESS_TOKENS = (
    "✅", "➕", "💳", "💰", "🎁", "🔥", "تایید", "تأیید",
    "پرداخت", "خرید", "تمدید", "افزودن", "ارسال", "ساخت", "فعال",
    "approve", "confirm", "pay", "buy", "renew", "add", "send", "enable",
)
_STRONG_SUCCESS_TOKENS = (
    "✅", "تایید", "تأیید", "پرداخت کردم", "تایید و پرداخت",
    "ارسال", "افزودن", "approve", "confirm", "send", "add",
)
_SHOP_TOKENS = (
    "💳", "💰", "🎁", "🔥", "🏷", "خرید", "تمدید", "پرداخت",
    "کیف پول", "شارژ", "کارت", "کوپن", "هدیه", "پلن", "بسته",
    "قیمت", "wallet", "coupon", "gift", "plan", "price",
)
_PRIMARY_TOKENS = (
    "🔙", "↩️", "➡️", "⬅️", "◀️", "▶️", "📊", "📈", "📋",
    "📁", "⚙️", "🌐", "🔗", "🔄", "بازگشت", "وضعیت", "لیست",
    "تنظیم", "راهنما", "جستجو", "noop", "back", "status", "list",
    "settings", "menu", "guide", "search",
)


def _normalize_button_theme(value: Any) -> str:
    theme = str(value or "smart").strip().lower()
    return theme if theme in BUTTON_THEME_META else "smart"


def _contains_any(haystack: str, tokens: tuple[str, ...]) -> bool:
    return any(token in haystack for token in tokens)


def _infer_button_style(
    text: Any,
    callback_data: Any = None,
    *,
    theme: str = "smart",
) -> str | None:
    haystack = f"{str(text or '')} {str(callback_data or '')}".lower()
    selected = _normalize_button_theme(theme)
    if _contains_any(haystack, _DANGER_TOKENS):
        return "danger"
    if selected == "minimal":
        return (
            "success"
            if _contains_any(haystack, _STRONG_SUCCESS_TOKENS)
            else None
        )
    if selected == "pro":
        if _contains_any(haystack, _STRONG_SUCCESS_TOKENS):
            return "success"
        return "primary" if _contains_any(haystack, _PRIMARY_TOKENS) else None
    if selected == "shop":
        if (
            _contains_any(haystack, _SUCCESS_TOKENS)
            or _contains_any(haystack, _SHOP_TOKENS)
        ):
            return "success"
        return "primary"
    if _contains_any(haystack, _SUCCESS_TOKENS):
        return "success"
    if _contains_any(haystack, _PRIMARY_TOKENS):
        return "primary"
    return "primary"


def _set_button_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    clean = dict(settings or {})
    _BUTTON_SETTINGS.set(clean)
    return clean


def InlineKeyboardButton(
    text: str,
    *args: Any,
    settings: dict[str, Any] | None = None,
    style: str | None = None,
    **kwargs: Any,
) -> TelegramInlineKeyboardButton:
    """Tenant-aware button constructor with native Telegram style support.

    PTB 22.7+ exposes style directly. The fallback keeps deployments that
    have not refreshed dependencies yet functional via api_kwargs.
    """
    current = (
        dict(settings)
        if settings is not None
        else dict(_BUTTON_SETTINGS.get())
    )
    selected_style: str | None = None
    if bool(current.get("colored_buttons", True)):
        selected_style = style or _infer_button_style(
            text,
            kwargs.get("callback_data"),
            theme=str(current.get("button_theme") or "smart"),
        )

    api_kwargs = dict(kwargs.pop("api_kwargs", None) or {})
    if selected_style:
        try:
            return TelegramInlineKeyboardButton(
                text,
                *args,
                style=selected_style,
                api_kwargs=api_kwargs or None,
                **kwargs,
            )
        except TypeError:
            # Compatibility fallback for an old PTB process that has not yet
            # reinstalled requirements after an update.
            if "style" not in api_kwargs:
                api_kwargs["style"] = selected_style

    if api_kwargs:
        kwargs["api_kwargs"] = api_kwargs
    return TelegramInlineKeyboardButton(text, *args, **kwargs)


def _button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    settings: dict | None = None,
) -> TelegramInlineKeyboardButton:
    return InlineKeyboardButton(
        text,
        callback_data=callback_data,
        url=url,
        settings=settings,
    )


def _column_rows(
    buttons: list[TelegramInlineKeyboardButton],
    columns: int,
) -> list[list[TelegramInlineKeyboardButton]]:
    cols = max(1, min(int(columns or 1), 3))
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def _ordered_indexed(
    items: list[Any],
    *,
    shuffle_enabled: bool,
) -> list[tuple[int, Any]]:
    indexed = list(enumerate(items))
    if shuffle_enabled and len(indexed) > 1:
        random.shuffle(indexed)
    return indexed


_CONFIG_URI_PREFIXES = (
    "vless://",
    "vmess://",
    "trojan://",
    "hysteria2://",
    "hy2://",
    "ss://",
    "ssr://",
    "tuic://",
    "wireguard://",
)


def _extract_config_items(content: str) -> list[str]:
    raw = str(content or "").strip()
    if not raw:
        return []
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    uri_lines = [
        line for line in lines
        if line.lower().startswith(_CONFIG_URI_PREFIXES)
    ]
    # Preserve opaque/base64/mixed panel output as one atomic config.
    if len(uri_lines) >= 2 and len(uri_lines) == len(lines):
        return uri_lines
    return [raw]


def _subscription_limit_lines(
    item: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[str, str]:
    usage_gb = max(0.0, float(item.get("usage_bytes") or 0) / (1024 ** 3))
    limit_gb = max(0.0, float(item.get("traffic_bytes") or 0) / (1024 ** 3))
    unlimited_volume = (
        bool(settings.get("renew_unlimited_volume", False))
        and limit_gb >= float(settings.get("renew_unlimited_volume_from_gb") or 1000)
    )
    usage_text = (
        f"{usage_gb:.2f}/نامحدود"
        if unlimited_volume
        else f"{usage_gb:.2f}/{limit_gb:.0f}GB"
    )

    expires_raw = str(item.get("expires_at") or "").strip()
    days_left: int | None = None
    try:
        expiry = parse_utc(expires_raw)
        seconds = (expiry - utcnow()).total_seconds()
        days_left = int(seconds // 86400)
        if seconds > 0 and seconds % 86400:
            days_left += 1
    except Exception:
        pass
    unlimited_time = (
        bool(settings.get("renew_unlimited_time", False))
        and days_left is not None
        and days_left >= int(settings.get("renew_unlimited_time_from_days") or 365)
    )
    expiry_text = "نامحدود" if unlimited_time else (expires_raw or "-")
    return usage_text, expiry_text


def _renewable_subscriptions(
    business: Any,
    actor: int,
) -> tuple[list[dict[str, Any]], int]:
    eligible: list[dict[str, Any]] = []
    blocked = 0
    for item in business.list_subscriptions(actor):
        if (
            not item.get("external_ref")
            or not item.get("server_id")
            or item.get("status") not in ("active", "disabled", "expired")
        ):
            continue
        try:
            result = business.renewal_eligibility(
                actor,
                subscription_id=int(item["id"]),
            )
        except TenantBusinessError:
            blocked += 1
            continue
        if bool(result.get("allowed")):
            eligible.append(item)
        else:
            blocked += 1
    return eligible, blocked


def _subscription_link_controls_enabled(settings: dict[str, Any]) -> bool:
    return any(
        bool(settings.get(key, default))
        for key, default in (
            ("show_direct_config", True),
            ("show_auto_sub_link", False),
            ("show_sub_link", True),
            ("show_sub_link_b64", False),
            ("show_multi_server", False),
            ("show_multi_server_b64", False),
        )
    )


def _subscription_identity_line(
    item: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    if not bool(settings.get("show_username", True)):
        return ""
    username = str(item.get("customer_username") or "").strip().lstrip("@")
    display_name = str(item.get("customer_display_name") or "").strip()
    value = f"@{username}" if username else display_name
    return f"\n  👤 نام: {value}" if value else ""


def _subscription_config_menu_rows(
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if bool(settings.get("show_user_page_link", True)):
        try:
            panel_link = business.panel_user_page_link(
                actor, subscription_id=int(subscription_id)
            )
        except TenantBusinessError:
            panel_link = ""
        if panel_link:
            rows.append([InlineKeyboardButton(
                "🌐 صفحه یوزر هیدیفای",
                url=panel_link,
            )])
    if bool(settings.get("show_direct_config", True)):
        rows.append([InlineKeyboardButton(
            "کانفیگ مستقیم",
            callback_data=f"shop:configs:{int(subscription_id)}",
        )])
    if bool(settings.get("show_auto_sub_link", False)):
        rows.append([InlineKeyboardButton(
            "اشتراک خودکار",
            callback_data=f"shop:autosub:{int(subscription_id)}",
        )])
    if bool(settings.get("show_sub_link", True)):
        rows.append([InlineKeyboardButton(
            "لینک اشتراک",
            callback_data=f"shop:sublink:{int(subscription_id)}",
        )])
    if bool(settings.get("show_sub_link_b64", False)):
        rows.append([InlineKeyboardButton(
            "لینک اشتراک b64",
            callback_data=f"shop:subb64:{int(subscription_id)}",
        )])
    if bool(settings.get("show_multi_server", False)):
        rows.append([InlineKeyboardButton(
            "🌐 لینک اشتراک هوشمند",
            callback_data=f"shop:smart:{int(subscription_id)}",
        )])
    if bool(settings.get("show_multi_server_b64", False)):
        rows.append([InlineKeyboardButton(
            "🌐 لینک اشتراک هوشمند b64",
            callback_data=f"shop:smartb64:{int(subscription_id)}",
        )])
    return rows


def _delivery_access_text(
    business: Any,
    actor: int,
    result: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    """Render only access methods that are currently enabled by AdminBot."""
    try:
        subscription_id = int(
            result.get("id") or result.get("subscription_id") or 0
        )
    except (TypeError, ValueError):
        subscription_id = 0
    if subscription_id <= 0:
        return "برای دریافت اطلاعات اتصال، وارد «📦 اشتراک‌های من» شوید."

    lines: list[str] = []
    if bool(settings.get("show_user_page_link", True)):
        try:
            value = business.panel_user_page_link(
                actor, subscription_id=subscription_id
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🌐 صفحه یوزر هیدیفای:", value])

    if bool(settings.get("show_sub_link", True)):
        try:
            value = business.subscription_link(
                actor, subscription_id=subscription_id
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🔗 لینک اشتراک:", value])

    if bool(settings.get("show_auto_sub_link", False)):
        try:
            value = business.automatic_subscription_link(
                actor, subscription_id=subscription_id
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🤖 لینک اشتراک خودکار:", value])

    if bool(settings.get("show_sub_link_b64", False)):
        try:
            value = business.subscription_link_b64(
                actor, subscription_id=subscription_id
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🔐 لینک اشتراک b64:", value])

    if bool(settings.get("show_multi_server", False)):
        try:
            value = business.smart_subscription_link(
                actor,
                subscription_id=subscription_id,
                base64_output=False,
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🌐 لینک اشتراک هوشمند:", value])

    if bool(settings.get("show_multi_server_b64", False)):
        try:
            value = business.smart_subscription_link(
                actor,
                subscription_id=subscription_id,
                base64_output=True,
            )
        except TenantBusinessError:
            value = ""
        if value:
            lines.extend(["🌐 لینک اشتراک هوشمند b64:", value])

    if bool(settings.get("show_direct_config", True)):
        lines.append("📄 کانفیگ مستقیم: از «📦 اشتراک‌های من» قابل دریافت است.")

    return "\n".join(lines) or (
        "🔒 نمایش لینک و کانفیگ این اشتراک توسط ادمین غیرفعال است."
    )


def _sorted_purchase_plans(
    plans: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    items = list(plans)
    sort_mode = str(settings.get("plan_sort_mode") or "id")
    if sort_mode == "price_asc":
        items.sort(key=lambda p: (int(p.get("price") or 0), int(p["id"])))
    elif sort_mode == "price_desc":
        items.sort(key=lambda p: (-int(p.get("price") or 0), int(p["id"])))
    elif sort_mode == "traffic_asc":
        items.sort(key=lambda p: (int(p.get("traffic_gb") or 0), int(p["id"])))
    elif sort_mode == "traffic_desc":
        items.sort(key=lambda p: (-int(p.get("traffic_gb") or 0), int(p["id"])))
    else:
        items.sort(key=lambda p: int(p["id"]))
    if bool(settings.get("plan_sort_by_priority", True)):
        items.sort(key=lambda p: int(p.get("priority") or 0))
    return items


def _plan_buttons(
    plans: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[list[TelegramInlineKeyboardButton]]:
    buttons = [
        _button(
            (
                f"{p['name']} · {int(p.get('traffic_gb') or 0)}GB · "
                f"{int(p.get('duration_days') or 0)} روز · "
                f"{int(p.get('price') or 0):,} {p.get('currency') or ''}"
            ),
            callback_data=f"shop:plan:{int(p['id'])}",
            settings=settings,
        )
        for p in _sorted_purchase_plans(plans, settings)
    ]
    return _column_rows(
        buttons,
        int(settings.get("plan_columns") or 1),
    )


def _purchase_server_rows(
    servers: list[dict[str, Any]],
    *,
    plan_id: int,
    settings: dict[str, Any],
) -> list[list[TelegramInlineKeyboardButton]]:
    items = list(servers)
    if bool(settings.get("shuffle_server_layout", True)) and len(items) > 1:
        random.shuffle(items)
    buttons = [
        _button(
            str(server.get("label") or f"سرور #{server['id']}"),
            callback_data=f"shop:server:{int(plan_id)}:{int(server['id'])}",
            settings=settings,
        )
        for server in items
    ]
    return _column_rows(
        buttons,
        int(settings.get("server_columns") or 1),
    )


BTN_STATUS = "📊وضعیت اشتراک"
BTN_RENEW = "♾تمدید اشتراک"
BTN_BUY = "💳خرید اشتراک"
BTN_CONNECT = "🔗اتصال اشتراک"
BTN_TRIAL = "🔥تست رایگان"
BTN_WALLET = "💰کیف پول"
BTN_SUPPORT = "📩پشتیبانی"
BTN_GUIDE = "📚راهنما"
BTN_FAQ = "❗️سوالات متداول"
BTN_REFERRAL = "💌دعوت دوستان"
BTN_GIFT = "🎁دریافت هدیه"


def _main_keyboard(spec: RuntimeBotSpec, business) -> ReplyKeyboardMarkup:
    """Persistent customer menu shown at the bottom of Telegram."""
    settings = business.runtime_userbot_settings()
    growth = business._ensure_growth_settings()
    rows = []

    if bool(settings.get("show_user_status", True)):
        rows.append([
            KeyboardButton(BTN_STATUS, settings=settings),
        ])

    commerce_row = []
    if bool(settings.get("show_renew_in_main_menu", True)):
        commerce_row.append(KeyboardButton(BTN_RENEW, settings=settings))
    if bool(settings.get("enable_buy", True)):
        commerce_row.append(KeyboardButton(BTN_BUY, settings=settings))
    if commerce_row:
        rows.append(commerce_row)

    rows.append([
        KeyboardButton(BTN_CONNECT, settings=settings),
    ])

    wallet_row = []
    if bool(growth.get("trial_enabled")):
        wallet_row.append(KeyboardButton(BTN_TRIAL, settings=settings))
    wallet_row.append(KeyboardButton(BTN_WALLET, settings=settings))
    rows.append(wallet_row)

    rows.append([
        KeyboardButton(BTN_SUPPORT, settings=settings),
        KeyboardButton(BTN_GUIDE, settings=settings),
        KeyboardButton(BTN_FAQ, settings=settings),
    ])

    if bool(growth.get("referral_enabled")):
        rows.append([
            KeyboardButton(BTN_REFERRAL, settings=settings),
        ])

    if bool(settings.get("show_gift_button", True)):
        rows.append([
            KeyboardButton(BTN_GIFT, settings=settings),
        ])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
    )


def _home_inline_markup(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _button(
            "🏠 منو",
            callback_data="runtime:home",
            settings=settings,
        )
    ]])


async def _handle_main_reply_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    spec: RuntimeBotSpec,
    business: Any,
    actor: int,
    text: str,
) -> bool:
    settings = _set_button_settings(business.runtime_userbot_settings())

    if text == BTN_STATUS:
        if not bool(settings.get("show_user_status", True)):
            raise TenantBusinessError("user status is disabled")
        await show_status(update, context)
        return True

    if text == BTN_WALLET:
        await update.effective_message.reply_text(
            _wallet_text(business.wallet_summary(actor)),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "➕ شارژ کیف پول",
                    callback_data="shop:wallettopup",
                )],
            ]),
        )
        return True

    if text == BTN_GIFT:
        if not bool(settings.get("show_gift_button", True)):
            raise TenantBusinessError("gift is disabled")
        context.user_data["biz_flow"] = {"kind": "gift_redeem"}
        await update.effective_message.reply_text(
            "🎁 کد هدیه را ارسال کنید."
        )
        return True

    if text == BTN_REFERRAL:
        growth = business._ensure_growth_settings()
        if not bool(growth.get("referral_enabled")):
            raise TenantBusinessError("referral is disabled")
        summary = business.referral_summary(actor)
        referral_settings = dict(summary.get("settings") or {})
        username = str(getattr(context.bot, "username", None) or "").strip()
        code = str(summary["referral_code"])
        invite = (
            f"https://t.me/{username}?start=ref_{code}"
            if username
            else f"/start ref_{code}"
        )
        rewards = list(summary.get("rewards") or [])
        reward_lines = [
            f"• {x.get('reward_type')}: {int(x.get('amount') or 0):,} "
            f"{x.get('currency') or ''} ({int(x.get('count') or 0)} مورد)"
            for x in rewards
        ] or ["• هنوز پاداشی ثبت نشده است."]
        body = "\n".join([
            "🤝 دعوت دوستان",
            f"وضعیت: {'فعال' if int(referral_settings.get('referral_enabled') or 0) else 'خاموش'}",
            f"دعوت موفق ثبت‌شده: {int(summary.get('referred_count') or 0)}",
            f"پاداش تست: {int(referral_settings.get('referral_trial_reward') or 0):,} "
            f"{referral_settings.get('referral_currency') or ''}",
            f"پاداش اولین خرید: {int(referral_settings.get('referral_purchase_reward') or 0):,} "
            f"{referral_settings.get('referral_currency') or ''}",
            "",
            "🔗 لینک دعوت شما:",
            invite,
            "",
            "🎁 پاداش‌ها",
            *reward_lines,
        ])
        await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "💰 کیف پول",
                    callback_data="shop:wallet",
                )
            ]]),
            disable_web_page_preview=True,
        )
        return True

    if text == BTN_TRIAL:
        growth = business._ensure_growth_settings()
        if not bool(growth.get("trial_enabled")):
            raise TenantBusinessError("free trial is disabled")
        result = business.claim_free_trial(actor)
        await update.effective_message.reply_text(
            "✅ تست رایگان فعال شد.\n\n"
            + _delivery_access_text(business, actor, result, settings),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📦 اشتراک‌های من",
                    callback_data="shop:subs",
                )
            ]]),
            disable_web_page_preview=True,
        )
        return True

    if text == BTN_BUY:
        if not bool(settings.get("enable_buy", True)):
            raise TenantBusinessError("purchase is disabled")
        categories = (
            business.list_plan_categories()
            if bool(settings.get("plan_categories_enabled", True))
            else []
        )
        plans = [
            p for p in business.list_plans()
            if not str(p.get("name") or "").startswith("__WHITELABEL_")
        ]
        if categories:
            rows = [
                [
                    _button(
                        f"📂 {category['title']}",
                        callback_data=f"shop:buycat:{int(category['id'])}",
                        settings=settings,
                    )
                ]
                for category in categories
            ]
            if any(p.get("category_id") is None for p in plans):
                rows.append([
                    _button(
                        "📋 سایر پلن‌ها",
                        callback_data="shop:buycat:0",
                        settings=settings,
                    )
                ])
            body = (
                str(settings.get("plans_list_text") or "").strip()
                or "📂 دسته‌بندی پلن‌ها\nدسته موردنظر را انتخاب کنید:"
            )
        else:
            rows = _plan_buttons(plans, settings)
            if not rows:
                rows = [[InlineKeyboardButton(
                    "پلنی موجود نیست",
                    callback_data="noop",
                )]]
            body = (
                str(settings.get("plans_list_text") or "").strip()
                or "📋 پلن موردنظر را انتخاب کنید:"
            )
        await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if text == BTN_RENEW:
        if not bool(settings.get("enable_renew", True)):
            await update.effective_message.reply_text(
                "🚫 تمدید اشتراک در حال حاضر غیرفعال است.",
                reply_markup=_main_keyboard(spec, business),
            )
            return True
        items, blocked = _renewable_subscriptions(business, actor)
        rows = [
            [InlineKeyboardButton(
                f"♾ #{item['id']} · {item['plan_name']}",
                callback_data=f"shop:renew:{item['id']}",
            )]
            for item in items
        ]
        body = "♾ تمدید اشتراک\nاشتراک موردنظر را انتخاب کنید:"
        if not rows:
            rows = [[InlineKeyboardButton(
                "اشتراک قابل تمدیدی وجود ندارد",
                callback_data="noop",
            )]]
            if blocked:
                body = business.renewal_not_allowed_text()
        await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if text == BTN_CONNECT:
        items = [
            item
            for item in business.list_subscriptions(actor)
            if item.get("status") == "active"
            and item.get("external_ref")
            and item.get("server_id")
        ]
        rows = []
        for item in items:
            subscription_id = int(item["id"])
            title = str(item.get("plan_name") or f"اشتراک #{subscription_id}")
            menu_rows = _subscription_config_menu_rows(
                business, actor, subscription_id, settings
            )
            if menu_rows:
                rows.append([InlineKeyboardButton(
                    f"📝 {title}",
                    callback_data=f"shop:configmenu:{subscription_id}",
                )])
        if not rows:
            rows.append([InlineKeyboardButton(
                "اشتراک فعالی برای اتصال وجود ندارد",
                callback_data="noop",
            )])
        rows.append([InlineKeyboardButton(
            "📦 اشتراک‌های من",
            callback_data="shop:subs",
        )])
        await update.effective_message.reply_text(
            "🔗 اتصال اشتراک\n"
            "اشتراک موردنظر را انتخاب کنید تا روش‌های فعال اتصال همان سرویس نمایش داده شود:",
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
        return True

    if text == BTN_SUPPORT:
        items = business.list_tickets(actor)
        body = (
            str(settings.get("ticket_panel_text") or "").strip()
            or "🎫 تیکت‌های من"
        )
        body += "\n" + (
            "\n".join(
                f"• #{x['id']} · {x['subject']} · {x['status']}"
                f"{' · پاسخ داده شد' if x.get('admin_reply') else ''}"
                for x in items
            )
            or "تیکتی ندارید."
        )
        rows = [
            [InlineKeyboardButton(
                f"🎫 #{x['id']} · {x['subject']}"[:60],
                callback_data=f"shop:ticket:{x['id']}",
            )]
            for x in items[:15]
        ]
        rows.append([InlineKeyboardButton(
            "➕ تیکت جدید",
            callback_data="shop:newticket",
        )])
        await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if text == BTN_GUIDE:
        guide = str(settings.get("guide_text") or "").strip()
        rows = [
            [
                InlineKeyboardButton(
                    "📱 اندروید",
                    callback_data="shop:guide:android",
                ),
                InlineKeyboardButton(
                    "📱 IOS",
                    callback_data="shop:guide:ios",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖥️ ویندوز",
                    callback_data="shop:guide:windows",
                ),
                InlineKeyboardButton(
                    "💻 مک",
                    callback_data="shop:guide:mac",
                ),
            ],
            [InlineKeyboardButton(
                "🖥️ لینوکس",
                callback_data="shop:guide:linux",
            )],
        ]
        await update.effective_message.reply_text(
            "💡 راهنما\n" + (guide or "انتخاب سیستم عامل ⬇️"),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if text == BTN_FAQ:
        faq = str(settings.get("faq_text") or "").strip()
        await update.effective_message.reply_text(
            "📕 سوالات متداول\n" + (faq or "متنی تنظیم نشده است.")
        )
        return True

    return False


async def _force_join_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE, business) -> bool:
    settings = _set_button_settings(business.runtime_userbot_settings())
    if not bool(settings.get("force_join_enabled", False)):
        return True
    channel = str(settings.get("force_join_channel") or "").strip()
    if not channel:
        # Misconfiguration must not lock every customer out.
        return True
    actor = int(update.effective_user.id) if update.effective_user else 0
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=actor)
        status = str(getattr(member, "status", "") or "")
        if status in ("creator", "administrator", "member", "restricted"):
            return True
    except Exception:
        # Telegram can only check reliably when the bot can access the target.
        # Fail open on API/config errors to avoid an accidental global lockout.
        return True

    text = "🔒 برای استفاده از ربات ابتدا در کانال اعلام‌شده عضو شوید."
    rows = []
    if channel.startswith("@"):
        rows.append([InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{channel[1:]}")])
    rows.append([InlineKeyboardButton("✅ بررسی عضویت", callback_data="runtime:home")])
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        await update.callback_query.answer("ابتدا عضویت را انجام دهید.", show_alert=True)
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            if update.effective_chat:
                await update.effective_chat.send_message(text, reply_markup=markup)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return False


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, state_store, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot handler registered for non-user role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
    if not await _force_join_allowed(update, context, business):
        return
    state = state_store.load(user_id)
    visits = int(state.get("visits") or 0) + 1
    state_store.save(user_id, {**state, "visits": visits, "screen": "home"})
    user = update.effective_user
    business.register_customer(
        user_id,
        display_name=str(getattr(user, "full_name", None) or "کاربر"),
        username=getattr(user, "username", None),
    )
    args = list(getattr(context, "args", None) or [])
    gift_notice = ""
    if args:
        payload = str(args[0] or "").strip()
        if payload.startswith("ref_"):
            try:
                business.register_referral(user_id, referral_code=payload[4:])
            except TenantBusinessError:
                pass
        elif payload.startswith("gift_"):
            try:
                gift = business.redeem_gift_voucher(
                    user_id, code=payload[5:]
                )
                gift_notice = (
                    "\n\n🎁 هدیه شما دریافت شد: "
                    f"{int(gift['amount']):,} {gift['currency']}\n"
                    f"💰 موجودی جدید: {int(gift['resulting_balance']):,} "
                    f"{gift['currency']}"
                )
            except TenantBusinessError:
                gift_notice = "\n\n❌ کد هدیه نامعتبر، منقضی یا قبلاً استفاده شده است."
    settings = business.runtime_userbot_settings()
    custom_welcome = str(settings.get("welcome_message") or "").strip()
    text = (
        custom_welcome
        or (
            f"👋 به {spec.tenant_name} خوش آمدید.\n\n"
            "ربات فروشگاهی فعال است.\n"
            f"تعداد ورود: {visits}"
        )
    ) + gift_notice
    keyboard = _main_keyboard(spec, business)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        if update.effective_chat:
            await update.effective_chat.send_message(
                text,
                reply_markup=keyboard,
            )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=keyboard,
        )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, policy, state_store, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot handler registered for non-user role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
    decision = policy.check(spec, telegram_user_id=user_id)
    if not decision.allowed:
        await _deny_update(update, decision.reason)
        raise ApplicationHandlerStop

    settings = _set_button_settings(business.runtime_userbot_settings())
    if not bool(settings.get("show_user_status", True)):
        text = "🚫 نمایش وضعیت اشتراک در حال حاضر توسط ادمین غیرفعال است."
        markup = InlineKeyboardMarkup([[
            _button(
                "🔙بازگشت",
                callback_data="runtime:home",
                settings=settings,
            )
        ]])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text,
                reply_markup=markup,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text,
                reply_markup=markup,
            )
        return

    state = state_store.load(user_id)
    state_store.save(user_id, {**state, "screen": "status"})
    try:
        summary = business.customer_account_summary(user_id, refresh=True)
        subscriptions = business.list_subscriptions(user_id, limit=100)
        customer = dict(summary.get("customer") or {})
        subs = dict(summary.get("subscriptions") or {})
        active_items = [
            item
            for item in subscriptions
            if str(item.get("status") or "") == "active"
        ]

        lines = [
            "📊 وضعیت حساب و اشتراک‌ها",
            "",
            f"👤 وضعیت کاربر: {_account_status_label(customer.get('status'))}",
        ]
        if bool(settings.get("show_username", True)):
            username = str(customer.get("username") or "").strip().lstrip("@")
            if username:
                lines.append(f"🆔 نام کاربری: @{username}")
        lines.extend([
            "",
            "📦 خلاصه سرویس‌ها",
            f"🟢 فعال: {int(subs.get('active') or 0)}",
            f"🟡 غیرفعال: {int(subs.get('disabled') or 0)}",
            f"🔴 منقضی: {int(subs.get('expired') or 0)}",
            f"🟠 در انتظار فعال‌سازی: {int(subs.get('pending') or 0)}",
            f"🧾 سفارش باز: {int(summary.get('pending_orders') or 0)}",
        ])

        if active_items:
            lines.extend(["", "⚡ سرویس‌های فعال"])
            for item in active_items[:3]:
                metrics = _subscription_metrics(item, settings)
                volume = (
                    f"{metrics['usage_gb']:.1f} گیگ مصرف"
                    if metrics["unlimited_volume"]
                    else (
                        f"{metrics['usage_gb']:.1f}/"
                        f"{metrics['limit_gb']:.1f} گیگ"
                    )
                )
                days = metrics["days_left"]
                remaining = (
                    "نامحدود"
                    if metrics["unlimited_time"]
                    else (
                        "منقضی"
                        if days is not None and days <= 0
                        else f"{days} روز" if days is not None else "نامشخص"
                    )
                )
                lines.append(
                    f"• #{item['id']} · {item.get('plan_name') or '-'}"
                    f" · {volume} · {remaining}"
                )
            if len(active_items) > 3:
                lines.append(
                    f"• و {len(active_items) - 3} سرویس فعال دیگر..."
                )
        else:
            lines.extend(["", "⚪ سرویس فعالی ندارید."])
        text = "\n".join(lines)
    except (TenantBusinessError, ValueError, sqlite3.Error):
        text = (
            "📊 وضعیت حساب و اشتراک‌ها\n\n"
            "⏳ دریافت وضعیت سرویس‌ها موقتاً ممکن نیست."
        )
        subs = {}

    status_markup = InlineKeyboardMarkup([
        [
            _button(
                "👤 پروفایل",
                callback_data="shop:account",
                settings=settings,
            ),
            _button(
                "🧾 سفارش‌های من",
                callback_data="shop:orders",
                settings=settings,
            ),
        ],
        [
            _button(
                f"🟢 فعال ({int(subs.get('active') or 0)})",
                callback_data="shop:subs:active",
                settings=settings,
            ),
            _button(
                f"🔴 منقضی ({int(subs.get('expired') or 0)})",
                callback_data="shop:subs:expired",
                settings=settings,
            ),
        ],
        [
            _button(
                "📦 همه اشتراک‌ها",
                callback_data="shop:subs",
                settings=settings,
            ),
            _button(
                "🔗 اتصال اشتراک",
                callback_data="shop:connect",
                settings=settings,
            ),
        ],
        [
            _button(
                "🔙بازگشت",
                callback_data="runtime:home",
                settings=settings,
            )
        ],
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text,
            reply_markup=status_markup,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=status_markup,
        )


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
    if data == "noop":
        await update.callback_query.answer()
        return
    spec, _, _, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot callback registered for non-user role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if not await _force_join_allowed(update, context, business):
        return
    settings = _set_button_settings(business.runtime_userbot_settings())
    try:
        if data == "shop:account":
            summary = business.customer_account_summary(actor, refresh=True)
            await update.callback_query.edit_message_text(
                _user_account_text(summary, settings),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🟢 سرویس‌های فعال",
                            callback_data="shop:subs:active",
                        ),
                        InlineKeyboardButton(
                            "🔴 منقضی‌شده‌ها",
                            callback_data="shop:subs:expired",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "📦 همه اشتراک‌ها",
                            callback_data="shop:subs",
                        ),
                        InlineKeyboardButton(
                            "🧾 سفارش‌های من",
                            callback_data="shop:orders",
                        ),
                    ],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            )
            return
        if data == "shop:orders":
            items = business.list_customer_orders(actor, limit=15)
            text = "🧾 سفارش‌های من\n" + (
                "\n".join(
                    f"• #{x['id']} · {x.get('plan_name') or '-'}\n"
                    f"  {_order_status_label(x.get('status'))} · "
                    f"{int(x.get('amount') or 0):,} {x.get('currency') or ''}"
                    for x in items
                )
                or "سفارشی ندارید."
            )
            rows = [
                [InlineKeyboardButton(
                    f"🧾 جزئیات سفارش #{x['id']}",
                    callback_data=f"shop:order:{x['id']}",
                )]
                for x in items
            ]
            rows.extend([
                [InlineKeyboardButton("👤 پروفایل", callback_data="shop:account")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:order:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = business.customer_order(actor, order_id=order_id)
            rows = []
            if order["status"] == "paid":
                rows.append([InlineKeyboardButton(
                    f"🔁 تلاش فعال‌سازی سفارش #{order_id}",
                    callback_data=f"shop:retryorder:{order_id}",
                )])
            elif order["status"] == "pending_payment":
                rows.append([InlineKeyboardButton(
                    f"❌ لغو سفارش #{order_id}",
                    callback_data=f"shop:cancelorder:{order_id}",
                )])
            rows.extend([
                [InlineKeyboardButton("🔙 سفارش‌های من", callback_data="shop:orders")],
                [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                _order_detail_text(order),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:retryorder:"):
            order_id = int(data.rsplit(":", 1)[1])
            result = business.retry_own_paid_order(actor, order_id=order_id)
            await update.callback_query.edit_message_text(
                f"✅ سفارش #{order_id} انجام شد.\n\n"
                + _delivery_access_text(business, actor, result, settings),
                reply_markup=_home_inline_markup(settings),
                disable_web_page_preview=True,
            ); return
        if data.startswith("shop:cancelorder:"):
            order_id = int(data.rsplit(":", 1)[1])
            business.cancel_order(actor, order_id=order_id)
            await update.callback_query.edit_message_text(
                f"✅ سفارش #{order_id} لغو شد و در صورت وجود، سهمیه کوپن آزاد شد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧾 سفارش‌های من", callback_data="shop:orders")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:wallet":
            await update.callback_query.edit_message_text(
                _wallet_text(business.wallet_summary(actor)),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ شارژ کیف پول", callback_data="shop:wallettopup")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:wallettopup":
            context.user_data["biz_flow"] = {"kind": "wallet_topup_create"}
            await update.callback_query.edit_message_text(
                "💰 مبلغ | ارز را ارسال کنید.\nمثال: 100000 | IRR",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")]
                ]),
            ); return
        if data.startswith("shop:wallettopupmethod:"):
            _, _, topup_id, method_id = data.split(":", 3)
            topup = business.wallet_topup(actor, int(topup_id))
            method = business.method(int(method_id), currency=str(topup["currency"]))
            context.user_data["biz_flow"] = {
                "kind": "wallet_receipt",
                "topup_id": int(topup_id),
                "method_id": int(method_id),
            }
            await update.callback_query.edit_message_text(
                f"پرداخت شارژ کیف پول به: {method['destination']}\n"
                f"{method.get('instructions') or ''}\n"
                "کد پیگیری یا عکس رسید را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")]
                ]),
            ); return
        if data == "shop:gift":
            context.user_data["biz_flow"] = {"kind": "gift_redeem"}
            await update.callback_query.edit_message_text(
                "🎁 کد هدیه را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ منو", callback_data="runtime:home")
                ]]),
            )
            return
        if data == "shop:referral":
            growth = business._ensure_growth_settings()
            if not bool(growth.get("referral_enabled")):
                raise TenantBusinessError("referral is disabled")
            summary = business.referral_summary(actor)
            settings = dict(summary.get("settings") or {})
            username = str(getattr(context.bot, "username", None) or "").strip()
            code = str(summary["referral_code"])
            invite = (
                f"https://t.me/{username}?start=ref_{code}"
                if username
                else f"/start ref_{code}"
            )
            rewards = list(summary.get("rewards") or [])
            reward_lines = [
                f"• {x.get('reward_type')}: {int(x.get('amount') or 0):,} "
                f"{x.get('currency') or ''} ({int(x.get('count') or 0)} مورد)"
                for x in rewards
            ] or ["• هنوز پاداشی ثبت نشده است."]
            text = "\n".join([
                "🤝 دعوت دوستان",
                f"وضعیت: {'فعال' if int(settings.get('referral_enabled') or 0) else 'خاموش'}",
                f"دعوت موفق ثبت‌شده: {int(summary.get('referred_count') or 0)}",
                f"پاداش تست: {int(settings.get('referral_trial_reward') or 0):,} {settings.get('referral_currency') or ''}",
                f"پاداش اولین خرید: {int(settings.get('referral_purchase_reward') or 0):,} {settings.get('referral_currency') or ''}",
                "",
                "🔗 لینک دعوت شما:",
                invite,
                "",
                "🎁 پاداش‌ها",
                *reward_lines,
            ])
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 کیف پول", callback_data="shop:wallet")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:trial":
            growth = business._ensure_growth_settings()
            if not bool(growth.get("trial_enabled")):
                raise TenantBusinessError("free trial is disabled")
            result = business.claim_free_trial(actor)
            await update.callback_query.edit_message_text(
                "✅ تست رایگان فعال شد.\n\n"
                + _delivery_access_text(business, actor, result, settings),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:buy":
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            categories = (
                business.list_plan_categories()
                if bool(settings.get("plan_categories_enabled", True))
                else []
            )
            plans = [
                p for p in business.list_plans()
                if not str(p.get("name") or "").startswith("__WHITELABEL_")
            ]
            if categories:
                rows = [
                    [
                        _button(
                            f"📂 {category['title']}",
                            callback_data=f"shop:buycat:{int(category['id'])}",
                            settings=settings,
                        )
                    ]
                    for category in categories
                ]
                if any(p.get("category_id") is None for p in plans):
                    rows.append([
                        _button(
                            "📋 سایر پلن‌ها",
                            callback_data="shop:buycat:0",
                            settings=settings,
                        )
                    ])
                rows.append([
                    _button(
                        "🔙بازگشت",
                        callback_data="runtime:home",
                        settings=settings,
                    )
                ])
                await update.callback_query.edit_message_text(
                    str(settings.get("plans_list_text") or "").strip()
                    or "📂 دسته‌بندی پلن‌ها\nدسته موردنظر را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return

            rows = _plan_buttons(plans, settings)
            if not rows:
                rows = [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
            rows.append([
                _button(
                    "🔙بازگشت",
                    callback_data="runtime:home",
                    settings=settings,
                )
            ])
            await update.callback_query.edit_message_text(
                str(settings.get("plans_list_text") or "").strip()
                or "📋 پلن موردنظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:buycat:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            category_id = int(data.rsplit(":", 1)[1])
            if category_id > 0:
                category = business.plan_category(category_id)
                plans = business.list_plans(category_id=category_id)
                title = f"📂 {category['title']}"
            else:
                plans = [
                    p for p in business.list_plans()
                    if p.get("category_id") is None
                ]
                title = "📋 سایر پلن‌ها"
            plans = [
                p for p in plans
                if not str(p.get("name") or "").startswith("__WHITELABEL_")
            ]
            rows = _plan_buttons(plans, settings)
            if not rows:
                rows = [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
            rows.append([
                _button(
                    "🔙 دسته‌بندی‌ها",
                    callback_data="shop:buy",
                    settings=settings,
                )
            ])
            await update.callback_query.edit_message_text(
                f"{title}\n\n"
                + (
                    str(settings.get("plans_list_text") or "").strip()
                    or "پلن موردنظر را انتخاب کنید:"
                ),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:plan:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            plan_id = int(data.rsplit(":", 1)[1])
            plan = business.plan(plan_id, public=True)
            servers = business.list_purchase_servers()
            rows = _purchase_server_rows(
                servers,
                plan_id=plan_id,
                settings=settings,
            )
            if not rows:
                rows = [[
                    InlineKeyboardButton(
                        "سرور قابل خریدی موجود نیست",
                        callback_data="noop",
                    )
                ]]
            category_id = int(plan.get("category_id") or 0)
            back_callback = (
                f"shop:buycat:{category_id}"
                if bool(settings.get("plan_categories_enabled", True))
                and category_id > 0
                else "shop:buy"
            )
            rows.append([
                _button(
                    "🔙 بازگشت به پلن‌ها",
                    callback_data=back_callback,
                    settings=settings,
                )
            ])
            await update.callback_query.edit_message_text(
                (
                    str(settings.get("servers_list_text") or "").strip()
                    or "🛰 سرور موردنظر را انتخاب کنید:"
                )
                + "\n\n"
                + f"📦 {plan['name']} · {int(plan['traffic_gb'])}GB · "
                + f"{int(plan['duration_days'])} روز · "
                + f"{int(plan['price']):,} {plan['currency']}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:server:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            parts = data.split(":")
            if len(parts) != 4:
                raise TenantBusinessError("invalid purchase server")
            plan_id = int(parts[2])
            server_id = int(parts[3])
            order = business.create_order(
                actor,
                plan_id,
                server_id=server_id,
            )
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=_checkout_markup(
                    int(order["id"]),
                    settings,
                    back_callback=f"shop:changeserver:{int(order['id'])}",
                    back_label="↩️ تغییر سرور",
                ),
            )
            return

        if data.startswith("shop:changeserver:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            if (
                str(order.get("operation") or "") != "purchase"
                or str(order.get("status") or "") != "pending_payment"
            ):
                raise TenantBusinessError("purchase order server cannot be changed")
            servers = business.list_purchase_servers()
            rows = _purchase_server_rows(
                servers,
                plan_id=int(order["plan_id"]),
                settings=settings,
            )
            # Convert new-order callbacks to in-place order server changes.
            converted: list[list[TelegramInlineKeyboardButton]] = []
            for row in rows:
                converted_row = []
                for button in row:
                    callback = str(button.callback_data or "")
                    server_id = int(callback.rsplit(":", 1)[1])
                    converted_row.append(
                        _button(
                            button.text,
                            callback_data=(
                                f"shop:orderserver:{order_id}:{server_id}"
                            ),
                            settings=settings,
                        )
                    )
                converted.append(converted_row)
            rows = converted
            if not rows:
                rows = [[
                    InlineKeyboardButton(
                        "سرور قابل خریدی موجود نیست",
                        callback_data="noop",
                    )
                ]]
            rows.append([
                _button(
                    "↩️ سفارش",
                    callback_data=f"shop:checkout:{order_id}",
                    settings=settings,
                )
            ])
            await update.callback_query.edit_message_text(
                str(settings.get("servers_list_text") or "").strip()
                or "🛰 سرور جدید را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:orderserver:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            parts = data.split(":")
            if len(parts) != 4:
                raise TenantBusinessError("invalid order server")
            order_id = int(parts[2])
            server_id = int(parts[3])
            order = business.change_purchase_order_server(
                actor,
                order_id=order_id,
                server_id=server_id,
            )
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=_checkout_markup(
                    order_id,
                    settings,
                    back_callback=f"shop:changeserver:{order_id}",
                    back_label="↩️ تغییر سرور",
                ),
            )
            return

        if data.startswith("shop:checkout:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            back_callback = "runtime:home"
            back_label = "↩️ منو"
            if (
                str(order.get("operation") or "") == "purchase"
                and str(order.get("status") or "") == "pending_payment"
                and order.get("selected_server_id") is not None
            ):
                back_callback = f"shop:changeserver:{order_id}"
                back_label = "↩️ تغییر سرور"
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=_checkout_markup(
                    order_id,
                    settings,
                    back_callback=back_callback,
                    back_label=back_label,
                ),
            ); return
        if data.startswith("shop:coupon:"):
            if not bool(settings.get("enable_discount_code", True)):
                raise TenantBusinessError("discount codes are disabled")
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            if order["status"] != "pending_payment":
                raise TenantBusinessError("order is not awaiting payment")
            context.user_data["biz_flow"] = {
                "kind": "coupon_apply",
                "order_id": order_id,
            }
            await update.callback_query.edit_message_text(
                "🎟 کد تخفیف را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ سفارش", callback_data=f"shop:checkout:{order_id}")]
                ]),
            ); return
        if data.startswith("shop:walletpay:"):
            order_id = int(data.rsplit(":", 1)[1])
            result = business.pay_order_with_wallet(actor, order_id=order_id)
            if result.get("fulfillment_pending"):
                text = (
                    f"✅ مبلغ سفارش #{order_id} از کیف پول پرداخت شد.\n"
                    "⚠️ فعال‌سازی پنل فعلاً انجام نشد؛ پرداخت محفوظ است و می‌توانید از «سفارش‌های من» دوباره تلاش کنید."
                )
            else:
                text = (
                    f"✅ سفارش #{order_id} با کیف پول پرداخت و فعال شد.\n\n"
                    + _delivery_access_text(business, actor, result, settings)
                )
            await update.callback_query.edit_message_text(
                text, reply_markup=_home_inline_markup(settings)
            ); return
        if data.startswith("shop:paymethods:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            if order["status"] != "pending_payment":
                raise TenantBusinessError("order is not awaiting payment")
            methods = business.list_methods(currency=str(order["currency"]))
            rows = [[InlineKeyboardButton(
                f"{m['title']} ({m['kind']})",
                callback_data=f"shop:pay:{order_id}:{m['id']}"
            )] for m in methods] or [[InlineKeyboardButton("روش پرداخت موجود نیست", callback_data="noop")]]
            rows.append([InlineKeyboardButton("↩️ سفارش", callback_data=f"shop:checkout:{order_id}")])
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)) + "\n\nروش پرداخت را انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:pay:"):
            _, _, order_id, method_id = data.split(":", 3); order = business.order(actor, int(order_id)); method = business.method(int(method_id), currency=str(order['currency']))
            context.user_data["biz_flow"] = {"kind": "receipt", "order_id": int(order_id), "method_id": int(method_id)}
            await update.callback_query.edit_message_text(
                f"پرداخت به: {method['destination']}\n"
                f"{method.get('instructions') or ''}\n"
                "کد پیگیری یا عکس رسید را ارسال کنید.",
                reply_markup=_home_inline_markup(settings),
            ); return
        if data == "shop:renewmenu":
            if not bool(settings.get("enable_renew", True)):
                await update.callback_query.edit_message_text(
                    "🚫 تمدید اشتراک در حال حاضر غیرفعال است.",
                    reply_markup=_home_inline_markup(settings),
                )
                return
            items, blocked = _renewable_subscriptions(business, actor)
            rows = [
                [InlineKeyboardButton(
                    f"♾ #{item['id']} · {item['plan_name']}",
                    callback_data=f"shop:renew:{item['id']}",
                )]
                for item in items
            ]
            body = "♾ تمدید اشتراک\nاشتراک موردنظر را انتخاب کنید:"
            if not rows:
                rows = [[InlineKeyboardButton(
                    "اشتراک قابل تمدیدی وجود ندارد",
                    callback_data="noop",
                )]]
                if blocked:
                    body = business.renewal_not_allowed_text()
            rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")])
            await update.callback_query.edit_message_text(
                body,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data == "shop:connect":
            items = [
                item
                for item in business.list_subscriptions(actor)
                if item.get("status") == "active"
                and item.get("external_ref")
                and item.get("server_id")
            ]
            rows: list[list[InlineKeyboardButton]] = []
            for item in items:
                subscription_id = int(item["id"])
                title = str(item.get("plan_name") or f"اشتراک #{subscription_id}")
                if _subscription_config_menu_rows(
                    business, actor, subscription_id, settings
                ):
                    rows.append([InlineKeyboardButton(
                        f"📝 {title}",
                        callback_data=f"shop:configmenu:{subscription_id}",
                    )])
            if not rows:
                rows.append([InlineKeyboardButton(
                    "اشتراک فعالی برای اتصال وجود ندارد",
                    callback_data="noop",
                )])
            rows.append([InlineKeyboardButton(
                "📦 اشتراک‌های من",
                callback_data="shop:subs",
            )])
            rows.append([InlineKeyboardButton(
                "🔙بازگشت", callback_data="runtime:home"
            )])
            await update.callback_query.edit_message_text(
                "🔗 اتصال اشتراک\n"
                "اشتراک موردنظر را انتخاب کنید تا روش‌های فعال اتصال همان سرویس نمایش داده شود:",
                reply_markup=InlineKeyboardMarkup(rows),
                disable_web_page_preview=True,
            )
            return
        if data == "shop:subs" or data.startswith("shop:subs:"):
            view = (
                data.split(":", 2)[2]
                if data.startswith("shop:subs:") and len(data.split(":", 2)) == 3
                else "all"
            )
            refreshed = business.refresh_customer_subscription_statuses(
                actor,
                limit=100,
            )
            if view == "active":
                items = [
                    item for item in refreshed
                    if str(item.get("status") or "") == "active"
                ]
                title = "🟢 سرویس‌های فعال"
            elif view == "expired":
                items = [
                    item for item in refreshed
                    if str(item.get("status") or "") == "expired"
                ]
                title = "🔴 سرویس‌های منقضی"
            else:
                items = refreshed
                view = "all"
                title = "📦 همه اشتراک‌های من"

            detail_lines = []
            for x in items[:20]:
                metrics = _subscription_metrics(x, settings)
                volume = (
                    f"{metrics['usage_gb']:.2f} گیگ مصرف"
                    if metrics["unlimited_volume"]
                    else (
                        f"{metrics['usage_gb']:.2f}/"
                        f"{metrics['limit_gb']:.2f} گیگ"
                    )
                )
                days = metrics["days_left"]
                time_left = (
                    "نامحدود"
                    if metrics["unlimited_time"]
                    else (
                        "منقضی"
                        if days is not None and days <= 0
                        else f"{days} روز" if days is not None else "نامشخص"
                    )
                )
                detail_lines.append(
                    f"• #{x['id']} · {x.get('plan_name') or '-'}\n"
                    f"  {_subscription_status_label(x)} · "
                    f"مصرف {volume} · {time_left}\n"
                    f"  آخرین اتصال: {_last_online_text(x.get('last_online'))}"
                )
            if len(items) > 20:
                detail_lines.append(
                    f"• و {len(items) - 20} سرویس دیگر..."
                )
            text = title + "\n\n" + (
                "\n".join(detail_lines) or "موردی وجود ندارد."
            )

            rows = [
                [InlineKeyboardButton(
                    f"📄 #{item['id']} · {item.get('plan_name') or 'اشتراک'}"[:60],
                    callback_data=f"shop:substatus:{item['id']}",
                )]
                for item in items[:20]
            ]
            rows.extend([
                [
                    InlineKeyboardButton(
                        "🟢 فعال",
                        callback_data="shop:subs:active",
                    ),
                    InlineKeyboardButton(
                        "🔴 منقضی",
                        callback_data="shop:subs:expired",
                    ),
                ],
                [InlineKeyboardButton(
                    "📦 همه اشتراک‌ها",
                    callback_data="shop:subs",
                )],
                [
                    InlineKeyboardButton(
                        "👤 پروفایل",
                        callback_data="shop:account",
                    ),
                    InlineKeyboardButton(
                        "📊 وضعیت",
                        callback_data="runtime:status",
                    ),
                ],
                [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:substatus:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            item = business.customer_subscription_status(
                actor,
                subscription_id=subscription_id,
                refresh=True,
            )
            rows = []
            if (
                str(item.get("status") or "") == "active"
                and _subscription_config_menu_rows(
                    business,
                    actor,
                    subscription_id,
                    settings,
                )
            ):
                rows.append([InlineKeyboardButton(
                    "📝 کانفیگ‌ها و لینک‌ها",
                    callback_data=f"shop:configmenu:{subscription_id}",
                )])
            if (
                item.get("external_ref")
                and item.get("server_id")
                and str(item.get("status") or "")
                in ("active", "disabled", "expired")
            ):
                rows.append([InlineKeyboardButton(
                    "♻️ تمدید اشتراک",
                    callback_data=f"shop:renew:{subscription_id}",
                )])
            rows.extend([
                [InlineKeyboardButton(
                    "🔙 همه اشتراک‌ها",
                    callback_data="shop:subs",
                )],
                [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                _subscription_detail_text(item, settings),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:configmenu:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            rows = _subscription_config_menu_rows(
                business, actor, subscription_id, settings
            )
            rows.append([InlineKeyboardButton(
                "↩️ اشتراک‌های من",
                callback_data="shop:subs",
            )])
            await update.callback_query.edit_message_text(
                f"📝 کانفیگ‌ها و لینک‌ها — اشتراک #{subscription_id}\n"
                "روش موردنظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
                disable_web_page_preview=True,
            )
            return

        if data.startswith((
            "shop:sublink:",
            "shop:autosub:",
            "shop:subb64:",
            "shop:smart:",
            "shop:smartb64:",
        )):
            action, raw_subscription_id = data.split(":", 2)[1:]
            subscription_id = int(raw_subscription_id)
            specs = {
                "sublink": (
                    "show_sub_link",
                    "🔗 لینک اشتراک",
                    business.subscription_link,
                    {},
                ),
                "autosub": (
                    "show_auto_sub_link",
                    "🤖 لینک اشتراک خودکار",
                    business.automatic_subscription_link,
                    {},
                ),
                "subb64": (
                    "show_sub_link_b64",
                    "🔐 لینک اشتراک b64",
                    business.subscription_link_b64,
                    {},
                ),
                "smart": (
                    "show_multi_server",
                    "🌐 لینک اشتراک هوشمند",
                    business.smart_subscription_link,
                    {"base64_output": False},
                ),
                "smartb64": (
                    "show_multi_server_b64",
                    "🌐 لینک اشتراک هوشمند b64",
                    business.smart_subscription_link,
                    {"base64_output": True},
                ),
            }
            setting_key, title, builder, kwargs = specs[action]
            if not bool(settings.get(setting_key, False)):
                await update.callback_query.edit_message_text(
                    f"❌ نمایش «{title}» خاموش است.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data=f"shop:configmenu:{subscription_id}",
                        )
                    ]]),
                )
                return
            try:
                link = builder(
                    actor,
                    subscription_id=subscription_id,
                    **kwargs,
                )
            except TenantBusinessError:
                await update.callback_query.edit_message_text(
                    f"❌ {title} برای این اشتراک در دسترس نیست.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data=f"shop:configmenu:{subscription_id}",
                        )
                    ]]),
                )
                return
            await update.callback_query.edit_message_text(
                f"{title}:\n{link}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔙 بازگشت",
                        callback_data=f"shop:configmenu:{subscription_id}",
                    )
                ]]),
                disable_web_page_preview=True,
            )
            return

        if data.startswith("shop:configs:"):
            if not bool(settings.get("show_direct_config", True)):
                subscription_id = int(data.rsplit(":", 1)[1])
                await update.callback_query.edit_message_text(
                    "❌ نمایش کانفیگ مستقیم خاموش است.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data=f"shop:configmenu:{subscription_id}",
                        )
                    ]]),
                )
                return
            subscription_id = int(data.rsplit(":", 1)[1])
            configs = business.subscription_configs(
                actor, subscription_id=subscription_id
            )
            ordered_servers = _ordered_indexed(
                configs,
                shuffle_enabled=bool(
                    settings.get("shuffle_server_layout", True)
                ),
            )
            server_buttons = [
                InlineKeyboardButton(
                    f"🖥 {item.get('server') or f'سرور {index + 1}'}",
                    callback_data=(
                        f"shop:configserver:{subscription_id}:{index}"
                    ),
                )
                for index, item in ordered_servers
            ]
            rows = _column_rows(
                server_buttons,
                int(settings.get("server_columns") or 1),
            )
            rows.extend([
                [
                    InlineKeyboardButton(
                        "🔗 اتصال اشتراک",
                        callback_data="shop:connect",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↩️ اشتراک‌های من",
                        callback_data="shop:subs",
                    )
                ],
            ])
            await update.callback_query.edit_message_text(
                "📄 کانفیگ‌های مستقیم\n"
                "سرور موردنظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:configserver:"):
            _, _, raw_subscription_id, raw_server_index = data.split(":", 3)
            subscription_id = int(raw_subscription_id)
            if not bool(settings.get("show_direct_config", True)):
                await update.callback_query.edit_message_text(
                    "❌ نمایش کانفیگ مستقیم خاموش است.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data=f"shop:configmenu:{subscription_id}",
                        )
                    ]]),
                )
                return
            server_index = int(raw_server_index)
            configs = business.subscription_configs(
                actor, subscription_id=subscription_id
            )
            if server_index < 0 or server_index >= len(configs):
                raise TenantBusinessError("config server is unavailable")
            selected = configs[server_index]
            config_items = _extract_config_items(str(selected.get("content") or ""))
            if not config_items:
                raise TenantBusinessError("configs are unavailable")

            if len(config_items) == 1:
                body = config_items[0]
                if len(body) > 3800:
                    body = body[:3750] + "\n…"
                await update.callback_query.edit_message_text(
                    f"🖥 {selected.get('server') or 'سرور'}\n\n{body}",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🔙 بازگشت به سرورها",
                                callback_data=f"shop:configs:{subscription_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🏠 منو",
                                callback_data="runtime:home",
                            )
                        ],
                    ]),
                    disable_web_page_preview=True,
                )
                return

            ordered_configs = _ordered_indexed(
                config_items,
                shuffle_enabled=bool(settings.get("shuffle_configs", True)),
            )
            config_buttons = [
                InlineKeyboardButton(
                    f"📄 کانفیگ {config_index + 1}",
                    callback_data=(
                        f"shop:configitem:{subscription_id}:"
                        f"{server_index}:{config_index}"
                    ),
                )
                for config_index, _content in ordered_configs
            ]
            if (
                bool(settings.get("shuffle_config_layout", True))
                and len(config_buttons) > 1
            ):
                random.shuffle(config_buttons)
            rows = _column_rows(config_buttons, 2)
            rows.extend([
                [
                    InlineKeyboardButton(
                        "🔙 بازگشت به سرورها",
                        callback_data=f"shop:configs:{subscription_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 منو",
                        callback_data="runtime:home",
                    )
                ],
            ])
            await update.callback_query.edit_message_text(
                f"🖥 {selected.get('server') or 'سرور'}\n"
                "کانفیگ موردنظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:configitem:"):
            parts = data.split(":")
            if len(parts) != 5:
                raise TenantBusinessError("invalid config item")
            subscription_id = int(parts[2])
            if not bool(settings.get("show_direct_config", True)):
                await update.callback_query.edit_message_text(
                    "❌ نمایش کانفیگ مستقیم خاموش است.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data=f"shop:configmenu:{subscription_id}",
                        )
                    ]]),
                )
                return
            server_index = int(parts[3])
            config_index = int(parts[4])
            configs = business.subscription_configs(
                actor, subscription_id=subscription_id
            )
            if server_index < 0 or server_index >= len(configs):
                raise TenantBusinessError("config server is unavailable")
            selected = configs[server_index]
            config_items = _extract_config_items(str(selected.get("content") or ""))
            if config_index < 0 or config_index >= len(config_items):
                raise TenantBusinessError("config is unavailable")
            body = config_items[config_index]
            if len(body) > 3800:
                body = body[:3750] + "\n…"
            await update.callback_query.edit_message_text(
                f"🖥 {selected.get('server') or 'سرور'}\n"
                f"📄 کانفیگ {config_index + 1}\n\n{body}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔙 بازگشت به کانفیگ‌ها",
                            callback_data=(
                                f"shop:configserver:{subscription_id}:"
                                f"{server_index}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 منو",
                            callback_data="runtime:home",
                        )
                    ],
                ]),
                disable_web_page_preview=True,
            )
            return

        if data.startswith("shop:renew:"):
            if not bool(settings.get("enable_renew", True)):
                await update.callback_query.edit_message_text(
                    "🚫 تمدید اشتراک در حال حاضر غیرفعال است.",
                    reply_markup=_home_inline_markup(settings),
                )
                return
            subscription_id = int(data.rsplit(":", 1)[1])
            owned = next((x for x in business.list_subscriptions(actor) if int(x["id"]) == subscription_id), None)
            if owned is None or owned["status"] not in ("active", "disabled", "expired"):
                raise TenantBusinessError("subscription cannot be renewed")
            eligibility = business.renewal_eligibility(
                actor,
                subscription_id=subscription_id,
            )
            if not bool(eligibility.get("allowed")):
                await update.callback_query.edit_message_text(
                    business.renewal_not_allowed_text(),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "↩️ اشتراک‌های من",
                            callback_data="shop:subs",
                        )
                    ]]),
                )
                return
            plans = business.list_plans()
            plan_buttons = [
                InlineKeyboardButton(
                    f"{p['name']} · {p['traffic_gb']}GB · "
                    f"{p['duration_days']} روز · {p['price']:,} {p['currency']}",
                    callback_data=f"shop:renewplan:{subscription_id}:{p['id']}",
                )
                for p in plans
            ]
            rows = _column_rows(
                plan_buttons,
                int(settings.get("plan_columns") or 1),
            )
            if not rows:
                rows = [[
                    InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")
                ]]
            rows.append([
                InlineKeyboardButton(
                    "↩️ اشتراک‌های من", callback_data="shop:subs"
                )
            ])
            await update.callback_query.edit_message_text(
                f"♻️ پلن تمدید اشتراک #{subscription_id} را انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:renewplan:"):
            if not bool(settings.get("enable_renew", True)):
                raise TenantBusinessError("renewal is disabled")
            _, _, subscription_id, plan_id = data.split(":", 3)
            order = business.create_renewal_order(
                actor,
                subscription_id=int(subscription_id),
                plan_id=int(plan_id),
            )
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=_checkout_markup(
                    int(order["id"]),
                    settings,
                    back_callback="shop:subs",
                    back_label="↩️ اشتراک‌های من",
                ),
            ); return
        if data == "shop:tickets":
            items = business.list_tickets(actor)
            text = "🎫 تیکت‌های من\n" + (
                "\n".join(
                    f"• #{x['id']} · {x['subject']} · {x['status']}"
                    f"{' · پاسخ داده شد' if x.get('admin_reply') else ''}"
                    for x in items
                )
                or "تیکتی ندارید."
            )
            rows = [
                [InlineKeyboardButton(
                    f"🎫 #{x['id']} · {x['subject']}"[:60],
                    callback_data=f"shop:ticket:{x['id']}",
                )]
                for x in items[:15]
            ]
            rows.extend([
                [InlineKeyboardButton("➕ تیکت جدید", callback_data="shop:newticket")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            ); return
        if data == "shop:newticket":
            context.user_data["biz_flow"] = {"kind": "ticket"}
            await update.callback_query.edit_message_text(
                "موضوع | متن تیکت را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ تیکت‌های من", callback_data="shop:tickets")]
                ]),
            ); return
        if data.startswith("shop:ticket:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            ticket = next(
                (
                    x for x in business.list_tickets(actor)
                    if int(x["id"]) == ticket_id
                ),
                None,
            )
            if ticket is None:
                raise TenantBusinessError("ticket not found")
            await update.callback_query.edit_message_text(
                f"🎫 تیکت #{ticket_id}\n"
                f"وضعیت: {ticket['status']}\n"
                f"موضوع: {ticket['subject']}\n\n"
                f"پیام شما:\n{ticket['body']}\n\n"
                f"پاسخ پشتیبانی:\n{ticket.get('admin_reply') or 'هنوز پاسخی ثبت نشده است.'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ تیکت‌های من", callback_data="shop:tickets")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:guide":
            guide = str(settings.get("guide_text") or "").strip()
            rows = [
                [
                    InlineKeyboardButton("📱 اندروید", callback_data="shop:guide:android"),
                    InlineKeyboardButton("📱 IOS", callback_data="shop:guide:ios"),
                ],
                [
                    InlineKeyboardButton("🖥️ ویندوز", callback_data="shop:guide:windows"),
                    InlineKeyboardButton("💻 مک", callback_data="shop:guide:mac"),
                ],
                [InlineKeyboardButton("🖥️ لینوکس", callback_data="shop:guide:linux")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ]
            await update.callback_query.edit_message_text(
                "💡 راهنما\n" + (guide or "انتخاب سیستم عامل ⬇️"),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:guide:"):
            platform = data.rsplit(":", 1)[1]
            labels = {
                "android": "📱 راهنمای اندروید",
                "ios": "📱 راهنمای IOS",
                "windows": "🖥️ راهنمای ویندوز",
                "mac": "💻 راهنمای مک",
                "linux": "🖥️ راهنمای لینوکس",
            }
            if platform not in labels:
                raise TenantBusinessError("invalid guide platform")
            body = str(settings.get(f"guide_{platform}_text") or "").strip()
            await update.callback_query.edit_message_text(
                labels[platform] + "\n\n" + (body or "هنوز متنی تنظیم نشده است."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙بازگشت", callback_data="shop:guide")],
                    [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
                ]),
                disable_web_page_preview=True,
            )
            return
        if data == "shop:faq":
            faq = str(settings.get("faq_text") or "").strip()
            await update.callback_query.edit_message_text(
                "📕 سوالات متداول\n" + (faq or "متنی تنظیم نشده است."),
                reply_markup=_home_inline_markup(settings),
            )
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.callback_query.answer("درخواست قابل انجام نیست.", show_alert=True)
        return
    await update.callback_query.answer("این دکمه معتبر نیست.", show_alert=True)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, _, business = _services(context)
    _set_button_settings(business.runtime_userbot_settings())
    if spec.role != "user":
        raise RuntimeError("UserBot text handler registered for non-user role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if not await _force_join_allowed(update, context, business):
        return
    flow = context.user_data.get("biz_flow")
    text = str(update.effective_message.text or "").strip() if update.effective_message else ""
    try:
        main_labels = {
            BTN_STATUS,
            BTN_RENEW,
            BTN_BUY,
            BTN_CONNECT,
            BTN_TRIAL,
            BTN_WALLET,
            BTN_SUPPORT,
            BTN_GUIDE,
            BTN_FAQ,
            BTN_REFERRAL,
            BTN_GIFT,
        }
        if text in main_labels:
            context.user_data.pop("biz_flow", None)
            handled = await _handle_main_reply_action(
                update,
                context,
                spec=spec,
                business=business,
                actor=actor,
                text=text,
            )
            if handled:
                return
        if isinstance(flow, dict):
            fields = [part.strip() for part in text.split("|")]
            kind = flow.get("kind")
            if kind == "wallet_topup_create":
                if len(fields) != 2:
                    raise ValueError("invalid wallet topup")
                topup = business.create_wallet_topup(
                    actor,
                    amount=int(fields[0]),
                    currency=fields[1],
                )
                methods = business.list_methods(currency=str(topup["currency"]))
                context.user_data.pop("biz_flow", None)
                rows = [[InlineKeyboardButton(
                    f"{m['title']} ({m['kind']})",
                    callback_data=f"shop:wallettopupmethod:{topup['id']}:{m['id']}",
                )] for m in methods]
                if not rows:
                    rows = [[InlineKeyboardButton("روش پرداخت موجود نیست", callback_data="noop")]]
                rows.append([InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")])
                await update.effective_message.reply_text(
                    f"شارژ #{topup['id']} · {int(topup['amount']):,} {topup['currency']}\n"
                    "روش پرداخت را انتخاب کنید.",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
            if kind == "wallet_receipt":
                business.submit_wallet_topup_receipt(
                    actor,
                    topup_id=int(flow["topup_id"]),
                    method_id=int(flow["method_id"]),
                    reference=text,
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ رسید شارژ کیف پول برای بررسی ارسال شد.",
                    reply_markup=_main_keyboard(spec, business),
                )
                return
            if kind == "coupon_apply":
                order_id = int(flow["order_id"])
                order = business.apply_coupon(
                    actor,
                    order_id=order_id,
                    code=text,
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ کد تخفیف اعمال شد.\n\n"
                    + _checkout_text(order, business.wallet_summary(actor)),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "💰 پرداخت کیف پول",
                                callback_data=f"shop:walletpay:{order_id}",
                            ),
                            InlineKeyboardButton(
                                "💳 روش‌های پرداخت",
                                callback_data=f"shop:paymethods:{order_id}",
                            ),
                        ],
                        [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                    ]),
                )
                return
            if kind == "gift_redeem":
                gift = business.redeem_gift_voucher(actor, code=text)
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ هدیه دریافت شد.\n"
                    f"🎁 مبلغ: {int(gift['amount']):,} {gift['currency']}\n"
                    f"💰 موجودی جدید: {int(gift['resulting_balance']):,} {gift['currency']}",
                    reply_markup=_main_keyboard(spec, business),
                )
                return
            if kind == "receipt":
                business.submit_receipt(
                    actor,
                    order_id=int(flow["order_id"]),
                    method_id=int(flow["method_id"]),
                    reference=text,
                )
            elif kind == "ticket" and len(fields) == 2:
                business.create_ticket(actor, subject=fields[0], body=fields[1])
            else:
                raise ValueError("invalid input")
            context.user_data.pop("biz_flow", None)
            await update.effective_message.reply_text("✅ ذخیره شد.", reply_markup=_main_keyboard(spec, business))
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ قالب یا وضعیت معتبر نیست.", reply_markup=_main_keyboard(spec, business))
        return
    if update.effective_message:
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_main_keyboard(spec, business))


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a photo only while this user has an owned receipt flow."""
    if update.effective_message is None or not update.effective_message.photo:
        return
    spec, _, _, business = _services(context)
    _set_button_settings(business.runtime_userbot_settings())
    flow = context.user_data.get("biz_flow")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if not await _force_join_allowed(update, context, business):
        return
    if spec.role != "user" or not isinstance(flow, dict) or flow.get("kind") not in ("receipt", "wallet_receipt"):
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_main_keyboard(spec, business)); return
    try:
        if flow.get("kind") == "wallet_receipt":
            business.submit_wallet_topup_receipt(
                actor,
                topup_id=int(flow["topup_id"]),
                method_id=int(flow["method_id"]),
                reference=str(update.effective_message.caption or "").strip() or None,
                telegram_file_id=str(update.effective_message.photo[-1].file_id),
            )
            success_text = "✅ تصویر رسید شارژ کیف پول برای بررسی ارسال شد."
        else:
            business.submit_receipt(actor, order_id=int(flow['order_id']), method_id=int(flow['method_id']), reference=str(update.effective_message.caption or "").strip() or None, telegram_file_id=str(update.effective_message.photo[-1].file_id))
            success_text = "✅ تصویر رسید برای بررسی ارسال شد."
        context.user_data.pop("biz_flow", None)
        await update.effective_message.reply_text(success_text, reply_markup=_main_keyboard(spec, business))
    except (ValueError, TenantBusinessError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ ثبت تصویر رسید انجام نشد.", reply_markup=_main_keyboard(spec, business))



def register_user_handlers(application: Application) -> None:
    spec = application.bot_data.get("runtime_spec")
    if not isinstance(spec, RuntimeBotSpec) or spec.role != "user":
        raise RuntimeError("UserBot handlers require a user runtime spec")
    application.add_handler(TypeHandler(Update, runtime_access_gate), group=-1)
    application.add_handler(CommandHandler("start", show_home), group=0)
    application.add_handler(CommandHandler("menu", show_home), group=0)
    application.add_handler(CommandHandler("status", show_status), group=0)
    application.add_handler(CallbackQueryHandler(on_callback), group=0)
    application.add_handler(MessageHandler(filters.PHOTO, receipt_photo), group=0)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text), group=0
    )
    application.add_error_handler(runtime_error)

