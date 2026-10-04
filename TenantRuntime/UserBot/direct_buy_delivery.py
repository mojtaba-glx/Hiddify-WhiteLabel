"""
Phase 3 — Direct-buy payment delivery loop.
Ported from Hiddify-SellBot UserBot/main.py:
  _direct_buy_delivery_loop
  _process_approved_direct_buy_payments
  _deliver_direct_buy_after_sms_notice
  _warn_admin_direct_delivery_exhausted

Multi-tenant adapter:
  - No global ADMIN_ID / ADMIN_BOT_TOKEN globals.
  - Uses business.owner_telegram_id and _sibling_admin_bot_token(business).
  - Relies on business layer for order/fulfillment management.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from TenantRuntime.business import TenantBusinessError

logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# Retry policy (mirrors SellBot env vars)
# -----------------------------------------------------------
DIRECT_DELIVERY_MAX_RETRIES: int = 5
DIRECT_DELIVERY_RETRY_DELAY_SECONDS: float = 60.0
DIRECT_DELIVERY_LOOP_INTERVAL_SECONDS: float = 6.0


# -----------------------------------------------------------
# Admin warn helpers
# -----------------------------------------------------------

async def _warn_admin_direct_delivery_exhausted(
    business: Any,
    *,
    order_id: int,
    subscription_id: int,
    telegram_id: int,
    attempts: int,
) -> None:
    """
    اخطار به ادمین وقتی تحویل خرید مستقیم پس از چند تلاش شکست خورده.
    معادل SellBot: _warn_admin_direct_delivery_exhausted
    """
    from TenantRuntime.UserBot.handlers import _sibling_admin_bot_token  # noqa: F401

    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=(
                    "\u26a0\ufe0f \u062a\u062d\u0648\u06cc\u0644 \u062e\u0631\u06cc\u062f \u0645\u0633\u062a\u0642\u06cc\u0645 \u067e\u0633 \u0627\u0632 \u0686\u0646\u062f \u062a\u0644\u0627\u0634 \u0645\u0648\u0641\u0642 \u0646\u0634\u062f.\n"
                    f"\U0001f194 \u0633\u0641\u0627\u0631\u0634: {order_id}\n"
                    f"\U0001f4e6 \u0627\u0634\u062a\u0631\u0627\u06a9: {subscription_id}\n"
                    f"\U0001f464 Telegram ID: {telegram_id}\n"
                    f"\U0001f504 \u062a\u0639\u062f\u0627\u062f \u062a\u0644\u0627\u0634: {attempts}\n\n"
                    "\u0644\u0637\u0641\u0627\u064b \u0648\u0636\u0639\u06cc\u062a \u0633\u0631\u0648\u0631/\u067e\u0646\u0644 \u0631\u0627 \u0628\u0631\u0631\u0633\u06cc \u0648 \u062f\u0631 \u0635\u0648\u0631\u062a \u0646\u06cc\u0627\u0632 \u062f\u0633\u062a\u06cc \u062a\u062d\u0648\u06cc\u0644 \u062f\u0647\u06cc\u062f."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "\U0001f4e6 \u0628\u0631\u0631\u0633\u06cc \u0633\u0641\u0627\u0631\u0634",
                        callback_data=f"admin:order:{order_id}",
                    )
                ]]),
            )
    except Exception as exc:
        logger.warning(
            "Failed to warn admin delivery_exhausted order=%s: %s",
            order_id, exc,
        )
    finally:
        token = ""


async def _warn_admin_pending_node_sync(
    business: Any,
    *,
    server_id: int,
    failed_servers: list[str],
    service_label: str,
) -> None:
    """
    اگر تمدید/ساخت روی بعضی نودها با خطا مواجه شد، به ادمین هشدار بده.
    معادل SellBot: _warn_admin_pending_node_sync
    """
    from TenantRuntime.UserBot.handlers import _sibling_admin_bot_token  # noqa: F401

    if not failed_servers:
        return
    token = ""
    try:
        token = _sibling_admin_bot_token(business)
        fail_lines = "\n".join(
            f"\u2022 {t}" for t in list(dict.fromkeys(failed_servers))[:10]
        )
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=(
                    f"\u26a0\ufe0f {service_label} \u0631\u0648\u06cc \u0628\u0639\u0636\u06cc \u0646\u0648\u062f\u0647\u0627 \u062a\u062d\u0648\u06cc\u0644 \u0646\u0634\u062f.\n\n"
                    f"\u0633\u0631\u0648\u0631\u0647\u0627\u06cc \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u0628\u0648\u062f\u0646\u062f:\n{fail_lines}\n\n"
                    "\u0633\u0631\u0648\u06cc\u0633 \u0631\u0648\u06cc \u0628\u0642\u06cc\u0647 \u0646\u0648\u062f\u0647\u0627 \u062a\u062d\u0648\u06cc\u0644 \u062f\u0627\u062f\u0647 \u0634\u062f."
                    " \u0628\u0639\u062f \u0627\u0632 \u0628\u0627\u0632\u06af\u0634\u062a \u0622\u0646 \u0633\u0631\u0648\u0631\u0647\u0627\u060c"
                    " \u0628\u0631\u0631\u0633\u06cc \u06a9\u0646\u06cc\u062f \u0648 \u06a9\u0627\u0631\u0628\u0631\u0627\u0646 \u062c\u0627\u0627\u0641\u062a\u0627\u062f\u0647 \u0631\u0627 \u0628\u0633\u0627\u0632\u06cc\u062f."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "\U0001f504 \u0647\u0645\u06af\u0627\u0645\u200c\u0633\u0627\u0632\u06cc \u0646\u0648\u062f\u0647\u0627",
                        callback_data=f"admin:server:{server_id}:sync_nodes_missing",
                    )
                ]]),
            )
    except Exception as exc:
        logger.warning(
            "Failed to warn admin node_sync server_id=%s: %s",
            server_id, exc,
        )
    finally:
        token = ""


# -----------------------------------------------------------
# Core delivery processing
# -----------------------------------------------------------

async def _process_approved_direct_buy_payments(
    business: Any,
    bot: Any,
) -> None:
    """
    اسکن سفارش‌های تأییدشده با تحویل معلق و ارائه سرویس Hiddify.
    معادل SellBot: _process_approved_direct_buy_payments

    به‌جای دسترسی مستقیم به DB، از لایه business استفاده می‌کند تا:
    - multi-tenant باشد
    - لاجیک فعال‌سازی پنل را ادمین‌بات نداشته باشد
    """
    try:
        pending_orders = business.list_pending_fulfillment_orders()
    except Exception as exc:
        logger.warning("list_pending_fulfillment_orders failed: %s", exc)
        return

    for order in pending_orders or []:
        order_id = int(order.get("id") or 0)
        if order_id <= 0:
            continue

        telegram_id = int(order.get("customer_telegram_id") or 0)
        subscription_id = int(order.get("subscription_id") or 0)
        attempts = int(order.get("fulfillment_attempts") or 0)

        try:
            result = business.fulfill_order(order_id=order_id)
            if not result:
                continue

            # Notify user with access info
            if telegram_id > 0:
                try:
                    from TenantRuntime.UserBot.handlers import _delivery_access_text  # noqa: F401
                    settings = business.runtime_userbot_settings()
                    access_text = _delivery_access_text(
                        business, telegram_id, result, settings
                    )
                    await bot.send_message(
                        chat_id=telegram_id,
                        text="\u2705 \u0633\u0631\u0648\u06cc\u0633 \u0634\u0645\u0627 \u0622\u0645\u0627\u062f\u0647 \u0634\u062f!\n\n" + access_text,
                        disable_web_page_preview=True,
                    )
                except Exception as notify_exc:
                    logger.warning(
                        "notify user failed order=%s user=%s: %s",
                        order_id, telegram_id, notify_exc,
                    )

            # Event channel (best-effort)
            try:
                from TenantRuntime.UserBot.handlers import _send_purchase_event_report  # noqa: F401
                display_name = str(
                    order.get("customer_display_name")
                    or order.get("customer_username")
                    or str(telegram_id)
                )
                await _send_purchase_event_report(
                    business,
                    telegram_id=telegram_id,
                    display_name=display_name,
                    order=order,
                    result=result,
                )
            except Exception:
                pass

            logger.info(
                "Direct-buy delivered order=%s subscription=%s user=%s",
                order_id, subscription_id, telegram_id,
            )

        except TenantBusinessError as biz_exc:
            attempts += 1
            logger.warning(
                "Delivery failed order=%s attempt=%s: %s",
                order_id, attempts, biz_exc,
            )
            try:
                business.record_fulfillment_attempt(
                    order_id=order_id, error=str(biz_exc)
                )
            except Exception:
                pass

            if attempts >= DIRECT_DELIVERY_MAX_RETRIES:
                try:
                    await _warn_admin_direct_delivery_exhausted(
                        business,
                        order_id=order_id,
                        subscription_id=subscription_id,
                        telegram_id=telegram_id,
                        attempts=attempts,
                    )
                except Exception:
                    pass

        except Exception as exc:
            logger.warning(
                "Unexpected error during delivery order=%s: %s",
                order_id, exc,
            )


async def _deliver_direct_buy_after_sms_notice(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    telegram_id: int,
    order_id: int,
) -> None:
    """
    ارائه فوری سرویس وقتی پرداخت auto-approve شد (بدون نیاز به بررسی ادمین).
    معادل SellBot: _deliver_direct_buy_after_sms_notice

    فراخوانی‌پذیر بلافاصله بعد از تأیید خودکار کارت‌به‌کارت.
    """
    try:
        result = business.fulfill_order(order_id=order_id)
        if not result:
            return

        settings = business.runtime_userbot_settings()
        from TenantRuntime.UserBot.handlers import _delivery_access_text  # noqa: F401
        access_text = _delivery_access_text(business, telegram_id, result, settings)

        await context.bot.send_message(
            chat_id=telegram_id,
            text="\u2705 \u067e\u0631\u062f\u0627\u062e\u062a \u062a\u0623\u06cc\u06cc\u062f \u0634\u062f! \u0633\u0631\u0648\u06cc\u0633 \u0634\u0645\u0627 \u0622\u0645\u0627\u062f\u0647 \u0627\u0633\u062a:\n\n" + access_text,
            disable_web_page_preview=True,
        )

        # Event channel (best-effort)
        try:
            from TenantRuntime.UserBot.handlers import _send_purchase_event_report  # noqa: F401
            order_info = business.customer_order(telegram_id, order_id=order_id)
            display_name = str(
                order_info.get("customer_display_name") or str(telegram_id)
            )
            await _send_purchase_event_report(
                business,
                telegram_id=telegram_id,
                display_name=display_name,
                order=order_info,
                result=result,
            )
        except Exception:
            pass

    except TenantBusinessError as exc:
        logger.warning(
            "Immediate delivery failed order=%s user=%s: %s",
            order_id, telegram_id, exc,
        )
    except Exception as exc:
        logger.warning(
            "Unexpected error in immediate delivery order=%s: %s",
            order_id, exc,
        )


# -----------------------------------------------------------
# Background loop (asyncio task)
# -----------------------------------------------------------

async def direct_buy_delivery_loop(business: Any, bot: Any) -> None:
    """
    Background asyncio task — هر 6 ثانیه سفارش‌های معلق را پردازش می‌کند.
    معادل SellBot: _direct_buy_delivery_loop

    راه‌اندازی:
        task = application.create_task(
            direct_buy_delivery_loop(business, application.bot)
        )
        application.bot_data['_direct_buy_delivery_task'] = task

    خاموش کردن:
        task.cancel(); await task  (در post_shutdown)
    """
    while True:
        try:
            await _process_approved_direct_buy_payments(business, bot)
        except asyncio.CancelledError:
            logger.info("Direct-buy delivery loop cancelled.")
            return
        except Exception as exc:
            logger.warning("Direct-buy delivery loop error: %s", exc)
        await asyncio.sleep(DIRECT_DELIVERY_LOOP_INTERVAL_SECONDS)
