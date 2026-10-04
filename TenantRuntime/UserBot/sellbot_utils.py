"""Utility helpers ported from Hiddify-SellBot UserBot/main.py.

All functions here are adapted to work inside the WhiteLabel multi-tenant
architecture. They are pure helpers or context-data-based (in-memory) helpers
that do not depend on the process-wide SellBot singletons (userbot_db, database, etc.).
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes
from telegram.helpers import escape_markdown as escape

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anti-spam configuration (can be overridden per-tenant via settings)
# ---------------------------------------------------------------------------
USERBOT_ANTI_SPAM_ENABLED: bool = True
USERBOT_ACTION_COOLDOWN_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# Type / number helpers
# ---------------------------------------------------------------------------
def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_en_digits(text: str) -> str:
    return text.translate(str.maketrans('\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9', '0123456789'))


def _optional_int_from_any(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _older_than_days(dt_str: Optional[str], days: int) -> bool:
    """Return True if ISO-8601 datetime string is older than `days` days."""
    if not dt_str:
        return False
    import datetime
    try:
        from dateutil import parser as dtparser
        dt = dtparser.parse(str(dt_str))
    except Exception:
        try:
            dt = datetime.datetime.fromisoformat(str(dt_str).replace('Z', '+00:00'))
        except Exception:
            return False
    now = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (now - dt).days >= days


# ---------------------------------------------------------------------------
# Unlimited plan helpers
# ---------------------------------------------------------------------------
UNLIMITED_VOLUME_SENTINELS = {-1, 0, 99999, 999999}
UNLIMITED_TIME_SENTINELS = {-1, 0, 99999, 999999}


def _is_unlimited_volume(gb: Any) -> bool:
    v = _to_int(gb, 0)
    return v < 0 or v in UNLIMITED_VOLUME_SENTINELS


def _is_unlimited_time(days: Any) -> bool:
    d = _to_int(days, 0)
    return d < 0 or d in UNLIMITED_TIME_SENTINELS


# ---------------------------------------------------------------------------
# Plan sort
# ---------------------------------------------------------------------------
def _sort_plans(plans: list[dict], txp: dict) -> list[dict]:
    mode = str(txp.get('plan_sort_mode') or 'price').strip().lower()
    desc = bool(txp.get('plan_sort_desc', False))
    key_map = {
        'price': lambda p: (_to_int(p.get('price'), 0),),
        'gb': lambda p: (_to_int(p.get('traffic_gb') or p.get('gb'), 0),),
        'days': lambda p: (_to_int(p.get('duration_days') or p.get('days'), 0),),
        'priority': lambda p: (_to_int(p.get('priority'), 0),),
    }
    key_fn = key_map.get(mode, key_map['price'])
    return sorted(plans, key=key_fn, reverse=desc)


# ---------------------------------------------------------------------------
# Card payment helpers
# ---------------------------------------------------------------------------
def _normalize_card_last4(text: str) -> str:
    return re.sub(r'[^0-9]', '', _to_en_digits(str(text or '')))[-4:]


def _parse_exact_card_last4(text: str) -> Optional[str]:
    digits = re.sub(r'[^0-9]', '', _to_en_digits(str(text or '')))
    if len(digits) == 4:
        return digits
    if len(digits) == 16:
        return digits[-4:]
    return None


def _apply_random_tx_marker(
    amount_toman: int,
    tx_settings: Optional[dict] = None,
) -> tuple[int, int]:
    """
    If random_tx_spec is enabled, add a small random marker to make each
    card-to-card transaction uniquely identifiable.
    Returns: (final_amount_toman, marker_delta_toman)
    """
    try:
        base_amount = int(amount_toman or 0)
    except Exception:
        base_amount = 0
    if base_amount <= 0:
        return 0, 0
    settings = tx_settings if isinstance(tx_settings, dict) else {}
    if not bool(settings.get('random_tx_spec', False)):
        return base_amount, 0
    marker = random.randint(101, 997)
    if marker % 10 == 0:
        marker += 1
    return base_amount + marker, marker


def _build_card_to_card_payment_text(
    *,
    amount_toman: int,
    card_number: str,
    card_owner: str,
    card_bank: str,
    text_settings: dict,
) -> str:
    owner_safe = str(card_owner or '')
    bank_safe = str(card_bank or '')
    card_safe = str(card_number or '')
    template = str((text_settings or {}).get('card_to_card_text') or '').strip()
    if not template or template == '0':
        bank_line = f'\U0001f3e6 \u0628\u0627\u0646\u06a9: {bank_safe}\n' if bank_safe else ''
        return (
            f'\U0001f4b0 \u0644\u0637\u0641\u0627 \u062f\u0642\u06cc\u0642\u0627 \u0645\u0628\u0644\u063a: <code>{int(amount_toman) * 10:d}</code> \u0631\u06cc\u0627\u0644\n'
            f'\U0001f4b0 \u0645\u0639\u0627\u062f\u0644: {int(amount_toman):,} \u062a\u0648\u0645\u0627\u0646\n'
            f'\U0001f4b3 \u0628\u0647 \u0634\u0645\u0627\u0631\u0647 \u06a9\u0627\u0631\u062a: <code>{card_safe}</code>\n'
            f'\U0001f464 \u0628\u0647 \u0646\u0627\u0645: {owner_safe}\n'
            f'{bank_line}'
            '\u2757 \u0628\u0639\u062f \u0627\u0632 \u0648\u0627\u0631\u06cc\u0632 \u0645\u0628\u0644\u063a \u0627\u0633\u06a9\u0631\u06cc\u0646 \u0634\u0627\u062a \u0627\u0632 \u062a\u0631\u0627\u06a9\u0646\u0634 \u0628\u0631\u0627\u06cc \u0645\u0627 \u0627\u0631\u0633\u0627\u0644 \u06a9\u0646\u06cc\u062f.'
        )
    return (
        template
        .replace('{CARD}', card_safe)
        .replace('{HOLDER}', owner_safe)
        .replace('{BANK}', bank_safe)
        .replace('{AMOUNT}', f'{int(amount_toman):,}')
        .replace('{RIAL}', f'{int(amount_toman) * 10:d}')
    )


def _card_payment_result_user_text(
    *,
    status: str,
    amount_toman: int,
    text_settings: dict,
) -> str:
    status = str(status or '').strip().lower()
    texts = text_settings or {}
    if status == 'approved':
        tpl = str(texts.get('card_approved_text') or '').strip()
        return tpl if tpl and tpl != '0' else '\u2705 \u067e\u0631\u062f\u0627\u062e\u062a \u0634\u0645\u0627 \u062a\u0627\u06cc\u06cc\u062f \u0634\u062f!'
    tpl = str(texts.get('card_rejected_text') or '').strip()
    return tpl if tpl and tpl != '0' else '\u274c \u067e\u0631\u062f\u0627\u062e\u062a \u0634\u0645\u0627 \u0631\u062f \u0634\u062f.'


# ---------------------------------------------------------------------------
# Dynamic pricing
# ---------------------------------------------------------------------------
def _calc_dynamic_price(
    gb: int,
    months: int,
    dyn_settings: Optional[dict[str, Any]],
) -> tuple[int, int]:
    """
    Calculate dynamic plan final price and applied discount percent.
    Discount modes:
      - 'volume': discount based on GB purchased
      - 'time': discount based on months purchased
      - 'both': whichever is higher
      - disabled or unknown: no discount
    Returns: (final_price_toman, discount_percent)
    """
    s = dyn_settings if isinstance(dyn_settings, dict) else {}
    price_gb = max(0, _to_int(s.get('price_gb'), 0))
    price_month = max(0, _to_int(s.get('price_month'), 0))
    base = int(gb) * price_gb + int(months) * price_month
    if base <= 0:
        return 0, 0

    discount_mode = str(s.get('discount_mode') or 'disabled').strip().lower()
    if discount_mode == 'disabled':
        return base, 0

    vol_tiers = s.get('volume_tiers') or []
    time_tiers = s.get('time_tiers') or []

    def best_tier(tiers: list, value: int, key: str) -> int:
        best = 0
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            threshold = _to_int(tier.get(key), 0)
            pct = _to_int(tier.get('percent'), 0)
            if value >= threshold > 0 and pct > best:
                best = pct
        return max(0, min(100, best))

    vol_pct = best_tier(vol_tiers, int(gb), 'min_gb') if discount_mode in {'volume', 'both'} else 0
    time_pct = best_tier(time_tiers, int(months), 'min_months') if discount_mode in {'time', 'both'} else 0
    pct = max(vol_pct, time_pct)
    discount = int(base * pct / 100)
    final = max(0, base - discount)
    return final, pct


# ---------------------------------------------------------------------------
# Anti-spam / rate limiting
# ---------------------------------------------------------------------------
def _check_action_rate_limit(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action_key: str,
    *,
    cooldown: Optional[float] = None,
    event_ts: Optional[float] = None,
) -> tuple[bool, float]:
    """
    Anti-spam throttle per user.
    Returns: (is_throttled: bool, wait_seconds: float)
    """
    now = float(event_ts) if event_ts is not None else time.time()
    data = context.user_data.setdefault('_rate_limit', {})

    if not USERBOT_ANTI_SPAM_ENABLED:
        return False, 0.0

    global_key = f'{user_id}:__global__'
    last_global = float(data.get(global_key) or 0.0)
    if now - last_global < 0.35:
        return True, max(0.0, 0.35 - (now - last_global))
    data[global_key] = now

    cd = float(cooldown if cooldown is not None else USERBOT_ACTION_COOLDOWN_SECONDS)
    action_full_key = f'{user_id}:{action_key}'
    last = float(data.get(action_full_key) or 0.0)
    if now - last < cd:
        return True, max(0.0, cd - (now - last))
    data[action_full_key] = now
    return False, 0.0


# ---------------------------------------------------------------------------
# Stale startup update filter
# ---------------------------------------------------------------------------
def _should_skip_stale_startup_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    """
    On bot startup, skip queued updates older than the boot timestamp.
    Prevents replaying stale messages from before the last restart.
    """
    boot_ts: Optional[float] = context.bot_data.get('_boot_ts')
    if boot_ts is None:
        return False
    message = getattr(update, 'effective_message', None)
    if message is None:
        return False
    msg_ts = message.date
    if msg_ts is None:
        return False
    try:
        ts = msg_ts.timestamp()
    except Exception:
        return False
    if ts < boot_ts - 5:
        logger.debug(
            'Skipping stale startup update user_id=%s ts=%s boot=%s',
            user_id, ts, boot_ts,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Ban middleware (WhiteLabel-adapted)
# ---------------------------------------------------------------------------
async def userbot_ban_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business,
) -> None:
    """
    WhiteLabel-adapted ban middleware.
    Raise ApplicationHandlerStop for banned users.
    business.is_customer_banned(telegram_id) -> bool
    """
    user = getattr(update, 'effective_user', None)
    if not user:
        return
    try:
        is_banned = business.is_customer_banned(int(user.id))
    except Exception:
        logger.exception(
            'Failed to check ban status for telegram_id=%s', user.id
        )
        query = getattr(update, 'callback_query', None)
        message = getattr(update, 'effective_message', None)
        try:
            if query:
                await query.answer('\u23f3 \u0633\u0631\u0648\u06cc\u0633 \u0645\u0648\u0642\u062a\u0627\u064b \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u06cc\u0633\u062a.', show_alert=True)
            elif message:
                await message.reply_text('\u23f3 \u0633\u0631\u0648\u06cc\u0633 \u0645\u0648\u0642\u062a\u0627\u064b \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u06cc\u0633\u062a.')
        except Exception:
            pass
        raise ApplicationHandlerStop
    if not is_banned:
        return
    query = getattr(update, 'callback_query', None)
    message = getattr(update, 'effective_message', None)
    try:
        if query:
            await query.answer('\U0001f6ab \u062d\u0633\u0627\u0628 \u0634\u0645\u0627 \u062a\u0648\u0633\u0637 \u0645\u062f\u06cc\u0631 \u0645\u0633\u062f\u0648\u062f \u0634\u062f\u0647 \u0627\u0633\u062a.', show_alert=True)
        elif message:
            await message.reply_text('\U0001f6ab \u062d\u0633\u0627\u0628 \u0634\u0645\u0627 \u062a\u0648\u0633\u0637 \u0645\u062f\u06cc\u0631 \u0645\u0633\u062f\u0648\u062f \u0634\u062f\u0647 \u0627\u0633\u062a.'
)
    except Exception:
        pass
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Message sending helpers
# ---------------------------------------------------------------------------
async def _send_long_message(
    message,
    text: str,
    parse_mode: str = 'HTML',
    chunk_size: int = 4000,
    **kwargs,
) -> None:
    """Send a long text by splitting it into safe Telegram chunks."""
    text = str(text or '')
    if len(text) <= chunk_size:
        await message.reply_text(text, parse_mode=parse_mode, **kwargs)
        return
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for chunk in chunks:
        await message.reply_text(chunk, parse_mode=parse_mode)


async def _safe_edit_message_text(
    query,
    text: str,
    parse_mode: str = 'HTML',
    **kwargs,
) -> bool:
    """Edit inline message text; return False on Telegram 'not modified' error."""
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, **kwargs)
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if 'message is not modified' in msg or 'message to edit not found' in msg:
            return False
        logger.warning('edit_message_text failed: %s', exc)
        return False


async def _safe_edit_message_reply_markup(query, reply_markup) -> bool:
    """Edit inline keyboard; return False on Telegram 'not modified' error."""
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return True
    except Exception as exc:
        if 'message is not modified' in str(exc).lower():
            return False
        return False


# ---------------------------------------------------------------------------
# Text / input normalizers
# ---------------------------------------------------------------------------
def _is_back_or_cancel_text(text: str) -> bool:
    normalized = str(text or '').strip()
    return normalized in {
        '\U0001f519 \u0628\u0627\u0632\u06af\u0634\u062a', '\u0628\u0627\u0632\u06af\u0634\u062a',
        '\u274c \u0644\u063a\u0648', '\u0644\u063a\u0648',
        'back', 'cancel', '\U0001f519', '\u274c',
    }


def _normalize_action_text(text: str) -> str:
    return str(text or '').strip().lower()


def _iter_text_values(settings: dict, *keys: str):
    """Yield non-empty string values for the given setting keys."""
    for key in keys:
        val = settings.get(key)
        if isinstance(val, str) and val.strip() and val != '0':
            yield key, val.strip()
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item.strip():
                    yield key, item.strip()


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------
def _generate_7_digit_code() -> str:
    return str(random.randint(1000000, 9999999))


def _generate_order_id(
    user_id: int,
    plan_id: int,
    salt: Optional[str] = None,
) -> str:
    """Generate a short human-readable order reference."""
    import hashlib
    raw = f'{user_id}-{plan_id}-{salt or time.time()}'
    digest = hashlib.md5(raw.encode()).hexdigest()[:6].upper()
    return f'ORD-{digest}'
