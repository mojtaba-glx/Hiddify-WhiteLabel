"""Card payment helpers – Phase 2 SellBot parity.

Adapts SellBot UserBot/main.py card-to-card payment flow for WhiteLabel
multi-tenant architecture. Replaces global singletons (userbot_db, ADMIN_ID,
ADMIN_BOT_TOKEN) with per-tenant `business` object.

Provides:
  finalize_pending_card_payment   – submit receipt + archive media + notify admin
  finalize_pending_wallet_topup   – same for wallet top-up flow
  pending_card_admin_notify_job   – PTB job_queue periodic admin reminder
  card_payment_result_text        – user-facing result message after submission
  build_card_payment_instruction  – card details shown before user transfers

Usage (register job in handlers.py post_init):
    from TenantRuntime.UserBot.card_payment import pending_card_admin_notify_job
    app.job_queue.run_repeating(pending_card_admin_notify_job, interval=25, first=8,
                                name='pending-card-admin-notifier')
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from telegram import Bot, Update
from telegram.ext import ContextTypes

from TenantRuntime.business import TenantBusinessError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _get_admin_bot_token(business: Any) -> str:
    """Retrieve the sibling AdminBot token via the shared handler helper."""
    try:
        from TenantRuntime.UserBot.handlers import _sibling_admin_bot_token
        return _sibling_admin_bot_token(business)
    except Exception as exc:
        raise TenantBusinessError(f"Cannot resolve AdminBot token: {exc}") from exc


# ---------------------------------------------------------------------------
# Admin notification
# ---------------------------------------------------------------------------

async def _notify_admin_card_payment(
    business: Any,
    *,
    order_id: int,
    amount_toman: int,
    user_display: str,
    telegram_id: int,
    flow: str = "buy",
) -> None:
    """
    Best-effort notification to AdminBot when a card payment receipt is submitted.
    SellBot parity: mirrors _notify_admin_new_ticket pattern but for payments.
    """
    token = ""
    try:
        token = _get_admin_bot_token(business)
        flow_label = {
            "buy": "🛒 خرید اشتراک",
            "buy_payment": "🛒 خرید اشتراک",
            "renewal": "♾ تمدید اشتراک",
            "direct_buy_payment": "📦 خرید مستقیم",
            "wallet_topup": "💰 شارژ کیف پول",
        }.get(str(flow or ""), "💳 پرداخت")
        text = (
            "💳 رسید کارت به کارت جدید\n"
            f"🔖 نوع: {flow_label}\n"
            f"🧾 سفارش: #{int(order_id)}\n"
            f"👤 کاربر: {user_display}\n"
            f"📱 تلگرام: {int(telegram_id)}\n"
            f"💰 مبلغ: {int(amount_toman):,} تومان\n\n"
            "⏳ در انتظار تایید شماست."
        )
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=int(business.owner_telegram_id),
                text=text,
            )
    except Exception as exc:
        logger.debug("_notify_admin_card_payment failed (non-critical): %s", exc)
    finally:
        token = ""


# ---------------------------------------------------------------------------
# Core card payment finalizer (order flow)
# ---------------------------------------------------------------------------

async def finalize_pending_card_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    order_id: int,
    method_id: int,
    photo_file_id: str,
    amount_toman: int,
    payer_last4: str = "",
    flow: str = "buy",
    extra_meta: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """
    SellBot _finalize_pending_card_payment parity.

    Steps:
      1. Download receipt image from Telegram.
      2. Submit receipt via business.submit_receipt().
      3. Archive media via business.attach_payment_receipt_media().
      4. Notify AdminBot.
      5. Return (auto_approved: bool, status: str).

    status: "approved" | "pending" | "rejected"
    """
    auto_approved = False
    status = "pending"
    photo_bytes: bytes = b""
    payment_key = ""

    # 1. Download receipt image (best-effort)
    try:
        tg_file = await context.bot.get_file(str(photo_file_id))
        photo_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning("Card receipt download failed (continuing): %s", exc)

    # 2. Submit receipt
    try:
        reference = str(payer_last4 or "").strip() or None
        receipt = business.submit_receipt(
            actor,
            order_id=int(order_id),
            method_id=int(method_id),
            reference=reference,
            telegram_file_id=str(photo_file_id),
        )
        payment_key = str(receipt.get("payment_key") or "")
        status = str(receipt.get("status") or "pending").strip().lower()
        auto_approved = status == "approved"
    except TenantBusinessError as exc:
        logger.warning("submit_receipt failed for order %s: %s", order_id, exc)
        status = "pending"

    # 3. Archive media
    if photo_bytes and payment_key:
        try:
            business.attach_payment_receipt_media(
                actor,
                payment_key=payment_key,
                media=photo_bytes,
                mime_type="image/jpeg",
            )
        except Exception as exc:
            logger.warning("attach_payment_receipt_media failed: %s", exc)

    # 4. Notify admin
    user = update.effective_user
    display = str(getattr(user, "full_name", None) or str(actor)).strip()
    await _notify_admin_card_payment(
        business,
        order_id=int(order_id),
        amount_toman=int(amount_toman),
        user_display=display,
        telegram_id=int(actor),
        flow=flow,
    )

    return auto_approved, status


# ---------------------------------------------------------------------------
# Wallet top-up finalizer
# ---------------------------------------------------------------------------

async def finalize_pending_wallet_topup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    topup_id: int,
    method_id: int,
    photo_file_id: str,
    amount_toman: int,
    payer_last4: str = "",
) -> tuple[bool, str]:
    """
    SellBot wallet top-up card payment finalizer.
    Mirrors finalize_pending_card_payment but calls submit_wallet_topup_receipt.
    """
    auto_approved = False
    status = "pending"
    photo_bytes: bytes = b""
    payment_key = ""

    try:
        tg_file = await context.bot.get_file(str(photo_file_id))
        photo_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning("Wallet topup receipt download failed: %s", exc)

    try:
        reference = str(payer_last4 or "").strip() or None
        receipt = business.submit_wallet_topup_receipt(
            actor,
            topup_id=int(topup_id),
            method_id=int(method_id),
            reference=reference,
            telegram_file_id=str(photo_file_id),
        )
        payment_key = str(receipt.get("payment_key") or "")
        status = str(receipt.get("status") or "pending").strip().lower()
        auto_approved = status == "approved"
    except TenantBusinessError as exc:
        logger.warning("submit_wallet_topup_receipt failed for topup %s: %s", topup_id, exc)
        status = "pending"

    if photo_bytes and payment_key:
        try:
            business.attach_payment_receipt_media(
                actor,
                payment_key=payment_key,
                media=photo_bytes,
                mime_type="image/jpeg",
            )
        except Exception as exc:
            logger.warning("attach wallet topup media failed: %s", exc)

    user = update.effective_user
    display = str(getattr(user, "full_name", None) or str(actor)).strip()
    await _notify_admin_card_payment(
        business,
        order_id=int(topup_id),
        amount_toman=int(amount_toman),
        user_display=display,
        telegram_id=int(actor),
        flow="wallet_topup",
    )

    return auto_approved, status


# ---------------------------------------------------------------------------
# Periodic admin reminder job
# ---------------------------------------------------------------------------

async def pending_card_admin_notify_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    PTB job_queue periodic callback.
    SellBot parity: _pending_card_admin_notify_job (interval=25s, first=8s).

    Re-notifies admin about card payments pending review too long.
    Requires context.bot_data["business"] populated at startup.
    """
    try:
        business = context.bot_data.get("business")
        if business is None:
            return

        settings = business.runtime_userbot_settings()
        if not bool(settings.get("pending_card_notify_admin_enabled", True)):
            return

        threshold_minutes = max(1, int(
            settings.get("pending_card_notify_threshold_minutes") or 5
        ))

        try:
            pending = business.list_pending_card_payments(
                older_than_minutes=threshold_minutes,
                notified=False,
            )
        except (AttributeError, TenantBusinessError):
            return  # method not yet in this WL version

        if not pending:
            return

        token = ""
        try:
            token = _get_admin_bot_token(business)
            owner_id = int(business.owner_telegram_id)
            async with Bot(token=token) as admin_bot:
                for item in list(pending)[:10]:
                    try:
                        oid = int(item.get("order_id") or item.get("id") or 0)
                        amt = int(item.get("amount") or 0)
                        disp = str(
                            item.get("display_name") or item.get("telegram_id") or "-"
                        )
                        await admin_bot.send_message(
                            chat_id=owner_id,
                            text=(
                                f"⏰ یادآوری: رسید کارت به کارت در انتظار بررسی\n"
                                f"🧾 سفارش: #{oid}\n"
                                f"👤 کاربر: {disp}\n"
                                f"💰 مبلغ: {amt:,} تومان\n"
                                "لطفاً از پنل ادمین بررسی کنید."
                            ),
                        )
                        try:
                            business.mark_card_payment_notified(oid)
                        except (AttributeError, TenantBusinessError):
                            pass
                    except Exception as item_exc:
                        logger.debug("notify_job item error: %s", item_exc)
        except Exception as send_exc:
            logger.debug("pending_card_admin_notify_job send error: %s", send_exc)
        finally:
            token = ""
    except Exception as exc:
        logger.warning("pending_card_admin_notify_job error: %s", exc)


