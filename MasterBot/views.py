"""Pure text and keyboard rendering for the MasterBot UI."""

from __future__ import annotations

from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from MasterBot.service import Page
from Shared.timeutils import format_tehran, parse_utc


MAIN_MENU = (
    ("👤 کاربران فروشگاه", "menu:customers"),
    ("🤖 ربات‌های مشتریان", "menu:tenants"),
    ("🔐 لایسنس‌ها", "menu:licenses"),
    ("📦 پلن‌ها", "menu:plans"),
    ("📊 آمار", "menu:stats"),
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
            InlineKeyboardButton(by_callback["menu:payments"], callback_data="menu:payments"),
            InlineKeyboardButton(by_callback["menu:stats"], callback_data="menu:stats"),
        ],
        [
            InlineKeyboardButton(by_callback["menu:warnings"], callback_data="menu:warnings"),
            InlineKeyboardButton(by_callback["menu:audit"], callback_data="menu:audit"),
        ],
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
                f"• {order['public_id']} · {order['kind']} · "
                f"{int(order['amount']):,} {order['currency']} · {order['status']}"
            )
    next_status = "blocked" if customer["status"] == "active" else "active"
    keyboard = InlineKeyboardMarkup([
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
                f"{icon} {item['name']} · {item['slug']}",
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
    text = (
        f"👤 مشتری #{int(row['id'])}\n\n"
        f"نام: {row['name']}\n"
        f"شناسه: {row['slug']}\n"
        f"مالک تلگرام: {int(row['owner_telegram_id'])}\n"
        f"وضعیت: {row['status']}\n"
        f"AdminBot: {'✅' if readiness['admin'] else '❌'}\n"
        f"UserBot: {'✅' if readiness['user'] else '❌'}\n"
        f"Runtime: {readiness.get('runtime_status', 'missing')}\n"
        f"آماده اجرا: {'✅' if readiness.get('ready') else '❌'}"
    )
    tenant_id = int(row["id"])
    next_status = "suspended" if row["status"] == "active" else "active"
    status_label = "⛔ تعلیق" if next_status == "suspended" else "✅ فعال‌سازی"
    rows = [
        [
            InlineKeyboardButton("🤖 ثبت AdminBot", callback_data=f"tenant:bot:{tenant_id}:admin"),
            InlineKeyboardButton("🛍 ثبت UserBot", callback_data=f"tenant:bot:{tenant_id}:user"),
        ],
    ]
    secret_buttons: list[InlineKeyboardButton] = []
    if readiness["admin"]:
        secret_buttons.append(
            InlineKeyboardButton("🔑 Secret Admin", callback_data=f"tenant:webhook:{tenant_id}:admin")
        )
    if readiness["user"]:
        secret_buttons.append(
            InlineKeyboardButton("🔑 Secret User", callback_data=f"tenant:webhook:{tenant_id}:user")
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
            f"📦 {item['name']} · {int(item['duration_days'])} روز · {item['status']}",
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


def plan_detail(row: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"📦 پلن #{int(row['id'])}\n\n"
        f"نام: {row['name']}\n"
        f"مدت: {int(row['duration_days'])} روز\n"
        f"قیمت: {int(row['price']):,}\n"
        f"حداکثر سرور: {int(row['max_servers'])}\n"
        f"حداکثر کاربر: {int(row['max_users'])}\n"
        f"ارز: {row.get('currency', 'USD')}\n"
        f"فروش عمومی: {'بله' if int(row.get('is_public', 1)) else 'خیر'}\n"
        f"تست: {int(row.get('trial_days', 0))} روز\n"
        f"وضعیت: {row['status']}"
    )
    plan_id = int(row["id"])
    target = "archived" if row["status"] == "active" else "active"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"plan:edit:{plan_id}")],
        [InlineKeyboardButton("🛍 فروش و لایسنس تست", callback_data=f"plan:commerce:{plan_id}")],
        [InlineKeyboardButton(
            "🗄 آرشیو" if target == "archived" else "✅ فعال‌سازی",
            callback_data=f"plan:status:{plan_id}:{target}",
        )],
        [InlineKeyboardButton("↩️ پلن‌ها", callback_data="plan:page:0")],
    ])
    return text, keyboard


def licenses_view(page: Page, *, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        rows.append([InlineKeyboardButton(
            f"🔐 #{int(item['id'])} · مشتری {int(item['tenant_id'])} · {item['status']}",
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
        f"وضعیت: {row['status']}\n"
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



def platform_settings_view(settings: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    sales = "🟢 فعال" if settings.get("sales_enabled") else "🔴 غیرفعال"
    trial = "🟢 فعال" if settings.get("trial_enabled") else "🔴 غیرفعال"
    support = str(settings.get("support_contact") or "تنظیم نشده")
    text = (
        "⚙️ تنظیمات فروشگاه\n\n"
        f"🏷 نام فروشگاه: {settings.get('store_name') or '-'}\n"
        f"☎️ پشتیبانی: {support}\n"
        f"🛒 فروش: {sales}\n"
        f"🎁 لایسنس تست: {trial}\n\n"
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
        [InlineKeyboardButton("📝 پیام توقف فروش", callback_data="settings:edit:maintenance_message")],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ]
    return text, InlineKeyboardMarkup(rows)


def payment_method_detail(method: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    icon = "💳" if method["kind"] == "card" else "💎"
    lines = [
        f"{icon} روش پرداخت #{int(method['id'])}",
        "",
        f"عنوان: {method['title']}",
        f"نوع: {method['kind']}",
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
