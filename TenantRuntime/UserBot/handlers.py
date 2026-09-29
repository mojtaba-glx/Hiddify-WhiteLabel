"""Tenant UserBot handlers.

All end-user shop menus, callbacks, receipts and text flows live in this module.
Tenant-admin management handlers are intentionally kept out of this package.
"""

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



def _user_account_text(summary: dict) -> str:
    customer = dict(summary.get("customer") or {})
    subs = dict(summary.get("subscriptions") or {})
    username = str(customer.get("username") or "").strip()
    return "\n".join([
        "👤 حساب من",
        f"نام: {customer.get('display_name') or 'کاربر'}",
        f"یوزرنیم: {'@' + username.lstrip('@') if username else '-'}",
        f"Telegram ID: {customer.get('telegram_user_id')}",
        f"وضعیت حساب: {customer.get('status')}",
        "",
        "📦 اشتراک‌ها",
        f"• فعال: {int(subs.get('active') or 0)}",
        f"• غیرفعال: {int(subs.get('disabled') or 0)}",
        f"• منقضی: {int(subs.get('expired') or 0)}",
        f"• در انتظار فعال‌سازی: {int(subs.get('pending') or 0)}",
        f"🧾 سفارش باز: {int(summary.get('pending_orders') or 0)}",
        "",
        "💰 پرداخت‌های تأییدشده",
        *_money_lines(list(summary.get("paid_totals") or [])),
    ])


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
    return "\n".join([
        f"🧾 {op_title} سفارش #{order['id']}",
        f"پلن: {order.get('plan_name') or '-'}",
        f"مبلغ اصلی: {original:,} {order.get('currency') or ''}",
        f"تخفیف: {discount:,} {order.get('currency') or ''}",
        f"مبلغ نهایی: {final:,} {order.get('currency') or ''}",
        f"کیف پول: {balance:,} {order.get('currency') or ''}",
    ])



