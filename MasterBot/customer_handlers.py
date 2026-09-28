"""Telegram interaction layer for normal PlatformBot customers."""

from __future__ import annotations

import sqlite3
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes

from MasterBot.customer_service import CustomerPortalError, CustomerPortalService
from MasterBot.customer_views import (
    BUY,
    FEATURES,
    GUIDE,
    SERVICES,
    SETUP,
    TRIAL,
    WALLET,
    customer_home_text,
    customer_main_keyboard,
    features_text,
    guide_text,
    orders_keyboard,
    orders_text,
    payment_instructions,
    payment_keyboard,
    plan_keyboard,
    plan_text,
    plans_keyboard,
    services_keyboard,
    services_text,
    setup_keyboard,
)
from MasterBot.service import MasterServiceError, NotFoundError
from Provisioning.service import PreparedBot, ProvisioningError
from Shared.redaction import get_logger, safe_format_exception

logger = get_logger(__name__)


def portal(context: ContextTypes.DEFAULT_TYPE) -> CustomerPortalService:
    value = context.application.bot_data.get("customer_portal_service")
    if not isinstance(value, CustomerPortalService):
        raise RuntimeError("customer portal service is unavailable")
    return value


def actor_id(update: Update) -> int:
    if update.effective_user is None or int(update.effective_user.id) <= 0:
        raise CustomerPortalError("Telegram identity is unavailable")
    return int(update.effective_user.id)


async def render(update: Update, text: str, keyboard: Any) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
            return
        except Exception:
            # Reply keyboards cannot replace an inline message. Send a fresh
            # message for home and keep all other edit failures observable.
            if keyboard.__class__.__name__ != "ReplyKeyboardMarkup":
                raise
    if update.effective_message:
        await update.effective_message.reply_text(text=text, reply_markup=keyboard)
    elif update.effective_chat:
        await update.effective_chat.send_message(text=text, reply_markup=keyboard)


def _display_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "کاربر"
    return str(getattr(user, "full_name", None) or getattr(user, "first_name", None) or "کاربر")[:120]


async def _notify_master_new_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    receipt: dict[str, Any],
    order: dict[str, Any],
    method: dict[str, Any],
) -> None:
    service = portal(context)
    username = (
        f"@{getattr(update.effective_user, 'username', '')}"
        if update.effective_user and getattr(update.effective_user, "username", None)
        else "—"
    )
    text = (
        "🧾 رسید پرداخت جدید\n\n"
        f"مشتری: {_display_name(update)}\n"
        f"یوزرنیم: {username}\n"
        f"شناسه تلگرام: {actor_id(update)}\n"
        f"سفارش: {order['public_id']}\n"
        f"مبلغ: {int(order['amount']):,} {order['currency']}\n"
        f"روش: {method['title']}\n\n"
        "برای بررسی، دکمه زیر را بزنید."
    )
    try:
        await context.bot.send_message(
            chat_id=int(service.master_admin_id),
            text=text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🔎 بررسی رسید #{int(receipt['id'])}",
                    callback_data=f"payment:receipt:{int(receipt['id'])}",
                )
            ]]),
        )
    except Exception as exc:
        logger.warning(
            "Master receipt notification could not be sent: %s",
            safe_format_exception(exc),
        )


async def _notify_master_provisioned(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tenant_id: int,
    tenant_name: str,
    admin_username: str | None,
    user_username: str | None,
    plan_name: str,
) -> None:
    service = portal(context)
    lines = [
        "✅ ربات مشتری راه‌اندازی شد",
        "",
        f"مشتری: {_display_name(update)}",
        f"شناسه تلگرام: {actor_id(update)}",
        f"نام فروشگاه: {tenant_name}",
        f"پلن: {plan_name}",
        f"ربات مدیریت: @{admin_username}" if admin_username else "ربات مدیریت: آماده",
        f"ربات کاربران: @{user_username}" if user_username else "ربات کاربران: آماده",
    ]
    try:
        await context.bot.send_message(
            chat_id=int(service.master_admin_id),
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🤖 مشاهده ربات مشتری",
                    callback_data=f"tenant:view:{int(tenant_id)}",
                )
            ]]),
        )
    except Exception as exc:
        logger.warning(
            "Master provisioning notification could not be sent: %s",
            safe_format_exception(exc),
        )


