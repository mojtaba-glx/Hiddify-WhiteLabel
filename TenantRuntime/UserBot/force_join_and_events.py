"""
Phase 5 — Force-join channel enforcement + purchase event channel reporting.
Ported from Hiddify-SellBot UserBot/main.py force-join and event channel sections.
Adapted for WhiteLabel multi-tenant architecture.

This module provides:
  - Force-join enforcement per tenant (settings from business layer)
  - Per-purchase event channel reporting via AdminBot token
  - Background task registration helper (mirrors SellBot _post_init_set_menu)
  - Graceful shutdown helper (mirrors SellBot _post_shutdown_userbot)

Differences from SellBot:
  - No globals: ADMIN_ID, ADMIN_BOT_TOKEN replaced by business.owner_telegram_id
    and _sibling_admin_bot_token(business).
  - Force-join settings read from business.runtime_userbot_settings().
  - Core force-join helpers (_force_join_config, _force_join_markup,
    _force_join_membership, _force_join_allowed) are in handlers.py.
    This module builds on top of them with orchestration utilities.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Bot
from telegram.ext import Application, ContextTypes, Update

from TenantRuntime.business import TenantBusinessError

logger = logging.getLogger(__name__)


# -----------------------------------------------------------
# Event channel
# -----------------------------------------------------------

async def send_event_channel_message(
    business: Any,
    *,
    telegram_id: int,
    display_name: str,
    event_type: str,
    plan_name: str,
    server_label: str,
    traffic_gb: float,
    duration_days: int,
    amount: int,
    currency: str,
    service_code: str,
) -> None:
    """
    ارسال گزارش رویداد خرید/تمدید به کانال event_channel.
    معادل SellBot: بخش event_channel در _finalize_order.

    از AdminBot token استفاده می‌کند تا پیام با هویت AdminBot ارسال شود.
    خطاهای این تابع هیچ‌وقت checkout را خراب نمی‌کنند (best-effort).
    """
    from TenantRuntime.UserBot.handlers import _sibling_admin_bot_token  # noqa: F401

    settings = business.runtime_userbot_settings()
    if not bool(settings.get("purchase_event_channel_enabled", False)):
        return
    target = str(settings.get("purchase_event_channel_id") or "").strip()
    if not target:
        return

    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        action = {
            "renewal": "\u062a\u0645\u062f\u06cc\u062f \u0627\u0634\u062a\u0631\u0627\u06a9",
            "trial": "\u062a\u0633\u062a \u0631\u0627\u06cc\u06af\u0627\u0646",
        }.get(str(event_type or ""), "\u062e\u0631\u06cc\u062f \u0627\u0634\u062a\u0631\u0627\u06a9")

        chat_target: Any = int(target) if target.lstrip("-").isdigit() else target
        text = (
            "\U0001f4e3 \u06af\u0632\u0627\u0631\u0634 \u0631\u0648\u06cc\u062f\u0627\u062f \u0627\u0634\u062a\u0631\u0627\u06a9\n"
            f"\U0001f516 \u0646\u0648\u0639 \u0639\u0645\u0644\u06cc\u0627\u062a: {action}\n"
            f"\U0001f464 \u06a9\u0627\u0631\u0628\u0631: {display_name}\n"
            f"\U0001f194 \u0634\u0646\u0627\u0633\u0647 \u062a\u0644\u06af\u0631\u0627\u0645: {telegram_id}\n"
            f"\U0001f3f7 \u0646\u0627\u0645 \u0627\u0634\u062a\u0631\u0627\u06a9: {plan_name}\n"
            f"\U0001f6f0 \u0633\u0631\u0648\u0631: {server_label}\n"
            f"\U0001f4ca \u062d\u062c\u0645: {traffic_gb:.1f} \u06af\u06cc\u06af\u0627\u0628\u0627\u06cc\u062a\n"
            f"\u23f3 \u0632\u0645\u0627\u0646: {duration_days} \u0631\u0648\u0632\n"
            f"\U0001f4b0 \u0645\u0628\u0644\u063a: {amount:,} {currency}\n"
            f"\U0001f511 \u0634\u0646\u0627\u0633\u0647 \u0633\u0631\u0648\u06cc\u0633: {service_code}"
        )
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=chat_target, text=text)

    except TenantBusinessError:
        pass
    except Exception as exc:
        logger.debug("Event channel send failed tenant=%s: %s", business.tenant_id, exc)
    finally:
        token = ""


# -----------------------------------------------------------
# Force-join extended helpers
# -----------------------------------------------------------

async def enforce_force_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
) -> bool:
    """
    Gate: برگشت True اگر کاربر مجاز باشد، False اگر force-join لازم باشه.
    این wrapper به handlers._force_join_allowed ارجاع می‌دهد.
    معادل SellBot: _enforce_force_join
    """
    from TenantRuntime.UserBot.handlers import _force_join_allowed  # noqa: F401
    return await _force_join_allowed(update, context, business)


async def check_force_join_membership(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    settings: dict[str, Any],
) -> bool | None:
    """
    بررسی عضویت کاربر در کانال force-join.
    True=عضو، False=عضو نیست، None=پیکربندی اشتباه یا خطای API.
    معادل SellBot: _user_joined_force_channel
    """
    from TenantRuntime.UserBot.handlers import (
        _force_join_config,
        _force_join_membership,
    )
    if not bool(settings.get("force_join_enabled", False)):
        return True
    target, _url, _guide = _force_join_config(settings)
    if target is None:
        return None
    return await _force_join_membership(context, user_id=user_id, settings=settings)


# -----------------------------------------------------------
# Background task registration
# -----------------------------------------------------------

def register_background_tasks(
    application: Application,
    business: Any,
) -> None:
    """
    ثبت تمام background task ها و job_queue job ها برای یک tenant UserBot.
    معادل بلوک task-registration در SellBot: _post_init_set_menu.

    فراخوانی از post_init hook بعد از build شدن application:

        async def _post_init(app):
            register_background_tasks(app, business)
    """
    # 1. Direct-buy delivery loop
    try:
        from TenantRuntime.UserBot.direct_buy_delivery import direct_buy_delivery_loop  # noqa: F401
        existing = application.bot_data.get("_direct_buy_delivery_task")
        if existing is not None and existing.done():
            application.bot_data.pop("_direct_buy_delivery_task", None)
            existing = None
        if existing is None:
            task = application.create_task(
                direct_buy_delivery_loop(business, application.bot)
            )
            application.bot_data["_direct_buy_delivery_task"] = task
            logger.info(
                "Direct-buy delivery loop started tenant=%s", business.tenant_id
            )
    except Exception as exc:
        logger.warning("Failed to start direct-buy delivery loop: %s", exc)

    # 2. Pending card payment admin notifier job
    try:
        from TenantRuntime.UserBot.card_payment import pending_card_admin_notify_job  # noqa: F401
        jq = application.job_queue
        key = "_card_notify_job_registered"
        if jq is not None and not application.bot_data.get(key):
            jq.run_repeating(
                pending_card_admin_notify_job,
                interval=25,
                first=8,
                name=f"pending-card-admin-notifier-{business.tenant_id}",
            )
            application.bot_data[key] = True
            logger.info(
                "Card admin notifier job registered tenant=%s", business.tenant_id
            )
    except Exception as exc:
        logger.warning("Failed to register card notifier job: %s", exc)


async def shutdown_background_tasks(application: Application) -> None:
    """
    خاموش کردن graceful تمام background task ها.
    معادل SellBot: _post_shutdown_userbot.

    فراخوانی از post_shutdown hook:

        async def _post_shutdown(app):
            await shutdown_background_tasks(app)
    """
    import asyncio
    try:
        task = application.bot_data.pop("_direct_buy_delivery_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Direct-buy loop shutdown error: %s", exc)
            logger.info("Direct-buy delivery loop stopped.")
    except Exception as exc:
        logger.warning("shutdown_background_tasks error: %s", exc)


# -----------------------------------------------------------
# Ticket auto-close job registration
# -----------------------------------------------------------

def register_ticket_autoclose_job(
    application: Application,
    *,
    enabled: bool = True,
    autoclose_hours: int = 24,
    interval_seconds: int = 600,
    tenant_id: str = "",
) -> None:
    """
    ثبت job بستن خودکار تیکت‌های استیل.
    معادل SellBot: USERBOT_TICKET_AUTOCLOSE_* block در _post_init_set_menu.
    """
    if not enabled:
        return
    jq = application.job_queue
    if jq is None:
        return
    key = "_ticket_autoclose_job_registered"
    if application.bot_data.get(key):
        return
    try:
        from TenantRuntime.UserBot.handlers import _ticket_autoclose_job  # type: ignore[attr-defined]
    except ImportError:
        logger.debug("_ticket_autoclose_job not found in handlers — skipping")
        return
    try:
        jq.run_repeating(
            _ticket_autoclose_job,
            interval=max(60, int(interval_seconds)),
            first=45,
            name=f"userbot-ticket-autoclose-{tenant_id}",
        )
        application.bot_data[key] = True
        logger.info(
            "Ticket auto-close job registered tenant=%s threshold=%sh interval=%ss",
            tenant_id, autoclose_hours, max(60, interval_seconds),
        )
    except Exception as exc:
        logger.warning("Failed to register ticket autoclose job: %s", exc)