def _menu(spec: RuntimeBotSpec) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💳 خرید اشتراک", callback_data="shop:buy"), InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs")],
        [InlineKeyboardButton("👤 حساب من", callback_data="shop:account"), InlineKeyboardButton("🧾 سفارش‌های من", callback_data="shop:orders")],
        [InlineKeyboardButton("💰 کیف پول", callback_data="shop:wallet"), InlineKeyboardButton("🤝 دعوت دوستان", callback_data="shop:referral")],
        [InlineKeyboardButton("🎁 تست رایگان", callback_data="shop:trial"), InlineKeyboardButton("🎫 پشتیبانی", callback_data="shop:tickets")],
        [InlineKeyboardButton("📖 راهنما", callback_data="shop:guide")],
        [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
        [InlineKeyboardButton("📊 وضعیت", callback_data="runtime:status")],
    ]
    return InlineKeyboardMarkup(rows)


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, state_store, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot handler registered for non-user role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
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
    if args:
        payload = str(args[0] or "").strip()
        if payload.startswith("ref_"):
            try:
                business.register_referral(user_id, referral_code=payload[4:])
            except TenantBusinessError:
                pass
    text = (
        f"👋 به {spec.tenant_name} خوش آمدید.\n\n"
        "ربات فروشگاهی فعال است.\n"
        f"تعداد ورود: {visits}"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_menu(spec))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_menu(spec))


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, policy, state_store, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot handler registered for non-user role")
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
        "نوع: UserBot\n"
        f"لایسنس: {decision.license_status}\n"
        "Runtime: ready"
    )
    try:
        summary = business.customer_account_summary(user_id)
        subs = dict(summary.get("subscriptions") or {})
        text = (
            base
            + "\n\n📦 وضعیت حساب"
            + f"\nاشتراک فعال: {int(subs.get('active') or 0)}"
            + f"\nمنقضی: {int(subs.get('expired') or 0)}"
            + f"\nسفارش باز: {int(summary.get('pending_orders') or 0)}"
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
    if spec.role != "user":
        raise RuntimeError("UserBot callback registered for non-user role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    try:
        if data == "shop:account":
            summary = business.customer_account_summary(actor)
            await update.callback_query.edit_message_text(
                _user_account_text(summary),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs"),
                        InlineKeyboardButton("🧾 سفارش‌های من", callback_data="shop:orders"),
                    ],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:orders":
            items = business.list_customer_orders(actor, limit=15)
            labels = {"purchase": "خرید", "renewal": "تمدید", "trial": "تست رایگان"}
            text = "🧾 سفارش‌های من\n" + (
                "\n".join(
                    f"• #{x['id']} · {labels.get(str(x.get('operation')), 'خرید')} · "
                    f"{x.get('plan_name') or '-'} · {x.get('status')}\n"
                    f"  {int(x.get('amount') or 0):,} {x.get('currency') or ''}"
                    f"{' · تخفیف: '+format(int(x.get('discount_amount') or 0), ',') if int(x.get('discount_amount') or 0) else ''}"
                    f"{' · پرداخت: ' + str(x.get('paid_at')) if x.get('paid_at') else ''}"
                    for x in items
                )
                or "سفارشی ندارید."
            )
            rows = []
            for x in items:
                if x["status"] == "paid":
                    rows.append([InlineKeyboardButton(
                        f"🔁 تلاش فعال‌سازی سفارش #{x['id']}",
                        callback_data=f"shop:retryorder:{x['id']}",
                    )])
                elif x["status"] == "pending_payment":
                    rows.append([InlineKeyboardButton(
                        f"❌ لغو سفارش #{x['id']}",
                        callback_data=f"shop:cancelorder:{x['id']}",
                    )])
            rows.extend([
                [InlineKeyboardButton("👤 حساب من", callback_data="shop:account")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ])
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:retryorder:"):
            order_id = int(data.rsplit(":", 1)[1])
            result = business.retry_own_paid_order(actor, order_id=order_id)
            await update.callback_query.edit_message_text(
                f"✅ سفارش #{order_id} انجام شد.\n🔗 {result.get('subscription_url') or '-'}",
                reply_markup=_menu(spec),
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
        if data == "shop:referral":
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
            result = business.claim_free_trial(actor)
            await update.callback_query.edit_message_text(
                "✅ تست رایگان فعال شد.\n"
                f"🔗 {result.get('subscription_url') or '-'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data == "shop:buy":
            plans = business.list_plans(); rows = [[InlineKeyboardButton(f"{p['name']} · {p['price']:,} {p['currency']}", callback_data=f"shop:plan:{p['id']}")] for p in plans] or [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
            rows.append([InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]); await update.callback_query.edit_message_text("💳 خرید اشتراک", reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("shop:plan:"):
            order = business.create_order(actor, int(data.rsplit(":", 1)[1]))
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎟 کد تخفیف", callback_data=f"shop:coupon:{order['id']}"),
                        InlineKeyboardButton("💰 پرداخت کیف پول", callback_data=f"shop:walletpay:{order['id']}"),
                    ],
                    [InlineKeyboardButton("💳 روش‌های پرداخت", callback_data=f"shop:paymethods:{order['id']}")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data.startswith("shop:checkout:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎟 کد تخفیف", callback_data=f"shop:coupon:{order_id}"),
                        InlineKeyboardButton("💰 پرداخت کیف پول", callback_data=f"shop:walletpay:{order_id}"),
                    ],
                    [InlineKeyboardButton("💳 روش‌های پرداخت", callback_data=f"shop:paymethods:{order_id}")],
                    [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                ]),
            ); return
        if data.startswith("shop:coupon:"):
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
                    f"✅ سفارش #{order_id} با کیف پول پرداخت و فعال شد.\n"
                    f"🔗 {result.get('subscription_url') or '-'}"
                )
            await update.callback_query.edit_message_text(
                text, reply_markup=_menu(spec)
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
            await update.callback_query.edit_message_text(f"پرداخت به: {method['destination']}\n{method.get('instructions') or ''}\nکد پیگیری یا عکس رسید را ارسال کنید.", reply_markup=_menu(spec)); return
        if data == "shop:subs":
            items = business.list_subscriptions(actor)
            text = "📦 اشتراک‌های من\n" + ("\n".join(
                f"• #{x['id']} · {x['plan_name']} · "
                f"{'در حال قطع خودکار' if int(x.get('enforcement_pending') or 0) else x['status']}\n"
                f"  مصرف: {int(x['usage_bytes']) / (1024**3):.2f}/{int(x['traffic_bytes']) / (1024**3):.0f}GB · "
                f"انقضا: {x['expires_at']}\n"
                f"  آخرین اتصال: {x.get('last_online') or '-'}"
                for x in items
            ) or "اشتراکی ندارید.")
            rows = []
            for item in items:
                if item.get("external_ref") and item.get("server_id") and item["status"] in ("active", "disabled", "expired"):
                    rows.append([InlineKeyboardButton(
                        f"♻️ تمدید اشتراک #{item['id']}",
                        callback_data=f"shop:renew:{item['id']}"
                    )])
                if item["status"] == "active" and item.get("external_ref") and item.get("server_id"):
                    try:
                        link = business.subscription_link(actor, subscription_id=int(item["id"]))
                        rows.append([InlineKeyboardButton(f"🔗 لینک اشتراک #{item['id']}", url=link)])
                    except TenantBusinessError:
                        pass
            rows.extend(_menu(spec).inline_keyboard)
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("shop:renew:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            owned = next((x for x in business.list_subscriptions(actor) if int(x["id"]) == subscription_id), None)
            if owned is None or owned["status"] not in ("active", "disabled", "expired"):
                raise TenantBusinessError("subscription cannot be renewed")
            plans = business.list_plans()
            rows = [[InlineKeyboardButton(
                f"{p['name']} · {p['traffic_gb']}GB · {p['duration_days']} روز · {p['price']:,} {p['currency']}",
                callback_data=f"shop:renewplan:{subscription_id}:{p['id']}"
            )] for p in plans] or [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
            rows.append([InlineKeyboardButton("↩️ اشتراک‌های من", callback_data="shop:subs")])
            await update.callback_query.edit_message_text(
                f"♻️ پلن تمدید اشتراک #{subscription_id} را انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:renewplan:"):
            _, _, subscription_id, plan_id = data.split(":", 3)
            order = business.create_renewal_order(
                actor,
                subscription_id=int(subscription_id),
                plan_id=int(plan_id),
            )
            await update.callback_query.edit_message_text(
                _checkout_text(order, business.wallet_summary(actor)),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎟 کد تخفیف", callback_data=f"shop:coupon:{order['id']}"),
                        InlineKeyboardButton("💰 پرداخت کیف پول", callback_data=f"shop:walletpay:{order['id']}"),
                    ],
                    [InlineKeyboardButton("💳 روش‌های پرداخت", callback_data=f"shop:paymethods:{order['id']}")],
                    [InlineKeyboardButton("↩️ اشتراک‌های من", callback_data="shop:subs")],
                ]),
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
            await update.callback_query.edit_message_text(
                "📖 راهنما\nبرای خرید، پلن را انتخاب کنید و در Checkout می‌توانید کد تخفیف، کیف پول یا روش پرداخت را بزنید. "
                "برای تمدید از «اشتراک‌های من» روی ♻️ تمدید بزنید. تست رایگان فقط یک‌بار و برای کاربر بدون خرید قبلی است. "
                "پاداش دعوت دوستان به کیف پول اضافه می‌شود. سرویس منقضی بدون تمدید دوباره فعال نمی‌شود.",
                reply_markup=_menu(spec),
            ); return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.callback_query.answer("درخواست قابل انجام نیست.", show_alert=True)
        return
    await update.callback_query.answer("این دکمه معتبر نیست.", show_alert=True)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, _, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot text handler registered for non-user role")
    actor = int(update.effective_user.id) if update.effective_user else 0
    flow = context.user_data.get("biz_flow")
    text = str(update.effective_message.text or "").strip() if update.effective_message else ""
    try:
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
                reply_markup=_menu(spec),
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
            await update.effective_message.reply_text("✅ ذخیره شد.", reply_markup=_menu(spec))
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ قالب یا وضعیت معتبر نیست.", reply_markup=_menu(spec))
        return
    if update.effective_message:
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_menu(spec))


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a photo only while this user has an owned receipt flow."""
    if update.effective_message is None or not update.effective_message.photo:
        return
    spec, _, _, business = _services(context)
    flow = context.user_data.get("biz_flow")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if spec.role != "user" or not isinstance(flow, dict) or flow.get("kind") not in ("receipt", "wallet_receipt"):
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_menu(spec)); return
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
        await update.effective_message.reply_text(success_text, reply_markup=_menu(spec))
    except (ValueError, TenantBusinessError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("❌ ثبت تصویر رسید انجام نشد.", reply_markup=_menu(spec))



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