async def show_customer_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    service = portal(context)
    customer = service.register_customer(
        actor_id(update),
        display_name=_display_name(update),
        username=(getattr(user, "username", None) if user else None),
    )
    context.user_data.clear()
    if customer["status"] != "active":
        await render(
            update,
            "⛔ حساب شما در حال حاضر غیرفعال است.\n\nبرای بررسی وضعیت با پشتیبانی تماس بگیرید.",
            ReplyKeyboardRemove(),
        )
        return
    settings = service.storefront_settings()
    await render(
        update,
        customer_home_text(
            _display_name(update),
            store_name=str(settings["store_name"]),
            support_contact=str(settings["support_contact"]),
        ),
        customer_main_keyboard(),
    )


async def _show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = portal(context)
    settings = service.storefront_settings()
    if not settings["sales_enabled"]:
        await render(
            update,
            f"🛒 خرید ربات\n\n{settings['maintenance_message']}",
            plans_keyboard([]),
        )
        return
    plans = service.list_public_plans()
    if not plans:
        await render(
            update,
            "🛒 خرید ربات\n\nدر حال حاضر پلن فعالی برای فروش ثبت نشده است.",
            plans_keyboard([]),
        )
        return
    if len(plans) == 1:
        plan = plans[0]
        await render(
            update,
            plan_text(plan),
            plan_keyboard(int(plan["id"])),
        )
        return
    await render(
        update,
        "🛒 خرید ربات\n\nپلن موردنظر را انتخاب کنید:",
        plans_keyboard(plans),
    )


async def _show_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = portal(context).list_services(actor_id(update))
    timezone = str(context.application.bot_data.get("display_timezone") or "Asia/Tehran")
    await render(update, services_text(items, timezone_name=timezone), services_keyboard(items))


