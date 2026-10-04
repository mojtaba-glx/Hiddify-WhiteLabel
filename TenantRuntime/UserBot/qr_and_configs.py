"""
Phase 4 — QR code delivery, direct config delivery, sub HTTP server.
Ported from Hiddify-SellBot UserBot/main.py subscription config sections.
Adapted for WhiteLabel multi-tenant architecture.

This module handles:
  - QR code image generation + send for subscription link
  - Direct config URI delivery (individual vless://, vmess://, etc.)
  - Auto/smart subscription link delivery
  - Sub HTTP server integration (tenant-aware)

Differences from SellBot:
  - No global SUB_SERVICE_BASE_URL / SUB_SERVER_* globals.
  - Uses business.subscription_link(), business.subscription_raw_content() etc.
  - QR generation uses same Shared.qr_utils.make_qr_image as SellBot.
"""

from __future__ import annotations

import io
import logging
import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, Update

from TenantRuntime.business import TenantBusinessError

logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# Config URI detection
# -----------------------------------------------------------

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

MAX_CONFIGS_PER_SEND = 20  # same practical limit as SellBot


def _extract_config_items(content: str) -> list[str]:
    """
    اگر محتوای ساب چندین URI جداگانه باشد، آن‌ها را جدا برمی‌گرداند.
    در غیر این صورت (base64 یا مخلوط) به‌صورت یک بلوک برمی‌گرداند.
    معادل handlers.py: _extract_config_items
    """
    raw = str(content or "").strip()
    if not raw:
        return []
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    uri_lines = [
        line for line in lines
        if line.lower().startswith(_CONFIG_URI_PREFIXES)
    ]
    if len(uri_lines) >= 2 and len(uri_lines) == len(lines):
        return uri_lines
    return [raw]


# -----------------------------------------------------------
# QR code
# -----------------------------------------------------------

async def send_qr_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
) -> None:
    """
    تولید QR از لینک اشتراک و ارسال به کاربر.
    معادل SellBot: بخش نمایش QR در اکشن‌های کانفیگ.
    """
    try:
        link = business.subscription_link(actor, subscription_id=subscription_id)
    except TenantBusinessError as exc:
        await _reply_error(update, context, str(exc))
        return

    link = str(link or "").strip()
    if not link:
        await _reply_error(update, context, "\u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u06cc\u0633\u062a.")
        return

    back_kb = _back_kb(subscription_id)

    try:
        from Shared.qr_utils import make_qr_image  # type: ignore[import]
        qr_bytes: bytes = make_qr_image(link)
        bio = io.BytesIO(qr_bytes)
        bio.name = "subscription_qr.png"
        bio.seek(0)
    except Exception as qr_exc:
        logger.warning(
            "QR generation failed subscription=%s: %s",
            subscription_id, qr_exc,
        )
        await _send_or_edit(
            update, context,
            text=f"\U0001f517 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9:\n\n<code>{link}</code>",
            parse_mode="HTML",
            reply_markup=back_kb,
            disable_web_page_preview=True,
        )
        return

    if update.callback_query:
        await update.callback_query.answer()
    chat_id = update.effective_chat.id if update.effective_chat else actor
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=bio,
        caption="\U0001f4f7 QR \u06a9\u062f \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u0634\u0645\u0627",
        reply_markup=back_kb,
    )
    logger.info("QR sent subscription=%s user=%s", subscription_id, actor)


# -----------------------------------------------------------
# Direct configs
# -----------------------------------------------------------

async def send_direct_configs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
) -> None:
    """
    دریافت محتوای خام ساب، تقسیم به URI های جداگانه و ارسال هر کدام.
    معادل SellBot: بخش ارسال کانفیگ‌های مستقیم.
    """
    if not bool(settings.get("show_direct_config", True)):
        if update.callback_query:
            await update.callback_query.answer(
                "\u0646\u0645\u0627\u06cc\u0634 \u06a9\u0627\u0646\u0641\u06cc\u06af \u0645\u0633\u062a\u0642\u06cc\u0645 \u062a\u0648\u0633\u0637 \u0627\u062f\u0645\u06cc\u0646 \u063a\u06cc\u0631\u0641\u0639\u0627\u0644 \u0627\u0633\u062a.",
                show_alert=True,
            )
        return

    try:
        raw_content = business.subscription_raw_content(
            actor, subscription_id=subscription_id
        )
    except TenantBusinessError as exc:
        if update.callback_query:
            await update.callback_query.answer(str(exc), show_alert=True)
        else:
            await _reply_error(update, context, str(exc))
        return

    configs = _extract_config_items(str(raw_content or "").strip())
    if not configs:
        if update.callback_query:
            await update.callback_query.answer("\u06a9\u0627\u0646\u0641\u06cc\u06af\u06cc \u06cc\u0627\u0641\u062a \u0646\u0634\u062f.", show_alert=True)
        return

    if update.callback_query:
        await update.callback_query.answer()

    if bool(settings.get("shuffle_configs", True)) and len(configs) > 1:
        configs = list(configs)
        random.shuffle(configs)
    configs = configs[:MAX_CONFIGS_PER_SEND]

    chat_id = update.effective_chat.id if update.effective_chat else actor
    back_kb = _back_kb(subscription_id)

    for idx, cfg in enumerate(configs):
        is_last = idx == len(configs) - 1
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"<code>{cfg}</code>",
                parse_mode="HTML",
                reply_markup=back_kb if is_last else None,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send config #%s: %s", idx + 1, exc)

    logger.info(
        "Sent %s direct config(s) subscription=%s user=%s",
        len(configs), subscription_id, actor,
    )


