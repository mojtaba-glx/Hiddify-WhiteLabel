"""Telegram handlers for the owner-only MasterBot interface."""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from MasterBot.service import MasterService, MasterServiceError, NotFoundError
from MasterBot.customer_handlers import (
    handle_customer_callback,
    handle_customer_photo,
    handle_customer_text,
    show_customer_home,
)
from MasterBot.customer_service import CustomerPortalError, CustomerPortalService
from Provisioning.service import PreparedBot, ProvisioningError
from MasterBot.views import (
    audit_text,
    back_keyboard,
    confirm_keyboard,
    license_detail,
    licenses_view,
    main_menu_keyboard,
    financial_dashboard_view,
    order_detail_view,
    orders_view,
    payment_method_detail,
    platform_customer_detail,
    platform_customers_view,
    platform_settings_view,
    plan_detail,
    plans_view,
    tenant_detail,
    tenants_view,
)
from Shared.access import is_master_admin
from Shared.redaction import get_logger, safe_format_exception

logger = get_logger(__name__)


def _actor_id(update: Update) -> Optional[int]:
    return int(update.effective_user.id) if update.effective_user else None


def _service(context: ContextTypes.DEFAULT_TYPE) -> MasterService:
    service = context.application.bot_data.get("master_service")
    if not isinstance(service, MasterService):
        raise RuntimeError("master service is unavailable")
    return service


