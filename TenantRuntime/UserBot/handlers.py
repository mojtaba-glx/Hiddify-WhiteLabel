"""Tenant UserBot handlers.

All end-user shop menus, callbacks, receipts and text flows live in this module.
Tenant-admin management handlers are intentionally kept out of this package.
"""

from __future__ import annotations

import asyncio
import random
import re
import sqlite3
from html import escape
from io import BytesIO

from telegram.error import BadRequest, TelegramError
from contextvars import ContextVar
from typing import Any

from Shared.redaction import get_logger

logger = get_logger(__name__)

from telegram import (
    Bot,
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

from Database.repositories import BotRepository
from Gateway.catalog import RuntimeBotSpec
from TenantRuntime.business import DEFAULT_REFERRAL_INVITE_TEXT, TenantBusinessError
from TenantRuntime.button_styles import keyboard_button as KeyboardButton
from Shared.crypto import fingerprint_token
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


def _format_text_template(template: object, **kwargs: Any) -> str:
    """Format admin-managed text without allowing a bad placeholder to break UI."""
    try:
        return str(template or "").format(**kwargs)
    except (KeyError, ValueError, IndexError):
        return str(template or "")


def _setting_text(
    settings: dict[str, Any],
    key: str,
    fallback: str,
    **kwargs: Any,
) -> str:
    raw = str(settings.get(key) or "").strip() or str(fallback)
    return _format_text_template(raw, **kwargs).strip()


def _ticket_panel_body(
    settings: dict[str, Any],
    items: list[dict[str, Any]],
) -> str:
    intro = _setting_text(
        settings,
        "ticket_panel_text",
        "📩 برای ارتباط با پشتیبانی، پیام خود را ارسال کنید.",
    )
    history = (
        "\n".join(
            f"• #{x['id']} · {x['subject']} · {x['status']}"
            f"{' · پاسخ داده شد' if x.get('admin_reply') else ''}"
            for x in items[:15]
        )
        or ""
    )
    return f"{intro}\n\n{history}".strip()


def _ticket_status_label(status: object) -> str:
    return {
        "open": "📨 در انتظار پاسخ",
        "answered": "📬 باز / پاسخ‌داده",
        "closed": "📩 بسته",
    }.get(str(status or ""), str(status or "-"))


def _ticket_thread_text(
    ticket: dict[str, Any],
    messages: list[dict[str, Any]],
) -> str:
    lines = [
        f"🎫 تیکت #{int(ticket['id'])}",
        f"وضعیت: {_ticket_status_label(ticket.get('status'))}",
        f"موضوع: {ticket.get('subject') or '-'}",
        "",
    ]
    if not messages:
        lines.append("هنوز پیامی ثبت نشده است.")
    for item in messages[-30:]:
        who = "👤 شما" if item.get("sender_type") == "user" else "🛟 پشتیبانی"
        lines.append(f"{who}:")
        body = str(item.get("message_text") or "").strip()
        if body:
            lines.append(body)
        if item.get("has_media"):
            lines.append("🖼 تصویر پیوست دارد")
        lines.append("")
    result = "\n".join(lines).strip()
    if len(result) > 3800:
        header = "\n".join(lines[:4]).strip()
        tail_size = max(500, 3800 - len(header) - 16)
        result = header + "\n\n…\n" + result[-tail_size:]
    return result


def _ticket_detail_markup(
    ticket: dict[str, Any],
    messages: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    ticket_id = int(ticket["id"])
    rows: list[list[TelegramInlineKeyboardButton]] = []
    media_buttons = [
        InlineKeyboardButton(
            f"🖼 تصویر پیام #{int(item['id'])}",
            callback_data=f"shop:ticketmedia:{ticket_id}:{int(item['id'])}",
        )
        for item in messages
        if item.get("has_media")
    ]
    for i in range(0, len(media_buttons), 2):
        rows.append(media_buttons[i:i+2])
    if str(ticket.get("status") or "") != "closed":
        rows.append([InlineKeyboardButton(
            "📩پاسخ",
            callback_data=f"shop:ticketreply:{ticket_id}",
        )])
        rows.append([InlineKeyboardButton(
            "🚫بستن تیکت",
            callback_data=f"shop:ticketclose:{ticket_id}",
        )])
    rows.append([InlineKeyboardButton(
        "🔙بازگشت", callback_data="shop:tickets"
    )])
    return InlineKeyboardMarkup(rows)


def _ticket_confirm_markup(mode: str) -> InlineKeyboardMarkup:
    clean = "reply" if mode == "reply" else "new"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ارسال", callback_data=f"shop:ticketflow:{clean}:send"),
            InlineKeyboardButton("✏️ویرایش", callback_data=f"shop:ticketflow:{clean}:edit"),
        ],
        [InlineKeyboardButton("❌لغو", callback_data=f"shop:ticketflow:{clean}:cancel")],
    ])


def _ticket_skip_markup(mode: str) -> InlineKeyboardMarkup:
    clean = "reply" if mode == "reply" else "new"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️رد کردن", callback_data=f"shop:ticketflow:{clean}:skip")],
        [InlineKeyboardButton("❌لغو", callback_data=f"shop:ticketflow:{clean}:cancel")],
    ])


def _ticket_preview_text(flow: dict[str, Any], *, mode: str) -> str:
    if mode == "reply":
        return (
            "👁 پیش‌نمایش پاسخ تیکت\n\n"
            f"{str(flow.get('reply_text') or '').strip()}\n\n"
            f"🖼 تصویر: {'دارد' if flow.get('photo_file_id') else 'ندارد'}"
        )
    return (
        "👁 پیش‌نمایش تیکت\n\n"
        f"موضوع: {str(flow.get('subject') or '').strip()}\n\n"
        f"{str(flow.get('body') or '').strip()}\n\n"
        f"🖼 تصویر: {'دارد' if flow.get('photo_file_id') else 'ندارد'}"
    )


async def _show_ticket_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: dict[str, Any],
    *,
    mode: str,
) -> None:
    text = _ticket_preview_text(flow, mode=mode)
    markup = _ticket_confirm_markup(mode)
    photo_id = str(flow.get("photo_file_id") or "").strip()
    if photo_id and update.effective_chat is not None:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=photo_id,
            caption="🖼 پیش‌نمایش تصویر پیوست تیکت",
        )
        if update.effective_message is not None:
            await update.effective_message.reply_text(text, reply_markup=markup)
        return
    if update.callback_query is not None:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def _ticket_media_bytes(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> bytes:
    if not str(file_id or "").strip():
        return b""
    tg_file = await context.bot.get_file(str(file_id))
    return bytes(await tg_file.download_as_bytearray())


def _sibling_admin_bot_token(business: Any) -> str:
    if business.secret_cipher is None:
        raise TenantBusinessError("AdminBot encryption is unavailable")
    row = BotRepository(business.conn).get_by_tenant_role(
        business.tenant_id, "admin"
    )
    if row is None or row.get("status") != "active":
        raise TenantBusinessError("AdminBot is not active")
    plain = business.secret_cipher.decrypt(str(row["encrypted_token"]))
    if fingerprint_token(plain) != str(row["token_fingerprint"]):
        raise TenantBusinessError("AdminBot credential integrity failed")
    return plain


async def _send_purchase_event_report(
    business: Any,
    *,
    telegram_id: int,
    display_name: str,
    order: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Best-effort SellBot-style purchase/renew event delivery."""
    settings = business.runtime_userbot_settings()
    if not bool(settings.get("purchase_event_channel_enabled", False)):
        return
    target = str(settings.get("purchase_event_channel_id") or "").strip()
    if not target:
        return
    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        action = (
            "تمدید اشتراک"
            if str(result.get("operation") or order.get("operation") or "") == "renewal"
            else "خرید اشتراک"
        )
        plan_name = str(order.get("plan_name") or "-")
        server_title = str(order.get("selected_server_label") or "-")
        amount = int(order.get("amount") or 0)
        currency = str(order.get("currency") or "")
        service_code = str(
            result.get("smart_code")
            or result.get("id")
            or order.get("id")
            or "-"
        )
        text = (
            "📣 گزارش رویداد اشتراک\n"
            f"🔖 نوع عملیات: {action}\n"
            f"👤 کاربر: {display_name}\n"
            f"🆔 شناسه تلگرام: {int(telegram_id)}\n"
            f"🏷 نام اشتراک: {plan_name}\n"
            f"🛰 سرور: {server_title}\n"
            f"📊حجم: {float(order.get('traffic_gb') or 0):.1f} گیگابایت\n"
            f"⏳زمان: {int(order.get('duration_days') or 0)} روز\n"
            f"💰مبلغ: {amount:,} {currency}\n"
            f"🔑شناسه اشتراک:{service_code}"
        )
        chat_target: Any = (
            int(target) if target.lstrip("-").isdigit() else target
        )
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=chat_target, text=text)
    except Exception:
        # Event channels are observational only and must never break checkout.
        return
    finally:
        token = ""


async def _notify_admin_ticket(
    business: Any,
    *,
    ticket: dict[str, Any],
    event: str,
) -> None:
    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        title = "🆕 تیکت جدید" if event == "new" else "💬 پاسخ جدید تیکت"
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=(
                    f"{title}\n"
                    f"🎫 #{int(ticket['id'])}\n"
                    f"👤 {ticket.get('display_name') or ticket.get('username') or ticket.get('telegram_user_id') or '-'}\n"
                    f"موضوع: {ticket.get('subject') or '-'}"
                ),
            )
    except Exception:
        return
    finally:
        token = ""


def _receipt_toman(amount: object, currency: object) -> tuple[int, str]:
    """Display amount for admin reports (IRR rials -> toman)."""
    try:
        value = int(amount or 0)
    except (TypeError, ValueError):
        value = 0
    if str(currency or "").upper() == "IRR":
        return value // 10, "تومان"
    return value, str(currency or "")


def _receipt_report_content(
    *,
    payment_key: str,
    display_name: str,
    customer_id: int = 0,
    amount: int = 0,
    currency: str = "",
    method_title: str = "کارت به کارت",
    last4: str = "",
    reference: str = "",
    settings: dict[str, Any] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """SellBot-style admin payment report caption + approve/reject buttons."""
    style_settings = dict(
        settings or {"colored_buttons": True, "button_theme": "smart"}
    )
    toman, unit = _receipt_toman(amount, currency)
    caption = "\n".join([
        "💸گزارش تایید پرداخت🕊",
        "",
        f"🔖شیوه پرداخت:{method_title}",
        f"🔑شناسه تراکنش:{payment_key}",
        f"💰مبلغ پرداخت:{toman:,} {unit}",
        *(
            [f"💳۴ رقم آخر کارت مبدا: {last4}"]
            if str(last4 or "").strip()
            else []
        ),
        *(
            [f"🧾 پیگیری: {str(reference).strip()}"]
            if str(reference or "").strip()
            else []
        ),
    ])
    rows: list[list[TelegramInlineKeyboardButton]] = [[
        _button(
            "رد ❌",
            callback_data=f"userbot:pay:review:{payment_key}:no",
            settings=style_settings,
        ),
        _button(
            "تایید ✅",
            callback_data=f"userbot:pay:review:{payment_key}:yes",
            settings=style_settings,
        ),
    ]]
    if int(customer_id or 0) > 0:
        rows.append([_button(
            f"👤 {display_name}",
            callback_data=f"userbot:user:{int(customer_id)}",
            settings=style_settings,
        )])
    return caption, InlineKeyboardMarkup(rows)


async def _notify_admin_receipt(
    business: Any,
    *,
    payment_key: str,
    display_name: str,
    customer_id: int = 0,
    amount: int = 0,
    currency: str = "",
    method_title: str = "کارت به کارت",
    last4: str = "",
    reference: str = "",
    photo_file_id: str = "",
    photo_bytes: bytes | None = None,
) -> None:
    """Best-effort SellBot-style payment report to the AdminBot.

    Carries working approve/reject buttons (userbot:pay:review:*) so the
    admin can review without opening the payments menu. Never raises.

    NOTE: Telegram file_ids are bot-specific, so a UserBot file_id is
    useless to the AdminBot. Like SellBot, prefer raw bytes (downloaded
    with the UserBot and re-uploaded here).
    """
    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        caption, markup = _receipt_report_content(
            payment_key=payment_key,
            display_name=display_name,
            customer_id=customer_id,
            amount=amount,
            currency=currency,
            method_title=method_title,
            last4=last4,
            reference=reference,
        )
        async with Bot(token=token) as bot:
            payload = bytes(photo_bytes or b"")
            if payload:
                try:
                    stream = BytesIO(payload)
                    stream.name = "receipt.jpg"
                    await bot.send_photo(
                        chat_id=int(business.owner_telegram_id),
                        photo=stream,
                        caption=caption,
                        reply_markup=markup,
                    )
                    return
                except (BadRequest, TelegramError) as exc:
                    logger.warning(
                        "Admin receipt photo upload failed (%s bytes): %s",
                        len(payload),
                        type(exc).__name__,
                    )
            elif str(photo_file_id or "").strip():
                try:
                    await bot.send_photo(
                        chat_id=int(business.owner_telegram_id),
                        photo=str(photo_file_id).strip(),
                        caption=caption,
                        reply_markup=markup,
                    )
                    return
                except (BadRequest, TelegramError) as exc:
                    # UserBot file_ids are unusable by the AdminBot; bytes
                    # forwarding (photo_bytes) is the supported path.
                    logger.warning(
                        "Admin receipt photo by file_id failed: %s",
                        type(exc).__name__,
                    )
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=caption,
                reply_markup=markup,
            )
    except Exception as exc:
        logger.warning(
            "Admin receipt report failed: %s", type(exc).__name__
        )
        return
    finally:
        token = ""


async def _notify_receipt_submitted(
    update: Update,
    business: Any,
    actor: int,
    flow: dict[str, Any],
    *,
    receipt_id: int,
    file_id: str = "",
    photo_bytes: bytes | None = None,
    context=None,
) -> None:
    """Gather receipt context and push the SellBot-style admin report."""
    if context is not None:
        try:
            seen = context.bot_data.setdefault("receipt_push_sent", set())
            if int(receipt_id) in seen:
                logger.warning(
                    "Skipping duplicate admin push for receipt %s",
                    int(receipt_id),
                )
                return
        except Exception:
            seen = None
    else:
        seen = None
    try:
        kind = str(flow.get("kind") or "")
        last4 = str(flow.get("last4") or "")
        display_amount = int(flow.get("display_amount") or 0)
        currency = ""
        method_title = "کارت به کارت"
        customer_id = 0
        display_name = ""
        if kind == "wallet_receipt":
            topup = business.wallet_topup(actor, int(flow["topup_id"]))
            currency = str(topup.get("currency") or "")
            if not display_amount:
                display_amount = int(topup.get("amount") or 0)
        else:
            order = business.order(actor, int(flow["order_id"]))
            currency = str(order.get("currency") or "")
            if not display_amount:
                display_amount = int(order.get("amount") or 0)
        try:
            customer = business._customer(actor)
            customer_id = int(customer.get("id") or 0)
            display_name = str(
                customer.get("display_name") or customer.get("username") or ""
            ).strip()
        except (TenantBusinessError, ValueError):
            customer = None
        if not display_name and update.effective_user is not None:
            display_name = str(
                getattr(update.effective_user, "full_name", None) or actor
            ).strip()
        try:
            method = business.method(int(flow["method_id"]), currency=None)
            method_title = str(
                method.get("provider_title")
                or ("کارت به کارت" if str(method.get("kind") or "") == "card" else method.get("kind"))
                or "کارت به کارت"
            )
        except (TenantBusinessError, ValueError):
            pass
        await _notify_admin_receipt(
            business,
            payment_key=(
                f"wallet_topup:{int(receipt_id)}"
                if kind == "wallet_receipt"
                else f"order:{int(receipt_id)}"
            ),
            display_name=display_name or str(actor),
            customer_id=customer_id,
            amount=display_amount,
            currency=currency,
            method_title=method_title,
            last4=last4,
            photo_file_id=file_id,
            photo_bytes=photo_bytes,
        )
        logger.info(
            "Admin receipt push sent receipt=%s photo=%s",
            int(receipt_id),
            bool(photo_bytes),
        )
        if seen is not None:
            try:
                seen.add(int(receipt_id))
            except Exception:
                pass
    except Exception:
        return


def _referral_content(
    business: Any,
    actor: int,
    context: ContextTypes.DEFAULT_TYPE,
    settings: dict[str, Any],
) -> dict[str, Any]:
    summary = business.referral_summary(actor)
    referral_settings = dict(summary.get("settings") or {})
    bot_username = str(getattr(context.bot, "username", None) or "").strip()
    code = str(summary.get("referral_code") or "").strip()
    invite_link = (
        f"https://t.me/{bot_username}?start=ref_{code}"
        if bot_username and code
        else (f"/start ref_{code}" if code else "—")
    )
    currency = str(referral_settings.get("referral_currency") or "IRR")
    trial_enabled = bool(
        referral_settings.get("referral_trial_reward_enabled", True)
    )
    purchase_enabled = bool(
        referral_settings.get("referral_purchase_reward_enabled", True)
    )
    trial_amount = int(referral_settings.get("referral_trial_reward") or 0)
    purchase_amount = int(
        referral_settings.get("referral_purchase_reward") or 0
    )
    trial_reward = (
        f"{trial_amount:,} {currency}" if trial_enabled else "غیرفعال"
    )
    purchase_reward = (
        f"{purchase_amount:,} {currency}" if purchase_enabled else "غیرفعال"
    )

    variables = {
        "invite_link": invite_link,
        "trial_reward": trial_reward,
        "purchase_reward": purchase_reward,
        "referred_count": int(summary.get("referred_count") or 0),
    }

    # Phase 10 adds an optional referral-specific page text.  When it is not
    # configured, preserve the Phase 7 invite_info_text + invite_text behavior
    # exactly so existing tenant customizations remain live.
    custom_referral_text = str(
        referral_settings.get("referral_invite_text") or ""
    ).strip()
    if custom_referral_text:
        intro = _format_text_template(
            custom_referral_text,
            **variables,
        ).strip()
    else:
        invite_info_text = _setting_text(
            settings,
            "invite_info_text",
            "🎁 دوستان خود را دعوت کنید و از پاداش‌های فعال بهره‌مند شوید.",
            **variables,
        )
        invite_text = _setting_text(
            settings,
            "invite_text",
            "💌 لینک دعوت شما:\n{invite_link}",
            **variables,
        )
        intro = "\n\n".join(
            item for item in (invite_info_text, invite_text) if item
        ).strip()
        if not intro:
            intro = _format_text_template(
                DEFAULT_REFERRAL_INVITE_TEXT,
                **variables,
            ).strip()

    banner_text = _setting_text(
        settings,
        "invite_banner_text",
        "🎁 بنر دعوت اختصاصی شما\n\n🔗 لینک دعوت شما:\n{invite_link}",
        **variables,
    )
    labels = {
        "trial": "پاداش تست",
        "purchase": "پاداش خرید",
        "manual": "پاداش دستی",
    }
    rewards = list(summary.get("rewards") or [])
    reward_lines = [
        f"• {labels.get(str(x.get('reward_type') or ''), str(x.get('reward_type') or ''))}: "
        f"{int(x.get('amount') or 0):,} {x.get('currency') or ''} "
        f"({int(x.get('count') or 0)} مورد)"
        for x in rewards
    ] or ["• هنوز پاداشی ثبت نشده است."]
    body = "\n".join([
        intro,
        "",
        f"👥 دعوت موفق ثبت‌شده: {variables['referred_count']}",
        "",
        "🎁 پاداش‌های دریافت‌شده",
        *reward_lines,
    ]).strip()
    return {
        "body": body,
        "banner_text": banner_text,
        "photo_id": str(settings.get("invite_banner_photo_id") or "").strip(),
        "invite_link": invite_link,
    }


def _invite_home_markup() -> InlineKeyboardMarkup:
    """SellBot-style دعوت دوستان keyboard (5 buttons)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 لینک دعوت من", callback_data="shop:invitebanner")],
        [
            InlineKeyboardButton("🎁 جوایز من", callback_data="shop:inviterewards"),
            InlineKeyboardButton("👥 دعوت‌های من", callback_data="shop:invitelist"),
        ],
        [
            InlineKeyboardButton("📊 آمار دعوت", callback_data="shop:invitestats"),
            InlineKeyboardButton("📜 تاریخچه جوایز", callback_data="shop:invitehistory"),
        ],
    ])


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
    if metrics["unlimited_volume"]:
        usage_line = f"{metrics['usage_gb']:.1f} از نامحدود"
    elif metrics["limit_gb"] > 0:
        usage_line = (
            f"{metrics['usage_gb']:.1f} از "
            f"{metrics['limit_gb']:.1f} گیگ"
        )
    else:
        usage_line = f"{metrics['usage_gb']:.1f} گیگ"

    if metrics["unlimited_time"]:
        days_text = "نامحدود"
    elif metrics["days_left"] is None:
        days_text = "نامشخص"
    else:
        days_text = f"{int(metrics['days_left'])} روز"

    code = str(item.get("service_code") or "").strip() or str(item.get("id"))
    lines = ["📄اطلاعات اشتراک شما", ""]
    if bool(settings.get("show_username", True)):
        lines.append(
            f"👤نام: {_subscription_name(item)}"
        )
    lines.extend([
        f"📡سرور: {str(item.get('server_label') or 'نامشخص')}",
        f"📊میزان استفاده: {usage_line}",
        f"⏳زمان باقی مانده: {days_text}",
        f"💰قیمت اشتراک: {_subscription_price(item)}",
        f"🔑شناسه: `{code}`",
    ])
    return "\n".join(lines)