# ---------------------------------------------------------------------------
# User-facing result text
# ---------------------------------------------------------------------------

def card_payment_result_text(
    amount_toman: int,
    status: str,
    *,
    flow: str = "buy",
    text_settings: Optional[dict] = None,
    direct_note: bool = False,
) -> str:
    """
    SellBot _card_payment_result_user_text parity.
    status: "approved" | "pending" | "rejected"
    """
    texts = text_settings or {}
    s = str(status or "pending").strip().lower()

    if s == "approved":
        tpl = str(texts.get("card_approved_text") or "").strip()
        return tpl if (tpl and tpl != "0") else "✅ پرداخت شما تایید شد!"

    if s == "rejected":
        tpl = str(texts.get("card_rejected_text") or "").strip()
        return tpl if (tpl and tpl != "0") else "❌ پرداخت شما رد شد."

    if flow == "wallet_topup":
        return (
            "✅ رسید شارژ کیف پول ثبت شد و در انتظار بررسی است.\n"
            "از «💰 کیف پول ← وضعیت پرداخت‌ها» می‌توانید نتیجه را ببینید."
        )

    direct_line = ""
    if direct_note and flow == "direct_buy_payment":
        direct_line = "\n📦 پس از تایید، سرویس شما خودکار فعال خواهد شد."

    return (
        "✅ رسید ثبت شد و در انتظار بررسی است.\n"
        "از «💰 کیف پول ← وضعیت پرداخت‌ها» می‌توانید نتیجه را ببینید." + direct_line
    )


