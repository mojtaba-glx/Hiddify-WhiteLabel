"""Pure text and keyboard rendering for the MasterBot UI."""

from __future__ import annotations

from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from MasterBot.service import Page
from Shared.timeutils import format_tehran, parse_utc


TENANT_STATUS_FA = {
    "active": "🟢 فعال",
    "suspended": "🟠 معلق",
    "disabled": "🔴 غیرفعال",
}

RUNTIME_STATUS_FA = {
    "provisioning": "🟡 در حال راه‌اندازی",
    "ready": "🟢 آماده",
    "disabled": "🔴 غیرفعال",
    "error": "🔴 خطا",
    "missing": "⚪ ثبت‌نشده",
}

PLAN_STATUS_FA = {
    "active": "🟢 فعال",
    "archived": "🗄 آرشیوشده",
    "disabled": "🔴 غیرفعال",
}

LICENSE_STATUS_FA = {
    "pending": "⏳ در انتظار",
    "active": "🟢 فعال",
    "grace": "🟡 مهلت تمدید",
    "suspended": "🟠 معلق",
    "expired": "🔴 منقضی",
    "cancelled": "❌ لغوشده",
}

PAYMENT_KIND_FA = {
    "card": "کارت‌به‌کارت",
    "crypto": "ارز دیجیتال",
}


MAIN_MENU = (
    ("👤 کاربران فروشگاه", "menu:customers"),
    ("🤖 ربات‌های مشتریان", "menu:tenants"),
    ("🔐 لایسنس‌ها", "menu:licenses"),
    ("📦 پلن‌ها", "menu:plans"),
    ("🧾 سفارش‌ها", "menu:orders"),
    ("📊 آمار و فروش", "menu:stats"),
    ("💳 پرداخت‌ها", "menu:payments"),
    ("⚠️ هشدارها", "menu:warnings"),
    ("🧾 تاریخچه", "menu:audit"),
    ("⚙️ تنظیمات", "menu:settings"),
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    by_callback = {callback: label for label, callback in MAIN_MENU}
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(by_callback["menu:customers"], callback_data="menu:customers"),
            InlineKeyboardButton(by_callback["menu:tenants"], callback_data="menu:tenants"),
        ],
        [
            InlineKeyboardButton(by_callback["menu:licenses"], callback_data="menu:licenses"),
            InlineKeyboardButton(by_callback["menu:plans"], callback_data="menu:plans"),
        ],
        [
            InlineKeyboardButton(by_callback["menu:orders"], callback_data="menu:orders"),
            InlineKeyboardButton(by_callback["menu:payments"], callback_data="menu:payments"),
        ],
        [
            InlineKeyboardButton(by_callback["menu:stats"], callback_data="menu:stats"),
            InlineKeyboardButton(by_callback["menu:warnings"], callback_data="menu:warnings"),
        ],
        [InlineKeyboardButton(by_callback["menu:audit"], callback_data="menu:audit")],
        [InlineKeyboardButton(by_callback["menu:settings"], callback_data="menu:settings")],
    ])


def back_keyboard(callback: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ بازگشت", callback_data=callback)]])


def _pager(prefix: str, page: Page) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if page.has_previous:
        buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"{prefix}:{page.page - 1}")
        )
    buttons.append(InlineKeyboardButton(f"{page.page + 1}", callback_data="noop"))
    if page.has_next:
        buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"{prefix}:{page.page + 1}")
        )
    return buttons