def _subscription_name(item: dict[str, Any]) -> str:
    return str(item.get("service_name") or item.get("plan_name") or f"اشتراک #{item['id']}")


def _subscription_price(item: dict[str, Any]) -> str:
    value = item.get("subscription_price")
    if value is None:
        return "نامشخص"
    try:
        price = max(0, int(value))
    except (TypeError, ValueError):
        return "نامشخص"
    currency = str(item.get("subscription_currency") or "").upper()
    if currency == "IRR":
        toman = price // 10
        if toman >= 1000:
            toman = (toman // 1000) * 1000
        return f"{toman:,} تومان"
    if not currency:
        return "نامشخص"
    return f"{price:,} {currency}"


def _subscription_status_rows(business, actor, item, settings):
    sid = int(item["id"])
    return [
        [InlineKeyboardButton("کانفیگ ها📝", callback_data=f"shop:configmenu:{sid}")],
        [InlineKeyboardButton("تمدید اشتراک♾", callback_data=f"shop:renew:{sid}")],
        [InlineKeyboardButton("تغییر نام اشتراک✏️", callback_data=f"shop:subrename:{sid}")],
        [InlineKeyboardButton("تغییر لینک اشتراک🚨", callback_data=f"shop:subrotate:{sid}")],
    ]


def _subscription_list(business, actor, settings, *, view="all", page=0, items=None):
    if items is None:
        status = view if view in {
            "active", "expired", "disabled", "pending_provisioning"
        } else None
        items = business.list_subscriptions(
            actor,
            status=status,
            limit=500,
            offset=0,
        )
    rows = [
        [InlineKeyboardButton(
            _subscription_name(item)[:60],
            callback_data=f"shop:substatus:{int(item['id'])}",
        )]
        for item in items
    ]
    rows.append([
        InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home")
    ])
    text = (
        "👇 لطفا یکی از اشتراک‌های خود را انتخاب نمایید"
        if items
        else "❌ اشتراک وجود ندارد."
    )
    return text, InlineKeyboardMarkup(rows)


_USER_ERROR_ALERTS = {
    "purchase is disabled": "🚫 خرید غیرفعال است.",
    "renewal is disabled": "🚫 تمدید غیرفعال است.",
    "no purchase server": "❌ سروری در دسترس نیست.",
    "purchase server is unavailable": "❌ سرور قابل خریدی موجود نیست.",
    "quote expired": "❌ زمان نشست تمام شده، لطفا دوباره تلاش کنید.",
    "wrong renewal server": "❌ سرور تمدید معتبر نیست.",
    "plan is not available on selected server": "❌ این پلن روی سرور انتخاب‌شده موجود نیست.",
    "subscription cannot be renewed": "❌ این اشتراک قابل تمدید نیست.",
    "order is not awaiting payment": "❌ سفارش در انتظار پرداخت نیست.",
    "gift is disabled": "🚫 کد هدیه غیرفعال است.",
    "referral is disabled": "🚫 دعوت دوستان غیرفعال است.",
    "ticket flow is not active": "❌ جریان تیکت فعال نیست.",
    "invalid ticket flow": "❌ جریان تیکت معتبر نیست.",
    "renewal policy does not allow renewal": "❌ تمدید این اشتراک مجاز نیست.",
}


def _user_error_alert(exc: BaseException) -> str:
    """Map known business errors to SellBot-style alerts."""
    return _USER_ERROR_ALERTS.get(str(exc).strip(), "درخواست قابل انجام نیست.")


async def _reply_md(message, text: str, reply_markup=None, **kwargs):
    """Send with Markdown; fall back to plain text on parse errors."""
    try:
        await message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown", **kwargs
        )
    except BadRequest:
        await message.reply_text(text, reply_markup=reply_markup, **kwargs)


async def _edit_md(query, text: str, reply_markup=None, **kwargs):
    """Edit with Markdown; fall back to plain text on parse errors."""
    try:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown", **kwargs
        )
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return
        if _is_photo_edit_error(err):
            await _replace_photo_with_text(query, text, reply_markup=reply_markup, **kwargs)
            return
        await query.edit_message_text(text, reply_markup=reply_markup, **kwargs)


def _is_photo_edit_error(err: str) -> bool:
    """True when edit_message_text fails because the message is a photo/QR."""
    return any(
        needle in err
        for needle in (
            "there is no text in the message to edit",
            "message is not a text message",
            "message can't be edited",
            "message to edit not found",
        )
    )


async def _replace_photo_with_text(query, text: str, reply_markup=None, **kwargs):
    """SellBot _safe_edit_message_text parity: delete the photo, send text."""
    kwargs.pop("parse_mode", None)
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    await query.message.reply_text(
        text, reply_markup=reply_markup, disable_web_page_preview=True, **kwargs
    )


async def _safe_edit_text(query, text: str, reply_markup=None, **kwargs):
    """Edit text when possible; photo/QR messages fall back to a new message."""
    try:
        await query.edit_message_text(
            text, reply_markup=reply_markup, disable_web_page_preview=True, **kwargs
        )
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return
        if _is_photo_edit_error(err):
            await _replace_photo_with_text(query, text, reply_markup=reply_markup, **kwargs)
            return
        raise