# -----------------------------------------------------------
# Auto / Smart sub link
# -----------------------------------------------------------

async def send_auto_sub_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
) -> None:
    """ارسال لینک اشتراک خودکار (auto sub). معادل SellBot: auto-sub delivery."""
    if not bool(settings.get("show_auto_sub_link", False)):
        if update.callback_query:
            await update.callback_query.answer(
                "\u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u062e\u0648\u062f\u06a9\u0627\u0631 \u062a\u0648\u0633\u0637 \u0627\u062f\u0645\u06cc\u0646 \u063a\u06cc\u0631\u0641\u0639\u0627\u0644 \u0627\u0633\u062a.",
                show_alert=True,
            )
        return
    try:
        link = business.automatic_subscription_link(
            actor, subscription_id=subscription_id
        )
    except TenantBusinessError as exc:
        if update.callback_query:
            await update.callback_query.answer(str(exc), show_alert=True)
        return

    await _send_or_edit(
        update, context,
        text=f"\U0001f916 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u062e\u0648\u062f\u06a9\u0627\u0631:\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=_back_kb(subscription_id),
        disable_web_page_preview=True,
    )


async def send_smart_sub_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
    base64_output: bool = False,
) -> None:
    """ارسال لینک اشتراک هوشمند multi-server. معادل SellBot: smart sub link."""
    key = "show_multi_server_b64" if base64_output else "show_multi_server"
    if not bool(settings.get(key, False)):
        if update.callback_query:
            await update.callback_query.answer(
                "\u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u0647\u0648\u0634\u0645\u0646\u062f \u062a\u0648\u0633\u0637 \u0627\u062f\u0645\u06cc\u0646 \u063a\u06cc\u0631\u0641\u0639\u0627\u0644 \u0627\u0633\u062a.",
                show_alert=True,
            )
        return
    try:
        link = business.smart_subscription_link(
            actor,
            subscription_id=subscription_id,
            base64_output=base64_output,
        )
    except TenantBusinessError as exc:
        if update.callback_query:
            await update.callback_query.answer(str(exc), show_alert=True)
        return

    label = "\U0001f310 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u0647\u0648\u0634\u0645\u0646\u062f b64" if base64_output else "\U0001f310 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u0647\u0648\u0634\u0645\u0646\u062f"
    await _send_or_edit(
        update, context,
        text=f"{label}:\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=_back_kb(subscription_id),
        disable_web_page_preview=True,
    )


async def send_sub_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    subscription_id: int,
    settings: dict[str, Any],
    base64_output: bool = False,
) -> None:
    """ارسال لینک اشتراک معمولی یا b64. معادل SellBot: sub link / sub link b64."""
    default_enabled = not base64_output
    key = "show_sub_link_b64" if base64_output else "show_sub_link"
    if not bool(settings.get(key, default_enabled)):
        if update.callback_query:
            await update.callback_query.answer(
                "\u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 \u062a\u0648\u0633\u0637 \u0627\u062f\u0645\u06cc\u0646 \u063a\u06cc\u0631\u0641\u0639\u0627\u0644 \u0627\u0633\u062a.",
                show_alert=True,
            )
        return
    try:
        if base64_output:
            link = business.subscription_link_b64(actor, subscription_id=subscription_id)
        else:
            link = business.subscription_link(actor, subscription_id=subscription_id)
    except TenantBusinessError as exc:
        if update.callback_query:
            await update.callback_query.answer(str(exc), show_alert=True)
        return

    label = "\U0001f510 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9 b64" if base64_output else "\U0001f517 \u0644\u06cc\u0646\u06a9 \u0627\u0634\u062a\u0631\u0627\u06a9"
    await _send_or_edit(
        update, context,
        text=f"{label}:\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=_back_kb(subscription_id),
        disable_web_page_preview=True,
    )


# -----------------------------------------------------------
# Sub HTTP server
# -----------------------------------------------------------

def start_tenant_sub_server(
    business: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """
    راه‌اندازی HTTP server برای سرو لینک‌های ساب (هر tenant جداگانه).
    در WhiteLabel در سطح tenant runner مدیریت می‌شود.
    معادل SellBot: sub_http_server.start_sub_server() در main().
    این تابع یک wrapper stub است — فعال‌سازی واقعی در runtime runner.
    """
    try:
        settings = business.runtime_userbot_settings()
        enabled = bool(settings.get("sub_server_enabled", True))
        if not enabled:
            logger.debug("Sub server disabled for tenant %s", business.tenant_id)
            return
        logger.info(
            "Sub server managed by runner for tenant %s (host=%s port=%s)",
            business.tenant_id, host, port,
        )
    except Exception as exc:
        logger.warning("start_tenant_sub_server: %s", exc)


# -----------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------

def _back_kb(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "\U0001f519 \u0628\u0627\u0632\u06af\u0634\u062a",
            callback_data=f"shop:configmenu:{int(subscription_id)}",
        )
    ]])


async def _reply_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(f"\u274c {message}")


async def _send_or_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    **kwargs: Any,
) -> None:
    """ویرایش پیام موجود اگر callback_query باشد، وگرنه ارسال پیام جدید."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
            return
        except Exception:
            pass
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    elif update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)