def platform_customers_view(
    page: Page, *, query: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    text = "👤 کاربران فروشگاه"
    if query:
        text += f"\n🔎 نتیجه جست‌وجو: {query[:40]}"
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        icon = "🟢" if item["status"] == "active" else "🔴"
        username = f"@{item['username']}" if item.get("username") else str(item["telegram_user_id"])
        rows.append([
            InlineKeyboardButton(
                f"{icon} {item['display_name']} · {username}",
                callback_data=f"customeradmin:view:{int(item['id'])}",
            )
        ])
    if not page.items:
        text += "\n\nکاربری پیدا نشد."
    rows.append(_pager("customeradmin:page", page))
    rows.append([InlineKeyboardButton("🔎 جست‌وجو", callback_data="customeradmin:search")])
    rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
    return text, InlineKeyboardMarkup(rows)


def platform_customer_detail(customer: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    username = f"@{customer['username']}" if customer.get("username") else "—"
    wallets = customer.get("wallets") or []
    wallet_text = "ندارد"
    if wallets:
        wallet_text = " | ".join(
            f"{int(item['balance']):,} {item['currency']}" for item in wallets
        )
    lines = [
        f"👤 کاربر فروشگاه #{int(customer['id'])}",
        "",
        f"نام: {customer['display_name']}",
        f"یوزرنیم: {username}",
        f"شناسه تلگرام: {int(customer['telegram_user_id'])}",
        f"وضعیت: {'فعال' if customer['status'] == 'active' else 'مسدود'}",
        f"کیف پول: {wallet_text}",
        f"سفارش‌ها: {int(customer.get('order_count') or 0)}",
        f"ربات‌های ساخته‌شده: {int(customer.get('tenant_count') or 0)}",
        f"رسید در انتظار: {int(customer.get('pending_receipts') or 0)}",
    ]
    recent = customer.get("recent_orders") or []
    if recent:
        lines.extend(["", "🧾 آخرین سفارش‌ها:"])
        for order in recent:
            lines.append(
                f"• {order['public_id']} · "
                f"{_ORDER_KIND_FA.get(str(order['kind']), str(order['kind']))} · "
                f"{int(order['amount']):,} {order['currency']} · "
                f"{_ORDER_STATUS_FA.get(str(order['status']), str(order['status']))}"
            )
    next_status = "blocked" if customer["status"] == "active" else "active"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش کیف پول",
                callback_data=f"customeradmin:wallet:add:{int(customer['id'])}",
            ),
            InlineKeyboardButton(
                "➖ کاهش کیف پول",
                callback_data=f"customeradmin:wallet:subtract:{int(customer['id'])}",
            ),
        ],
        [InlineKeyboardButton(
            "⛔ مسدودکردن" if next_status == "blocked" else "✅ فعال‌کردن",
            callback_data=f"customeradmin:status:{int(customer['id'])}:{next_status}",
        )],
        [InlineKeyboardButton("↩️ کاربران فروشگاه", callback_data="customeradmin:page:0")],
    ])
    return "\n".join(lines), keyboard