def _portal(context: ContextTypes.DEFAULT_TYPE) -> CustomerPortalService:
    portal = context.application.bot_data.get("customer_portal_service")
    if not isinstance(portal, CustomerPortalService):
        raise RuntimeError("customer portal service is unavailable")
    return portal


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return is_master_admin(_actor_id(update), _service(context).master_admin_id)


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Role firewall: customer routes are public, owner routes are not."""
    service = _service(context)
    if is_master_admin(_actor_id(update), service.master_admin_id):
        return
    if _actor_id(update) is None or int(_actor_id(update) or 0) <= 0:
        raise ApplicationHandlerStop
    # Customer callback payloads are deliberately in their own namespace.
    # A copied owner callback can never enter the owner handler below.
    if update.callback_query and str(update.callback_query.data or "").startswith("customer:"):
        return
    if update.callback_query:
        await update.callback_query.answer("Access denied", show_alert=True)
        raise ApplicationHandlerStop
    if update.effective_message:
        text = str(getattr(update.effective_message, "text", "") or "").strip()
        if text.startswith("/") and text.split(maxsplit=1)[0].split("@", 1)[0] not in {
            "/start", "/menu", "/cancel"
        }:
            await update.effective_message.reply_text("Access denied")
            raise ApplicationHandlerStop
        # Customer text, image receipts and the three public commands proceed.
        return
    raise ApplicationHandlerStop


async def _render(update: Update, text: str, keyboard: Any) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text=text, reply_markup=keyboard)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        await show_customer_home(update, context)
        return
    context.user_data.clear()
    await _render(
        update,
        "🧭 پنل مدیریت سیستم فروش ربات\n\n"
        "از این بخش کاربران، ربات‌ها، لایسنس‌ها، پرداخت‌ها و تنظیمات کل سیستم را مدیریت می‌کنید.",
        main_menu_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main(update, context)


def _parse_int(raw: str, label: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


async def _show_orders(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    actor = int(_actor_id(update) or 0)
    status = str(context.user_data.get("order_status_filter") or "all")
    query = str(context.user_data.get("order_query") or "")
    page = _portal(context).list_all_orders(
        actor, page=page_number, status=status, query=query
    )
    text, keyboard = orders_view(page, status=status, query=query)
    await _render(update, text, keyboard)


async def _show_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    order = _portal(context).get_order_admin(actor, int(order_id))
    text, keyboard = order_detail_view(order)
    await _render(update, text, keyboard)


async def _show_platform_customers(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    actor = int(_actor_id(update) or 0)
    query = str(context.user_data.get("platform_customer_query") or "")
    page = _portal(context).list_platform_customers(
        actor, page=page_number, query=query
    )
    text, keyboard = platform_customers_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_platform_customer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, customer_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    customer = _portal(context).get_platform_customer_admin(actor, int(customer_id))
    text, keyboard = platform_customer_detail(customer)
    await _render(update, text, keyboard)


async def _show_tenants(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("tenant_query") or "")
    page = _service(context).list_tenants(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = tenants_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_tenant(update: Update, context: ContextTypes.DEFAULT_TYPE, tenant_id: int) -> None:
    service = _service(context)
    actor = int(_actor_id(update) or 0)
    row = service.get_tenant(actor, tenant_id)
    state = service.provisioning_status(actor, tenant_id)
    readiness = {
        "admin": state.admin_bot,
        "user": state.user_bot,
        "ready": state.ready,
        "runtime_status": state.runtime_status,
    }
    text, keyboard = tenant_detail(row, readiness)
    await _render(update, text, keyboard)


async def _show_plans(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("plan_query") or "")
    page = _service(context).list_plans(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = plans_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_licenses(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("license_query") or "")
    page = _service(context).list_licenses(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = licenses_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_license(
    update: Update, context: ContextTypes.DEFAULT_TYPE, license_id: int
) -> None:
    row = _service(context).get_license(int(_actor_id(update) or 0), license_id)
    timezone_name = str(context.application.bot_data.get("display_timezone") or "Asia/Tehran")
    text, keyboard = license_detail(row, timezone_name=timezone_name)
    await _render(update, text, keyboard)


async def _show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = int(_actor_id(update) or 0)
    settings = _portal(context).get_platform_settings(actor)
    text, keyboard = platform_settings_view(settings)
    await _render(update, text, keyboard)


async def _show_payment_method(
    update: Update, context: ContextTypes.DEFAULT_TYPE, method_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    method = _portal(context).get_payment_method_admin(actor, int(method_id))
    text, keyboard = payment_method_detail(method)
    await _render(update, text, keyboard)


async def _show_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = int(_actor_id(update) or 0)
    portal = _portal(context)
    methods = portal.list_all_payment_methods(actor)
    receipts = portal.list_pending_receipts(actor, limit=15)
    lines = ["💳 مدیریت پرداخت‌ها", "", f"رسیدهای در انتظار: {len(receipts)}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for receipt in receipts:
        lines.append(
            f"#{int(receipt['id'])} · {receipt['display_name']} · "
            f"{int(receipt['amount']):,} {receipt['currency']} · {receipt['method_title']}"
        )
        rows.append([InlineKeyboardButton(
            f"🧾 بررسی رسید #{int(receipt['id'])}", callback_data=f"payment:receipt:{int(receipt['id'])}"
        )])
    if not receipts:
        lines.append("رسیدی برای بررسی وجود ندارد.")
    lines.extend(["", "روش‌های فعال/غیرفعال:"])
    if not methods:
        lines.append("هنوز روشی ثبت نشده است.")
    for method in methods:
        state = "🟢" if method["status"] == "active" else "⚫"
        lines.append(f"{state} #{int(method['id'])} · {method['kind']} · {method['title']} · {method['currency']}")
        rows.append([InlineKeyboardButton(
            f"{state} {method['title']} · {method['currency']}",
            callback_data=f"payment:view:{int(method['id'])}",
        )])
    rows.extend([
        [
            InlineKeyboardButton("➕ کارت‌به‌کارت", callback_data="payment:add:card"),
            InlineKeyboardButton("➕ ارز دیجیتال", callback_data="payment:add:crypto"),
        ],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ])
    await _render(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = str(query.data or "")
    if data.startswith("customer:"):
        await handle_customer_callback(update, context, data)
        return
    actor = int(_actor_id(update) or 0)
    service = _service(context)
    try:
        if data == "noop":
            return
        if data == "menu:main":
            await show_main(update, context)
            return
        if data == "menu:orders":
            context.user_data["order_status_filter"] = "all"
            context.user_data.pop("order_query", None)
            await _show_orders(update, context)
            return
        if data.startswith("orderadmin:filter:"):
            status = data.split(":", 2)[2]
            context.user_data["order_status_filter"] = status
            context.user_data.pop("order_query", None)
            await _show_orders(update, context)
            return
        if data.startswith("orderadmin:page:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid order page callback")
            context.user_data["order_status_filter"] = parts[2]
            await _show_orders(update, context, _parse_int(parts[3], "page"))
            return
        if data == "orderadmin:search":
            context.user_data["flow"] = {"kind": "order_search"}
            await _render(
                update,
                "🔎 شناسه سفارش، نام، یوزرنیم یا شناسه تلگرام مشتری را بفرستید:",
                back_keyboard("menu:orders"),
            )
            return
        if data.startswith("orderadmin:view:"):
            await _show_order(
                update, context, _parse_int(data.rsplit(":", 1)[1], "order id")
            )
            return
        if data == "menu:payments":
            await _show_payments(update, context)
            return
        if data.startswith("payment:view:"):
            await _show_payment_method(
                update, context, _parse_int(data.rsplit(":", 1)[1], "method id")
            )
            return
        if data.startswith("payment:edit:"):
            method_id = _parse_int(data.rsplit(":", 1)[1], "method id")
            method = _portal(context).get_payment_method_admin(actor, method_id)
            context.user_data["flow"] = {
                "kind": "payment_method_edit",
                "method_id": method_id,
                "payment_kind": str(method["kind"]),
            }
            instructions = (
                "عنوان | ارز | شماره کارت | نام صاحب کارت | توضیح اختیاری"
                if method["kind"] == "card" else
                "عنوان | ارز | آدرس کیف پول | شبکه | توضیح اختیاری"
            )
            await _render(
                update,
                f"✏️ ویرایش روش پرداخت #{method_id}\n\n{instructions}",
                back_keyboard(f"payment:view:{method_id}"),
            )
            return
        if data.startswith("payment:add:"):
            kind = data.rsplit(":", 1)[1]
            if kind not in ("card", "crypto"):
                raise ValueError("invalid payment kind")
            context.user_data["flow"] = {"kind": "payment_method_new", "payment_kind": kind}
            instructions = (
                "عنوان | ارز | شماره کارت | نام صاحب کارت | توضیح اختیاری"
                if kind == "card" else
                "عنوان | ارز | آدرس کیف پول | شبکه | توضیح اختیاری"
            )
            await _render(update, f"➕ ثبت روش پرداخت\n\n{instructions}", back_keyboard("menu:payments"))
            return
        if data.startswith("payment:method:"):
            _, _, method_raw, status = data.split(":", 3)
            method_id = _parse_int(method_raw, "method id")
            _portal(context).set_payment_method_status(actor, method_id, status)
            await _show_payment_method(update, context, method_id)
            return
        if data.startswith("payment:receipt:"):
            receipt_id = _parse_int(data.rsplit(":", 1)[1], "receipt id")
            receipt = next(
                (row for row in _portal(context).list_pending_receipts(actor, limit=100)
                 if int(row["id"]) == receipt_id),
                None,
            )
            if receipt is None:
                raise NotFoundError("receipt not found")
            reference = str(receipt.get("reference") or "تصویر رسید")
            text = (
                f"🧾 رسید #{receipt_id}\n\n"
                f"مشتری: {receipt['display_name']} · {receipt['telegram_user_id']}\n"
                f"سفارش: {receipt['public_id']} · {receipt['kind']}\n"
                f"مبلغ: {int(receipt['amount']):,} {receipt['currency']}\n"
                f"روش: {receipt['method_title']}\n"
                f"پیگیری: {reference}"
            )
            context.user_data["receipt_preview"] = receipt
            await _render(
                update, text, InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"payment:approve:{receipt_id}")],
                    [InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payment:reject:{receipt_id}")],
                    [InlineKeyboardButton("↩️ پرداخت‌ها", callback_data="menu:payments")],
                ])
            )
            if receipt.get("telegram_file_id") and update.effective_chat:
                await update.effective_chat.send_photo(
                    photo=str(receipt["telegram_file_id"]), caption=f"تصویر رسید #{receipt_id}"
                )
            return
        if data.startswith("payment:approve:") or data.startswith("payment:reject:"):
            parts = data.split(":")
            action, receipt_raw = parts[1], parts[2]
            receipt_id = _parse_int(receipt_raw, "receipt id")
            context.user_data["confirm"] = {
                "kind": "payment_review", "receipt_id": receipt_id, "approve": action == "approve",
            }
            await _render(
                update,
                "تأیید نهایی می‌کنید؟ این عملیات فقط یک‌بار انجام می‌شود.",
                confirm_keyboard("payment_review", f"payment:receipt:{receipt_id}"),
            )
            return
        if data == "menu:customers":
            context.user_data.pop("platform_customer_query", None)
            await _show_platform_customers(update, context)
            return
        if data.startswith("customeradmin:page:"):
            await _show_platform_customers(
                update, context, _parse_int(data.rsplit(":", 1)[1], "page")
            )
            return
        if data == "customeradmin:search":
            context.user_data["flow"] = {"kind": "platform_customer_search"}
            await _render(
                update,
                "🔎 نام، یوزرنیم، شناسه تلگرام یا شناسه داخلی کاربر را بفرستید:",
                back_keyboard("customeradmin:page:0"),
            )
            return
        if data.startswith("customeradmin:view:"):
            await _show_platform_customer(
                update, context, _parse_int(data.rsplit(":", 1)[1], "customer id")
            )
            return
        if data.startswith("customeradmin:status:"):
            _, _, customer_raw, status = data.split(":", 3)
            customer_id = _parse_int(customer_raw, "customer id")
            if status not in ("active", "blocked"):
                raise ValueError("invalid customer status")
            context.user_data["confirm"] = {
                "kind": "platform_customer_status",
                "customer_id": customer_id,
                "status": status,
            }
            await _render(
                update,
                "تغییر وضعیت این کاربر را تأیید می‌کنید؟",
                confirm_keyboard(
                    "platform_customer_status",
                    f"customeradmin:view:{customer_id}",
                ),
            )
            return
        if data == "menu:tenants":
            context.user_data.pop("tenant_query", None)
            await _show_tenants(update, context)
            return
        if data.startswith("tenant:page:"):
            await _show_tenants(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "tenant:new":
            context.user_data["flow"] = {"kind": "tenant_new"}
            await _render(
                update,
                "➕ اطلاعات مشتری را بفرستید:\nنام | slug | شناسه عددی مالک",
                back_keyboard("tenant:page:0"),
            )
            return
        if data == "tenant:provision":
            context.user_data["flow"] = {"kind": "provision_details"}
            await _render(
                update,
                "🚀 اطلاعات مشتری را بفرستید:\nنام | slug | شناسه عددی مالک",
                back_keyboard("tenant:page:0"),
            )
            return
        if data == "tenant:search":
            context.user_data["flow"] = {"kind": "tenant_search"}
            await _render(update, "🔎 نام، slug یا شناسه مالک را بفرستید:", back_keyboard("tenant:page:0"))
            return
        if data.startswith("tenant:view:"):
            await _show_tenant(update, context, _parse_int(data.rsplit(":", 1)[1], "tenant id"))
            return
        if data.startswith("tenant:edit:"):
            tenant_id = _parse_int(data.rsplit(":", 1)[1], "tenant id")
            service.get_tenant(actor, tenant_id)
            context.user_data["flow"] = {"kind": "tenant_edit", "tenant_id": tenant_id}
            await _render(
                update,
                "✏️ اطلاعات جدید را بفرستید:\nنام | slug | شناسه عددی مالک",
                back_keyboard(f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:bot:"):
            _, _, tenant_raw, role = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if role not in ("admin", "user"):
                raise ValueError("invalid bot role")
            context.user_data["flow"] = {
                "kind": "bot_token", "tenant_id": tenant_id, "role": role
            }
            await _render(
                update,
                "🔐 توکن ربات را ارسال کنید. پیام توکن پس از دریافت حذف می‌شود.",
                back_keyboard(f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:webhook:"):
            _, _, tenant_raw, role = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if role not in ("admin", "user"):
                raise ValueError("invalid bot role")
            context.user_data["confirm"] = {
                "kind": "webhook_rotate",
                "tenant_id": tenant_id,
                "role": role,
            }
            await _render(
                update,
                "Secret فعلی بلافاصله نامعتبر می‌شود. Secret جدید ساخته شود؟",
                confirm_keyboard("webhook_rotate", f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:status:"):
            _, _, tenant_raw, status = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if status not in ("active", "suspended", "disabled"):
                raise ValueError("invalid tenant status")
            context.user_data["confirm"] = {
                "kind": "tenant_status", "tenant_id": tenant_id, "status": status
            }
            await _render(
                update,
                f"آیا تغییر وضعیت مشتری به {status} تأیید می‌شود؟",
                confirm_keyboard("tenant_status", f"tenant:view:{tenant_id}"),
            )
            return

        if data == "menu:plans":
            context.user_data.pop("plan_query", None)
            await _show_plans(update, context)
            return
        if data.startswith("plan:page:"):
            await _show_plans(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "plan:new":
            context.user_data["flow"] = {"kind": "plan_new"}
            await _render(
                update,
                "➕ پلن را بفرستید:\nنام | روز | قیمت | حداکثر سرور | حداکثر کاربر",
                back_keyboard("plan:page:0"),
            )
            return
        if data == "plan:search":
            context.user_data["flow"] = {"kind": "plan_search"}
            await _render(update, "🔎 نام، وضعیت یا شناسه پلن را بفرستید:", back_keyboard("plan:page:0"))
            return
        if data.startswith("plan:view:"):
            row = service.get_plan(actor, _parse_int(data.rsplit(":", 1)[1], "plan id"))
            text, keyboard = plan_detail(row)
            await _render(update, text, keyboard)
            return
        if data.startswith("plan:edit:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            service.get_plan(actor, plan_id)
            context.user_data["flow"] = {"kind": "plan_edit", "plan_id": plan_id}
            await _render(
                update,
                "✏️ پلن جدید را بفرستید:\nنام | روز | قیمت | حداکثر سرور | حداکثر کاربر",
                back_keyboard(f"plan:view:{plan_id}"),
            )
            return
        if data.startswith("plan:commerce:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            service.get_plan(actor, plan_id)
            context.user_data["flow"] = {"kind": "plan_commerce", "plan_id": plan_id}
            await _render(
                update,
                "🛍 تنظیمات فروش پلن را بفرستید:\nارز | نمایش عمومی (0 یا 1) | روزهای لایسنس تست\n\nمثال: USD | 1 | 7",
                back_keyboard(f"plan:view:{plan_id}"),
            )
            return
        if data.startswith("plan:status:"):
            _, _, plan_raw, status = data.split(":", 3)
            plan_id = _parse_int(plan_raw, "plan id")
            service.get_plan(actor, plan_id)
            if status not in ("active", "archived", "disabled"):
                raise ValueError("invalid plan status")
            context.user_data["confirm"] = {
                "kind": "plan_status", "plan_id": plan_id, "status": status
            }
            await _render(
                update,
                f"آیا تغییر وضعیت پلن به {status} تأیید می‌شود؟",
                confirm_keyboard("plan_status", f"plan:view:{plan_id}"),
            )
            return

        if data == "menu:licenses":
            context.user_data.pop("license_query", None)
            await _show_licenses(update, context)
            return
        if data.startswith("license:page:"):
            await _show_licenses(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "license:new":
            context.user_data["flow"] = {"kind": "license_new"}
            await _render(
                update,
                "➕ لایسنس را بفرستید:\nشناسه مشتری | شناسه پلن | روز مهلت (اختیاری)",
                back_keyboard("license:page:0"),
            )
            return
        if data == "license:search":
            context.user_data["flow"] = {"kind": "license_search"}
            await _render(
                update,
                "🔎 شناسه لایسنس/مشتری/پلن یا نام مشتری/پلن را بفرستید:",
                back_keyboard("license:page:0"),
            )
            return
        if data.startswith("license:view:"):
            await _show_license(update, context, _parse_int(data.rsplit(":", 1)[1], "license id"))
            return
        if data.startswith("license:renew:"):
            license_id = _parse_int(data.rsplit(":", 1)[1], "license id")
            service.get_license(actor, license_id)
            context.user_data["flow"] = {"kind": "license_renew", "license_id": license_id}
            await _render(
                update,
                "♻️ مدت تمدید را بفرستید:\nتعداد روز | روز مهلت (اختیاری)",
                back_keyboard(f"license:view:{license_id}"),
            )
            return
        if data.startswith("license:suspend:") or data.startswith("license:reactivate:"):
            parts = data.split(":")
            kind = parts[1]
            license_id = _parse_int(parts[2], "license id")
            service.get_license(actor, license_id)
            context.user_data["confirm"] = {"kind": kind, "license_id": license_id}
            await _render(
                update,
                "این عملیات روی دسترسی مشتری اثر می‌گذارد. تأیید می‌کنید؟",
                confirm_keyboard(kind, f"license:view:{license_id}"),
            )
            return

        if data.startswith("confirm:"):
            expected = data.split(":", 1)[1]
            pending = context.user_data.pop("confirm", None)
            if not isinstance(pending, dict) or pending.get("kind") != expected:
                raise ValueError("confirmation expired")
            if expected == "platform_customer_status":
                row = _portal(context).set_platform_customer_status(
                    actor, int(pending["customer_id"]), str(pending["status"])
                )
                await _show_platform_customer(update, context, int(row["id"]))
                return
            if expected == "tenant_status":
                row = service.set_tenant_status(
                    actor, int(pending["tenant_id"]), str(pending["status"])
                )
                await _show_tenant(update, context, int(row["id"]))
                return
            if expected == "webhook_rotate":
                tenant_id = int(pending["tenant_id"])
                enrollment = service.rotate_tenant_webhook_secret(
                    actor, tenant_id, role=str(pending["role"])
                )
                await update.effective_chat.send_message(
                    f"🔑 Webhook secret جدید {enrollment.bot.role}:\n"
                    f"{enrollment.webhook_secret}\n\n"
                    "پس از استفاده این پیام را حذف کنید.",
                    protect_content=True,
                )
                await _show_tenant(update, context, tenant_id)
                return
            if expected == "plan_status":
                row = service.set_plan_status(
                    actor, int(pending["plan_id"]), str(pending["status"])
                )
                text, keyboard = plan_detail(row)
                await _render(update, text, keyboard)
                return
            if expected == "renew":
                row = service.renew(
                    actor,
                    int(pending["license_id"]),
                    extra_days=int(pending["extra_days"]),
                    grace_days=int(pending["grace_days"]),
                )
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "suspend":
                row = service.suspend(actor, int(pending["license_id"]))
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "reactivate":
                row = service.reactivate(actor, int(pending["license_id"]))
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "payment_review":
                reviewed = _portal(context).review_receipt(
                    actor, int(pending["receipt_id"]), approve=bool(pending["approve"])
                )
                notification = (
                    "✅ پرداخت شما تأیید شد."
                    if bool(pending["approve"]) else "❌ پرداخت شما تأیید نشد."
                )
                if bool(pending["approve"]) and reviewed["order_status"] == "paid":
                    notification += " اکنون از بخش «راه‌اندازی ربات» ادامه دهید."
                try:
                    await context.bot.send_message(chat_id=int(reviewed["telegram_user_id"]), text=notification)
                except Exception as exc:
                    logger.warning("Payment notification could not be sent: %s", safe_format_exception(exc))
                await _show_payments(update, context)
                return
            raise ValueError("unknown confirmation")

        if data == "menu:stats":
            stats = service.statistics(actor)
            finance = _portal(context).financial_summary(actor)
            text, keyboard = financial_dashboard_view(stats, finance)
            await _render(update, text, keyboard)
            return
        if data == "menu:warnings" or data.startswith("warning:page:"):
            page_no = 0 if data == "menu:warnings" else _parse_int(data.rsplit(":", 1)[1], "page")
            page = service.list_warnings(actor, page=page_no)
            lines = ["⚠️ هشدارهای باز"]
            for item in page.items:
                lines.append(f"#{int(item['id'])} · {item['event_type']} · {item['status']}")
            if not page.items:
                lines.append("هشداری وجود ندارد.")
            buttons = []
            if page.has_previous:
                buttons.append(InlineKeyboardButton("⬅️", callback_data=f"warning:page:{page.page-1}"))
            if page.has_next:
                buttons.append(InlineKeyboardButton("➡️", callback_data=f"warning:page:{page.page+1}"))
            rows = [buttons] if buttons else []
            rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
            await _render(update, "\n".join(lines), InlineKeyboardMarkup(rows))
            return
        if data == "menu:audit" or data.startswith("audit:page:"):
            page_no = 0 if data == "menu:audit" else _parse_int(data.rsplit(":", 1)[1], "page")
            page = service.list_audit(actor, page=page_no)
            buttons = []
            if page.has_previous:
                buttons.append(InlineKeyboardButton("⬅️", callback_data=f"audit:page:{page.page-1}"))
            if page.has_next:
                buttons.append(InlineKeyboardButton("➡️", callback_data=f"audit:page:{page.page+1}"))
            rows = [buttons] if buttons else []
            rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
            await _render(update, audit_text(page.items), InlineKeyboardMarkup(rows))
            return
        if data == "menu:settings":
            await _show_settings(update, context)
            return
        if data.startswith("settings:edit:"):
            key = data.rsplit(":", 1)[1]
            prompts = {
                "store_name": "🏷 نام جدید فروشگاه را بفرستید:",
                "support_contact": "☎️ آیدی یا راه ارتباطی پشتیبانی را بفرستید. برای حذف، - ارسال کنید.",
                "maintenance_message": "📝 پیام زمان توقف فروش را بفرستید:",
            }
            if key not in prompts:
                raise ValueError("invalid setting key")
            context.user_data["flow"] = {"kind": "platform_setting_text", "setting_key": key}
            await _render(update, prompts[key], back_keyboard("menu:settings"))
            return
        if data == "settings:toggle:sales":
            current = _portal(context).get_platform_settings(actor)
            _portal(context).set_platform_bool_setting(
                actor, "sales_enabled", not bool(current["sales_enabled"])
            )
            await _show_settings(update, context)
            return
        if data == "settings:toggle:trial":
            current = _portal(context).get_platform_settings(actor)
            _portal(context).set_platform_bool_setting(
                actor, "trial_enabled", not bool(current["trial_enabled"])
            )
            await _show_settings(update, context)
            return
        raise ValueError("unknown callback")
    except (
        ValueError,
        sqlite3.IntegrityError,
        MasterServiceError,
        NotFoundError,
        ProvisioningError,
    ) as exc:
        logger.warning("MasterBot callback rejected: %s", safe_format_exception(exc))
        await _render(update, "❌ درخواست معتبر نیست یا دیگر قابل اجرا نیست.", back_keyboard())


def _split_fields(text: str, minimum: int, maximum: int) -> list[str]:
    parts = [part.strip() for part in str(text or "").split("|")]
    if not minimum <= len(parts) <= maximum or any(not part for part in parts[:minimum]):
        raise ValueError("invalid input format")
    return parts


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        if await handle_customer_text(update, context):
            return
        await show_customer_home(update, context)
        return
    flow = context.user_data.get("flow")
    if not isinstance(flow, dict) or update.effective_message is None:
        await show_main(update, context)
        return
    kind = str(flow.get("kind") or "")
    actor = int(_actor_id(update) or 0)
    service = _service(context)
    text = str(update.effective_message.text or "").strip()
    try:
        if kind == "provision_details":
            fields = _split_fields(text, 3, 3)
            context.user_data["flow"] = {
                "kind": "provision_admin_token",
                "name": fields[0],
                "slug": fields[1],
                "owner_telegram_id": _parse_int(fields[2], "owner id"),
            }
            await update.effective_message.reply_text(
                "🔐 توکن AdminBot مشتری را بفرستید؛ پیام بلافاصله حذف می‌شود.",
                reply_markup=back_keyboard("tenant:page:0"),
            )
            return
        if kind in ("provision_admin_token", "provision_user_token"):
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete a provisioning token message: %s",
                    safe_format_exception(exc),
                )
            role = "admin" if kind == "provision_admin_token" else "user"
            prepared = await service.prepare_tenant_bot(
                actor, role=role, plain_token=text
            )
            text = ""
            if role == "admin":
                flow["admin_bot"] = prepared
                flow["kind"] = "provision_user_token"
                await update.effective_chat.send_message(
                    "✅ AdminBot تأیید شد. اکنون توکن UserBot مشتری را بفرستید.",
                    reply_markup=back_keyboard("tenant:page:0"),
                )
                return
            admin_bot = flow.get("admin_bot")
            if not isinstance(admin_bot, PreparedBot):
                raise ProvisioningError("provisioning draft expired")
            result = service.provision_tenant_prepared(
                actor,
                name=str(flow["name"]),
                slug=str(flow["slug"]),
                owner_telegram_id=int(flow["owner_telegram_id"]),
                admin_bot=admin_bot,
                user_bot=prepared,
            )
            context.user_data.pop("flow", None)
            await update.effective_chat.send_message(
                "🔐 Secretهای یک‌بارمصرف Webhook\n\n"
                f"AdminBot: {result.webhook_secrets.admin}\n"
                f"UserBot: {result.webhook_secrets.user}\n\n"
                "پس از ثبت Webhook این پیام را حذف کنید؛ فقط Hash در دیتابیس ذخیره شده است.",
                protect_content=True,
            )
            row = service.get_tenant(actor, result.tenant_id)
            state = service.provisioning_status(actor, result.tenant_id)
            detail, keyboard = tenant_detail(
                row,
                {
                    "admin": state.admin_bot,
                    "user": state.user_bot,
                    "ready": state.ready,
                    "runtime_status": state.runtime_status,
                },
            )
            await update.effective_chat.send_message(detail, reply_markup=keyboard)
            return
        if kind == "tenant_new":
            fields = _split_fields(text, 3, 3)
            row = service.create_tenant(
                actor, name=fields[0], slug=fields[1], owner_telegram_id=_parse_int(fields[2], "owner id")
            )
            context.user_data.pop("flow", None)
            await _show_tenant(update, context, int(row["id"]))
            return
        if kind == "tenant_edit":
            fields = _split_fields(text, 3, 3)
            row = service.update_tenant(
                actor,
                int(flow["tenant_id"]),
                name=fields[0],
                slug=fields[1],
                owner_telegram_id=_parse_int(fields[2], "owner id"),
            )
            context.user_data.pop("flow", None)
            await _show_tenant(update, context, int(row["id"]))
            return
        if kind == "order_search":
            context.user_data.pop("flow", None)
            context.user_data["order_query"] = text[:100]
            await _show_orders(update, context)
            return
        if kind == "platform_customer_search":
            context.user_data.pop("flow", None)
            context.user_data["platform_customer_query"] = text[:100]
            await _show_platform_customers(update, context)
            return
        if kind == "tenant_search":
            context.user_data.pop("flow", None)
            context.user_data["tenant_query"] = text[:100]
            await _show_tenants(update, context)
            return
        if kind == "plan_search":
            context.user_data.pop("flow", None)
            context.user_data["plan_query"] = text[:100]
            await _show_plans(update, context)
            return
        if kind == "license_search":
            context.user_data.pop("flow", None)
            context.user_data["license_query"] = text[:100]
            await _show_licenses(update, context)
            return
        if kind in ("plan_new", "plan_edit"):
            fields = _split_fields(text, 5, 5)
            kwargs: dict[str, Any] = {
                "name": fields[0],
                "duration_days": _parse_int(fields[1], "days"),
                "price": _parse_int(fields[2], "price"),
                "max_servers": _parse_int(fields[3], "max servers"),
                "max_users": _parse_int(fields[4], "max users"),
            }
            if kind == "plan_new":
                row = service.create_plan(actor, **kwargs)
            else:
                row = service.update_plan(actor, int(flow["plan_id"]), **kwargs)
            context.user_data.pop("flow", None)
            detail, keyboard = plan_detail(row)
            await _render(update, detail, keyboard)
            return
        if kind == "plan_commerce":
            fields = _split_fields(text, 3, 3)
            public_raw = _parse_int(fields[1], "public flag")
            if public_raw not in (0, 1):
                raise ValueError("public flag must be 0 or 1")
            row = _portal(context).configure_plan_commerce(
                actor, int(flow["plan_id"]), currency=fields[0],
                is_public=bool(public_raw), trial_days=_parse_int(fields[2], "trial days"),
            )
            context.user_data.pop("flow", None)
            detail, keyboard = plan_detail(row)
            await _render(update, detail, keyboard)
            return
        if kind in ("payment_method_new", "payment_method_edit"):
            fields = _split_fields(text, 4, 5)
            payment_kind = str(flow.get("payment_kind") or "")
            if payment_kind not in ("card", "crypto"):
                raise ValueError("invalid payment kind")
            if payment_kind == "card":
                title, currency, destination, recipient = fields[:4]
                network = None
            else:
                title, currency, destination, network = fields[:4]
                recipient = None
            instructions = fields[4] if len(fields) == 5 else ""
            if kind == "payment_method_new":
                row = _portal(context).add_payment_method(
                    actor, kind=payment_kind, title=title, currency=currency,
                    destination=destination, recipient=recipient, network=network,
                    instructions=instructions,
                )
            else:
                row = _portal(context).update_payment_method(
                    actor, int(flow["method_id"]), title=title, currency=currency,
                    destination=destination, recipient=recipient, network=network,
                    instructions=instructions,
                )
            context.user_data.pop("flow", None)
            await _show_payment_method(update, context, int(row["id"]))
            return
        if kind == "platform_setting_text":
            key = str(flow.get("setting_key") or "")
            _portal(context).set_platform_text_setting(actor, key, text)
            context.user_data.pop("flow", None)
            await _show_settings(update, context)
            return
        if kind == "license_new":
            fields = _split_fields(text, 2, 3)
            row = service.create_license(
                actor,
                tenant_id=_parse_int(fields[0], "tenant id"),
                plan_id=_parse_int(fields[1], "plan id"),
                grace_days=_parse_int(fields[2], "grace days") if len(fields) == 3 else 0,
            )
            context.user_data.pop("flow", None)
            await _show_license(update, context, int(row["id"]))
            return
        if kind == "license_renew":
            fields = _split_fields(text, 1, 2)
            days = _parse_int(fields[0], "days")
            if days <= 0:
                raise ValueError("days must be positive")
            grace = _parse_int(fields[1], "grace days") if len(fields) == 2 else 0
            context.user_data.pop("flow", None)
            context.user_data["confirm"] = {
                "kind": "renew",
                "license_id": int(flow["license_id"]),
                "extra_days": days,
                "grace_days": grace,
            }
            await update.effective_message.reply_text(
                f"تمدید {days} روزه با {grace} روز مهلت تأیید شود؟",
                reply_markup=confirm_keyboard("renew", f"license:view:{int(flow['license_id'])}"),
            )
            return
        if kind == "bot_token":
            # Never keep the raw token in user_data, logs or an exception.
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete the submitted token message: %s",
                    safe_format_exception(exc),
                )
            enrollment = await service.register_tenant_bot_with_secret(
                actor,
                int(flow["tenant_id"]),
                role=str(flow["role"]),
                plain_token=text,
            )
            text = ""
            context.user_data.pop("flow", None)
            await update.effective_chat.send_message(
                f"✅ ربات @{enrollment.bot.telegram_username or 'unknown'} ثبت شد.\n"
                f"Webhook secret: {enrollment.webhook_secret}\n"
                "این Secret فقط همین یک‌بار نمایش داده می‌شود؛ پس از استفاده پیام را حذف کنید.",
                reply_markup=back_keyboard(f"tenant:view:{int(flow['tenant_id'])}"),
                protect_content=True,
            )
            return
        raise ValueError("unknown input flow")
    except (
        ValueError,
        sqlite3.IntegrityError,
        MasterServiceError,
        NotFoundError,
        ProvisioningError,
        CustomerPortalError,
    ) as exc:
        # Flow remains active so the owner can correct the input. The actual
        # message content is deliberately absent from this log entry.
        logger.warning("MasterBot input rejected: %s", safe_format_exception(exc))
        error_text = "❌ اطلاعات معتبر نیست. قالب خواسته‌شده را دوباره ارسال کنید."
        if kind in ("bot_token", "provision_admin_token", "provision_user_token"):
            await update.effective_chat.send_message(
                error_text, reply_markup=back_keyboard()
            )
        else:
            await update.effective_message.reply_text(
                error_text, reply_markup=back_keyboard()
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled MasterBot error: %s", safe_format_exception(context.error or RuntimeError()))


def register_handlers(application: Application) -> None:
    # Group -1 is an application-wide role firewall.  Customer traffic is
    # restricted to its namespaced callbacks while all owner callbacks remain
    # blocked before their handlers run.
    application.add_handler(TypeHandler(Update, access_gate), group=-1)
    application.add_handler(CommandHandler("start", show_main), group=0)
    application.add_handler(CommandHandler("menu", show_main), group=0)
    application.add_handler(CommandHandler("cancel", cancel), group=0)
    application.add_handler(CallbackQueryHandler(on_callback), group=0)
    application.add_handler(MessageHandler(filters.PHOTO, handle_customer_photo), group=0)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=0)
    application.add_error_handler(on_error)
