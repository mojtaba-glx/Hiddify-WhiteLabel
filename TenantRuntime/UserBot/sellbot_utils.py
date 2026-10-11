"""Small pure helpers shared by the tenant UserBot.

Only helpers with live callers live here. Dead ports from Hiddify-SellBot
were removed so stale user-facing texts cannot linger unnoticed (the
parity-diff script flags them otherwise).
"""
from __future__ import annotations

import random
from typing import Optional


def _to_en_digits(text: str) -> str:
    return text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))


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