async def _show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = portal(context)
    plans = service.list_public_plans()
    currencies = sorted({str(row["currency"]) for row in plans}) or ["USD"]
    lines = ["👛 کیف پول"]
    for currency in currencies:
        lines.append(f"\n💰 موجودی {currency}: {service.wallet_balance(actor_id(update), currency):,}")
    lines.append("\nبرای خرید می‌توانید هنگام ثبت سفارش از موجودی کافی استفاده کنید.")
    rows = [[InlineKeyboardButton(
        f"➕ شارژ کیف پول {currency}", callback_data=f"customer:topup:{currency}"
    )] for currency in currencies]
    rows.extend([
        [InlineKeyboardButton("🧾 سفارش‌های من", callback_data="customer:orders")],
        [InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")],
    ])
    await render(
        update,
        "".join(lines),
        InlineKeyboardMarkup(rows),
    )


async def handle_customer_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> bool:
    """Handle namespaced callbacks; return False for owner/admin callbacks."""
    if not data.startswith("customer:"):
        return False
    service = portal(context)
    actor = actor_id(update)
    try:
        if data == "customer:home":
            await show_customer_home(update, context)
            return True
        if data == "customer:plans":
            await _show_plans(update, context)
            return True
        if data.startswith("customer:plan:"):
            plan = service.get_public_plan(int(data.rsplit(":", 1)[1]))
            await render(update, plan_text(plan), plan_keyboard(int(plan["id"])))
            return True
        if data.startswith("customer:order:"):
            order = service.create_purchase_order(actor, int(data.rsplit(":", 1)[1]))
            methods = service.list_payment_methods(str(order["currency"]))
            balance = service.wallet_balance(actor, str(order["currency"]))
            text = (
                f"✅ سفارش {order['public_id']} ثبت شد.\n\n"
                f"مبلغ: {int(order['amount']):,} {order['currency']}\n"
                "روش پرداخت را انتخاب کنید:"
            )
            if not methods and balance < int(order["amount"]):
                text += "\n\nفعلاً روش پرداخت فعالی برای این ارز تنظیم نشده است."
            await render(update, text, payment_keyboard(order, methods, wallet_balance=balance))
            return True
        if data.startswith("customer:pay:"):
            _, _, order_raw, method_raw = data.split(":", 3)
            order = service.get_order(actor, int(order_raw))
            method = service.get_payment_method(int(method_raw), currency=str(order["currency"]))
            if order["status"] != "pending_payment":
                raise CustomerPortalError("order is not awaiting payment")
            context.user_data["customer_flow"] = {
                "kind": "receipt", "order_id": int(order["id"]),
                "payment_method_id": int(method["id"]),
            }
            await render(
                update,
                payment_instructions(method, order),
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "❌ لغو", callback_data="customer:orders"
                )]]),
            )
            return True
        if data.startswith("customer:walletpay:"):
            order = service.pay_order_from_wallet(actor, int(data.rsplit(":", 1)[1]))
            next_text = (
                f"✅ سفارش {order['public_id']} از کیف پول پرداخت شد."
                if order["kind"] != "renewal"
                else f"✅ تمدید {order['public_id']} با موفقیت انجام شد."
            )
            if order["kind"] != "renewal":
                next_text += "\n\n🔑 حالا از «راه‌اندازی ربات» ادامه دهید."
            await render(
                update,
                next_text,
                setup_keyboard(service.setup_candidates(actor))
                if order["kind"] != "renewal"
                else InlineKeyboardMarkup([[InlineKeyboardButton(
                    "📋 سرویس‌های من", callback_data="customer:services"
                )]]),
            )
            return True
        if data.startswith("customer:topup:"):
            currency = data.rsplit(":", 1)[1].upper()
            if not currency.isalnum() or not 3 <= len(currency) <= 8:
                raise ValueError("invalid currency")
            context.user_data["customer_flow"] = {"kind": "wallet_topup_amount", "currency": currency}
            await render(
                update,
                f"➕ مبلغ شارژ کیف پول ({currency}) را فقط به‌صورت عدد صحیح بفرستید.",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="customer:home")]]),
            )
            return True
        if data == "customer:orders":
            context.user_data.pop("customer_flow", None)
            orders = service.list_orders(actor)
            await render(
                update,
                orders_text(orders),
                orders_keyboard(orders),
            )
            return True
        if data.startswith("customer:cancelorder:confirm:"):
            order_id = int(data.rsplit(":", 1)[1])
            cancelled = service.cancel_order(actor, order_id)
            orders = service.list_orders(actor)
            await render(
                update,
                f"✅ سفارش {cancelled['public_id']} لغو شد.\n\n" + orders_text(orders),
                orders_keyboard(orders),
            )
            return True
        if data.startswith("customer:cancelorder:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = service.get_order(actor, order_id)
            if order["status"] != "pending_payment":
                raise CustomerPortalError("فقط سفارش در انتظار پرداخت قابل لغو است.")
            await render(
                update,
                f"❌ سفارش {order['public_id']} لغو شود؟\n"
                f"مبلغ: {int(order['amount']):,} {order['currency']}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ بله، لغو شود",
                        callback_data=f"customer:cancelorder:confirm:{order_id}",
                    )],
                    [InlineKeyboardButton("↩️ بازگشت", callback_data="customer:orders")],
                ]),
            )
            return True
        if data == "customer:services":
            await _show_services(update, context)
            return True
        if data.startswith("customer:renew:"):
            tenant_id = int(data.rsplit(":", 1)[1])
            owned = {int(item["id"]) for item in service.list_services(actor)}
            if tenant_id not in owned:
                raise NotFoundError("owned service not found")
            plans = service.list_public_plans()
            rows = [[InlineKeyboardButton(
                f"♻️ {plan['name']} · {int(plan['price']):,} {plan['currency']}",
                callback_data=f"customer:reneworder:{tenant_id}:{int(plan['id'])}",
            )] for plan in plans]
            rows.append([InlineKeyboardButton("↩️ سرویس‌ها", callback_data="customer:services")])
            await render(update, "♻️ پلن تمدید را انتخاب کنید:", InlineKeyboardMarkup(rows))
            return True
        if data.startswith("customer:reneworder:"):
            _, _, tenant_raw, plan_raw = data.split(":", 3)
            order = service.create_renewal_order(
                actor, tenant_id=int(tenant_raw), plan_id=int(plan_raw)
            )
            methods = service.list_payment_methods(str(order["currency"]))
            balance = service.wallet_balance(actor, str(order["currency"]))
            await render(
                update,
                f"♻️ سفارش تمدید {order['public_id']}\nمبلغ: {int(order['amount']):,} {order['currency']}",
                payment_keyboard(order, methods, wallet_balance=balance),
            )
            return True
        if data == "customer:setup":
            orders = service.setup_candidates(actor)
            text = "🔑 راه‌اندازی ربات\n\nسفارش آماده را انتخاب کنید:"
            if not orders:
                text = "🔑 راه‌اندازی ربات\n\nسفارش پرداخت‌شده‌ای برای راه‌اندازی وجود ندارد."
            await render(update, text, setup_keyboard(orders))
            return True
        if data.startswith("customer:setup:"):
            order_id = int(data.rsplit(":", 1)[1])
            if order_id not in {int(item["id"]) for item in service.setup_candidates(actor)}:
                raise CustomerPortalError("order is not ready for setup")
            context.user_data["customer_flow"] = {"kind": "setup_details", "order_id": order_id}
            await render(
                update,
                "🔑 راه‌اندازی ربات\n\n"
                "نام فروشگاه یا برند خود را بفرستید.\n\n"
                "مثال: Speed VPN\n\n"
                "شناسه داخلی به‌صورت خودکار ساخته می‌شود.",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="customer:setup")]]),
            )
            return True
        if data == "customer:trial":
            order = service.claim_trial(actor)
            await render(
                update,
                f"🎁 لایسنس تست برای شما رزرو شد ({int(order['trial_days'])} روز).\n"
                "از «راه‌اندازی ربات» دو توکن خود را ثبت کنید.",
                setup_keyboard(service.setup_candidates(actor)),
            )
            return True
        raise ValueError("unknown customer callback")
    except CustomerPortalError as exc:
        logger.warning("Customer callback rejected: %s", safe_format_exception(exc))
        await render(
            update,
            str(exc),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")]]),
        )
        return True
    except (ValueError, sqlite3.IntegrityError, MasterServiceError, ProvisioningError) as exc:
        logger.warning("Customer callback rejected: %s", safe_format_exception(exc))
        await render(
            update,
            "❌ این درخواست قابل انجام نیست؛ وضعیت سفارش یا اطلاعات را بررسی کنید.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی مشتری", callback_data="customer:home")]]),
        )
        return True


