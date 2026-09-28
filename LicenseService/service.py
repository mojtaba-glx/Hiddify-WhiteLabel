"""License operations: atomic, audited transitions + short status cache.

Transaction discipline (see Database/connection.py): connections are
autocommit; every multi-step operation below opens ONE locked
transaction (BEGIN IMMEDIATE), reads the row *inside* it, computes,
writes and audits, then commits. A caller-supplied tenant_id is always
re-verified against the row read in that same transaction — a mismatch
raises TenantMismatchError with zero writes.

Phase 1 introduced pure evaluation and transactional writes. Phase 3 builds
the scheduler and runtime gate on these primitives.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from Database.connection import transaction
from Database.repositories import (
    AuditRepository,
    LicenseRepository,
    TenantMismatchError,
)
from LicenseService import states
from Shared.timeutils import ensure_utc, iso_utc, parse_utc

# How many times a CAS renew retries after losing a race to another writer.
RENEW_MAX_ATTEMPTS = 5


class LicenseTransitionError(RuntimeError):
    """Illegal transition or lost atomic compare-and-set."""


@dataclass
class CachedStatus:
    status: str
    cached_at: float
    expires_at: float


class LicenseStatusCache:
    """Tiny TTL cache so the runtime gate does not hit DB per update."""

    def __init__(
        self, ttl_seconds: float = 30.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._items: dict[int, CachedStatus] = {}

    def get(self, license_id: int) -> Optional[str]:
        item = self._items.get(int(license_id))
        if item is None:
            return None
        if self._clock() >= item.expires_at:
            self._items.pop(int(license_id), None)
            return None
        return item.status

    def set(
        self, license_id: int, status: str, *, max_age_seconds: Optional[float] = None
    ) -> None:
        now = self._clock()
        lifetime = self.ttl
        if max_age_seconds is not None:
            lifetime = min(lifetime, max(0.0, float(max_age_seconds)))
        self._items[int(license_id)] = CachedStatus(
            status=status,
            cached_at=now,
            expires_at=now + lifetime,
        )

    def invalidate(self, license_id: int) -> None:
        self._items.pop(int(license_id), None)

    def clear(self) -> None:
        self._items.clear()


def evaluate_license(license_row: dict[str, Any], *, now: datetime) -> str:
    """Compute effective status for a license row at `now` (no writes)."""
    moment = ensure_utc(now)
    grace_raw = license_row.get("grace_until")
    return states.compute_effective_status(
        stored_status=str(license_row.get("status") or ""),
        now=moment,
        expires_at=parse_utc(str(license_row.get("expires_at") or "")),
        grace_until=parse_utc(str(grace_raw)) if grace_raw else None,
    )


def _check_tenant(row: dict[str, Any], tenant_id: int) -> int:
    """Return the row's tenant, or raise TenantMismatchError (no write done)."""
    owner = int(row.get("tenant_id") or 0)
    if owner <= 0 or owner != int(tenant_id):
        raise TenantMismatchError("license does not belong to this tenant")
    return owner


def transition_license(
    conn: sqlite3.Connection,
    *,
    license_id: int,
    tenant_id: int,
    new_status: str,
    actor_id: int,
) -> dict[str, Any]:
    """Atomic audited transition.

    The row is read *inside* the locked transaction; the caller-supplied
    ``tenant_id`` must equal the row's tenant or nothing is written.
    """
    repo = LicenseRepository(conn)
    audits = AuditRepository(conn)
    with transaction(conn):
        current = repo.get_by_id(int(license_id))
        if current is None:
            raise LicenseTransitionError("license not found")
        owner = _check_tenant(current, int(tenant_id))
        old_status = str(current.get("status") or "")
        if not states.can_transition(old_status, new_status):
            raise LicenseTransitionError(f"illegal transition {old_status} -> {new_status}")
        ok = repo.transition(int(license_id), expected=old_status, new=new_status)
        if not ok:
            raise LicenseTransitionError("concurrent status change; retry")
        audits.append(
            actor_id=int(actor_id),
            tenant_id=owner,
            action="license.transition",
            entity_type="license",
            entity_id=str(license_id),
            metadata={"from": old_status, "to": new_status},
        )
    updated = repo.get_by_id(int(license_id))
    assert updated is not None
    return updated


def renew_license(
    conn: sqlite3.Connection,
    *,
    license_id: int,
    tenant_id: int,
    extra_days: int,
    grace_days: int = 0,
    actor_id: int,
    now: Optional[datetime] = None,
    max_attempts: int = RENEW_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Extend expiry (and grace) and re-activate; atomic + audited; never deletes.

    Read, date math, compare-and-set update and audit all happen inside ONE
    locked transaction per attempt, so two concurrent renewals serialize and
    both increments land (no lost update from stale reads). A lost CAS race
    re-reads the fresh row and retries.
    """
    if int(extra_days) <= 0:
        raise ValueError("extra_days must be positive")
    if int(grace_days) < 0:
        raise ValueError("grace_days must be >= 0")
    if int(max_attempts) < 1:
        raise ValueError("max_attempts must be >= 1")
    repo = LicenseRepository(conn)
    audits = AuditRepository(conn)
    moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)

    last_error: Optional[Exception] = None
    for _ in range(int(max_attempts)):
        try:
            with transaction(conn):
                current = repo.get_by_id(int(license_id))
                if current is None:
                    raise LicenseTransitionError("license not found")
                owner = _check_tenant(current, int(tenant_id))
                old_status = str(current.get("status") or "")
                if old_status == states.CANCELLED:
                    raise LicenseTransitionError("cancelled licenses cannot be renewed")
                base = parse_utc(str(current.get("expires_at") or ""))
                anchor = base if base > moment else moment
                new_expires = anchor + timedelta(days=int(extra_days))
                new_grace = (
                    (new_expires + timedelta(days=int(grace_days)))
                    if int(grace_days) > 0
                    else None
                )
                applied = repo.cas_renew(
                    int(license_id),
                    expected_status=old_status,
                    expected_expires_at=str(current.get("expires_at") or ""),
                    new_expires_at=iso_utc(new_expires),
                    new_grace_until=iso_utc(new_grace) if new_grace else None,
                    new_status=states.ACTIVE,
                )
                if not applied:
                    raise LicenseTransitionError("concurrent renew conflict; retry")
                audits.append(
                    actor_id=int(actor_id),
                    tenant_id=owner,
                    action="license.renew",
                    entity_type="license",
                    entity_id=str(license_id),
                    metadata={
                        "extra_days": int(extra_days),
                        "grace_days": int(grace_days),
                        "new_expires_at": iso_utc(new_expires),
                    },
                )
        except LicenseTransitionError as exc:
            if "concurrent renew conflict" in str(exc):
                last_error = exc
                continue
            raise
        updated = repo.get_by_id(int(license_id))
        assert updated is not None
        return updated
    raise LicenseTransitionError(
        f"renew lost CAS race after {int(max_attempts)} attempts"
    ) from last_error


def is_tenant_usable(license_row: Optional[dict[str, Any]], *, now: datetime) -> bool:
    """Runtime gate: only active/grace licenses may be served (fail-closed)."""
    if not license_row:
        return False
    try:
        return evaluate_license(license_row, now=now) in (states.ACTIVE, states.GRACE)
    except Exception:
        return False
