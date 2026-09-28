"""Shared handlers for every provisioned TenantAdminBot and TenantUserBot."""

from __future__ import annotations

import sqlite3

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

from Gateway.catalog import RuntimeBotSpec
from Gateway.policy import RuntimePolicy
from Shared.redaction import get_logger, safe_format_exception
from TenantRuntime.state import TenantStateStore
from TenantRuntime.business import TenantBusinessError, TenantBusinessService

logger = get_logger(__name__)


def _services(context: ContextTypes.DEFAULT_TYPE):
    spec = context.application.bot_data.get("runtime_spec")
    policy = context.application.bot_data.get("runtime_policy")
    state = context.application.bot_data.get("runtime_state")
    business = context.application.bot_data.get("tenant_business")
    if not isinstance(spec, RuntimeBotSpec):
        raise RuntimeError("runtime bot specification is unavailable")
    if not isinstance(policy, RuntimePolicy):
        raise RuntimeError("runtime policy is unavailable")
    if not isinstance(state, TenantStateStore):
        raise RuntimeError("runtime state store is unavailable")
    if not isinstance(business, TenantBusinessService):
        raise RuntimeError("tenant business service is unavailable")
    return spec, policy, state, business


def _menu(spec: RuntimeBotSpec) -> InlineKeyboardMarkup:
    if spec.role == "admin":
        rows = [
            [InlineKeyboardButton("🖥 سرورها", callback_data="biz:servers"), InlineKeyboardButton("🔗 نودها", callback_data="biz:nodes")],
            [InlineKeyboardButton("📦 پلن‌های فروش", callback_data="biz:plans"), InlineKeyboardButton("💳 پرداخت", callback_data="biz:payments")],
            [InlineKeyboardButton("🧾 سفارش‌ها", callback_data="biz:orders"), InlineKeyboardButton("🎫 تیکت‌ها", callback_data="biz:tickets")],
            [InlineKeyboardButton("🔗 لینک هوشمند", callback_data="biz:links")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("💳 خرید اشتراک", callback_data="shop:buy"), InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs")],
            [InlineKeyboardButton("🎫 پشتیبانی", callback_data="shop:tickets"), InlineKeyboardButton("📖 راهنما", callback_data="shop:guide")],
        ]
    rows.extend([[InlineKeyboardButton("🏠 منو", callback_data="runtime:home")], [InlineKeyboardButton("📊 وضعیت", callback_data="runtime:status")]])
    return InlineKeyboardMarkup(rows)


async def _deny_update(update: Update, reason: str) -> None:
    if update.callback_query:
        message = (
            "Access denied"
            if reason == "admin_access_denied"
            else "ربات موقتاً در دسترس نیست."
        )
        await update.callback_query.answer(message, show_alert=True)
    elif update.effective_message:
        if reason == "admin_access_denied":
            await update.effective_message.reply_text("Access denied")
        else:
            await update.effective_message.reply_text(
                "⛔ ربات موقتاً در دسترس نیست. لطفاً با مدیر سرویس تماس بگیرید."
            )


async def runtime_access_gate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    spec, policy, _, _ = _services(context)
    user_id = int(update.effective_user.id) if update.effective_user else None
    decision = policy.check(spec, telegram_user_id=user_id)
    if decision.allowed:
        return
    await _deny_update(update, decision.reason)
    raise ApplicationHandlerStop


def _home_text(spec: RuntimeBotSpec, visits: int) -> str:
    if spec.role == "admin":
        return (
            f"⚙️ مدیریت {spec.tenant_name}\n\n"
            "ربات مدیریتی شما فعال است.\n"
            f"ورودهای شما: {visits}"
        )
    return (
        f"👋 به {spec.tenant_name} خوش آمدید.\n\n"
        "ربات فروشگاهی فعال است.\n"
        f"تعداد ورود: {visits}"
    )


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, state_store, business = _services(context)
    user_id = int(update.effective_user.id) if update.effective_user else 0
    state = state_store.load(user_id)
    visits = int(state.get("visits") or 0) + 1
    state_store.save(user_id, {**state, "visits": visits, "screen": "home"})
    if spec.role == "user":
        user = update.effective_user
        business.register_customer(user_id, display_name=str(getattr(user, "full_name", None) or "کاربر"), username=getattr(user, "username", None))
    text = _home_text(spec, visits)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_menu(spec))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_menu(spec))


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, policy, state_store, _ = _services(context)
    user_id = int(update.effective_user.id) if update.effective_user else 0
    decision = policy.check(spec, telegram_user_id=user_id)
    if not decision.allowed:
        await _deny_update(update, decision.reason)
        raise ApplicationHandlerStop
    state = state_store.load(user_id)
    state_store.save(user_id, {**state, "screen": "status"})
    role_label = "AdminBot" if spec.role == "admin" else "UserBot"
    text = (
        f"📊 وضعیت ربات\n\n"
        f"مجموعه: {spec.tenant_name}\n"
        f"نوع: {role_label}\n"
        f"لایسنس: {decision.license_status}\n"
        "Runtime: ready"
    )
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
    actor = int(update.effective_user.id) if update.effective_user else 0
    try:
        if spec.role == "admin":
            if data == "biz:servers":
                rows = [[InlineKeyboardButton(f"🖥 {item['label']} · {item['panel_kind']} · {item['status']}", callback_data=f"biz:server:{item['id']}")] for item in business.list_servers()]
                rows += [[InlineKeyboardButton("➕ سرور", callback_data="biz:addserver")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]]
                await update.callback_query.edit_message_text("🖥 سرورهای این tenant", reply_markup=InlineKeyboardMarkup(rows)); return
            if data == "biz:addserver":
                context.user_data["biz_flow"] = {"kind": "server"}
                await update.callback_query.edit_message_text("نام | نوع پنل (manual/hiddify/xui) | آدرس اختیاری", reply_markup=_menu(spec)); return
            if data.startswith("biz:server:"):
                server_id = int(data.rsplit(":", 1)[1])
                server = business.server(server_id)
                panel = business.panel_status(server_id)
                text = (
                    f"🖥 {server['label']}\n"
                    f"نوع پنل: {server['panel_kind']}\n"
                    f"آدرس: {server.get('endpoint') or 'ثبت نشده'}\n"
                    f"کلید دسترسی: {'✅ ثبت شده' if panel['configured'] else '❌ ثبت نشده'}"
                )
                rows = [[InlineKeyboardButton("🔐 ثبت یا تعویض کلید پنل", callback_data=f"biz:secret:{server_id}")]]
                rows.append([InlineKeyboardButton("↩️ سرورها", callback_data="biz:servers")])
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
            if data.startswith("biz:secret:"):
                server_id = int(data.rsplit(":", 1)[1])
                business.server(server_id)
                context.user_data["biz_flow"] = {"kind": "panel_secret", "server_id": server_id}
                await update.callback_query.edit_message_text(
                    "کلید API یا رمز پنل را بفرستید. پیام شما پس از ثبت حذف می‌شود.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ سرورها", callback_data="biz:servers")]]),
                ); return
            if data == "biz:nodes":
                items = business.list_nodes(); text = "🔗 نودها\n" + ("\n".join(f"• {x['label']} · {x.get('location') or '-'}" for x in items) or "موردی نیست.")
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ نود", callback_data="biz:addnode")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]])); return
            if data == "biz:addnode":
                context.user_data["biz_flow"] = {"kind": "node"}; await update.callback_query.edit_message_text("نام نود | لوکیشن اختیاری", reply_markup=_menu(spec)); return
            if data == "biz:plans":
                items = business.list_plans(public=False); text = "📦 پلن‌های فروش\n" + ("\n".join(f"• {x['name']} · {x['traffic_gb']}GB · {x['duration_days']} روز · {x['price']:,} {x['currency']}" for x in items) or "موردی نیست.")
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
                text = "🧾 سفارش‌ها\n" + ("\n".join(f"#{x['id']} · {x['display_name']} · {x['plan_name']} · {x['status']}" for x in items) or "موردی نیست.")
                rows = [[InlineKeyboardButton(f"✅/❌ بررسی رسید #{x['id']} · {x['display_name']}", callback_data=f"biz:receipt:{x['id']}")] for x in receipts]
                rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")])
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
            if data.startswith("biz:receipt:"):
                receipt_id = int(data.rsplit(":", 1)[1]); receipt = next((x for x in business.list_receipts_admin(actor) if int(x['id']) == receipt_id), None)
                if receipt is None: raise TenantBusinessError("receipt not found")
                context.user_data["biz_confirm"] = {"receipt_id": receipt_id}
                await update.callback_query.edit_message_text(f"رسید #{receipt_id}\nمشتری: {receipt['display_name']}\nمبلغ: {receipt['amount']:,} {receipt['currency']}\nپیگیری: {receipt.get('reference') or 'تصویر'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید", callback_data="biz:receiptconfirm:yes")], [InlineKeyboardButton("❌ رد", callback_data="biz:receiptconfirm:no")], [InlineKeyboardButton("↩️ سفارش‌ها", callback_data="biz:orders")]])); return
            if data.startswith("biz:receiptconfirm:"):
                pending = context.user_data.pop("biz_confirm", None)
                if not isinstance(pending, dict): raise TenantBusinessError("confirmation expired")
                approve = data.rsplit(":", 1)[1] == "yes"
                business.review_receipt(actor, int(pending['receipt_id']), approve=approve)
                await update.callback_query.edit_message_text("✅ رسید بررسی شد." if approve else "❌ رسید رد شد.", reply_markup=_menu(spec)); return
            if data == "biz:tickets":
                items = business.list_tickets_admin(actor); text = "🎫 تیکت‌ها\n" + ("\n".join(f"#{x['id']} · {x['display_name']} · {x['subject']} · {x['status']}" for x in items) or "موردی نیست.")
                await update.callback_query.edit_message_text(text, reply_markup=_menu(spec)); return
            if data == "biz:links":
                items = business.list_smart_links(actor); text = "🔗 لینک‌های هوشمند\n" + ("\n".join(f"• {x['label']}: /start {x['code']}" for x in items) or "موردی نیست.")
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ لینک", callback_data="biz:addlink")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]])); return
            if data == "biz:addlink":
                context.user_data["biz_flow"] = {"kind": "link"}; await update.callback_query.edit_message_text("نام لینک | مقصد (مثال: buy)", reply_markup=_menu(spec)); return
        else:
            if data == "shop:buy":
                plans = business.list_plans(); rows = [[InlineKeyboardButton(f"{p['name']} · {p['price']:,} {p['currency']}", callback_data=f"shop:plan:{p['id']}")] for p in plans] or [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
                rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]); await update.callback_query.edit_message_text("💳 خرید اشتراک", reply_markup=InlineKeyboardMarkup(rows)); return
            if data.startswith("shop:plan:"):
                order = business.create_order(actor, int(data.rsplit(":", 1)[1])); methods = business.list_methods(currency=str(order['currency']))
                rows = [[InlineKeyboardButton(f"{m['title']} ({m['kind']})", callback_data=f"shop:pay:{order['id']}:{m['id']}")] for m in methods] or [[InlineKeyboardButton("روش پرداخت موجود نیست", callback_data="noop")]]
                await update.callback_query.edit_message_text(f"سفارش #{order['id']} · {order['amount']:,} {order['currency']}\nروش پرداخت را انتخاب کنید.", reply_markup=InlineKeyboardMarkup(rows)); return
            if data.startswith("shop:pay:"):
                _, _, order_id, method_id = data.split(":", 3); order = business.order(actor, int(order_id)); method = business.method(int(method_id), currency=str(order['currency']))
                context.user_data["biz_flow"] = {"kind": "receipt", "order_id": int(order_id), "method_id": int(method_id)}
                await update.callback_query.edit_message_text(f"پرداخت به: {method['destination']}\n{method.get('instructions') or ''}\nکد پیگیری یا عکس رسید را ارسال کنید.", reply_markup=_menu(spec)); return
            if data == "shop:subs":
                items = business.list_subscriptions(actor); text = "📦 اشتراک‌های من\n" + ("\n".join(f"• {x['plan_name']} · {x['status']} · {x['expires_at']}" for x in items) or "اشتراکی ندارید.")
                await update.callback_query.edit_message_text(text, reply_markup=_menu(spec)); return
            if data == "shop:tickets":
                context.user_data["biz_flow"] = {"kind": "ticket"}; await update.callback_query.edit_message_text("موضوع | متن تیکت را ارسال کنید.", reply_markup=_menu(spec)); return
            if data == "shop:guide":
                await update.callback_query.edit_message_text("📖 راهنما\nپلن را انتخاب کنید، پرداخت را ثبت کنید و پس از تأیید، اشتراک شما در صف فعال‌سازی قرار می‌گیرد.", reply_markup=_menu(spec)); return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.callback_query.answer("درخواست قابل انجام نیست.", show_alert=True); return
    await update.callback_query.answer("این دکمه معتبر نیست.", show_alert=True)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, _, business = _services(context)
    actor = int(update.effective_user.id) if update.effective_user else 0
    flow = context.user_data.get("biz_flow")
    text = str(update.effective_message.text or "").strip() if update.effective_message else ""
    try:
        if isinstance(flow, dict):
            fields = [part.strip() for part in text.split("|")]
            kind = flow.get("kind")
            if kind == "panel_secret":
                if not text:
                    raise ValueError("empty secret")
                business.set_panel_credential(actor, server_id=int(flow["server_id"]), secret=text)
                try:
                    await update.effective_message.delete()
                except Exception:
                    pass
            elif kind == "server" and 2 <= len(fields) <= 3:
                business.add_server(actor, label=fields[0], panel_kind=fields[1], endpoint=fields[2] if len(fields) == 3 else "")
            elif kind == "node" and 1 <= len(fields) <= 2:
                business.add_node(actor, label=fields[0], location=fields[1] if len(fields) == 2 else "")
            elif kind == "plan" and len(fields) == 5:
                business.add_plan(actor, name=fields[0], traffic_gb=int(fields[1]), duration_days=int(fields[2]), price=int(fields[3]), currency=fields[4])
            elif kind == "payment" and len(fields) == 6:
                business.add_payment_method(actor, kind=fields[0], title=fields[1], currency=fields[2], destination=fields[3], network=fields[4], instructions=fields[5])
            elif kind == "link" and len(fields) == 2:
                business.create_smart_link(actor, label=fields[0], target=fields[1])
            elif kind == "receipt":
                business.submit_receipt(actor, order_id=int(flow['order_id']), method_id=int(flow['method_id']), reference=text)
            elif kind == "ticket" and len(fields) == 2:
                business.create_ticket(actor, subject=fields[0], body=fields[1])
            else:
                raise ValueError("invalid input")
            context.user_data.pop("biz_flow", None)
            await update.effective_message.reply_text("✅ ذخیره شد.", reply_markup=_menu(spec)); return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ قالب یا وضعیت معتبر نیست.", reply_markup=_menu(spec)); return
    if update.effective_message:
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.", reply_markup=_menu(spec)
        )


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a photo only while this user has an owned receipt flow."""
    if update.effective_message is None or not update.effective_message.photo:
        return
    spec, _, _, business = _services(context)
    flow = context.user_data.get("biz_flow")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if spec.role != "user" or not isinstance(flow, dict) or flow.get("kind") != "receipt":
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_menu(spec)); return
    try:
        business.submit_receipt(actor, order_id=int(flow['order_id']), method_id=int(flow['method_id']), reference=str(update.effective_message.caption or "").strip() or None, telegram_file_id=str(update.effective_message.photo[-1].file_id))
        context.user_data.pop("biz_flow", None)
        await update.effective_message.reply_text("✅ تصویر رسید برای بررسی ارسال شد.", reply_markup=_menu(spec))
    except (ValueError, TenantBusinessError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ ثبت تصویر رسید انجام نشد.", reply_markup=_menu(spec))


async def runtime_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec = context.application.bot_data.get("runtime_spec")
    bot_id = spec.bot_id if isinstance(spec, RuntimeBotSpec) else 0
    tenant_id = spec.tenant_id if isinstance(spec, RuntimeBotSpec) else 0
    logger.error(
        "Tenant runtime error tenant=%s bot=%s: %s",
        tenant_id,
        bot_id,
        safe_format_exception(context.error or RuntimeError()),
    )


def register_runtime_handlers(application: Application) -> None:
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
