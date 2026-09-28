"""License state machine: pure, deterministic, fully tested.

States: pending, active, grace, suspended, expired, cancelled.
All time inputs are aware UTC datetimes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

PENDING = "pending"
ACTIVE = "active"
GRACE = "grace"
SUSPENDED = "suspended"
EXPIRED = "expired"
CANCELLED = "cancelled"

ALL_STATUSES = frozenset({PENDING, ACTIVE, GRACE, SUSPENDED, EXPIRED, CANCELLED})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({ACTIVE, CANCELLED}),
    ACTIVE: frozenset({GRACE, EXPIRED, SUSPENDED, CANCELLED}),
    GRACE: frozenset({ACTIVE, SUSPENDED, EXPIRED, CANCELLED}),
    SUSPENDED: frozenset({ACTIVE, EXPIRED, CANCELLED}),
    EXPIRED: frozenset({ACTIVE, SUSPENDED, CANCELLED}),
    CANCELLED: frozenset(),
}

# Days before expiry at which warnings fire (each at most once).
WARNING_DAYS = (7, 3, 1)


def can_transition(current: str, new: str) -> bool:
    """True iff moving from current to new status is allowed."""
    if current not in ALL_STATUSES or new not in ALL_STATUSES:
        return False
    return new in ALLOWED_TRANSITIONS[current]


def compute_effective_status(
    *,
    stored_status: str,
    now: datetime,
    expires_at: datetime,
    grace_until: Optional[datetime] = None,
) -> str:
    """Evaluate the time-derived status without writing to DB.

    Manual states (pending/suspended/cancelled) are returned as-is;
    active/grace follow the clock. Unknown statuses raise ValueError.
    """
    if stored_status not in ALL_STATUSES:
        raise ValueError(f"unknown license status: {stored_status}")
    if stored_status in (PENDING, SUSPENDED, CANCELLED, EXPIRED):
        return stored_status
    # stored is active or grace
    if now <= expires_at:
        return ACTIVE
    if grace_until is not None and now <= grace_until:
        return GRACE
    return EXPIRED


def warning_event_key(
    license_id: int, days_before: int, *, expires_at: str | None = None
) -> str:
    """Unique idempotency key per license period and warning stage.

    The legacy form without ``expires_at`` remains available for old callers.
    Jobs always include an expiry fingerprint so renewal of the same license
    gets a fresh 7/3/1 sequence and queued warnings from its old period can be
    recognized as stale before delivery.
    """
    base = f"license:{int(license_id)}:expiring:{int(days_before)}d"
    if expires_at is None:
        return base
    period = hashlib.sha256(str(expires_at).strip().encode("utf-8")).hexdigest()[:16]
    return f"{base}:{period}"


def days_until_expiry(*, now: datetime, expires_at: datetime) -> int:
    """Whole days remaining (negative when past expiry)."""
    delta = expires_at - now
    return int(delta.total_seconds() // 86400)


def due_warnings(*, now: datetime, expires_at: datetime) -> list[int]:
    """Which of WARNING_DAYS stages are due right now (expiry in future)."""
    remaining = days_until_expiry(now=now, expires_at=expires_at)
    return [day for day in WARNING_DAYS if remaining <= day and remaining >= 0]