def tenants_view(page: Page, *, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
    title = "👥 مدیریت مشتریان"
    if query:
        title += f"\n🔎 نتیجه جست‌وجو: {query[:40]}"
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        icon = "🟢" if item["status"] == "active" else "🔴"
        rows.append(
            [InlineKeyboardButton(
                f"{icon} {item['name']}",
                callback_data=f"tenant:view:{int(item['id'])}",
            )]
        )
    if not rows:
        title += "\n\nمشتری‌ای پیدا نشد."
    rows.append(_pager("tenant:page", page))
    rows.append([
        InlineKeyboardButton("🚀 راه‌اندازی کامل", callback_data="tenant:provision"),
    ])
    rows.append([
        InlineKeyboardButton("➕ مشتری جدید", callback_data="tenant:new"),
        InlineKeyboardButton("🔎 جست‌وجو", callback_data="tenant:search"),
    ])
    rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
    return title, InlineKeyboardMarkup(rows)


def tenant_detail(row: dict[str, Any], readiness: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    runtime_status = str(readiness.get("runtime_status") or "missing")
    text = (
        f"🤖 ربات مشتری #{int(row['id'])}\n\n"
        f"نام: {row['name']}\n"
        f"شناسه داخلی: {row['slug']}\n"
        f"مالک تلگرام: {int(row['owner_telegram_id'])}\n"
        f"وضعیت: {TENANT_STATUS_FA.get(str(row['status']), str(row['status']))}\n"
        f"ربات مدیریت: {'✅ ثبت‌شده' if readiness['admin'] else '❌ ثبت‌نشده'}\n"
        f"ربات کاربران: {'✅ ثبت‌شده' if readiness['user'] else '❌ ثبت‌نشده'}\n"
        f"اجرای سیستم: {RUNTIME_STATUS_FA.get(runtime_status, runtime_status)}\n"
        f"آماده اجرا: {'✅ بله' if readiness.get('ready') else '❌ خیر'}"
    )
    tenant_id = int(row["id"])
    next_status = "suspended" if row["status"] == "active" else "active"
    status_label = "⛔ تعلیق" if next_status == "suspended" else "✅ فعال‌سازی"
    rows = [
        [
            InlineKeyboardButton("🤖 ربات مدیریت", callback_data=f"tenant:bot:{tenant_id}:admin"),
            InlineKeyboardButton("🛍 ربات کاربران", callback_data=f"tenant:bot:{tenant_id}:user"),
        ],
    ]
    secret_buttons: list[InlineKeyboardButton] = []
    if readiness["admin"]:
        secret_buttons.append(
            InlineKeyboardButton("🔑 کلید ربات مدیریت", callback_data=f"tenant:webhook:{tenant_id}:admin")
        )
    if readiness["user"]:
        secret_buttons.append(
            InlineKeyboardButton("🔑 کلید ربات کاربران", callback_data=f"tenant:webhook:{tenant_id}:user")
        )
    if secret_buttons:
        rows.append(secret_buttons)
    rows.extend([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"tenant:edit:{tenant_id}")],
        [InlineKeyboardButton(status_label, callback_data=f"tenant:status:{tenant_id}:{next_status}")],
        [InlineKeyboardButton("↩️ مشتریان", callback_data="tenant:page:0")],
    ])
    return text, InlineKeyboardMarkup(rows)