# ---------------------------------------------------------------------------
# Card payment instruction (shown before bank transfer)
# ---------------------------------------------------------------------------

def build_card_payment_instruction(
    *,
    order_id: int,
    amount_toman: int,
    card_number: str,
    card_owner: str,
    card_bank: str,
    text_settings: dict,
    tx_marker: int = 0,
) -> str:
    """
    SellBot parity: full card-to-card instruction panel shown before transfer.
    Uses sellbot_utils._build_card_to_card_payment_text for core text.
    """
    from TenantRuntime.UserBot.sellbot_utils import _build_card_to_card_payment_text

    base_text = _build_card_to_card_payment_text(
        amount_toman=int(amount_toman),
        card_number=str(card_number or ""),
        card_owner=str(card_owner or ""),
        card_bank=str(card_bank or ""),
        text_settings=dict(text_settings or {}),
    )
    marker_line = ""
    if int(tx_marker or 0) > 0:
        marker_line = f"🔢 مشخصه تراکنش: +{int(tx_marker):,} تومان\n\n"
    order_line = f"🧾 سفارش: #{int(order_id)}\n\n"
    return order_line + marker_line + base_text


# ---------------------------------------------------------------------------
# Settings helper
# ---------------------------------------------------------------------------

def require_last4_enabled(business: Any) -> bool:
    """Check if tenant requires 4-digit card sender verification."""
    try:
        settings = business.runtime_userbot_settings()
        return bool(settings.get("require_last4_for_card_receipt", False))
    except Exception:
        return False
