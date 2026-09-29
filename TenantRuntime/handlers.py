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
            op = "تمدید" if order.get("operation") == "renewal" else "خرید"
            lines.append(
                f"• #{order['id']} · {op} · {order.get('plan_name') or '-'} · "
                f"{order.get('status')} · {int(order.get('amount') or 0):,} "
                f"{order.get('currency') or ''}"
            )
    return "\n".join(lines)


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
            [InlineKeyboardButton("📊 داشبورد", callback_data="biz:dashboard"), InlineKeyboardButton("📈 گزارش فروش", callback_data="biz:reports")],
            [InlineKeyboardButton("🖥 سرورها", callback_data="biz:servers"), InlineKeyboardButton("🔗 نودها", callback_data="biz:nodes")],
            [InlineKeyboardButton("📦 پلن‌های فروش", callback_data="biz:plans"), InlineKeyboardButton("💳 پرداخت", callback_data="biz:payments")],
            [InlineKeyboardButton("🧾 سفارش‌ها", callback_data="biz:orders"), InlineKeyboardButton("📡 سرویس‌ها", callback_data="biz:subs")],
            [InlineKeyboardButton("👥 مشتریان", callback_data="biz:customers"), InlineKeyboardButton("⚠️ نیازمند بررسی", callback_data="biz:attention")],
            [InlineKeyboardButton("🎯 فروش پیشرفته", callback_data="biz:growth"), InlineKeyboardButton("🔗 لینک هوشمند", callback_data="biz:links")],
            [InlineKeyboardButton("🎫 تیکت‌ها", callback_data="biz:tickets")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("💳 خرید اشتراک", callback_data="shop:buy"), InlineKeyboardButton("📦 اشتراک‌های من", callback_data="shop:subs")],
            [InlineKeyboardButton("👤 حساب من", callback_data="shop:account"), InlineKeyboardButton("🧾 سفارش‌های من", callback_data="shop:orders")],
            [InlineKeyboardButton("💰 کیف پول", callback_data="shop:wallet"), InlineKeyboardButton("🤝 دعوت دوستان", callback_data="shop:referral")],
            [InlineKeyboardButton("🎁 تست رایگان", callback_data="shop:trial"), InlineKeyboardButton("🎫 پشتیبانی", callback_data="shop:tickets")],
            [InlineKeyboardButton("📖 راهنما", callback_data="shop:guide")],
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
                    business.register_referral(
                        user_id, referral_code=payload[4:]
                    )
                except TenantBusinessError:
                    pass
    text = _home_text(spec, visits)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_menu(spec))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_menu(spec))


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, policy, state_store, business = _services(context)
    user_id = int(update.effective_user.id) if update.effective_user else 0
    decision = policy.check(spec, telegram_user_id=user_id)
    if not decision.allowed:
        await _deny_update(update, decision.reason)
        raise ApplicationHandlerStop
    state = state_store.load(user_id)
    state_store.save(user_id, {**state, "screen": "status"})
    role_label = "AdminBot" if spec.role == "admin" else "UserBot"
    base = (
        f"📊 وضعیت ربات\n\n"
        f"مجموعه: {spec.tenant_name}\n"
        f"نوع: {role_label}\n"
        f"لایسنس: {decision.license_status}\n"
        "Runtime: ready"
    )
    try:
        if spec.role == "admin":
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
        else:
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
    actor = int(update.effective_user.id) if update.effective_user else 0
    try:
        if spec.role == "admin":
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
                rows = [[InlineKeyboardButton(f"{'⭐ ' if int(item.get('is_default') or 0) else '🖥 '}{item['label']} · {item['panel_kind']} · {item['status']}", callback_data=f"biz:server:{item['id']}")] for item in business.list_servers()]
                rows += [[InlineKeyboardButton("➕ سرور", callback_data="biz:addserver")], [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")]]
                await update.callback_query.edit_message_text("🖥 سرورهای این tenant\n⭐ = سرور پیش‌فرض فروش", reply_markup=InlineKeyboardMarkup(rows)); return
            if data == "biz:addserver":
                context.user_data["biz_flow"] = {"kind": "server"}
                await update.callback_query.edit_message_text(
                    "فرمت Hiddify:\n"
                    "نام | hiddify | آدرس | مسیر ادمین | مسیر کاربر\n\n"
                    "فرمت X-UI:\n"
                    "نام | xui | آدرس پنل | sanaei/alireza | inboundها | آدرس عمومی اشتراک | مسیر اشتراک\n"
                    "inbound خالی = اولین inbound، عدد 0 = همه، یا مثل 1,2,3\n"
                    "مسیر اشتراک در صورت خالی بودن /sub/ است.\n\n"
                    "فرمت X-Net:\n"
                    "نام | xnet | آدرس API/پنل | inboundها | دامنه عمومی اشتراک | پورت اشتراک | مسیر اشتراک\n"
                    "شناسه inbound می‌تواند مثل in-9457dabf باشد؛ 0 = همه.\n"
                    "پورت خالی/0 = 2096 و مسیر خالی = sub.",
                    reply_markup=_menu(spec),
                ); return
            if data.startswith("biz:server:"):
                server_id = int(data.rsplit(":", 1)[1])
                server = business.server(server_id)
                panel = business.panel_status(server_id)
                provider_info = ""
                if server["panel_kind"] == "xui":
                    provider_info = (
                        f"\nنسخه X-UI: {server.get('xui_flavor') or '-'}"
                        f"\nInboundها: {server.get('xui_inbound_ids') or 'اولین فعال'}"
                        f"\nآدرس عمومی اشتراک: {server.get('xui_public_origin') or 'خود دامنه پنل'}"
                        f"\nمسیر اشتراک: {server.get('xui_sub_path') or '/sub/'}"
                    )
                elif server["panel_kind"] == "xnet":
                    provider_info = (
                        f"\nInboundها: {server.get('xnet_inbound_ids') or 'اولین فعال'}"
                        f"\nآدرس عمومی اشتراک: {server.get('xnet_public_origin') or 'دامنه API/پنل'}"
                        f"\nپورت اشتراک: {server.get('xnet_sub_port') or 2096}"
                        f"\nمسیر اشتراک: {server.get('xnet_sub_path') or 'sub'}"
                    )
                text = (
                    f"🖥 {server['label']}\n"
                    f"نوع پنل: {server['panel_kind']}\n"
                    f"آدرس: {server.get('endpoint') or 'ثبت نشده'}\n"
                    f"مسیر ادمین: {server.get('admin_path') or '-'}\n"
                    f"مسیر کاربر: {server.get('user_path') or '-'}"
                    f"{provider_info}\n"
                    f"کلید دسترسی: {'✅ ثبت شده' if panel['configured'] else '❌ ثبت نشده'}\n"
                    f"سرور پیش‌فرض فروش: {'⭐ بله' if int(server.get('is_default') or 0) else 'خیر'}"
                )
                rows = [[InlineKeyboardButton("🔐 ثبت یا تعویض دسترسی پنل", callback_data=f"biz:secret:{server_id}")]]
                if server["panel_kind"] in ("hiddify", "xui", "xnet") and panel["configured"] and not int(server.get("is_default") or 0):
                    rows.append([InlineKeyboardButton("⭐ انتخاب به عنوان سرور فروش", callback_data=f"biz:defaultserver:{server_id}")])
                rows.append([InlineKeyboardButton("↩️ سرورها", callback_data="biz:servers")])
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows)); return
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
        else:
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
                text = "🧾 سفارش‌های من\n" + (
                    "\n".join(
                        f"• #{x['id']} · "
                        f"{'تمدید' if x.get('operation') == 'renewal' else 'خرید'} · "
                        f"{x.get('plan_name') or '-'} · {x.get('status')}\n"
                        f"  {int(x.get('amount') or 0):,} {x.get('currency') or ''}"
                        f"{' · پرداخت: ' + str(x.get('paid_at')) if x.get('paid_at') else ''}"
                        for x in items
                    )
                    or "سفارشی ندارید."
                )
                await update.callback_query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("👤 حساب من", callback_data="shop:account")],
                        [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
                    ]),
                ); return
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
                methods = business.list_methods(currency=str(order["currency"]))
                rows = [[InlineKeyboardButton(
                    f"{m['title']} ({m['kind']})",
                    callback_data=f"shop:pay:{order['id']}:{m['id']}"
                )] for m in methods] or [[InlineKeyboardButton("روش پرداخت موجود نیست", callback_data="noop")]]
                await update.callback_query.edit_message_text(
                    f"تمدید سفارش #{order['id']} · {order['plan_name']} · {order['amount']:,} {order['currency']}\n"
                    "روش پرداخت را انتخاب کنید.",
                    reply_markup=InlineKeyboardMarkup(rows),
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
                    "📖 راهنما\nبرای خرید، پلن و روش پرداخت را انتخاب کنید. برای تمدید از «اشتراک‌های من» روی ♻️ تمدید بزنید. "
                    "سرویس منقضی بدون تمدید دوباره فعال نمی‌شود. مصرف و آخرین اتصال به‌صورت دوره‌ای از پنل همگام می‌شود.",
                    reply_markup=_menu(spec),
                ); return
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