async def handle_customer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle customer reply-keyboard buttons and active text flows."""
    if update.effective_message is None:
        return False
    text = str(update.effective_message.text or "").strip()
    service = portal(context)
    actor = actor_id(update)
    try:
        if text == BUY:
            await _show_plans(update, context)
            return True
        if text == SERVICES:
            await _show_services(update, context)
            return True
        if text == SETUP:
            orders = service.setup_candidates(actor)
            message = "🔑 سفارش آماده برای راه‌اندازی را انتخاب کنید:"
            if not orders:
                message = "🔑 سفارش پرداخت‌شده‌ای برای راه‌اندازی وجود ندارد."
            await render(update, message, setup_keyboard(orders))
            return True
        if text == WALLET:
            await _show_wallet(update, context)
            return True
        if text == GUIDE:
            await render(update, guide_text(), customer_main_keyboard())
            return True
        if text == FEATURES:
            await render(update, features_text(service.list_public_plans()), customer_main_keyboard())
            return True
        if text == TRIAL:
            order = service.claim_trial(actor)
            await render(
                update,
                f"🎁 لایسنس تست {int(order['trial_days'])} روزه آماده راه‌اندازی است.",
                setup_keyboard(service.setup_candidates(actor)),
            )
            return True

        flow = context.user_data.get("customer_flow")
        if not isinstance(flow, dict):
            return False
        kind = str(flow.get("kind") or "")
        if kind == "wallet_topup_amount":
            amount = int(text.replace(",", "").strip())
            order = service.create_wallet_topup(actor, currency=str(flow["currency"]), amount=amount)
            context.user_data.pop("customer_flow", None)
            methods = service.list_payment_methods(str(order["currency"]))
            await render(
                update,
                f"➕ سفارش شارژ {order['public_id']} ثبت شد.\n"
                f"مبلغ: {int(order['amount']):,} {order['currency']}\nروش پرداخت را انتخاب کنید:",
                payment_keyboard(order, methods, wallet_balance=0),
            )
            return True
        if kind == "receipt":
            order_id = int(flow["order_id"])
            method_id = int(flow["payment_method_id"])
            receipt = service.submit_receipt(
                actor,
                order_id=order_id,
                payment_method_id=method_id,
                reference=text,
            )
            order = service.get_order(actor, order_id)
            method = service.get_payment_method(method_id, currency=str(order["currency"]))
            context.user_data.pop("customer_flow", None)
            await _notify_master_new_receipt(
                update, context, receipt=receipt, order=order, method=method
            )
            await update.effective_message.reply_text(
                f"✅ رسید #{int(receipt['id'])} ثبت شد و پس از بررسی مدیر نتیجه برای شما ارسال می‌شود.",
                reply_markup=customer_main_keyboard(),
            )
            return True
        if kind == "setup_details":
            name = text.strip()
            if not name or len(name) > 120:
                raise ValueError("invalid store name")
            order_id = int(flow["order_id"])
            generated_slug = f"store-{actor}-{order_id}"
            context.user_data["customer_flow"] = {
                "kind": "setup_admin_token",
                "order_id": order_id,
                "name": name,
                "slug": generated_slug,
            }
            await update.effective_message.reply_text(
                "🤖 توکن ربات مدیریت را از BotFather بفرستید.\n"
                "🔐 پیام حاوی توکن بلافاصله حذف می‌شود."
            )
            return True
        if kind in ("setup_admin_token", "setup_user_token"):
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning("Could not delete customer token message: %s", safe_format_exception(exc))
            role = "admin" if kind == "setup_admin_token" else "user"
            prepared = await service.prepare_customer_bot(
                actor, order_id=int(flow["order_id"]), role=role, plain_token=text
            )
            text = ""
            if role == "admin":
                flow["admin_bot"] = prepared
                flow["kind"] = "setup_user_token"
                admin_username = prepared.telegram_username
                await update.effective_chat.send_message(
                    (
                        f"✅ ربات مدیریت @{admin_username} تأیید شد.\n"
                        if admin_username else "✅ ربات مدیریت تأیید شد.\n"
                    )
                    + "🛍 اکنون توکن ربات کاربران را از BotFather بفرستید."
                )
                return True
            admin_bot = flow.get("admin_bot")
            if not isinstance(admin_bot, PreparedBot):
                raise ProvisioningError("setup draft expired")
            result = service.provision_paid_order(
                actor,
                order_id=int(flow["order_id"]), name=str(flow["name"]), slug=str(flow["slug"]),
                admin_bot=admin_bot, user_bot=prepared,
            )
            context.user_data.pop("customer_flow", None)
            admin_name = result.provisioning.admin_bot.telegram_username
            user_name = result.provisioning.user_bot.telegram_username
            current_order = service.get_order(actor, int(flow["order_id"]))
            await _notify_master_provisioned(
                update,
                context,
                tenant_id=int(result.provisioning.tenant_id),
                tenant_name=str(flow["name"]),
                admin_username=admin_name,
                user_username=user_name,
                plan_name=str(current_order.get("plan_name") or "—"),
            )
            lines = [
                "✅ ربات اختصاصی شما با موفقیت راه‌اندازی شد.",
                "",
                f"🤖 ربات مدیریت: @{admin_name}" if admin_name else "🤖 ربات مدیریت: آماده",
                f"🛍 ربات کاربران: @{user_name}" if user_name else "🛍 ربات کاربران: آماده",
                "",
                "📋 از بخش «سرویس‌های من» می‌توانید وضعیت لایسنس را مشاهده و تمدید کنید.",
            ]
            await update.effective_chat.send_message(
                "\n".join(lines),
                reply_markup=customer_main_keyboard(),
            )
            return True
        return False
    except CustomerPortalError as exc:
        logger.warning("Customer input rejected: %s", safe_format_exception(exc))
        await update.effective_message.reply_text(
            str(exc),
            reply_markup=customer_main_keyboard(),
        )
        return True
    except (ValueError, sqlite3.IntegrityError, MasterServiceError, ProvisioningError) as exc:
        logger.warning("Customer input rejected: %s", safe_format_exception(exc))
        await update.effective_message.reply_text(
            "❌ اطلاعات یا وضعیت درخواست معتبر نیست. دوباره تلاش کنید.",
            reply_markup=customer_main_keyboard(),
        )
        return True


async def handle_customer_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("customer_flow")
    if not isinstance(flow, dict) or flow.get("kind") != "receipt":
        return False
    if update.effective_message is None or not update.effective_message.photo:
        return False
    try:
        service = portal(context)
        actor = actor_id(update)
        order_id = int(flow["order_id"])
        method_id = int(flow["payment_method_id"])
        receipt = service.submit_receipt(
            actor,
            order_id=order_id,
            payment_method_id=method_id,
            reference=str(update.effective_message.caption or "").strip() or None,
            telegram_file_id=str(update.effective_message.photo[-1].file_id),
        )
        order = service.get_order(actor, order_id)
        method = service.get_payment_method(method_id, currency=str(order["currency"]))
        context.user_data.pop("customer_flow", None)
        await _notify_master_new_receipt(
            update, context, receipt=receipt, order=order, method=method
        )
        await update.effective_message.reply_text(
            f"✅ تصویر رسید #{int(receipt['id'])} ثبت شد و در صف بررسی مدیر قرار گرفت.",
            reply_markup=customer_main_keyboard(),
        )
    except CustomerPortalError as exc:
        logger.warning("Customer receipt photo rejected: %s", safe_format_exception(exc))
        await update.effective_message.reply_text(str(exc), reply_markup=customer_main_keyboard())
    except (ValueError, sqlite3.IntegrityError, MasterServiceError) as exc:
        logger.warning("Customer receipt photo rejected: %s", safe_format_exception(exc))
        await update.effective_message.reply_text(
            "❌ ثبت رسید انجام نشد؛ دوباره تلاش کنید.",
            reply_markup=customer_main_keyboard(),
        )
    return True