def plans_view(page: Page, *, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        rows.append([InlineKeyboardButton(
            f"📦 {item['name']} · {int(item['duration_days'])} روز · "
            f"{PLAN_STATUS_FA.get(str(item['status']), str(item['status']))}",
            callback_data=f"plan:view:{int(item['id'])}",
        )])
    text = "📦 مدیریت پلن‌ها"
    if query:
        text += f"\n🔎 نتیجه جست‌وجو: {query[:40]}"
    text += "" if rows else "\n\nپلنی ثبت نشده است."
    rows.append(_pager("plan:page", page))
    rows.append([
        InlineKeyboardButton("➕ پلن جدید", callback_data="plan:new"),
        InlineKeyboardButton("🔎 جست‌وجو", callback_data="plan:search"),
    ])
    rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
    return text, InlineKeyboardMarkup(rows)


def plan_detail(
    row: dict[str, Any], *, trial_plan_id: int | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    plan_id = int(row["id"])
    is_public = bool(int(row.get("is_public", 1)))
    is_trial = trial_plan_id is not None and int(trial_plan_id) == plan_id
    text = (
        f"📦 پلن #{plan_id}\n\n"
        f"نام: {row['name']}\n"
        f"مدت اشتراک: {int(row['duration_days'])} روز\n"
        f"قیمت: {int(row['price']):,} {row.get('currency', 'USD')}\n"
        f"حداکثر سرور: {int(row['max_servers'])}\n"
        f"حداکثر کاربر: {int(row['max_users'])}\n"
        f"نمایش در فروشگاه: {'✅' if is_public else '❌'}\n"
        f"مدت تست: {int(row.get('trial_days', 0))} روز\n"
        f"پلن تست اصلی: {'✅' if is_trial else '—'}\n"
        f"وضعیت: {PLAN_STATUS_FA.get(str(row['status']), str(row['status']))}"
    )
    target = "archived" if row["status"] == "active" else "active"
    public_target = 0 if is_public else 1
    rows = [
        [
            InlineKeyboardButton("✏️ مشخصات پلن", callback_data=f"plan:edit:{plan_id}"),
            InlineKeyboardButton("💰 ارز و تست", callback_data=f"plan:commerce:{plan_id}"),
        ],
        [InlineKeyboardButton(
            "🙈 مخفی از فروشگاه" if is_public else "👁 نمایش در فروشگاه",
            callback_data=f"plan:public:{plan_id}:{public_target}",
        )],
    ]
    if (
        row["status"] == "active"
        and is_public
        and int(row.get("trial_days", 0)) > 0
    ):
        rows.append([InlineKeyboardButton(
            "✅ پلن تست اصلی" if is_trial else "🎁 انتخاب به‌عنوان پلن تست",
            callback_data=f"plan:trial:{plan_id}",
        )])
    rows.extend([
        [InlineKeyboardButton(
            "🗄 آرشیو" if target == "archived" else "✅ فعال‌سازی",
            callback_data=f"plan:status:{plan_id}:{target}",
        )],
        [InlineKeyboardButton("↩️ پلن‌ها", callback_data="plan:page:0")],
    ])
    return text, InlineKeyboardMarkup(rows)


def trial_plan_picker(
    plans: Iterable[dict[str, Any]], *, selected_plan_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        selected = selected_plan_id is not None and int(selected_plan_id) == int(plan["id"])
        rows.append([InlineKeyboardButton(
            f"{'✅' if selected else '🎁'} {plan['name']} · {int(plan['trial_days'])} روز",
            callback_data=f"settings:trialplan:set:{int(plan['id'])}",
        )])
    text = "🎁 پلن لایسنس تست\n\nپلنی که برای تست رایگان مشتریان استفاده می‌شود را انتخاب کنید."
    if not rows:
        text += "\n\nپلن واجد شرایطی وجود ندارد؛ ابتدا برای یک پلن فعال و عمومی، مدت تست بیشتر از صفر تنظیم کنید."
    rows.append([InlineKeyboardButton("♻️ انتخاب خودکار", callback_data="settings:trialplan:auto")])
    rows.append([InlineKeyboardButton("↩️ تنظیمات", callback_data="menu:settings")])
    return text, InlineKeyboardMarkup(rows)


def licenses_view(page: Page, *, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        rows.append([InlineKeyboardButton(
            f"🔐 #{int(item['id'])} · مشتری {int(item['tenant_id'])} · "
            f"{LICENSE_STATUS_FA.get(str(item['status']), str(item['status']))}",
            callback_data=f"license:view:{int(item['id'])}",
        )])
    text = "🔐 مدیریت لایسنس‌ها"
    if query:
        text += f"\n🔎 نتیجه جست‌وجو: {query[:40]}"
    text += "" if rows else "\n\nلایسنسی ثبت نشده است."
    rows.append(_pager("license:page", page))
    rows.append([
        InlineKeyboardButton("➕ لایسنس جدید", callback_data="license:new"),
        InlineKeyboardButton("🔎 جست‌وجو", callback_data="license:search"),
    ])
    rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
    return text, InlineKeyboardMarkup(rows)


def license_detail(row: dict[str, Any], *, timezone_name: str) -> tuple[str, InlineKeyboardMarkup]:
    expires = format_tehran(parse_utc(str(row["expires_at"])), timezone_name)
    text = (
        f"🔐 لایسنس #{int(row['id'])}\n\n"
        f"مشتری: {row['tenant_name']} (#{int(row['tenant_id'])})\n"
        f"پلن: {row['plan_name']}\n"
        f"وضعیت: {LICENSE_STATUS_FA.get(str(row['status']), str(row['status']))}\n"
        f"انقضا: {expires}"
    )
    license_id = int(row["id"])
    rows = [
        [InlineKeyboardButton("♻️ تمدید", callback_data=f"license:renew:{license_id}")]
    ]
    if row["status"] == "suspended":
        rows.append([InlineKeyboardButton("✅ فعال‌سازی", callback_data=f"license:reactivate:{license_id}")])
    elif row["status"] not in ("cancelled", "expired"):
        rows.append([InlineKeyboardButton("⛔ تعلیق", callback_data=f"license:suspend:{license_id}")])
    rows.append([InlineKeyboardButton("↩️ لایسنس‌ها", callback_data="license:page:0")])
    return text, InlineKeyboardMarkup(rows)




_ORDER_STATUS_FA = {
    "pending_payment": "در انتظار پرداخت",
    "payment_review": "در انتظار بررسی",
    "paid": "پرداخت‌شده",
    "fulfilled": "تکمیل‌شده",
    "rejected": "ردشده",
    "cancelled": "لغوشده",
}

_ORDER_KIND_FA = {
    "purchase": "خرید ربات",
    "renewal": "تمدید",
    "wallet_topup": "شارژ کیف پول",
    "trial": "لایسنس تست",
}


def _money_lines(values: dict[str, int], *, empty: str = "۰") -> str:
    if not values:
        return empty
    return " | ".join(f"{int(amount):,} {currency}" for currency, amount in sorted(values.items()))


def financial_dashboard_view(
    system: dict[str, Any], finance: dict[str, Any]
) -> tuple[str, InlineKeyboardMarkup]:
    statuses = finance.get("orders_by_status") or {}
    kinds = finance.get("orders_by_kind") or {}
    lines = [
        "📊 آمار و فروش",
        "",
        "💰 فروش سرویس",
        f"• کل: {_money_lines(finance.get('service_sales') or {})}",
        f"• ۲۴ ساعت اخیر: {_money_lines(finance.get('service_sales_24h') or {})}",
        "",
        "💳 جریان پرداخت",
        f"• رسیدهای تأییدشده: {_money_lines(finance.get('approved_receipts') or {})}",
        f"• شارژ کیف پول: {_money_lines(finance.get('wallet_topups') or {})}",
        f"• موجودی فعلی کیف پول‌ها: {_money_lines(finance.get('wallet_balances') or {})}",
        f"• رسید در انتظار: {int(finance.get('pending_receipts') or 0)}",
        "",
        "🧾 سفارش‌ها",
        f"• کل: {int(finance.get('orders_total') or 0)}",
        f"• خرید: {int(kinds.get('purchase') or 0)}",
        f"• تمدید: {int(kinds.get('renewal') or 0)}",
        f"• شارژ کیف پول: {int(kinds.get('wallet_topup') or 0)}",
        f"• تست: {int(kinds.get('trial') or 0)}",
        f"• در انتظار پرداخت: {int(statuses.get('pending_payment') or 0)}",
        f"• در انتظار بررسی: {int(statuses.get('payment_review') or 0)}",
        "",
        "👥 سامانه",
        f"• کاربران فروشگاه: {int(finance.get('customers_total') or 0)}"
        f" (فعال: {int(finance.get('customers_active') or 0)})",
        f"• ربات‌های مشتریان: {int(system.get('tenants_total') or 0)}"
        f" (فعال: {int(system.get('tenants_active') or 0)})",
        f"• ربات‌های تلگرام فعال: {int(system.get('bots_active') or 0)}",
        f"• لایسنس‌های فعال: {int(system.get('licenses_active') or 0)}",
        f"• لایسنس‌های معلق: {int(system.get('licenses_suspended') or 0)}",
        f"• هشدارهای باز: {int(system.get('warnings_open') or 0)}",
    ]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧾 سفارش‌ها", callback_data="menu:orders"),
            InlineKeyboardButton("💳 پرداخت‌ها", callback_data="menu:payments"),
        ],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ])
    return "\n".join(lines), keyboard


def orders_view(
    page: Page, *, status: str = "all", query: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    labels = {
        "all": "همه",
        "pending_payment": "در انتظار پرداخت",
        "payment_review": "بررسی رسید",
        "paid": "پرداخت‌شده",
        "fulfilled": "تکمیل‌شده",
        "rejected": "ردشده",
        "cancelled": "لغوشده",
    }
    text = f"🧾 سفارش‌ها · {labels.get(status, status)}"
    if query:
        text += f"\n🔎 {query[:40]}"
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        kind = _ORDER_KIND_FA.get(str(item["kind"]), str(item["kind"]))
        state = _ORDER_STATUS_FA.get(str(item["status"]), str(item["status"]))
        rows.append([InlineKeyboardButton(
            f"#{int(item['id'])} · {kind} · {int(item['amount']):,} {item['currency']} · {state}",
            callback_data=f"orderadmin:view:{int(item['id'])}",
        )])
    if not page.items:
        text += "\n\nسفارشی در این بخش وجود ندارد."
    rows.append(_pager(f"orderadmin:page:{status}", page))
    rows.extend([
        [
            InlineKeyboardButton("همه", callback_data="orderadmin:filter:all"),
            InlineKeyboardButton("⏳ پرداخت", callback_data="orderadmin:filter:pending_payment"),
        ],
        [
            InlineKeyboardButton("🧾 بررسی", callback_data="orderadmin:filter:payment_review"),
            InlineKeyboardButton("✅ تکمیل", callback_data="orderadmin:filter:fulfilled"),
        ],
        [
            InlineKeyboardButton("💵 پرداخت‌شده", callback_data="orderadmin:filter:paid"),
            InlineKeyboardButton("❌ ردشده", callback_data="orderadmin:filter:rejected"),
        ],
        [InlineKeyboardButton("🔎 جست‌وجو", callback_data="orderadmin:search")],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ])
    return text, InlineKeyboardMarkup(rows)


def order_detail_view(order: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    kind = _ORDER_KIND_FA.get(str(order["kind"]), str(order["kind"]))
    status = _ORDER_STATUS_FA.get(str(order["status"]), str(order["status"]))
    username = f"@{order['username']}" if order.get("username") else "—"
    lines = [
        f"🧾 سفارش #{int(order['id'])}",
        "",
        f"شناسه: {order['public_id']}",
        f"نوع: {kind}",
        f"وضعیت: {status}",
        f"مبلغ: {int(order['amount']):,} {order['currency']}",
        f"مشتری: {order['display_name']}",
        f"یوزرنیم: {username}",
        f"شناسه تلگرام: {int(order['telegram_user_id'])}",
    ]
    if order.get("plan_name"):
        lines.append(f"پلن: {order['plan_name']}")
    if order.get("tenant_name"):
        lines.append(f"ربات: {order['tenant_name']}")
    lines.append(f"ثبت: {str(order['created_at']).replace('T', ' ')[:19]}")
    if order.get("paid_at"):
        lines.append(f"پرداخت: {str(order['paid_at']).replace('T', ' ')[:19]}")
    lines.append(f"آخرین تغییر: {str(order['updated_at']).replace('T', ' ')[:19]}")

    receipts = order.get("receipts") or []
    wallet_txs = order.get("wallet_transactions") or []
    if wallet_txs:
        lines.extend(["", "👛 پرداخت از کیف پول:"])
        for tx in wallet_txs[:3]:
            lines.append(
                f"• {int(tx['amount']):,} · مانده {int(tx['resulting_balance']):,}"
            )
    if receipts:
        lines.extend(["", "💳 رسیدها:"])
        for receipt in receipts[:5]:
            lines.append(
                f"• #{int(receipt['id'])} · {receipt['method_title']} · "
                f"{_RECEIPT_STATUS_FA.get(str(receipt['status']), str(receipt['status']))}"
            )

    buttons: list[list[InlineKeyboardButton]] = []
    pending = next((r for r in receipts if r["status"] == "pending"), None)
    if pending is not None:
        buttons.append([InlineKeyboardButton(
            f"🧾 بررسی رسید #{int(pending['id'])}",
            callback_data=f"payment:receipt:{int(pending['id'])}",
        )])
    buttons.extend([
        [InlineKeyboardButton("👤 پروفایل مشتری", callback_data=f"customeradmin:view:{int(order['customer_id'])}")],
        [InlineKeyboardButton("↩️ سفارش‌ها", callback_data="menu:orders")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def platform_settings_view(settings: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    sales = "🟢 فعال" if settings.get("sales_enabled") else "🔴 غیرفعال"
    trial = "🟢 فعال" if settings.get("trial_enabled") else "🔴 غیرفعال"
    support = str(settings.get("support_contact") or "تنظیم نشده")
    trial_plan = str(settings.get("trial_plan_name") or "انتخاب خودکار")
    text = (
        "⚙️ تنظیمات فروشگاه\n\n"
        f"🏷 نام فروشگاه: {settings.get('store_name') or '-'}\n"
        f"☎️ پشتیبانی: {support}\n"
        f"🛒 فروش: {sales}\n"
        f"🎁 لایسنس تست: {trial}\n"
        f"📦 پلن تست: {trial_plan}\n\n"
        f"🔧 پیام توقف فروش:\n{settings.get('maintenance_message') or '-'}"
    )
    rows = [
        [
            InlineKeyboardButton("🏷 نام فروشگاه", callback_data="settings:edit:store_name"),
            InlineKeyboardButton("☎️ پشتیبانی", callback_data="settings:edit:support_contact"),
        ],
        [
            InlineKeyboardButton(
                "⛔ توقف فروش" if settings.get("sales_enabled") else "✅ فعال‌کردن فروش",
                callback_data="settings:toggle:sales",
            ),
            InlineKeyboardButton(
                "⛔ توقف تست" if settings.get("trial_enabled") else "✅ فعال‌کردن تست",
                callback_data="settings:toggle:trial",
            ),
        ],
        [InlineKeyboardButton("🎁 انتخاب پلن تست", callback_data="settings:trialplan")],
        [InlineKeyboardButton("📝 پیام توقف فروش", callback_data="settings:edit:maintenance_message")],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ]
    return text, InlineKeyboardMarkup(rows)



_RECEIPT_STATUS_FA = {
    "pending": "در انتظار بررسی",
    "approved": "تأییدشده",
    "rejected": "ردشده",
}


def payment_history_view(
    page: Page, *, status: str = "all", query: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    labels = {
        "all": "همه",
        "pending": "در انتظار",
        "approved": "تأییدشده",
        "rejected": "ردشده",
    }
    text = f"💳 تاریخچه پرداخت‌ها · {labels.get(status, status)}"
    if query:
        text += f"\n🔎 {query[:40]}"
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        state = _RECEIPT_STATUS_FA.get(str(item["status"]), str(item["status"]))
        rows.append([InlineKeyboardButton(
            f"#{int(item['id'])} · {int(item['amount']):,} {item['currency']} · {state}",
            callback_data=f"payment:history:view:{int(item['id'])}",
        )])
    if not page.items:
        text += "\n\nپرداختی در این بخش وجود ندارد."
    rows.append(_pager(f"payment:history:page:{status}", page))
    rows.extend([
        [
            InlineKeyboardButton("همه", callback_data="payment:history:filter:all"),
            InlineKeyboardButton("⏳ در انتظار", callback_data="payment:history:filter:pending"),
        ],
        [
            InlineKeyboardButton("✅ تأییدشده", callback_data="payment:history:filter:approved"),
            InlineKeyboardButton("❌ ردشده", callback_data="payment:history:filter:rejected"),
        ],
        [InlineKeyboardButton("🔎 جست‌وجو", callback_data="payment:history:search")],
        [InlineKeyboardButton("↩️ پرداخت‌ها", callback_data="menu:payments")],
    ])
    return text, InlineKeyboardMarkup(rows)


def payment_receipt_detail(receipt: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    state = _RECEIPT_STATUS_FA.get(str(receipt["status"]), str(receipt["status"]))
    kind = _ORDER_KIND_FA.get(str(receipt["kind"]), str(receipt["kind"]))
    username = f"@{receipt['username']}" if receipt.get("username") else "—"
    lines = [
        f"💳 پرداخت #{int(receipt['id'])}",
        "",
        f"وضعیت: {state}",
        f"سفارش: {receipt['public_id']}",
        f"نوع سفارش: {kind}",
        f"مبلغ: {int(receipt['amount']):,} {receipt['currency']}",
        f"روش: {receipt['method_title']}",
        f"مشتری: {receipt['display_name']}",
        f"یوزرنیم: {username}",
        f"شناسه تلگرام: {int(receipt['telegram_user_id'])}",
    ]
    if receipt.get("reference"):
        lines.append(f"پیگیری: {receipt['reference']}")
    lines.append(f"ثبت: {str(receipt['created_at']).replace('T', ' ')[:19]}")
    if receipt.get("reviewed_at"):
        lines.append(f"بررسی: {str(receipt['reviewed_at']).replace('T', ' ')[:19]}")
    if receipt.get("review_note"):
        lines.append(f"یادداشت بررسی: {receipt['review_note']}")

    rows: list[list[InlineKeyboardButton]] = []
    if receipt["status"] == "pending" and receipt["order_status"] == "payment_review":
        rows.append([
            InlineKeyboardButton(
                "✅ تأیید پرداخت",
                callback_data=f"payment:approve:{int(receipt['id'])}",
            ),
            InlineKeyboardButton(
                "❌ رد پرداخت",
                callback_data=f"payment:reject:{int(receipt['id'])}",
            ),
        ])
    rows.extend([
        [InlineKeyboardButton(
            "🧾 مشاهده سفارش",
            callback_data=f"orderadmin:view:{int(receipt['order_id'])}",
        )],
        [InlineKeyboardButton(
            "👤 پروفایل مشتری",
            callback_data=f"customeradmin:view:{int(receipt['customer_id'])}",
        )],
        [InlineKeyboardButton("↩️ تاریخچه پرداخت‌ها", callback_data="payment:history")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def payment_method_detail(method: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    icon = "💳" if method["kind"] == "card" else "💎"
    lines = [
        f"{icon} روش پرداخت #{int(method['id'])}",
        "",
        f"عنوان: {method['title']}",
        f"نوع: {PAYMENT_KIND_FA.get(str(method['kind']), str(method['kind']))}",
        f"ارز: {method['currency']}",
        f"مقصد: {method['destination']}",
    ]
    if method.get("recipient"):
        lines.append(f"به نام: {method['recipient']}")
    if method.get("network"):
        lines.append(f"شبکه: {method['network']}")
    if method.get("instructions"):
        lines.extend(["", f"توضیحات: {method['instructions']}"])
    lines.extend(["", f"وضعیت: {'فعال' if method['status'] == 'active' else 'غیرفعال'}"])
    next_state = "disabled" if method["status"] == "active" else "active"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"payment:edit:{int(method['id'])}")],
        [InlineKeyboardButton(
            "⛔ غیرفعال‌سازی" if next_state == "disabled" else "✅ فعال‌سازی",
            callback_data=f"payment:method:{int(method['id'])}:{next_state}",
        )],
        [InlineKeyboardButton("↩️ پرداخت‌ها", callback_data="menu:payments")],
    ])
    return "\n".join(lines), keyboard


def confirm_keyboard(action: str, cancel_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید نهایی", callback_data=f"confirm:{action}")],
        [InlineKeyboardButton("❌ لغو", callback_data=cancel_callback)],
    ])


def audit_text(items: Iterable[dict[str, Any]]) -> str:
    lines = ["🧾 آخرین رویدادها"]
    for item in items:
        lines.append(
            f"#{int(item['id'])} · {item['action']} · {item['entity_type']}:{item['entity_id']}"
        )
    if len(lines) == 1:
        lines.append("رویدادی ثبت نشده است.")
    return "\n".join(lines)
