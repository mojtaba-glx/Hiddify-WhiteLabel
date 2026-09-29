"""Shared safety/policy helpers for tenant AdminBot and UserBot runtimes."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from Gateway.catalog import RuntimeBotSpec
from Gateway.policy import RuntimePolicy
from Shared.redaction import get_logger, safe_format_exception
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.state import TenantStateStore

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