async def _edit_subscription(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return
        if _is_photo_edit_error(err):
            await _replace_photo_with_text(
                query, text,
                reply_markup=kwargs.get("reply_markup"),
            )
            return
        raise


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


def _wallet_currency_title(currency: object) -> str:
    code = str(currency or "").strip().upper()
    return "تومان" if code in {"IRR", "IRT"} else (code or "تومان")


def _wallet_primary(summary: dict) -> tuple[int, str]:
    primary = dict(summary.get("primary_account") or {})
    if primary:
        return int(primary.get("balance") or 0), str(
            primary.get("currency") or summary.get("primary_currency") or "IRR"
        )
    accounts = list(summary.get("accounts") or [])
    if accounts:
        row = accounts[0]
        return int(row.get("balance") or 0), str(row.get("currency") or "IRR")
    return 0, str(summary.get("primary_currency") or "IRR")


def _wallet_text(summary: dict, settings: dict | None = None) -> str:
    """SellBot-compatible wallet summary: one balance + account status.

    The status line follows the marketing ``show_user_status`` switch like
    SellBot (UserBot/main.py wallet branch).
    """
    balance, currency = _wallet_primary(summary)
    money = f"{balance:,} {_wallet_currency_title(currency)}"
    text = f"🔻 موجودی کیف پول شما {money} میباشد"
    if settings is None or bool(settings.get("show_user_status", True)):
        status = str(summary.get("customer_status") or "active").lower()
        status_title = "🔴 مسدود" if status == "blocked" else "🟢 فعال"
        text += f"\n👤 وضعیت کاربر: {status_title}"
    return text


def _wallet_method_rows(
    business: Any,
    settings: dict,
    *,
    include_back: bool = True,
):
    """SellBot wallet_inline_keyboard parity: methods + gift coupon + back.

    SellBot has no payment-history button in the wallet menu.
    """
    rows = []
    for method in business.list_methods(currency=None):
        title = str(method.get("title") or method.get("provider_title") or "روش پرداخت")
        icon = str(method.get("provider_icon") or "💳")
        rows.append([
            InlineKeyboardButton(
                f"{icon} {title}",
                callback_data=f"shop:wallettopupstart:{int(method['id'])}",
            )
        ])
    if bool(settings.get("show_gift_button", True)):
        rows.append([
            InlineKeyboardButton("🎁 اعمال کد هدیه", callback_data="shop:gift")
        ])
    if include_back:
        rows.append([
            InlineKeyboardButton("بازگشت", callback_data="runtime:home")
        ])
    return rows


def _order_price_display(order: dict) -> tuple[int, str]:
    """Display price (IRR rials -> toman), mirroring the plan builder."""
    try:
        amount = int(order.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    currency = str(order.get("currency") or "")
    if currency.upper() == "IRR":
        return amount // 10, "تومان"
    return amount, currency


def _summary_back_callback(order: dict) -> str:
    """SellBot-parity back target from the plan summary screen."""
    try:
        subid = int(order.get("renewal_subscription_id") or 0)
    except (TypeError, ValueError):
        subid = 0
    if subid > 0:
        return f"shop:renew:{subid}"
    try:
        server_id = int(order.get("selected_server_id") or 0)
    except (TypeError, ValueError):
        server_id = 0
    if server_id > 0:
        return f"shop:buyloc:{server_id}"
    return "shop:buy"


def _order_summary_text(order: dict) -> str:
    """SellBot selected-plan summary (no order internals shown)."""
    try:
        gb = int(order.get("traffic_gb") or 0)
    except (TypeError, ValueError):
        gb = 0
    try:
        days = int(order.get("duration_days") or 0)
    except (TypeError, ValueError):
        days = 0
    price, unit = _order_price_display(order)
    lines = [
        "📄 اطلاعات پلن انتخاب شده",
        "",
        f"📊 حجم: {gb} گیگ",
        f"⏳ زمان: {days} روز",
        f"💰 قیمت: {price:,} {unit}",
    ]
    return "\n".join(lines)


def _coupon_usable(settings: dict | None) -> bool:
    """SellBot parity: discount OR increase code enables the coupon flow."""
    values = dict(settings or {})
    return bool(values.get("enable_discount_code")) or bool(
        values.get("enable_increase_code")
    )


def _order_summary_markup(
    order_id: int,
    back_callback: str,
    settings: dict | None,
    *,
    show_coupon: bool = False,
) -> InlineKeyboardMarkup:
    """SellBot selected-plan buttons: direct pay, wallet pay, back."""
    rows = [
        [_button(
            "💳 پرداخت مستقیم",
            callback_data=f"shop:paymethods:{int(order_id)}",
            settings=settings,
        )],
        [_button(
            "💰 پرداخت از کیف پول",
            callback_data=f"shop:walletpay:{int(order_id)}",
            settings=settings,
        )],
    ]
    if show_coupon:
        rows.append([
            _button(
                "🎟 کد تخفیف",
                callback_data=f"shop:coupon:{int(order_id)}",
                settings=settings,
            )
        ])
    rows.append([_button(
        "بازگشت",
        callback_data=back_callback,
        settings=settings,
    )])
    return InlineKeyboardMarkup(rows)


async def _send_order_summary(
    query, business: Any, actor: int, settings: dict, order_id: int
) -> None:
    """Render the SellBot-style plan summary for a pending order."""
    order = business.order(actor, int(order_id))
    await query.edit_message_text(
        _order_summary_text(order),
        reply_markup=_order_summary_markup(
            int(order_id),
            _summary_back_callback(order),
            settings,
            show_coupon=_coupon_usable(settings),
        ),
    )


async def _start_order_payment(
    update, context, *, business: Any, actor: int, settings: dict,
    order_id: int, method_id: int,
) -> None:
    """Begin card/gateway payment for an order (SellBot instruction style)."""
    session = business.begin_order_payment(
        actor,
        order_id=int(order_id),
        method_id=int(method_id),
    )
    order = dict(session.get("order") or business.order(actor, int(order_id)))
    rows: list[list[TelegramInlineKeyboardButton]] = []
    text, parse_mode, tx_marker, display_rial = _receipt_start_content(
        session,
        amount=int(order.get("amount") or 0),
        currency=str(order.get("currency") or ""),
        settings=settings,
    )
    if session["action"] == "receipt":
        context.user_data["biz_flow"] = {
            "kind": "receipt",
            "order_id": int(order_id),
            "method_id": int(method_id),
            "confirmed": False,
            "tx_marker": tx_marker,
            "display_amount": display_rial,
            "is_card": str(
                dict(session.get("method") or {}).get("kind")
                or dict(session.get("method") or {}).get("provider_key")
                or ""
            ) in {"card", "card_manual"},
        }
    else:
        context.user_data.pop("biz_flow", None)
    if parse_mode == "HTML":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        await update.callback_query.answer()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=_receipt_confirm_keyboard(),
        )
        return
    if session.get("checkout_url"):
        rows.append([InlineKeyboardButton(
            "🌐 ورود به درگاه پرداخت",
            url=str(session["checkout_url"]),
        )])
    rows.append([InlineKeyboardButton(
        "↩️ سفارش",
        callback_data=f"shop:checkout:{int(order_id)}",
    )])
    await update.callback_query.edit_message_text(
        str(session["message"]),
        reply_markup=InlineKeyboardMarkup(rows),
    )


BTN_RECEIPT_DONE = "✅ پرداخت کردم، ارسال رسید"
BTN_RECEIPT_BACK = "بازگشت"


def _receipt_confirm_keyboard() -> ReplyKeyboardMarkup:
    """SellBot parity: confirm on top, red back below."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_RECEIPT_DONE)],
            [KeyboardButton(BTN_RECEIPT_BACK, style="danger")],
        ],
        resize_keyboard=True,
    )


def _receipt_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_RECEIPT_BACK, style="danger")]],
        resize_keyboard=True,
    )


def _split_card_destination(
    destination: object, network: object = ""
) -> tuple[str, str, str]:
    """Split admin-entered 'number | owner | bank' card destination."""
    parts = [part.strip() for part in str(destination or "").split("|")]
    number = parts[0] if len(parts) > 0 else ""
    owner = parts[1] if len(parts) > 1 else ""
    bank = parts[2] if len(parts) > 2 else str(network or "").strip()
    return number, owner, bank


def _parse_card_last4(value: object) -> str:
    """Accept only exactly 4 digits (Persian digits normalized)."""
    from TenantRuntime.UserBot.sellbot_utils import _to_en_digits

    raw = _to_en_digits(str(value or "")).strip()
    return raw if re.fullmatch(r"\d{4}", raw) else ""


def _receipt_last4_required(flow: dict[str, Any], settings: dict[str, Any]) -> bool:
    """SellBot parity: card receipts need the payer's last 4 digits."""
    if not bool(dict(settings or {}).get("require_last4_for_card_receipt", False)):
        return False
    if str(flow.get("last4") or ""):
        return False
    return bool(flow.get("is_card", False))


def _combine_receipt_reference(tracking: object, last4: object) -> str | None:
    ref = str(tracking or "").strip()
    tail = str(last4 or "").strip()
    if ref and tail:
        return f"{ref} | {tail}"
    return ref or tail or None


async def _download_receipt_bytes(context, file_id: str) -> bytes:
    """Fetch receipt bytes with the UserBot (AdminBot can't use its file_id)."""
    fid = str(file_id or "").strip()
    if not fid:
        return b""
    try:
        tg_file = await context.bot.get_file(fid)
        return bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning(
            "Receipt bytes download failed: %s", type(exc).__name__
        )
        return b""


async def _receipt_go_back(update, business: Any, actor: int, spec, flow: dict) -> None:
    """Back from the receipt step returns to the previous screen.

    The order/topup stays alive (nothing is cancelled); only the staged
    receipt input is discarded.
    """
    for key in (
        "awaiting_last4", "pending_ref", "pending_file_id", "last4",
        "confirmed",
    ):
        flow.pop(key, None)
    kind = str(flow.get("kind") or "")
    settings = business.runtime_userbot_settings()
    if kind == "wallet_receipt":
        try:
            topup = business.wallet_topup(actor, int(flow["topup_id"]))
            methods = business.list_methods(
                currency=str(topup.get("currency") or "")
            )
        except (TenantBusinessError, ValueError):
            topup, methods = {}, []
        rows = [[InlineKeyboardButton(
            f"{m.get('provider_icon') or '💳'} {m['title']} · {m.get('provider_title') or m['kind']}",
            callback_data=f"shop:wallettopupmethod:{int(flow.get('topup_id') or 0)}:{m['id']}",
        )] for m in methods]
        if not rows:
            rows = [[InlineKeyboardButton(
                "روش پرداخت موجود نیست", callback_data="noop"
            )]]
        rows.append([
            InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")
        ])
        try:
            amount = int((topup or {}).get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        currency = str((topup or {}).get("currency") or "")
        await update.effective_message.reply_text(
            f"شارژ #{int(flow.get('topup_id') or 0)} · {amount:,} {currency}\n"
            "روش پرداخت را انتخاب کنید.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    order = business.order(actor, int(flow["order_id"]))
    await update.effective_message.reply_text(
        _order_summary_text(order),
        reply_markup=_order_summary_markup(
            int(flow["order_id"]),
            _summary_back_callback(order),
            settings,
            show_coupon=_coupon_usable(settings),
        ),
    )


def _card_instruction_text(
    *,
    amount_rial: int,
    card_number: str,
    card_owner: str,
    card_bank: str,
    tx_marker: int = 0,
    template: str = "",
) -> str:
    """SellBot _build_card_to_card_payment_text parity (HTML).

    WhiteLabel IRR amounts are rials, so toman = rial // 10.
    A custom admin template supports {CARD}/{HOLDER}/{BANK}/{AMOUNT}/{RIAL}.
    """
    rial = int(amount_rial or 0)
    toman = rial // 10
    tpl = str(template or "").strip()
    if tpl and tpl != "0":
        body = (
            tpl.replace("{CARD}", escape(str(card_number or "-")))
            .replace("{HOLDER}", escape(str(card_owner or "-")))
            .replace("{BANK}", escape(str(card_bank or "")))
            .replace("{AMOUNT}", f"{toman:,}")
            .replace("{RIAL}", f"{rial:d}")
        )
    else:
        body = "\n".join([
            f"💰 لطفا دقیقا مبلغ: <code>{rial:d}</code> ریال",
            f"💰 معادل: {toman:,} تومان",
            f"💳 به شماره کارت: <code>{escape(str(card_number or '-'))}</code>",
            f"👤 به نام: {escape(str(card_owner or '-'))}",
            *(
                [f"🏦 بانک: {escape(str(card_bank))}"]
                if str(card_bank or "").strip()
                else []
            ),
            "❗ بعد از واریز مبلغ اسکرین شات از تراکنش برای ما ارسال کنید.",
        ])
    parts = []
    if int(tx_marker or 0) > 0:
        parts.append(f"🔢 مشخصه تراکنش اعمال شد: +{int(tx_marker):,} تومان")
    parts.append(body)
    parts.append(
        "⚡️ پس از تایید پرداخت، اشتراک شما به‌صورت خودکار ساخته و ارسال می‌شود."
    )
    return "\n\n".join(parts)


def _receipt_start_content(
    session: dict[str, Any],
    *,
    amount: int,
    currency: str,
    settings: dict[str, Any] | None = None,
) -> tuple[str, str | None, int, int]:
    """SellBot-style card instruction for IRR card payments.

    Returns (text, parse_mode, tx_marker_toman, display_rial). Falls back to
    the generic provider message for non-card methods and non-IRR currencies.
    """
    method = dict(session.get("method") or {})
    if (
        str(session.get("action") or "") == "receipt"
        and str(method.get("kind") or method.get("provider_key") or "")
        in {"card", "card_manual"}
        and str(currency or "").upper() == "IRR"
    ):
        number, owner, bank = _split_card_destination(
            method.get("destination"), method.get("network")
        )
        base_toman = int(amount or 0) // 10
        marker = 0
        settings = dict(settings or {})
        if bool(settings.get("random_tx_spec", False)):
            from TenantRuntime.UserBot.sellbot_utils import (
                _apply_random_tx_marker,
            )

            _, marker = _apply_random_tx_marker(base_toman, settings)
        display_rial = (base_toman + int(marker or 0)) * 10
        return _card_instruction_text(
            amount_rial=display_rial,
            card_number=number,
            card_owner=owner,
            card_bank=bank,
            tx_marker=int(marker or 0),
            template=str(settings.get("card_to_card_text") or ""),
        ), "HTML", int(marker or 0), display_rial
    return str(session.get("message") or ""), None, 0, int(amount or 0)



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
    *,
    rtl: bool = False,
) -> list[list[TelegramInlineKeyboardButton]]:
    cols = max(1, min(int(columns or 1), 3))
    rows = [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
    # SellBot parity: descending order mirrors multi-column rows (RTL).
    return [list(reversed(chunk)) if rtl else chunk for chunk in rows]


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
    items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    eligible: list[dict[str, Any]] = []
    blocked = 0
    for item in items if items is not None else business.list_subscriptions(actor):
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


USERBOT_STATUS_PROBE_CONCURRENCY = 3

_PANEL_MISSING_MARKERS = (
    "http 404",
    "http 410",
    "not found",
    "user not found",
    "does not exist",
    "no such user",
    "یافت نشد",
    "پیدا نشد",
    "وجود ندارد",
)


def _probe_subscription_state(adapter: Any, targets: list[tuple]) -> str:
    """Panel presence of one subscription: exists | missing | unreachable.

    Mirrors SellBot _service_probe_state: found on any target wins; all
    targets 404 means missing; any transport error means unreachable.
    """
    found_any = False
    missing_count = 0
    unreachable_count = 0
    for target, secret, ref in targets:
        try:
            adapter.get_user(
                target=target, secret=secret, external_ref=str(ref)
            )
            found_any = True
        except Exception as exc:
            text = str(exc).lower()
            if any(marker in text for marker in _PANEL_MISSING_MARKERS):
                missing_count += 1
            else:
                unreachable_count += 1
    if found_any:
        return "exists"
    if missing_count == len(targets) and targets:
        return "missing"
    if unreachable_count > 0:
        return "unreachable"
    return "missing"


async def _filter_existing_subscriptions(
    business: Any,
    actor: int,
    items: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Hide panel-vanished subscriptions; purge them after the TTL.

    Mirrors SellBot _filter_existing_services: exists -> seen+visible;
    unreachable -> temporarily hidden without streak; missing -> hidden
    with a streak, purged from the database only after
    missing_service_delete_days (default 7). Subscriptions without any
    panel identity stay visible (nothing verifiable to check).
    """
    try:
        ttl_days = max(1, int(settings.get("missing_service_delete_days") or 7))
    except (TypeError, ValueError):
        ttl_days = 7
    adapter = business.panel_adapter
    if not callable(getattr(adapter, "get_user", None)):
        # Test doubles / unconfigured adapters that cannot verify presence:
        # keep everything visible (same fail-open as broken credentials).
        return list(items)
    probed: list[tuple[dict, list[tuple]]] = []
    visible: list[dict[str, Any]] = []
    for item in items:
        try:
            sid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if str(item.get("status") or "") not in {
            "active", "disabled", "expired",
        }:
            visible.append(item)
            continue
        try:
            identities = business.subscription_panel_identities(actor, sid)
        except TenantBusinessError:
            visible.append(item)
            continue
        if not identities:
            visible.append(item)
            continue
        materials: list[tuple] = []
        material_failed = False
        for server_id, ref in identities:
            try:
                _, target, secret = business._panel_material(int(server_id))
                materials.append((target, secret, ref))
            except TenantBusinessError:
                material_failed = True
                break
        if material_failed or not materials:
            # Credential problem: cannot verify, keep visible without
            # streak (a broken credential must not hide real services).
            visible.append(item)
            continue
        probed.append((item, materials))

    semaphore = asyncio.Semaphore(USERBOT_STATUS_PROBE_CONCURRENCY)

    async def _probe_one(
        item: dict[str, Any], materials: list[tuple]
    ) -> tuple[dict[str, Any], str]:
        adapter = business.panel_adapter

        def _run() -> str:
            return _probe_subscription_state(adapter, materials)

        async with semaphore:
            try:
                return item, await asyncio.to_thread(_run)
            except Exception:
                return item, "unreachable"

    for item, state in await asyncio.gather(
        *(_probe_one(item, materials) for item, materials in probed)
    ):
        try:
            sid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if state == "exists":
            try:
                business.mark_service_seen(sid)
            except (TenantBusinessError, ValueError, sqlite3.Error):
                pass
            visible.append(item)
        elif state == "unreachable":
            continue
        else:
            try:
                info = business.mark_service_missing(sid)
            except (TenantBusinessError, ValueError, sqlite3.Error):
                continue
            try:
                if business._older_than_days(
                    info.get("first_missing_at"), ttl_days
                ):
                    try:
                        business.purge_missing_subscription(sid)
                    except (TenantBusinessError, ValueError, sqlite3.Error):
                        pass
            except (TenantBusinessError, ValueError, sqlite3.Error):
                pass
    return visible


def _single_subscription_visibility(
    business: Any,
    actor: int,
    item: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    """Probe one subscription like :func:`_filter_existing_subscriptions`.

    Returns ``"visible"`` when the card may be shown, ``"hidden"`` when the
    panel no longer has it (or is unreachable) and it must behave like
    SellBot: hidden from the user, kept for the grace TTL, purged only after
    ``missing_service_delete_days`` (default 7). Side effects (seen/missing/
    purge) mirror the list filter so direct ``shop:substatus`` links cannot
    resurrect a deleted service.
    """
    try:
        sid = int(item.get("id") or 0)
    except (TypeError, ValueError):
        return "hidden"
    if sid <= 0:
        return "hidden"
    if str(item.get("status") or "") not in {"active", "disabled", "expired"}:
        return "visible"
    try:
        identities = business.subscription_panel_identities(actor, sid)
    except Exception:
        return "visible"
    if not identities:
        return "visible"
    materials: list[tuple] = []
    for server_id, ref in identities:
        try:
            _, target, secret = business._panel_material(int(server_id))
            materials.append((target, secret, ref))
        except Exception:
            return "visible"
    if not materials:
        return "visible"
    if not callable(getattr(business.panel_adapter, "get_user", None)):
        # Adapter cannot verify presence (test double / unconfigured):
        # keep visible, like broken credentials.
        return "visible"
    try:
        ttl_days = max(1, int(settings.get("missing_service_delete_days") or 7))
    except (TypeError, ValueError):
        ttl_days = 7
    try:
        state = _probe_subscription_state(business.panel_adapter, materials)
    except Exception:
        return "hidden"
    if state == "exists":
        try:
            business.mark_service_seen(sid)
        except Exception:
            pass
        return "visible"
    if state == "unreachable":
        return "hidden"
    try:
        info = business.mark_service_missing(sid)
    except Exception:
        return "hidden"
    try:
        if business._older_than_days(info.get("first_missing_at"), ttl_days):
            try:
                business.purge_missing_subscription(sid)
            except Exception:
                pass
    except Exception:
        pass
    return "hidden"


def _renew_item_label(item: dict[str, Any]) -> str:
    """SellBot-parity renewal button: service name only, no id prefix."""
    name = str(item.get("service_name") or "").strip()
    if name:
        return name
    plan = str(item.get("plan_name") or "").strip()
    if plan:
        return plan
    return f"اشتراک #{int(item.get('id') or 0)}"


def _renew_menu_rows(
    items: list[dict[str, Any]], settings: dict[str, Any]
) -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-style renewal service picker rows (name-only buttons)."""
    return [
        [_button(
            _renew_item_label(item),
            callback_data=f"shop:renew:{item['id']}",
            settings=settings,
        )]
        for item in items
    ]


def _user_tickets_rows(
    items: list[dict[str, Any]], page: int = 1, page_size: int = 15
) -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-style ticket-code grid with page navigation."""
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), pages))
    selected = items[(page - 1) * page_size:page * page_size]
    rows: list[list[TelegramInlineKeyboardButton]] = []
    current: list[TelegramInlineKeyboardButton] = []
    for item in selected:
        try:
            ticket_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if ticket_id <= 0:
            continue
        code = str(item.get("ticket_code") or ticket_id).strip()
        current.append(InlineKeyboardButton(
            code,
            callback_data=f"shop:ticket:{ticket_id}",
        ))
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    nav: list[TelegramInlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"shop:tickets:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"shop:tickets:{page + 1}"))
    rows.append(nav)
    return rows


def _support_menu_rows() -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-style support panel rows."""
    return [
        [InlineKeyboardButton("❗️سوالات متداول", callback_data="shop:faq")],
        [InlineKeyboardButton("📬تیکت‌های من", callback_data="shop:tickets")],
        [InlineKeyboardButton("📩ایجاد تیکت", callback_data="shop:newticket")],
        [InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home")],
    ]


def _renew_plan_buttons(
    plans: list[dict[str, Any]],
    settings: dict[str, Any],
    subscription_id: int,
) -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-format plan buttons wired to renewal checkout."""
    visible = [
        p for p in _sorted_purchase_plans(plans, settings)
        if not str(p.get("name") or "").startswith("__WHITELABEL_")
    ]
    buttons = [
        _button(
            _sellbot_plan_label(p),
            callback_data=f"shop:renewplan:{int(subscription_id)}:{int(p['id'])}",
            settings=settings,
        )
        for p in visible
    ]
    return _column_rows(
        buttons,
        int(settings.get("plan_columns") or 1),
        rtl=bool(settings.get("plan_sort_desc", False)),
    )


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
    if bool(settings.get("show_sub_link", True)):
        rows.append([InlineKeyboardButton(
            "لینک اشتراک",
            callback_data=f"shop:sublink:{int(subscription_id)}",
        )])
    if bool(settings.get("show_auto_sub_link", False)):
        rows.append([InlineKeyboardButton(
            "اشتراک خودکار",
            callback_data=f"shop:autosub:{int(subscription_id)}",
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


def _trial_delivery_text(
    business: Any,
    actor: int,
    result: dict[str, Any],
    settings: dict[str, Any],
    *,
    announce_enabled: bool,
) -> str:
    access = _delivery_access_text(business, actor, result, settings)
    if announce_enabled:
        return (
            "✅ اکانت تست رایگان شما با موفقیت فعال شد.\n\n"
            + access
        )
    return access


def _delivery_link_candidates(
    business: Any,
    actor: int,
    result: dict[str, Any],
    settings: dict[str, Any],
) -> list[tuple[str, str]]:
    """Admin-enabled subscription links in SellBot order (label, link)."""
    try:
        subscription_id = int(
            result.get("id") or result.get("subscription_id") or 0
        )
    except (TypeError, ValueError):
        subscription_id = 0
    if subscription_id <= 0:
        return []
    candidates: list[tuple[str, str, Any]] = [
        ("🔗 لینک اشتراک:", bool(settings.get("show_sub_link", True)), lambda: business.subscription_link(
            actor, subscription_id=subscription_id)),
        ("🤖 لینک اشتراک خودکار:", bool(settings.get("show_auto_sub_link", False)), lambda: business.automatic_subscription_link(
            actor, subscription_id=subscription_id)),
        ("🔐 لینک اشتراک b64:", bool(settings.get("show_sub_link_b64", False)), lambda: business.subscription_link_b64(
            actor, subscription_id=subscription_id)),
        ("🌐 لینک اشتراک هوشمند:", bool(settings.get("show_multi_server", False)), lambda: business.smart_subscription_link(
            actor, subscription_id=subscription_id, base64_output=False)),
        ("🌐 لینک اشتراک هوشمند b64:", bool(settings.get("show_multi_server_b64", False)), lambda: business.smart_subscription_link(
            actor, subscription_id=subscription_id, base64_output=True)),
    ]
    links: list[tuple[str, str]] = []
    for label, enabled, fetch in candidates:
        if not enabled:
            continue
        try:
            value = str(fetch() or "").strip()
        except TenantBusinessError:
            value = ""
        if value:
            links.append((label, value))
    return links


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

    for label, link in _delivery_link_candidates(business, actor, result, settings):
        lines.extend([label, link])

    if bool(settings.get("show_direct_config", True)):
        lines.append("📄 کانفیگ مستقیم: از «📦 اشتراک‌های من» قابل دریافت است.")

    return "\n".join(lines) or (
        "🔒 نمایش لینک و کانفیگ این اشتراک توسط ادمین غیرفعال است."
    )


def _wallet_confirm_text(order_id: int) -> str:
    """SellBot-exact wallet confirmation (no raw links; delivery follows)."""
    return (
        "🎉 تراکنش شما تایید شد\n"
        "از طریق دکمه [📊وضعیت اشتراک📊] میتوانید به اطلاعات اشتراک خود دسترسی داشته باشید.\n\n"
        f"🎁 شناسه تراکنش:{int(order_id)}"
    )


def _wallet_qr_caption(label: str, link: str) -> str:
    """SellBot-exact single-link QR caption."""
    return (
        "🎉 اشتراک شما آماده‌ی استفاده است ✅\n\n"
        f"{label}\n"
        f"<code>{escape(link)}</code>\n\n"
        "📄 جهت کپی شدن لینک کافیست یک‌بار روی لینک بالا لمس کنید."
    )


def _wallet_qr_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """SellBot subscription_links_keyboard parity (guide + back).

    The guide button carries the subscription id so the OS chooser can
    return to the same subscription (SellBot guide:back:s{id} parity).
    """
    return InlineKeyboardMarkup([
        [TelegramInlineKeyboardButton(
            "📚 راهنمای اتصال",
            callback_data=f"shop:guidesub:{int(subscription_id)}",
        )],
        [TelegramInlineKeyboardButton(
            "🔙بازگشت",
            callback_data=f"shop:subrefresh:{int(subscription_id)}",
        )],
    ])


def _build_wallet_admin_caption(
    *,
    order: dict[str, Any],
    result: dict[str, Any],
    service_name: str = "",
    service_code: str = "",
    wallet_balance_after: int | None = None,
) -> str:
    """SellBot _build_subscription_created_caption parity for wallet orders."""
    is_renew = str(
        result.get("operation") or order.get("operation") or ""
    ).strip().lower() == "renewal"
    title = "📄 گزارش تمدید اشتراک" if is_renew else "📄 گزارش ایجاد اشتراک"
    name = str(service_name or "").strip() or str(order.get("plan_name") or "-")
    server_title = str(order.get("selected_server_label") or "-")
    try:
        gb = float(order.get("traffic_gb") or 0)
    except (TypeError, ValueError):
        gb = 0.0
    try:
        days = int(order.get("duration_days") or 0)
    except (TypeError, ValueError):
        days = 0
    amount = order.get("amount")
    currency = str(order.get("currency") or "")
    toman, unit = _receipt_toman(amount, currency)
    # SellBot shows the 7-digit service code, never the smart-link token.
    code = str(service_code or "").strip() or str(
        result.get("id")
        or result.get("subscription_id")
        or order.get("id")
        or "-"
    )
    lines = [
        title,
        "",
        f"👤اشتراک: {name}",
        f"🛰سرور: {server_title}",
        f"📊حجم: {gb:.1f} گیگابایت",
        f"⏳زمان: {int(days)} روز",
        f"💰مبلغ پرداختی: {toman:,} {unit}",
        "💳پرداخت از کیف پول کاربر — مبلغ کسر شد",
    ]
    if wallet_balance_after is not None:
        try:
            balance_val = int(wallet_balance_after)
        except (TypeError, ValueError):
            balance_val = 0
        balance_toman, balance_unit = _receipt_toman(balance_val, currency)
        lines.append(f"💼مانده کیف پول کاربر: {balance_toman:,} {balance_unit}")
    lines.append(f"🔑شناسه اشتراک:{code}")
    return "\n".join(lines)


async def _notify_admin_wallet_purchase(
    business: Any,
    *,
    actor: int = 0,
    order: dict[str, Any],
    result: dict[str, Any],
    display_name: str = "",
    customer_id: int = 0,
    wallet_balance_after: int | None = None,
) -> None:
    """Best-effort SellBot-style admin report for wallet purchases."""
    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        subscription_id = int(
            result.get("subscription_id") or result.get("id") or 0
        )
        service_name = str(order.get("plan_name") or "").strip()
        subscription_code = ""
        lookup_actor = int(actor or 0)
        if not lookup_actor:
            try:
                lookup_actor = int(order.get("customer_telegram_id") or 0)
            except (TypeError, ValueError):
                lookup_actor = 0
        if subscription_id > 0 and lookup_actor > 0:
            try:
                item = business.customer_subscription_status(
                    lookup_actor,
                    subscription_id=subscription_id,
                    refresh=False,
                )
                # customer_subscription_status is scoped to the customer;
                # fall back to plan name when the lookup is not permitted.
                if isinstance(item, dict):
                    service_name = str(
                        item.get("service_name") or service_name
                    ).strip() or service_name
                    # 7-digit service code (SellBot parity, never smart token).
                    subscription_code = str(
                        item.get("service_code") or ""
                    ).strip()
            except Exception:
                pass
        caption = _build_wallet_admin_caption(
            order=order,
            result=result,
            service_name=service_name,
            service_code=subscription_code,
            wallet_balance_after=wallet_balance_after,
        )
        rows: list[list[TelegramInlineKeyboardButton]] = []
        user_title = str(display_name or "").strip() or str(customer_id or "-")
        if int(customer_id or 0) > 0:
            rows.append([_button(
                f"👤 {user_title}",
                callback_data=f"userbot:user:{int(customer_id)}",
                settings={"colored_buttons": True, "button_theme": "smart"},
            )])
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=caption,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            )
    except Exception:
        return
    finally:
        token = ""


async def _deliver_wallet_purchase(
    context: Any,
    business: Any,
    actor: int,
    settings: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """SellBot-parity follow-up delivery after wallet confirmation.

    Single enabled link -> QR photo with SellBot caption + guide/back buttons.
    Multiple/zero links -> subscription status detail with action buttons.
    Never raises; checkout must not break on delivery errors.
    """
    try:
        subscription_id = int(
            result.get("subscription_id") or result.get("id") or 0
        )
    except (TypeError, ValueError):
        subscription_id = 0
    if subscription_id <= 0:
        return
    try:
        chat_id = int(actor)
    except (TypeError, ValueError):
        return
    try:
        candidates = _delivery_link_candidates(business, actor, result, settings)
    except Exception:
        candidates = []
    if len(candidates) == 1:
        label, link = candidates[0]
        caption = _wallet_qr_caption(label, link)
        markup = _wallet_qr_keyboard(subscription_id)
        try:
            import qrcode

            image = BytesIO()
            qrcode.make(link).save(image, format="PNG")
            image.seek(0)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return
    # Zero or multiple links: SellBot shows the subscription status screen.
    try:
        item = business.customer_subscription_status(
            actor, subscription_id=subscription_id, refresh=False
        )
    except Exception:
        item = None
    if isinstance(item, dict) and item:
        try:
            text = _subscription_detail_text(item, settings)
            markup = InlineKeyboardMarkup(
                _subscription_status_rows(business, actor, item, settings)
            )
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown",
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    try:
        fallback = _delivery_access_text(business, actor, result, settings)
        await context.bot.send_message(
            chat_id=chat_id, text=fallback,
            reply_markup=_home_inline_markup(settings),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


def _sorted_purchase_plans(
    plans: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """SellBot _sort_plans parity: mode price|gb|days + direction flag.

    Values tolerate decorated strings (e.g. "70,000 تومان").  The tenant
    priority column stays as a stable secondary key like before.
    """
    items = list(plans)
    mode = str(settings.get("plan_sort_mode") or "price").strip().lower()
    desc = bool(settings.get("plan_sort_desc", False))
    key_name = (
        "price"
        if mode == "price"
        else ("traffic_gb" if mode == "gb" else "duration_days")
    )

    def _sort_number(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip().replace(",", "").replace("،", "")
        if not text:
            return 0.0
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return 0.0
        try:
            return float(match.group(0))
        except (TypeError, ValueError):
            return 0.0

    items.sort(key=lambda p: _sort_number(p.get(key_name)), reverse=desc)
    if bool(settings.get("plan_sort_by_priority", True)):
        items.sort(key=lambda p: int(p.get("priority") or 0))
    return items


def _sellbot_plan_label(plan: dict[str, Any]) -> str:
    """SellBot plans_keyboard button text: title | vol | day - price."""
    try:
        gb = float(plan.get("traffic_gb") or 0)
    except (TypeError, ValueError):
        gb = 0.0
    try:
        days = int(plan.get("duration_days") or 0)
    except (TypeError, ValueError):
        days = 0
    try:
        price = int(plan.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    return (
        f"{plan.get('name')} | {gb:g} گیگ | "
        f"{days} روز - {price:,} {plan.get('currency') or ''}"
    )


def _plan_buttons(
    plans: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    server_id: int = 0,
) -> list[list[TelegramInlineKeyboardButton]]:
    buttons = [
        _button(
            _sellbot_plan_label(p),
            callback_data=(
                f"shop:plan:{int(server_id)}:{int(p['id'])}"
                if int(server_id or 0)
                else f"shop:plan:{int(p['id'])}"
            ),
            settings=settings,
        )
        for p in _sorted_purchase_plans(plans, settings)
    ]
    return _column_rows(
        buttons,
        int(settings.get("plan_columns") or 1),
        rtl=bool(settings.get("plan_sort_desc", False)),
    )


def _purchase_location_rows(
    servers: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-parity first step: choose the purchase location before plans."""
    items = list(servers)
    if bool(settings.get("shuffle_server_layout", True)) and len(items) > 1:
        random.shuffle(items)
    buttons = [
        _button(
            str(server.get("label") or f"سرور #{server['id']}"),
            callback_data=f"shop:buyloc:{int(server['id'])}",
            settings=settings,
        )
        for server in items
    ]
    return _column_rows(buttons, int(settings.get("server_columns") or 1))


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


def _trial_server_rows(
    servers: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[list[TelegramInlineKeyboardButton]]:
    """SellBot-style location chooser dedicated to free-trial provisioning."""
    items = list(servers)
    if bool(settings.get("shuffle_server_layout", True)) and len(items) > 1:
        random.shuffle(items)
    buttons = [
        _button(
            str(server.get("label") or f"سرور #{server['id']}"),
            callback_data=f"shop:trialserver:{int(server['id'])}",
            settings=settings,
        )
        for server in items
    ]
    return _column_rows(
        buttons,
        int(settings.get("server_columns") or 1),
    )


def _trial_state_message(state: dict[str, Any]) -> str:
    if not bool(state.get("enabled")):
        return "🚫 دریافت تست رایگان در حال حاضر غیرفعال است."
    if bool(state.get("used")):
        return "🚫 شما قبلا اکانت تست رایگان خود را دریافت نموده‌اید!"
    return ""


def _servers_list_default() -> str:
    return "📡 **لیست سرورها**\nلطفاً لوکیشن مورد نظر خود را انتخاب کنید:"


def _plans_list_default() -> str:
    return "🛒 **لطفاً پلن مورد نظر خود را انتخاب کنید:**"


def _trial_location_text(settings: dict[str, Any]) -> str:
    return (
        str(settings.get("servers_list_text") or "").strip()
        or _servers_list_default()
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

    if bool(growth.get("referral_enabled")) or bool(settings.get("show_gift_button", True)):
        rows.append([
            KeyboardButton(BTN_REFERRAL, settings=settings),
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
        summary = business.wallet_summary(actor)
        await update.effective_message.reply_text(
            _wallet_text(summary, settings),
            reply_markup=InlineKeyboardMarkup(
                _wallet_method_rows(
                    business, settings, include_back=True,
                )
            ),
        )
        return True

    if text == BTN_REFERRAL:
        growth = business._ensure_growth_settings()
        if not bool(growth.get("referral_enabled")):
            raise TenantBusinessError("referral is disabled")
        referral = _referral_content(
            business,
            actor,
            context,
            settings,
        )
        await update.effective_message.reply_text(
            str(referral["body"]),
            reply_markup=_invite_home_markup(),
            disable_web_page_preview=True,
        )
        return True

    if text == BTN_TRIAL:
        state = business.free_trial_state(actor)
        unavailable = _trial_state_message(state)
        if unavailable:
            await update.effective_message.reply_text(
                unavailable,
                reply_markup=_main_keyboard(spec, business),
            )
            return True
        servers = business.list_purchase_servers()
        if not servers:
            await update.effective_message.reply_text(
                "❌ سروری برای ارائه تست رایگان در دسترس نیست.",
                reply_markup=_main_keyboard(spec, business),
            )
            return True
        rows = _trial_server_rows(servers, settings)
        rows.append([
            InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home")
        ])
        await _reply_md(
            update.effective_message,
            _trial_location_text(settings),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if text == BTN_BUY:
        if not bool(settings.get("enable_buy", True)):
            await update.effective_message.reply_text(
                "🚫 خرید اشتراک در حال حاضر غیرفعال است.",
                reply_markup=_main_keyboard(spec, business),
            )
            return True
        servers = business.list_purchase_servers()
        if not servers:
            await update.effective_message.reply_text(
                "❌ هیچ سروری در دسترس نیست.",
                reply_markup=_main_keyboard(spec, business),
            )
            return True
        rows = _purchase_location_rows(servers, settings)
        rows.append([
            _button("🔙بازگشت", callback_data="runtime:home", settings=settings)
        ])
        await _reply_md(
            update.effective_message,
            str(settings.get("servers_list_text") or "").strip()
            or _servers_list_default(),
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
        visible = await _filter_existing_subscriptions(
            business, actor, business.list_subscriptions(actor), settings
        )
        items, blocked = _renewable_subscriptions(
            business, actor, items=visible
        )
        rows = _renew_menu_rows(items, settings)
        body = "👇 لطفا یکی از اشتراک‌های خود را برای تمدید انتخاب نمایید"
        if not rows:
            if blocked:
                body = business.renewal_not_allowed_text()
            else:
                body = "❌ اشتراک وجود ندارد."
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
        body = _ticket_panel_body(settings, [])
        await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❗️سوالات متداول", callback_data="shop:faq")],
                [InlineKeyboardButton("📬تیکت‌های من", callback_data="shop:tickets")],
                [InlineKeyboardButton("📩ایجاد تیکت", callback_data="shop:newticket")],
            ]),
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
                    "📱 iOS",
                    callback_data="shop:guide:ios",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖥️ ویندوز",
                    callback_data="shop:guide:windows",
                ),
                InlineKeyboardButton(
                    "💻 macOS",
                    callback_data="shop:guide:mac",
                ),
            ],
            [InlineKeyboardButton(
                "🖥️ لینوکس",
                callback_data="shop:guide:linux",
            )],
        ]
        await update.effective_message.reply_text(
            guide or "انتخاب سیستم عامل ⬇️",
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
        return True

    if text == BTN_FAQ:
        await update.effective_message.reply_text(
            _setting_text(
                settings,
                "faq_text",
                "❓ سوالات متداول\n\nهنوز متنی تنظیم نشده است.",
            ),
            disable_web_page_preview=True,
        )
        return True

    return False


def _force_join_config(settings: dict[str, Any]) -> tuple[Any | None, str, str]:
    username = str(settings.get("force_join_channel_username") or "").strip().lstrip("@")
    channel_id = str(settings.get("force_join_channel_id") or "").strip()
    legacy = str(settings.get("force_join_channel") or "").strip()
    if not username and legacy.startswith("@"):
        username = legacy.lstrip("@")
    if not channel_id and legacy.lstrip("-").isdigit():
        channel_id = legacy

    target: Any | None = None
    if username:
        target = f"@{username}"
    elif channel_id.lstrip("-").isdigit():
        target = int(channel_id)

    join_url = str(settings.get("force_join_channel_link") or "").strip()
    if not join_url and username:
        join_url = f"https://t.me/{username}"
    guide = (
        str(settings.get("force_join_guide_text") or "").strip()
        or "🔒 برای استفاده از ربات، ابتدا در کانال پشتیبانی عضو شوید.\n"
        "پس از عضویت روی «✅ بررسی عضویت» بزنید."
    )
    return target, join_url, guide


def _force_join_markup(join_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if str(join_url or "").strip():
        rows.append([
            InlineKeyboardButton("📢 عضویت در کانال", url=str(join_url).strip())
        ])
    rows.append([
        InlineKeyboardButton("✅ بررسی عضویت", callback_data="forcejoin:check")
    ])
    return InlineKeyboardMarkup(rows)


async def _force_join_membership(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    settings: dict[str, Any],
) -> bool | None:
    """True=member, False=definitely not joined, None=misconfigured/API error."""
    target, _join_url, _guide = _force_join_config(settings)
    if target is None:
        return None
    try:
        member = await context.bot.get_chat_member(
            chat_id=target,
            user_id=int(user_id),
        )
        status = str(getattr(member, "status", "") or "").lower()
        return status in {"member", "administrator", "creator", "owner"}
    except Exception:
        # Fail-open: a bad channel id, missing bot permission, Telegram outage,
        # or private-channel misconfiguration must not lock the whole UserBot.
        return None


async def _force_join_allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business,
) -> bool:
    settings = _set_button_settings(business.runtime_userbot_settings())
    if not bool(settings.get("force_join_enabled", False)):
        return True
    actor = int(update.effective_user.id) if update.effective_user else 0
    membership = await _force_join_membership(
        context,
        user_id=actor,
        settings=settings,
    )
    if membership is not False:
        return True

    _target, join_url, guide = _force_join_config(settings)
    markup = _force_join_markup(join_url)
    if update.callback_query:
        await update.callback_query.answer(
            "ابتدا در کانال عضو شوید.",
            show_alert=True,
        )
        try:
            await update.callback_query.edit_message_text(
                guide,
                reply_markup=markup,
            )
        except Exception:
            if update.effective_chat:
                await update.effective_chat.send_message(
                    guide,
                    reply_markup=markup,
                )
    elif update.effective_message:
        await update.effective_message.reply_text(
            guide,
            reply_markup=markup,
        )
    return False


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spec, _, state_store, business = _services(context)
    if spec.role != "user":
        raise RuntimeError("UserBot handler registered for non-user role")
    user_id = int(update.effective_user.id) if update.effective_user else 0
    user = update.effective_user

    # Match SellBot: persist the customer/referral before force-join can stop
    # /start, because the forcejoin:check callback cannot recover the original
    # Telegram deep-link payload afterwards.
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

    if not await _force_join_allowed(update, context, business):
        return

    context.user_data.pop("biz_flow", None)
    context.user_data.pop("subscription_rotation_confirmation", None)
    state = state_store.load(user_id)
    visits = int(state.get("visits") or 0) + 1
    state_store.save(user_id, {**state, "visits": visits, "screen": "home"})
    gift_notice = ""
    if args:
        payload = str(args[0] or "").strip()
        if payload.startswith("gift_"):
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
    username = str(getattr(user, "username", None) or "").strip()
    text = _setting_text(
        settings,
        "welcome_message",
        "سلام {full_name} عزیز 👋\nبه ربات ما خوش آمدید.",
        full_name=str(getattr(user, "full_name", None) or "کاربر"),
        username=f"@{username}" if username else "",
        id=user_id,
        tenant_name=spec.tenant_name,
        visits=visits,
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

    if not await _force_join_allowed(update, context, business):
        return
    context.user_data.pop("biz_flow", None)
    state = state_store.load(user_id)
    state_store.save(user_id, {**state, "screen": "status"})
    try:
        items = await _filter_existing_subscriptions(
            business, user_id, business.list_subscriptions(user_id, limit=4),
            settings,
        )
        if not items:
            text = "❌ اشتراک وجود ندارد."
            status_markup = InlineKeyboardMarkup([[
                _button("🔙بازگشت", callback_data="runtime:home", settings=settings)
            ]])
            direct_items: list[dict[str, Any]] = []
        elif len(items) > 3:
            full_visible = await _filter_existing_subscriptions(
                business, user_id, business.list_subscriptions(user_id, limit=500),
                settings,
            )
            text, status_markup = _subscription_list(
                business, user_id, settings, items=full_visible,
            )
            direct_items = []
        else:
            direct_items = [
                business.customer_subscription_status(
                    user_id,
                    subscription_id=int(item["id"]),
                    refresh=True,
                )
                for item in items
            ]
            text = ""
            status_markup = None
    except (TenantBusinessError, ValueError, sqlite3.Error):
        text = "⏳ دریافت وضعیت اشتراک‌ها موقتاً ممکن نیست."
        status_markup = InlineKeyboardMarkup([[
            _button("🔙بازگشت", callback_data="runtime:home", settings=settings)
        ]])
        direct_items = []

    if direct_items:
        multiple = len(direct_items) > 1
        if update.callback_query:
            await update.callback_query.answer()
            first, *rest = direct_items
            await _edit_md(
                update.callback_query,
                _subscription_detail_text(first, settings),
                reply_markup=InlineKeyboardMarkup(
                    _subscription_status_rows(
                        business, user_id, first, settings
                    )
                ),
            )
            for item in rest:
                await _reply_md(
                    update.callback_query.message,
                    _subscription_detail_text(item, settings),
                    reply_markup=InlineKeyboardMarkup(
                        _subscription_status_rows(
                            business, user_id, item, settings
                        )
                    ),
                )
        elif update.effective_message:
            for item in direct_items:
                await _reply_md(
                    update.effective_message,
                    _subscription_detail_text(item, settings),
                    reply_markup=InlineKeyboardMarkup(
                        _subscription_status_rows(
                            business, user_id, item, settings
                        )
                    ),
                )
        return

    if update.callback_query:
        await update.callback_query.answer()
        await _edit_subscription(
            update.callback_query,
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
    if not data.startswith(("shop:subrotate:", "shop:subrotateconfirm:")):
        context.user_data.pop("subscription_rotation_confirmation", None)

    if data == "forcejoin:check":
        spec, _, _, business = _services(context)
        settings = _set_button_settings(business.runtime_userbot_settings())
        await update.callback_query.answer()
        if not bool(settings.get("force_join_enabled", False)):
            if update.effective_chat:
                await update.effective_chat.send_message(
                    "✅ عضویت اجباری غیرفعال است.",
                    reply_markup=_main_keyboard(spec, business),
                )
            return
        actor = int(update.effective_user.id) if update.effective_user else 0
        membership = await _force_join_membership(
            context,
            user_id=actor,
            settings=settings,
        )
        if membership is not False:
            if update.effective_chat:
                await update.effective_chat.send_message(
                    "✅ عضویت شما تایید شد."
                    if membership is True
                    else "✅ دسترسی ربات فعال است.",
                    reply_markup=_main_keyboard(spec, business),
                )
            return
        _target, join_url, guide = _force_join_config(settings)
        if update.effective_chat:
            await update.effective_chat.send_message(
                guide,
                reply_markup=_force_join_markup(join_url),
            )
        return

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
        from TenantRuntime.UserBot import dynamic_plans
        if await dynamic_plans.handle_callback(update,context,business=business,actor=actor,settings=settings):
            return
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
            try:
                retry_order = business.order(actor, order_id)
            except Exception:
                retry_order = {}
            try:
                wallet_paid = int(retry_order.get("wallet_amount") or 0) > 0
            except (TypeError, ValueError):
                wallet_paid = False
            if wallet_paid:
                confirm = _wallet_confirm_text(order_id)
            else:
                confirm = f"✅ سفارش #{order_id} انجام شد."
            if wallet_paid:
                # SellBot parity: confirmation carries no inline buttons;
                # QR/status delivery with its own keyboard follows.
                await update.callback_query.edit_message_text(
                    confirm,
                    disable_web_page_preview=True,
                )
            else:
                await update.callback_query.edit_message_text(
                    confirm,
                    reply_markup=_home_inline_markup(settings),
                    disable_web_page_preview=True,
                )
            try:
                await _deliver_wallet_purchase(
                    context, business, actor, settings, result
                )
            except Exception:
                pass
            return
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
            summary = business.wallet_summary(actor)
            await update.callback_query.edit_message_text(
                _wallet_text(summary, settings),
                reply_markup=InlineKeyboardMarkup(
                    _wallet_method_rows(
                        business, settings, include_back=True,
                    )
                ),
            ); return
        if data == "shop:wallettopup":
            # Backward-compatible callback from older messages: show the real
            # enabled payment methods instead of asking users to type a currency.
            rows = _wallet_method_rows(business, settings, include_back=False)
            rows.append([InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")])
            await update.callback_query.edit_message_text(
                "💳 روش شارژ کیف پول را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:wallettopupstart:"):
            method_id = int(data.rsplit(":", 1)[1])
            method = business.method(method_id, currency=None)
            currency = str(method.get("currency") or "IRR")
            context.user_data["biz_flow"] = {
                "kind": "wallet_topup_amount",
                "method_id": method_id,
                "currency": currency,
            }
            await update.callback_query.edit_message_text(
                "🔻 لطفا مبلغی که قصد شارژ حساب خود دارید را "
                f"به {_wallet_currency_title(currency)} وارد کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")]
                ]),
            ); return
        if data.startswith("shop:wallettopupmethod:"):
            _, _, topup_id, method_id = data.split(":", 3)
            session = business.begin_wallet_topup_payment(
                actor,
                topup_id=int(topup_id),
                method_id=int(method_id),
            )
            topup = dict(session.get("topup") or {})
            rows: list[list[TelegramInlineKeyboardButton]] = []
            text, parse_mode, tx_marker, display_rial = _receipt_start_content(
                session,
                amount=int(topup.get("amount") or 0),
                currency=str(topup.get("currency") or ""),
                settings=settings,
            )
            if session["action"] == "receipt":
                context.user_data["biz_flow"] = {
                    "kind": "wallet_receipt",
                    "topup_id": int(topup_id),
                    "method_id": int(method_id),
                    "confirmed": False,
                    "tx_marker": tx_marker,
                    "display_amount": display_rial,
                    "is_card": str(
                        dict(session.get("method") or {}).get("kind")
                        or dict(session.get("method") or {}).get("provider_key")
                        or ""
                    ) in {"card", "card_manual"},
                }
            else:
                context.user_data.pop("biz_flow", None)
            if parse_mode == "HTML":
                try:
                    await update.callback_query.message.delete()
                except Exception:
                    pass
                await update.callback_query.answer()
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=_receipt_confirm_keyboard(),
                )
                return
            if session.get("checkout_url"):
                rows.append([InlineKeyboardButton(
                    "🌐 ورود به درگاه پرداخت",
                    url=str(session["checkout_url"]),
                )])
            rows.append([InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")])
            await update.callback_query.edit_message_text(
                str(session["message"]),
                reply_markup=InlineKeyboardMarkup(rows),
            ); return

        if data == "shop:gift":
            if not bool(settings.get("show_gift_button", True)):
                raise TenantBusinessError("gift is disabled")
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
            referral = _referral_content(
                business,
                actor,
                context,
                settings,
            )
            await update.callback_query.edit_message_text(
                str(referral["body"]),
                reply_markup=_invite_home_markup(),
                disable_web_page_preview=True,
            )
            return

        if data == "shop:invitebanner":
            growth = business._ensure_growth_settings()
            if not bool(growth.get("referral_enabled")):
                raise TenantBusinessError("referral is disabled")
            referral = _referral_content(
                business,
                actor,
                context,
                settings,
            )
            await update.callback_query.answer()
            chat_id = int(update.effective_chat.id) if update.effective_chat else actor
            photo_id = str(referral.get("photo_id") or "").strip()
            banner_text = str(referral.get("banner_text") or "").strip()
            if photo_id:
                try:
                    if len(banner_text) <= 1000:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=photo_id,
                            caption=banner_text,
                        )
                    else:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=photo_id,
                        )
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=banner_text,
                            disable_web_page_preview=True,
                        )
                    return
                except Exception:
                    pass
            await context.bot.send_message(
                chat_id=chat_id,
                text=banner_text,
                disable_web_page_preview=True,
            )
            return
        if data in {"shop:inviterewards", "shop:invitelist", "shop:invitestats", "shop:invitehistory"}:
            growth = business._ensure_growth_settings()
            if not bool(growth.get("referral_enabled")):
                raise TenantBusinessError("referral is disabled")
            summary = business.referral_summary(actor)
            chat_id = int(update.effective_chat.id) if update.effective_chat else actor
            await update.callback_query.answer()
            if data == "shop:inviterewards":
                rewards = list(summary.get("settings") or {} and summary.get("rewards") or [])
                trial_amount = int((summary.get("settings") or {}).get("referral_trial_reward") or 0)
                purchase_amount = int((summary.get("settings") or {}).get("referral_purchase_reward") or 0)
                total = sum(int(x.get("amount") or 0) for x in rewards)
                count = sum(int(x.get("count") or 0) for x in rewards)
                text = (
                    "🎁 جوایز من\n"
                    "❖ ◈━━━━━━━━━━━━━━━◈ ❖\n"
                    f"🤝 پاداش تست: {trial_amount:,} تومان\n"
                    f"🛒 پاداش خرید اول: {purchase_amount:,} تومان\n"
                    f"🎯 جوایز دریافتی: {count}\n"
                    f"💰 مجموع پاداش‌ها: {total:,} تومان\n"
                )
            elif data == "shop:invitelist":
                count = int(summary.get("referred_count") or 0)
                text = (
                    f"👥 دعوت‌های من ({count} نفر)\n❖ ◈━━━━━━━━━━━━━━━◈ ❖\n"
                    + (f"✅ {count} دعوت موفق ثبت شده است." if count else "👥 هنوز کسی را دعوت نکرده‌اید.")
                )
            elif data == "shop:invitestats":
                rewards = list(summary.get("rewards") or [])
                count = int(summary.get("referred_count") or 0)
                total = sum(int(x.get("amount") or 0) for x in rewards)
                paid = sum(int(x.get("count") or 0) for x in rewards)
                text = (
                    "📊 آمار دعوت\n"
                    "❖ ◈━━━━━━━━━━━━━━━◈ ❖\n"
                    f"👥 کل دعوت‌ها: {count}\n"
                    f"✅ دعوت‌های موفق: {count}\n"
                    f"🎁 مجموع پاداش‌ها: {total:,} تومان\n"
                    f"🧾 تعداد جوایز دریافتی: {paid}"
                )
            else:
                rewards = list(summary.get("rewards") or [])
                labels = {"trial": "پاداش تست", "purchase": "پاداش خرید", "manual": "پاداش دستی"}
                if not rewards:
                    text = "📜 هنوز پاداشی دریافت نکرده‌اید."
                else:
                    lines = [f"📜 تاریخچه جوایز ({len(rewards)} مورد)\n❖ ◈━━━━━━━━━━━━━━━◈ ❖"]
                    for idx, rw in enumerate(rewards, start=1):
                        rtype = str(rw.get("reward_type") or "")
                        lines.append(
                            f"{idx}. {labels.get(rtype, rtype)} | "
                            f"{int(rw.get('amount') or 0):,} {rw.get('currency') or ''} | "
                            f"✅ {int(rw.get('count') or 0)} مورد"
                        )
                    text = "\n".join(lines)
            await context.bot.send_message(chat_id=chat_id, text=text)
            return
        if data == "shop:trial":
            state = business.free_trial_state(actor)
            unavailable = _trial_state_message(state)
            if unavailable:
                await update.callback_query.edit_message_text(
                    unavailable,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ منو", callback_data="runtime:home")
                    ]]),
                )
                return
            servers = business.list_purchase_servers()
            if not servers:
                await update.callback_query.edit_message_text(
                    "❌ سروری برای ارائه تست رایگان در دسترس نیست.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ منو", callback_data="runtime:home")
                    ]]),
                )
                return
            rows = _trial_server_rows(servers, settings)
            rows.append([
                InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home")
            ])
            await _edit_md(
                update.callback_query,
                _trial_location_text(settings),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:trialserver:"):
            server_id = int(data.rsplit(":", 1)[1])
            state = business.free_trial_state(actor)
            unavailable = _trial_state_message(state)
            if unavailable:
                context.user_data.pop("biz_flow", None)
                await update.callback_query.edit_message_text(
                    unavailable,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ منو", callback_data="runtime:home")
                    ]]),
                )
                return
            servers = business.list_purchase_servers()
            selected = next(
                (item for item in servers if int(item["id"]) == server_id),
                None,
            )
            if selected is None:
                await update.callback_query.answer(
                    "این سرور برای تست در دسترس نیست.",
                    show_alert=True,
                )
                return
            context.user_data["biz_flow"] = {
                "kind": "trial_service_name",
                "server_id": server_id,
            }
            await update.callback_query.edit_message_text(
                "⬇️ لطفا نام خود را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌لغو", callback_data="shop:trialcancel")
                ]]),
            )
            return
        if data == "shop:trialcancel":
            context.user_data.pop("biz_flow", None)
            await update.callback_query.edit_message_text(
                "عملیات لغو شد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ منو", callback_data="runtime:home")
                ]]),
            )
            return
        if data == "shop:buy":
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            servers = business.list_purchase_servers()
            if not servers:
                raise TenantBusinessError("no purchase server")
            rows = _purchase_location_rows(servers, settings)
            rows.append([
                _button("🔙بازگشت", callback_data="runtime:home", settings=settings)
            ])
            await _edit_md(
                update.callback_query,
                str(settings.get("servers_list_text") or "").strip()
                or _servers_list_default(),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:buyloc:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            server_id = int(data.rsplit(":", 1)[1])
            server = next(
                (item for item in business.list_purchase_servers() if int(item["id"]) == server_id),
                None,
            )
            if server is None:
                raise TenantBusinessError("purchase server is unavailable")
            categories = (
                business.list_plan_categories()
                if bool(settings.get("plan_categories_enabled", True))
                else []
            )
            plans = [
                p for p in business.list_plans()
                if not str(p.get("name") or "").startswith("__WHITELABEL_")
                and p.get("server_id") in (None, server_id)
            ]
            rows: list[list[TelegramInlineKeyboardButton]] = []
            if categories:
                for category in categories:
                    category_plans = [
                        p for p in plans
                        if int(p.get("category_id") or 0) == int(category["id"])
                    ]
                    if category_plans:
                        rows.append([
                            _button(
                                str(category["title"]),
                                callback_data=f"shop:buycat:{server_id}:{int(category['id'])}",
                                settings=settings,
                            )
                        ])
                if any(p.get("category_id") is None for p in plans):
                    rows.append([
                        _button(
                            "📋 سایر پلن‌ها",
                            callback_data=f"shop:buycat:{server_id}:0",
                            settings=settings,
                        )
                    ])
            else:
                rows = _plan_buttons(
                    plans,
                    settings,
                    server_id=server_id,
                )
            from TenantRuntime.UserBot import dynamic_plans
            from TenantRuntime.server_admin import ServerAdminService
            # Fallback path for fixed servers and unpriced mixed/dynamic
            # servers (priced dynamic/mixed open the builder directly).
            # When the dynamic entry is available in mixed mode it comes
            # first, mirroring the builder-first mixed screen.
            mixed = (
                ServerAdminService(business).sales(server_id)["mode"] == "mixed"
            )
            dynamic_rows = dynamic_plans.rows(business, server_id=server_id)
            rows = dynamic_rows + rows if mixed else rows + dynamic_rows
            back_row = [[
                _button(
                    "🔙 بازگشت به لوکیشن‌ها",
                    callback_data="shop:buy",
                    settings=settings,
                )
            ]]
            if not rows:
                # SellBot parity for an empty fixed catalog.
                if bool(settings.get("plan_categories_enabled", True)) and not categories:
                    empty_text = "❌ دسته‌بندی برای این سرور یافت نشد."
                else:
                    empty_text = "❌ پلن ثابت برای این سرور یافت نشد."
                await _edit_md(
                    update.callback_query,
                    empty_text,
                    reply_markup=InlineKeyboardMarkup(back_row),
                )
                return
            rows.extend(back_row)
            if mixed and dynamic_rows:
                header = (
                    "🛒 **خرید اشتراک**\n\n"
                    "🎛 **بسته دلخواه خود را بسازید یا از پلن‌های آماده استفاده کنید:**"
                )
            else:
                header = (
                    str(settings.get("plans_list_text") or "").strip()
                    or _plans_list_default()
                )
            await _edit_md(
                update.callback_query,
                header,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:buycat:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            parts = data.split(":")
            if len(parts) == 4:
                server_id = int(parts[2])
                category_id = int(parts[3])
            else:
                # Compatibility for old keyboards/messages generated before
                # v1.2.4. New flow is always location -> category -> plan.
                category_id = int(parts[2])
                server_id = 0
            if server_id:
                business._purchase_server(server_id)
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
                and (not server_id or p.get("server_id") in (None, server_id))
            ]
            rows = _plan_buttons(plans, settings, server_id=server_id)
            if not rows:
                rows = [[InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")]]
            rows.append([
                _button(
                    "🔙 بازگشت به لوکیشن‌ها" if server_id else "🔙 بازگشت به دسته‌بندی‌ها",
                    callback_data="shop:buyloc:" + str(server_id) if server_id else "shop:buy",
                    settings=settings,
                )
            ])
            await _edit_md(
                update.callback_query,
                str(settings.get("plans_list_text") or "").strip()
                or _plans_list_default(),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("shop:plan:"):
            if not bool(settings.get("enable_buy", True)):
                raise TenantBusinessError("purchase is disabled")
            parts = data.split(":")
            if len(parts) == 4:
                server_id = int(parts[2])
                plan_id = int(parts[3])
            else:
                plan_id = int(parts[2])
                server_id = 0
            plan = business.plan(plan_id, public=True)
            if server_id:
                business._purchase_server(server_id)
                if plan.get("server_id") not in (None, server_id):
                    raise TenantBusinessError("plan is not available on selected server")
            else:
                # Legacy callback: preserve the previous server-selection path.
                servers = [
                    x for x in business.list_purchase_servers()
                    if plan.get("server_id") in (None, int(x["id"]))
                ]
                rows = _purchase_server_rows(servers, plan_id=plan_id, settings=settings)
                if not rows:
                    rows = [[InlineKeyboardButton("سرور قابل خریدی موجود نیست", callback_data="noop")]]
                rows.append([
                    _button("🔙 بازگشت به پلن‌ها", callback_data="shop:buy", settings=settings)
                ])
                await update.callback_query.edit_message_text(
                    (str(settings.get("servers_list_text") or "").strip()
                     or "🛰 سرور موردنظر را انتخاب کنید:")
                    + "\n\n"
                    + f"📦 {plan['name']} · {int(plan['traffic_gb'])}GB · "
                    + f"{int(plan['duration_days'])} روز · "
                    + f"{int(plan['price']):,} {plan['currency']}",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
            order = business.create_order(actor, plan_id, server_id=server_id)
            await _send_order_summary(
                update.callback_query, business, actor, settings,
                int(order["id"]),
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
            await _send_order_summary(
                update.callback_query, business, actor, settings,
                int(order["id"]),
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
            current_plan = business.plan(int(order["plan_id"]),public=False)
            servers = [x for x in business.list_purchase_servers() if current_plan.get("server_id") in (None,int(x["id"]))]
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
            business.change_purchase_order_server(
                actor,
                order_id=order_id,
                server_id=server_id,
            )
            await _send_order_summary(
                update.callback_query, business, actor, settings, order_id
            )
            return

        if data.startswith("shop:checkout:"):
            order_id = int(data.rsplit(":", 1)[1])
            await _send_order_summary(
                update.callback_query, business, actor, settings, order_id
            ); return
        if data.startswith("shop:coupon:"):
            if not _coupon_usable(settings):
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
                "⬇️ لطفا کد کوپن خود را ارسال کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ سفارش", callback_data=f"shop:checkout:{order_id}")]
                ]),
            ); return
        if data.startswith("shop:walletpay:"):
            order_id = int(data.rsplit(":", 1)[1])
            result = business.pay_order_with_wallet(actor, order_id=order_id)
            try:
                full_order = business.order(actor, order_id)
            except Exception:
                full_order = {}
            tg_user = update.effective_user
            display_name = str(
                getattr(tg_user, "full_name", None)
                or getattr(tg_user, "username", None)
                or actor
            ).strip()
            try:
                customer_id = int(full_order.get("customer_id") or 0)
            except (TypeError, ValueError):
                customer_id = 0
            wallet_after: int | None = None
            try:
                summary = business.wallet_summary(actor)
                primary = dict(summary.get("primary_account") or {})
                wallet_after = int(primary.get("balance") or 0)
            except Exception:
                wallet_after = None
            if not result.get("fulfillment_pending"):
                try:
                    await _send_purchase_event_report(
                        business,
                        telegram_id=actor,
                        display_name=display_name,
                        order=full_order or business.order(actor, order_id),
                        result=result,
                    )
                except Exception:
                    pass
                try:
                    await _notify_admin_wallet_purchase(
                        business,
                        actor=actor,
                        order=full_order or {},
                        result=result,
                        display_name=display_name,
                        customer_id=customer_id,
                        wallet_balance_after=wallet_after,
                    )
                except Exception:
                    pass
            else:
                # Even when panel fulfillment pends, the wallet charge + paid
                # state must still reach the admin for reconciliation.
                try:
                    await _notify_admin_wallet_purchase(
                        business,
                        actor=actor,
                        order=full_order or {},
                        result=result,
                        display_name=display_name,
                        customer_id=customer_id,
                        wallet_balance_after=wallet_after,
                    )
                except Exception:
                    pass
            if result.get("fulfillment_pending"):
                text = (
                    f"✅ مبلغ سفارش #{order_id} از کیف پول پرداخت شد.\n"
                    "⚠️ فعال‌سازی پنل فعلاً انجام نشد؛ پرداخت محفوظ است و می‌توانید از «سفارش‌های من» دوباره تلاش کنید."
                )
                await update.callback_query.edit_message_text(
                    text, reply_markup=_home_inline_markup(settings)
                )
                return
            await update.callback_query.edit_message_text(
                _wallet_confirm_text(order_id),
                disable_web_page_preview=True,
            )
            try:
                await _deliver_wallet_purchase(
                    context, business, actor, settings, result
                )
            except Exception:
                pass
            return
        if data.startswith("shop:paymethods:"):
            order_id = int(data.rsplit(":", 1)[1])
            order = business.order(actor, order_id)
            if order["status"] != "pending_payment":
                raise TenantBusinessError("order is not awaiting payment")
            methods = business.list_methods(currency=str(order["currency"]))
            if len(methods) == 1:
                # SellBot parity: a single method skips the picker entirely.
                await _start_order_payment(
                    update, context, business=business, actor=actor,
                    settings=settings, order_id=order_id,
                    method_id=int(methods[0]["id"]),
                )
                return
            rows = [[InlineKeyboardButton(
                f"{m.get('provider_icon') or '💳'} {m['title']} · {m.get('provider_title') or m['kind']}",
                callback_data=f"shop:pay:{order_id}:{m['id']}"
            )] for m in methods] or [[InlineKeyboardButton("روش پرداخت موجود نیست", callback_data="noop")]]
            rows.append([InlineKeyboardButton(
                "🔙 بازگشت", callback_data=f"shop:checkout:{order_id}"
            )])
            await update.callback_query.edit_message_text(
                "💳 روش پرداخت را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
            ); return
        if data.startswith("shop:pay:"):
            _, _, order_id, method_id = data.split(":", 3)
            await _start_order_payment(
                update, context, business=business, actor=actor,
                settings=settings, order_id=int(order_id),
                method_id=int(method_id),
            )
            return
        if data == "shop:renewmenu":
            if not bool(settings.get("enable_renew", True)):
                await update.callback_query.edit_message_text(
                    "🚫 تمدید اشتراک در حال حاضر غیرفعال است.",
                    reply_markup=_home_inline_markup(settings),
                )
                return
            await update.callback_query.answer()
            visible = await _filter_existing_subscriptions(
                business, actor, business.list_subscriptions(actor), settings
            )
            items, blocked = _renewable_subscriptions(
                business, actor, items=visible
            )
            rows = _renew_menu_rows(items, settings)
            body = "👇 لطفا یکی از اشتراک‌های خود را برای تمدید انتخاب نمایید"
            if not rows:
                if blocked:
                    body = business.renewal_not_allowed_text()
                else:
                    body = "❌ اشتراک وجود ندارد."
            rows.append([_button("🔙بازگشت", callback_data="runtime:home", settings=settings)])
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
            parts = data.split(":")
            view = parts[2] if len(parts) > 2 else "all"
            page = int(parts[3]) if len(parts) > 3 else 0
            context.user_data.pop("biz_flow", None)
            status_filter = view if view in {
                "active", "expired", "disabled", "pending_provisioning"
            } else None
            raw_items = business.list_subscriptions(
                actor, status=status_filter, limit=500, offset=0,
            )
            visible_items = await _filter_existing_subscriptions(
                business, actor, raw_items, settings,
            )
            text, markup = _subscription_list(
                business, actor, settings, view=view, page=page,
                items=visible_items,
            )
            await update.callback_query.answer()
            await _edit_subscription(update.callback_query, text, reply_markup=markup)
            return

        if data.startswith(("shop:substatus:", "shop:subrefresh:", "shop:subcopy:",
                            "shop:subrename:", "shop:subrotate:", "shop:subrotateconfirm:")):
            action = data.split(":")[1]
            subscription_id = int(data.rsplit(":", 1)[1])
            try:
                item = business.customer_subscription_status(actor,
                    subscription_id=subscription_id,
                    refresh=action in {"substatus", "subrefresh"})
            except (TenantBusinessError, ValueError):
                await update.callback_query.answer()
                await _edit_subscription(
                    update.callback_query,
                    "❌ اشتراک وجود ندارد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home"),
                    ]]),
                )
                return
            if _single_subscription_visibility(business, actor, item, settings) != "visible":
                await update.callback_query.answer()
                await _edit_subscription(
                    update.callback_query,
                    "❌ اشتراک وجود ندارد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙بازگشت", callback_data="runtime:home"),
                    ]]),
                )
                return
            back = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔙 بازگشت", callback_data=f"shop:subrefresh:{subscription_id}")]])
            if action == "subcopy":
                await update.callback_query.answer()
                code = str(item.get("service_code") or "").strip() or str(subscription_id)
                await _reply_md(
                    update.effective_message,
                    f"📋 شناسه اشتراک شما:\n`{code}`",
                    reply_markup=InlineKeyboardMarkup(
                        _subscription_status_rows(business, actor, item, settings)
                    ),
                )
                return
            if action == "subrename":
                context.user_data["biz_flow"] = {"kind": "subscription_rename", "subscription_id": subscription_id}
                await update.callback_query.answer()
                await _edit_subscription(update.callback_query,
                    "✏️ نام جدید اشتراک را ارسال کنید (۳ تا ۶۴ حرف).\nبرای لغو، دکمه بازگشت را بزنید.",
                    reply_markup=back)
                return
            context.user_data.pop("biz_flow", None)
            if action != "subrotateconfirm":
                context.user_data.pop("subscription_rotation_confirmation", None)
            if action == "subrotate":
                context.user_data["subscription_rotation_confirmation"] = {
                    "subscription_id": subscription_id,
                    "external_ref": item.get("external_ref"),
                }
                await update.callback_query.answer()
                await _edit_subscription(update.callback_query,
                    "⚠️ هشدار تغییر لینک اشتراک\n\n"
                    "با تغییر لینک اشتراک، لینک و کانفیگ قبلی از کار می‌افتد و باید لینک جدید را دوباره در برنامه وارد کنید.\n\n"
                    "مصرف، حجم و تاریخ انقضا حفظ می‌شود.\n"
                    "اگر مطمئن هستید، تایید تغییر لینک را بزنید.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("تایید تغییر لینک🚨", callback_data=f"shop:subrotateconfirm:{subscription_id}")],
                        back.inline_keyboard[0]]))
                return
            notice = ""
            if action == "subrotateconfirm":
                confirmation = context.user_data.pop("subscription_rotation_confirmation", None)
                if not confirmation or confirmation.get("subscription_id") != subscription_id or confirmation.get("external_ref") != item.get("external_ref"):
                    await update.callback_query.answer("ابتدا درخواست تغییر لینک را تایید کنید.", show_alert=True)
                    return
                await update.callback_query.answer("در حال تغییر لینک…")
                try:
                    item = business.rotate_customer_subscription(actor, subscription_id=subscription_id)
                    notice = "✅ لینک اشتراک تغییر کرد؛ لینک جدید را از «کانفیگ ها» دریافت کنید.\n\n"
                except TenantBusinessError:
                    item = business.customer_subscription_status(actor, subscription_id=subscription_id, refresh=False)
                    notice = (
                        "⚠️ تغییر لینک کامل نشد. برای ادامه، «ادامه تغییر لینک» را بزنید. در صورت تکرار خطا با پشتیبانی تماس بگیرید.\n\n"
                        if item.get("rotation_pending") else
                        "⚠️ تغییر لینک برای این پنل در دسترس نیست یا ارتباط برقرار نشد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.\n\n"
                    )
            else:
                await update.callback_query.answer()
            markup = InlineKeyboardMarkup(_subscription_status_rows(business, actor, item, settings))
            text = notice + _subscription_detail_text(item, settings)
            if action == "substatus":
                # For the >3-services path, keep the simple SellBot list intact
                # and send the selected service menu as a new message.
                await _reply_md(
                    update.effective_message,
                    text,
                    reply_markup=markup,
                )
            else:
                await _edit_md(update.callback_query, text, reply_markup=markup)
            return
        if data.startswith("shop:configmenu:"):
            subscription_id = int(data.rsplit(":", 1)[1])
            item = business.customer_subscription_status(actor, subscription_id=subscription_id, refresh=False)
            rows = _subscription_config_menu_rows(
                business, actor, subscription_id, settings
            )
            rows.append([InlineKeyboardButton(
                "🔙بازگشت",
                callback_data=f"shop:subrefresh:{subscription_id}",
            )])
            code = str(item.get("service_code") or "").strip() or str(subscription_id)
            await update.callback_query.edit_message_text(
                f"📝 کانفیگ‌ها و لینک‌ها — اشتراک `{code}`\n"
                "روش موردنظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="Markdown",
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
            caption = f"{title}\n\nجهت کپی شدن لینک، متن زیر را لمس کنید 👇\n\n<code>{escape(link)}</code>"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔙 بازگشت", callback_data=f"shop:configmenu:{subscription_id}")]])
            await update.callback_query.answer()
            if len(caption) < 1000:
                import qrcode
                image = BytesIO()
                try:
                    qrcode.make(link).save(image, format="PNG")
                    image.seek(0)
                    await update.effective_message.reply_photo(image, caption=caption,
                        parse_mode="HTML", reply_markup=markup)
                    return
                except (TelegramError, ValueError, qrcode.exceptions.DataOverflowError):
                    pass
            await update.effective_message.reply_text(caption, parse_mode="HTML",
                reply_markup=markup, disable_web_page_preview=True)
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
            from TenantRuntime.UserBot import dynamic_plans
            from TenantRuntime.server_admin import ServerAdminService
            server_id = int(owned["server_id"])
            sales = ServerAdminService(business).sales(server_id)
            time_price = (
                sales["price_month"]
                if dynamic_plans._month_mode(sales)
                else sales["price_day"]
            )
            # SellBot parity: dynamic-only servers skip the plan list entirely.
            if (
                sales["mode"] == "dynamic"
                and sales["price_gb"] + time_price > 0
            ):
                await dynamic_plans._open_plan_builder(
                    update.callback_query,
                    context,
                    business=business,
                    actor=actor,
                    sid=server_id,
                    subid=subscription_id,
                    settings=settings,
                )
                return
            label = _renew_item_label(owned)
            categories = (
                business.list_plan_categories()
                if bool(settings.get("plan_categories_enabled", True))
                else []
            )
            plans = business.list_plans(server_id=server_id)
            rows: list[list[TelegramInlineKeyboardButton]] = []
            if categories:
                for category in categories:
                    category_plans = [
                        p for p in plans
                        if int(p.get("category_id") or 0) == int(category["id"])
                    ]
                    if category_plans:
                        rows.append([
                            _button(
                                str(category["title"]),
                                callback_data=(
                                    f"shop:renewcat:{subscription_id}:{int(category['id'])}"
                                ),
                                settings=settings,
                            )
                        ])
                if any(p.get("category_id") is None for p in plans):
                    rows.append([
                        _button(
                            "📋 سایر پلن‌ها",
                            callback_data=f"shop:renewcat:{subscription_id}:0",
                            settings=settings,
                        )
                    ])
            else:
                rows = _renew_plan_buttons(plans, settings, subscription_id)
            dynamic_rows = dynamic_plans.rows(business, subscription=owned)
            # SellBot parity: mixed renewal shows the dynamic entry together
            # with the fixed list, dynamic entry first.
            rows = dynamic_rows + rows if sales["mode"] == "mixed" else rows + dynamic_rows
            if not rows:
                rows = [[
                    InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")
                ]]
            rows.append([
                _button(
                    "🔙بازگشت", callback_data="shop:renewmenu", settings=settings
                )
            ])
            renew_intro = (
                str(settings.get("plans_list_text") or "").strip()
                or _plans_list_default()
            )
            await _edit_md(
                update.callback_query,
                f"♻️ تمدید «{label}»\n\n{renew_intro}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:renewcat:"):
            if not bool(settings.get("enable_renew", True)):
                raise TenantBusinessError("renewal is disabled")
            _, _, subscription_id, category_id = data.split(":", 3)
            subscription_id = int(subscription_id)
            category_id = int(category_id)
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
            owned = business.customer_subscription_status(
                actor, subscription_id=subscription_id, refresh=False
            )
            if category_id > 0:
                category = business.plan_category(category_id)
                title = f"📂 {category['title']}"
                plans = business.list_plans(
                    server_id=int(owned["server_id"]),
                    category_id=category_id,
                )
            else:
                title = "📋 سایر پلن‌ها"
                plans = [
                    p for p in business.list_plans(
                        server_id=int(owned["server_id"])
                    )
                    if p.get("category_id") is None
                ]
            rows = _renew_plan_buttons(plans, settings, subscription_id)
            if not rows:
                rows = [[
                    InlineKeyboardButton("پلنی موجود نیست", callback_data="noop")
                ]]
            rows.append([
                _button(
                    "🔙بازگشت",
                    callback_data=f"shop:renew:{subscription_id}",
                    settings=settings,
                )
            ])
            renew_intro = (
                str(settings.get("plans_list_text") or "").strip()
                or _plans_list_default()
            )
            await _edit_md(
                update.callback_query,
                f"{title}\n\n{renew_intro}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:renewplan:"):
            if not bool(settings.get("enable_renew", True)):
                raise TenantBusinessError("renewal is disabled")
            _, _, subscription_id, plan_id = data.split(":", 3)
            eligibility = business.renewal_eligibility(
                actor,
                subscription_id=int(subscription_id),
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
            order = business.create_renewal_order(
                actor,
                subscription_id=int(subscription_id),
                plan_id=int(plan_id),
            )
            await _send_order_summary(
                update.callback_query, business, actor, settings,
                int(order["id"]),
            ); return
        if data == "shop:support":
            items = business.list_tickets(actor)
            await update.callback_query.edit_message_text(
                _ticket_panel_body(settings, []),
                reply_markup=InlineKeyboardMarkup(_support_menu_rows()),
            )
            return
        if data == "shop:tickets" or data.startswith("shop:tickets:"):
            items = business.list_tickets(actor)
            try:
                page = max(1, int(data.split(":")[2])) if data.startswith("shop:tickets:") else 1
            except (ValueError, IndexError):
                page = 1
            page_size = 15
            pages = max(1, (len(items) + page_size - 1) // page_size)
            page = min(page, pages)
            rows = _user_tickets_rows(items, page, page_size)
            rows.append([
                InlineKeyboardButton("🔙بازگشت", callback_data="shop:support")
            ])
            await update.callback_query.edit_message_text(
                f"📬 تیکت‌های من\n◈ تعداد: {len(items)}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data == "shop:newticket":
            context.user_data["biz_flow"] = {
                "kind": "ticket_new_title",
                "subject": "",
                "body": "",
                "photo_file_id": "",
            }
            await update.callback_query.edit_message_text(
                "✍️ لطفا موضوع درخواست خود را ارسال نمایید:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌لغو", callback_data="shop:ticketflow:new:cancel")
                ]]),
            )
            return
        if data.startswith("shop:ticketflow:"):
            parts = data.split(":")
            mode = parts[2] if len(parts) > 2 else ""
            action = parts[3] if len(parts) > 3 else ""
            if mode not in {"new", "reply"}:
                raise TenantBusinessError("invalid ticket flow")
            flow = context.user_data.get("biz_flow")
            if not isinstance(flow, dict):
                raise TenantBusinessError("ticket flow is not active")
            if action == "cancel":
                context.user_data.pop("biz_flow", None)
                await update.callback_query.edit_message_text(
                    "❌ عملیات تیکت لغو شد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 پنل پشتیبانی", callback_data="shop:support")
                    ]]),
                )
                return
            if action == "edit":
                if mode == "new":
                    flow.update(
                        kind="ticket_new_title",
                        subject="",
                        body="",
                        photo_file_id="",
                    )
                    prompt = "✍️ لطفا موضوع درخواست خود را ارسال نمایید:"
                else:
                    ticket_id = int(flow.get("ticket_id") or 0)
                    flow.update(
                        kind="ticket_reply_text",
                        ticket_id=ticket_id,
                        reply_text="",
                        photo_file_id="",
                    )
                    prompt = "✍️ لطفا پاسخ خود را به صورت کامل ارسال نمایید:"
                await update.effective_message.reply_text(
                    prompt,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "❌لغو",
                            callback_data=f"shop:ticketflow:{mode}:cancel",
                        )
                    ]]),
                )
                return
            if action == "skip":
                expected = "ticket_new_photo" if mode == "new" else "ticket_reply_photo"
                if flow.get("kind") != expected:
                    raise TenantBusinessError("ticket photo step is not active")
                flow["photo_file_id"] = ""
                flow["kind"] = "ticket_new_confirm" if mode == "new" else "ticket_reply_confirm"
                await _show_ticket_preview(update, context, flow, mode=mode)
                return
            if action == "send":
                expected = "ticket_new_confirm" if mode == "new" else "ticket_reply_confirm"
                if flow.get("kind") != expected:
                    raise TenantBusinessError("ticket confirmation is not active")
                media = b""
                file_id = str(flow.get("photo_file_id") or "").strip()
                if file_id:
                    media = await _ticket_media_bytes(context, file_id)
                if mode == "new":
                    ticket = business.create_ticket(
                        actor,
                        subject=str(flow.get("subject") or ""),
                        body=str(flow.get("body") or ""),
                        media=media or None,
                        media_mime="image/jpeg" if media else "",
                    )
                    success = "✅ تیکت شما با موفقیت ثبت شد.\nبه زودی پاسخ داده می‌شود."
                    event = "new"
                else:
                    ticket_id = int(flow.get("ticket_id") or 0)
                    ticket = business.reply_ticket(
                        actor,
                        ticket_id=ticket_id,
                        reply=str(flow.get("reply_text") or ""),
                        media=media or None,
                        media_mime="image/jpeg" if media else "",
                    )
                    success = "✅ پاسخ شما ثبت شد."
                    event = "reply"
                context.user_data.pop("biz_flow", None)
                await _notify_admin_ticket(business, ticket=ticket, event=event)
                messages = business.ticket_messages(actor, ticket_id=int(ticket["id"]))
                await update.effective_message.reply_text(
                    success + "\n\n" + _ticket_thread_text(ticket, messages),
                    reply_markup=_ticket_detail_markup(ticket, messages),
                )
                return
            raise TenantBusinessError("invalid ticket action")
        if data.startswith("shop:ticketreply:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            ticket = business.ticket(actor, ticket_id=ticket_id)
            if str(ticket.get("status") or "") == "closed":
                raise TenantBusinessError("ticket is closed")
            context.user_data["biz_flow"] = {
                "kind": "ticket_reply_text",
                "ticket_id": ticket_id,
                "reply_text": "",
                "photo_file_id": "",
            }
            await update.callback_query.edit_message_text(
                "✍️ لطفا پاسخ خود را به صورت کامل ارسال نمایید:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌لغو", callback_data="shop:ticketflow:reply:cancel")
                ]]),
            )
            return
        if data.startswith("shop:ticketclose:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            ticket = business.close_ticket(actor, ticket_id=ticket_id)
            messages = business.ticket_messages(actor, ticket_id=ticket_id)
            await update.callback_query.edit_message_text(
                _ticket_thread_text(ticket, messages),
                reply_markup=_ticket_detail_markup(ticket, messages),
            )
            return
        if data.startswith("shop:ticketmedia:"):
            parts = data.split(":")
            ticket_id = int(parts[2])
            message_id = int(parts[3])
            media = business.ticket_message_media(
                actor,
                ticket_id=ticket_id,
                message_id=message_id,
            )
            if media is None:
                raise TenantBusinessError("ticket media not found")
            stream = BytesIO(bytes(media["media"]))
            stream.name = "ticket-image.jpg"
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=stream,
                caption=f"🖼 تصویر پیام #{message_id} · تیکت #{ticket_id}",
            )
            return
        if data.startswith("shop:ticket:"):
            ticket_id = int(data.rsplit(":", 1)[1])
            ticket = business.ticket(actor, ticket_id=ticket_id)
            messages = business.ticket_messages(actor, ticket_id=ticket_id)
            await update.callback_query.edit_message_text(
                _ticket_thread_text(ticket, messages),
                reply_markup=_ticket_detail_markup(ticket, messages),
            )
            return
        if data.startswith("shop:guidesub:"):
            # Subscription-aware guide (SellBot guide:back:s{id} parity):
            # opened from the delivery QR, returns to the same subscription.
            parts = data.split(":")
            try:
                subscription_id = int(parts[2])
            except (TypeError, ValueError, IndexError):
                raise TenantBusinessError("invalid guide request")
            platform = parts[3] if len(parts) > 3 else ""
            if not platform:
                guide = str(settings.get("guide_text") or "").strip()
                rows = [
                    [
                        InlineKeyboardButton("📱 اندروید", callback_data=f"shop:guidesub:{subscription_id}:android"),
                        InlineKeyboardButton("📱 iOS", callback_data=f"shop:guidesub:{subscription_id}:ios"),
                    ],
                    [
                        InlineKeyboardButton("🖥️ ویندوز", callback_data=f"shop:guidesub:{subscription_id}:windows"),
                        InlineKeyboardButton("💻 macOS", callback_data=f"shop:guidesub:{subscription_id}:mac"),
                    ],
                    [InlineKeyboardButton("🖥️ لینوکس", callback_data=f"shop:guidesub:{subscription_id}:linux")],
                    [InlineKeyboardButton("🔙بازگشت", callback_data=f"shop:subrefresh:{subscription_id}")],
                ]
                await update.callback_query.answer()
                await _safe_edit_text(
                    update.callback_query,
                    guide or "انتخاب سیستم عامل ⬇️",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
            guide_specs = {
                "android": ("📱 راهنمای اندروید", "guide_android_text"),
                "ios": ("📱 راهنمای iOS", "guide_ios_text"),
                "windows": ("🖥️ راهنمای ویندوز", "guide_windows_text"),
                "mac": ("💻 راهنمای macOS", "guide_mac_text"),
                "linux": ("🖥️ راهنمای Linux", "guide_linux_text"),
            }
            if platform not in guide_specs:
                raise TenantBusinessError("invalid guide platform")
            label, setting_key = guide_specs[platform]
            body = str(settings.get(setting_key) or "").strip()
            await update.callback_query.answer()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=body or label,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙بازگشت", callback_data=f"shop:guidesub:{subscription_id}")],
                    [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
                ]),
                disable_web_page_preview=True,
            )
            return
        if data == "shop:guide":
            guide = str(settings.get("guide_text") or "").strip()
            rows = [
                [
                    InlineKeyboardButton("📱 اندروید", callback_data="shop:guide:android"),
                    InlineKeyboardButton("📱 iOS", callback_data="shop:guide:ios"),
                ],
                [
                    InlineKeyboardButton("🖥️ ویندوز", callback_data="shop:guide:windows"),
                    InlineKeyboardButton("💻 macOS", callback_data="shop:guide:mac"),
                ],
                [InlineKeyboardButton("🖥️ لینوکس", callback_data="shop:guide:linux")],
                [InlineKeyboardButton("↩️ منو", callback_data="runtime:home")],
            ]
            await _safe_edit_text(
                update.callback_query,
                guide or "انتخاب سیستم عامل ⬇️",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if data.startswith("shop:guide:"):
            platform = data.rsplit(":", 1)[1]
            guide_specs = {
                "android": ("📱 راهنمای اندروید", "guide_android_text"),
                "ios": ("📱 راهنمای iOS", "guide_ios_text"),
                "windows": ("🖥️ راهنمای ویندوز", "guide_windows_text"),
                "mac": ("💻 راهنمای macOS", "guide_mac_text"),
                "linux": ("🖥️ راهنمای Linux", "guide_linux_text"),
            }
            if platform not in guide_specs:
                raise TenantBusinessError("invalid guide platform")
            label, setting_key = guide_specs[platform]
            body = str(settings.get(setting_key) or "").strip()
            await _safe_edit_text(
                update.callback_query,
                body or label,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙بازگشت", callback_data="shop:guide")],
                    [InlineKeyboardButton("🏠 منو", callback_data="runtime:home")],
                ]),
            )
            return
        if data == "shop:faq":
            await update.callback_query.edit_message_text(
                _setting_text(
                    settings,
                    "faq_text",
                    "❓ سوالات متداول\n\nهنوز متنی تنظیم نشده است.",
                ),
                reply_markup=_home_inline_markup(settings),
                disable_web_page_preview=True,
            )
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError) as exc:
        await update.callback_query.answer(
            _user_error_alert(exc), show_alert=True
        )
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
            if kind == "trial_service_name":
                if text in {"لغو", "/cancel", "بازگشت", "❌لغو", "🔙بازگشت"}:
                    context.user_data.pop("biz_flow", None)
                    await update.effective_message.reply_text(
                        "عملیات لغو شد.",
                        reply_markup=_main_keyboard(spec, business),
                    )
                    return
                service_name = text.strip()
                if not service_name:
                    await update.effective_message.reply_text(
                        "❌ لطفاً نام خود را ارسال کنید:"
                    )
                    return
                state = business.free_trial_state(actor)
                unavailable = _trial_state_message(state)
                if unavailable:
                    context.user_data.pop("biz_flow", None)
                    await update.effective_message.reply_text(
                        unavailable,
                        reply_markup=_main_keyboard(spec, business),
                    )
                    return
                try:
                    result = business.claim_free_trial(
                        actor,
                        server_id=int(flow["server_id"]),
                        service_name=service_name,
                    )
                except TenantBusinessError as exc:
                    message = str(exc)
                    if "already used" in message:
                        context.user_data.pop("biz_flow", None)
                        await update.effective_message.reply_text(
                            "🚫 شما قبلا اکانت تست رایگان خود را دریافت نموده‌اید!",
                            reply_markup=_main_keyboard(spec, business),
                        )
                        return
                    if "disabled" in message:
                        context.user_data.pop("biz_flow", None)
                        await update.effective_message.reply_text(
                            "🚫 دریافت تست رایگان در حال حاضر غیرفعال است.",
                            reply_markup=_main_keyboard(spec, business),
                        )
                        return
                    await update.effective_message.reply_text(
                        "❌ ساخت اکانت تست رایگان انجام نشد. "
                        "لطفاً وضعیت سرور را بررسی کرده و دوباره تلاش کنید.",
                        reply_markup=_main_keyboard(spec, business),
                    )
                    return
                context.user_data.pop("biz_flow", None)
                fresh_state = business.free_trial_state(actor)
                await update.effective_message.reply_text(
                    _trial_delivery_text(
                        business,
                        actor,
                        result,
                        settings,
                        announce_enabled=bool(
                            fresh_state.get("announce_enabled", True)
                        ),
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "📦 اشتراک‌های من",
                            callback_data="shop:subs",
                        )],
                        [InlineKeyboardButton(
                            "↩️ منو",
                            callback_data="runtime:home",
                        )],
                    ]),
                    disable_web_page_preview=True,
                )
                return
            if kind == "subscription_rename":
                sid = int(flow["subscription_id"])
                if text in {"لغو", "/cancel", "بازگشت", "🔙بازگشت"}:
                    context.user_data.pop("biz_flow", None)
                    await update.effective_message.reply_text("تغییر نام لغو شد.", reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 بازگشت", callback_data=f"shop:subrefresh:{sid}")]]))
                    return
                try:
                    item = business.rename_customer_subscription(actor, subscription_id=sid, name=text)
                except ValueError:
                    await update.effective_message.reply_text("نام باید ۳ تا ۶۴ حرف و بدون خط جدید یا نویسه کنترلی باشد. دوباره ارسال کنید.")
                    return
                context.user_data.pop("biz_flow", None)
                settings = business.runtime_userbot_settings()
                await _reply_md(
                    update.effective_message,
                    "✅ نام اشتراک تغییر کرد.\n\n" + _subscription_detail_text(item, settings),
                    reply_markup=InlineKeyboardMarkup(
                        _subscription_status_rows(business, actor, item, settings)
                    ),
                )
                return
            if kind == "ticket_new_title":
                if not text:
                    raise ValueError("ticket subject")
                flow["subject"] = text
                flow["kind"] = "ticket_new_body"
                await update.effective_message.reply_text(
                    "✍️ لطفا سوال خود را به صورت کامل ارسال نمایید:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌لغو", callback_data="shop:ticketflow:new:cancel")
                    ]]),
                )
                return
            if kind == "ticket_new_body":
                if not text:
                    raise ValueError("ticket body")
                flow["body"] = text
                flow["kind"] = "ticket_new_photo"
                await update.effective_message.reply_text(
                    "🖼 لطفا اسکرین‌شات خود را ارسال کنید یا روی دکمه «▶️رد کردن» کلیک کنید.",
                    reply_markup=_ticket_skip_markup("new"),
                )
                return
            if kind == "ticket_new_photo":
                await update.effective_message.reply_text(
                    "❌ لطفا عکس ارسال کنید یا روی دکمه «▶️رد کردن» بزنید.",
                    reply_markup=_ticket_skip_markup("new"),
                )
                return
            if kind == "ticket_new_confirm":
                await update.effective_message.reply_text(
                    "برای ارسال تیکت از دکمه‌های «✅ارسال» یا «✏️ویرایش» استفاده کنید.",
                    reply_markup=_ticket_confirm_markup("new"),
                )
                return
            if kind == "ticket_reply_text":
                ticket_id = int(flow.get("ticket_id") or 0)
                ticket = business.ticket(actor, ticket_id=ticket_id)
                if str(ticket.get("status") or "") == "closed":
                    raise TenantBusinessError("ticket is closed")
                if not text:
                    raise ValueError("ticket reply")
                flow["reply_text"] = text
                flow["kind"] = "ticket_reply_photo"
                await update.effective_message.reply_text(
                    "🖼 لطفا اسکرین‌شات خود را ارسال کنید یا روی دکمه «▶️رد کردن» کلیک کنید.",
                    reply_markup=_ticket_skip_markup("reply"),
                )
                return
            if kind == "ticket_reply_photo":
                await update.effective_message.reply_text(
                    "❌ لطفا عکس ارسال کنید یا روی دکمه «▶️رد کردن» بزنید.",
                    reply_markup=_ticket_skip_markup("reply"),
                )
                return
            if kind == "ticket_reply_confirm":
                await update.effective_message.reply_text(
                    "برای ارسال پاسخ از دکمه‌های «✅ارسال» یا «✏️ویرایش» استفاده کنید.",
                    reply_markup=_ticket_confirm_markup("reply"),
                )
                return
            if kind == "wallet_topup_amount":
                amount = int(text.replace(",", "").replace("٬", "").strip())
                if amount <= 0:
                    raise ValueError("invalid wallet topup")
                settings = business.runtime_userbot_settings()
                try:
                    min_tx = int(settings.get("min_transaction_toman") or 1)
                except (TypeError, ValueError):
                    min_tx = 1
                if amount < min_tx:
                    await update.effective_message.reply_text(
                        f"❌ حداقل مبلغ مجاز تراکنش {min_tx:,} تومان است.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")
                        ]]),
                    )
                    return
                method_id = int(flow["method_id"])
                currency = str(flow["currency"])
                topup = business.create_wallet_topup(
                    actor,
                    amount=amount,
                    currency=currency,
                )
                session = business.begin_wallet_topup_payment(
                    actor,
                    topup_id=int(topup["id"]),
                    method_id=method_id,
                )
                rows: list[list[TelegramInlineKeyboardButton]] = []
                text, parse_mode, tx_marker, display_rial = _receipt_start_content(
                    session,
                    amount=int(topup.get("amount") or 0),
                    currency=str(topup.get("currency") or ""),
                    settings=business.runtime_userbot_settings(),
                )
                if session["action"] == "receipt":
                    context.user_data["biz_flow"] = {
                        "kind": "wallet_receipt",
                        "topup_id": int(topup["id"]),
                        "method_id": method_id,
                        "confirmed": False,
                        "tx_marker": tx_marker,
                        "display_amount": display_rial,
                        "is_card": str(
                            dict(session.get("method") or {}).get("kind")
                            or dict(session.get("method") or {}).get("provider_key")
                            or ""
                        ) in {"card", "card_manual"},
                    }
                else:
                    context.user_data.pop("biz_flow", None)
                if parse_mode == "HTML":
                    await update.effective_message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=_receipt_confirm_keyboard(),
                    )
                    return
                if session.get("checkout_url"):
                    rows.append([InlineKeyboardButton(
                        "🌐 ورود به درگاه پرداخت",
                        url=str(session["checkout_url"]),
                    )])
                rows.append([InlineKeyboardButton("↩️ کیف پول", callback_data="shop:wallet")])
                await update.effective_message.reply_text(
                    str(session["message"]),
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
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
                    f"{m.get('provider_icon') or '💳'} {m['title']} · {m.get('provider_title') or m['kind']}",
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
                if text == BTN_RECEIPT_DONE:
                    flow["confirmed"] = True
                    await update.effective_message.reply_text(
                        "⬇️ لطفا رسید پرداخت خود را در زیر این پیام ارسال کنید:",
                        reply_markup=_receipt_cancel_keyboard(),
                    )
                    return
                if text == BTN_RECEIPT_BACK:
                    await _receipt_go_back(
                        update, business, actor, spec, flow
                    )
                    return
                if _receipt_last4_required(
                    flow, business.runtime_userbot_settings()
                ):
                    if flow.get("awaiting_last4"):
                        last4 = _parse_card_last4(text)
                        if not last4:
                            await update.effective_message.reply_text(
                                "❌ ورودی نامعتبر است.\n"
                                "لطفا ۴ رقم آخر کارت مبدا را ارسال کنید:",
                                reply_markup=_receipt_cancel_keyboard(),
                            )
                            return
                        flow["last4"] = last4
                        reference = _combine_receipt_reference(
                            flow.pop("pending_ref", ""), last4
                        )
                        file_id = str(flow.pop("pending_file_id", "") or "").strip() or None
                        photo_bytes = await _download_receipt_bytes(context, file_id or "")
                    else:
                        flow["pending_ref"] = text
                        flow["awaiting_last4"] = True
                        await update.effective_message.reply_text(
                            "🔢 لطفا ۴ رقم آخر کارت مبدا را ارسال کنید:",
                            reply_markup=_receipt_cancel_keyboard(),
                        )
                        return
                else:
                    reference = text
                    file_id = None
                    photo_bytes = b""
                topup_receipt = business.submit_wallet_topup_receipt(
                    actor,
                    topup_id=int(flow["topup_id"]),
                    method_id=int(flow["method_id"]),
                    reference=reference,
                    telegram_file_id=file_id,
                )
                await _notify_receipt_submitted(
                    update, business, actor, flow,
                    receipt_id=int(topup_receipt["id"]),
                    photo_bytes=photo_bytes,
                    context=context,
                )
                context.user_data.pop("biz_flow", None)
                await update.effective_message.reply_text(
                    "✅ تراکنش شما در انتظار تایید توسط ادمین است.\n"
                    "لطفا صبر کنید و از ارسال رسید تکراری بپرهیزید.",
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
                    + _order_summary_text(order),
                    reply_markup=_order_summary_markup(
                        order_id,
                        _summary_back_callback(order),
                        business.runtime_userbot_settings(),
                        show_coupon=True,
                    ),
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
                if text == BTN_RECEIPT_DONE:
                    flow["confirmed"] = True
                    await update.effective_message.reply_text(
                        "⬇️ لطفا رسید پرداخت خود را در زیر این پیام ارسال کنید:",
                        reply_markup=_receipt_cancel_keyboard(),
                    )
                    return
                if text == BTN_RECEIPT_BACK:
                    await _receipt_go_back(
                        update, business, actor, spec, flow
                    )
                    return
                if _receipt_last4_required(
                    flow, business.runtime_userbot_settings()
                ):
                    if flow.get("awaiting_last4"):
                        last4 = _parse_card_last4(text)
                        if not last4:
                            await update.effective_message.reply_text(
                                "❌ ورودی نامعتبر است.\n"
                                "لطفا ۴ رقم آخر کارت مبدا را ارسال کنید:",
                                reply_markup=_receipt_cancel_keyboard(),
                            )
                            return
                        flow["last4"] = last4
                        reference = _combine_receipt_reference(
                            flow.pop("pending_ref", ""), last4
                        )
                        file_id = str(flow.pop("pending_file_id", "") or "").strip() or None
                        photo_bytes = await _download_receipt_bytes(context, file_id or "")
                    else:
                        flow["pending_ref"] = text
                        flow["awaiting_last4"] = True
                        await update.effective_message.reply_text(
                            "🔢 لطفا ۴ رقم آخر کارت مبدا را ارسال کنید:",
                            reply_markup=_receipt_cancel_keyboard(),
                        )
                        return
                else:
                    reference = text
                    file_id = None
                    photo_bytes = b""
                order_receipt = business.submit_receipt(
                    actor,
                    order_id=int(flow["order_id"]),
                    method_id=int(flow["method_id"]),
                    reference=reference,
                    telegram_file_id=file_id,
                )
                await _notify_receipt_submitted(
                    update, business, actor, flow,
                    receipt_id=int(order_receipt["id"]),
                    file_id=str(file_id or ""),
                    photo_bytes=photo_bytes,
                    context=context,
                )
            else:
                raise ValueError("invalid input")
            context.user_data.pop("biz_flow", None)
            await update.effective_message.reply_text(
                "✅ تراکنش شما در انتظار تایید توسط ادمین است.\n"
                "لطفا صبر کنید و از ارسال رسید تکراری بپرهیزید.",
                reply_markup=_main_keyboard(spec, business),
            )
            return
    except (ValueError, TenantBusinessError, PermissionError, sqlite3.IntegrityError) as exc:
        message = _USER_ERROR_ALERTS.get(str(exc).strip())
        await update.effective_message.reply_text(
            message if message else "❌ قالب یا وضعیت معتبر نیست.",
            reply_markup=_main_keyboard(spec, business),
        )
        return
    if update.effective_message:
        await update.effective_message.reply_text("از منوی ربات استفاده کنید.", reply_markup=_main_keyboard(spec, business))


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept photos for payment receipts and active ticket compose/reply flows."""
    if update.effective_message is None or not update.effective_message.photo:
        return
    spec, _, _, business = _services(context)
    _set_button_settings(business.runtime_userbot_settings())
    flow = context.user_data.get("biz_flow")
    actor = int(update.effective_user.id) if update.effective_user else 0
    if not await _force_join_allowed(update, context, business):
        return
    if spec.role != "user" or not isinstance(flow, dict):
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.",
            reply_markup=_main_keyboard(spec, business),
        )
        return

    kind = str(flow.get("kind") or "")
    if kind in {"ticket_new_photo", "ticket_reply_photo"}:
        flow["photo_file_id"] = str(update.effective_message.photo[-1].file_id)
        mode = "new" if kind == "ticket_new_photo" else "reply"
        flow["kind"] = "ticket_new_confirm" if mode == "new" else "ticket_reply_confirm"
        await _show_ticket_preview(update, context, flow, mode=mode)
        return

    if kind not in ("receipt", "wallet_receipt"):
        await update.effective_message.reply_text(
            "از منوی ربات استفاده کنید.",
            reply_markup=_main_keyboard(spec, business),
        )
        return
    if _receipt_last4_required(flow, business.runtime_userbot_settings()):
        flow["pending_file_id"] = str(update.effective_message.photo[-1].file_id)
        caption = str(update.effective_message.caption or "").strip()
        if caption and not str(flow.get("pending_ref") or ""):
            flow["pending_ref"] = caption
        flow["awaiting_last4"] = True
        await update.effective_message.reply_text(
            "🔢 لطفا ۴ رقم آخر کارت مبدا را ارسال کنید:",
            reply_markup=_receipt_cancel_keyboard(),
        )
        return
    try:
        pending_file_id = str(flow.pop("pending_file_id", "") or "").strip()
        file_id = (
            pending_file_id
            or str(update.effective_message.photo[-1].file_id)
        )
        caption = str(update.effective_message.caption or "").strip()
        pending_ref = str(flow.pop("pending_ref", "") or "").strip()
        flow.pop("awaiting_last4", None)
        last4 = str(flow.get("last4") or "").strip()
        if kind == "wallet_receipt":
            receipt = business.submit_wallet_topup_receipt(
                actor,
                topup_id=int(flow["topup_id"]),
                method_id=int(flow["method_id"]),
                reference=_combine_receipt_reference(
                    pending_ref or caption, last4
                ),
                telegram_file_id=file_id,
            )
            success_text = (
                "✅ تراکنش شما در انتظار تایید توسط ادمین است.\n"
                "لطفا صبر کنید و از ارسال رسید تکراری بپرهیزید."
            )
        else:
            receipt = business.submit_receipt(
                actor,
                order_id=int(flow["order_id"]),
                method_id=int(flow["method_id"]),
                reference=_combine_receipt_reference(
                    pending_ref or caption, last4
                ),
                telegram_file_id=file_id,
            )
            success_text = (
                "✅ تراکنش شما در انتظار تایید توسط ادمین است.\n"
                "لطفا صبر کنید و از ارسال رسید تکراری بپرهیزید."
            )
        media = b""
        try:
            tg_file = await context.bot.get_file(
                str(update.effective_message.photo[-1].file_id)
            )
            media = bytes(await tg_file.download_as_bytearray())
            business.attach_payment_receipt_media(
                actor,
                payment_key=str(receipt["payment_key"]),
                media=media,
                mime_type="image/jpeg",
            )
        except Exception:
            success_text += (
                "\n⚠️ ثبت تراکنش انجام شد اما آرشیو تصویر کامل نشد؛ "
                "کد پیگیری را برای پشتیبانی نگه دارید."
            )
        await _notify_receipt_submitted(
            update, business, actor, flow,
            receipt_id=int(receipt["id"]),
            file_id=file_id,
            photo_bytes=media,
            context=context,
        )
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

