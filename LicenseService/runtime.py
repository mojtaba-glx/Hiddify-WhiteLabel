"""Fail-closed, cached license gate for the shared tenant runtime."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from Database.repositories import (
    LicenseRepository,
    TenantRepository,
    TenantRuntimeRepository,
)
from LicenseService import states
from LicenseService.service import LicenseStatusCache, evaluate_license
from Shared.timeutils import ensure_utc, parse_utc, utcnow


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    status: str
    reason: str


class TenantRuntimeGate:
    """Check tenant + effective license state without depending on MasterBot.

    Database errors fail closed for that update and are not cached. Cached
    positive decisions never outlive the license/grace cutoff, even when the
    configured cache TTL is longer.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl_seconds: float = 15.0,
        cache: Optional[LicenseStatusCache] = None,
    ) -> None:
        self.conn = conn
        self.cache = cache or LicenseStatusCache(ttl_seconds)

    def check(self, tenant_id: int, *, now: Optional[datetime] = None) -> GateDecision:
        tenant_id = int(tenant_id)
        # Explicit clocks are used by deterministic jobs/tests and bypass a
        # cache populated for real wall-clock time.
        use_cache = now is None
        if use_cache:
            cached = self.cache.get(tenant_id)
            if cached is not None:
                allowed = cached in (states.ACTIVE, states.GRACE)
                return GateDecision(allowed, cached, "cached")
        moment = ensure_utc(now) if now is not None else utcnow()
        try:
            tenant = TenantRepository(self.conn).get_by_id(tenant_id)
            if tenant is None:
                if use_cache:
                    self.cache.set(tenant_id, "missing")
                return GateDecision(False, "missing", "tenant_not_found")
            if tenant["status"] != "active":
                if use_cache:
                    self.cache.set(tenant_id, str(tenant["status"]))
                return GateDecision(False, str(tenant["status"]), "tenant_disabled")
            runtime = TenantRuntimeRepository(self.conn).get_by_tenant(tenant_id)
            if runtime is None or runtime["status"] != "ready":
                runtime_status = str(runtime["status"]) if runtime else "missing"
                cached_status = f"runtime:{runtime_status}"
                if use_cache:
                    self.cache.set(tenant_id, cached_status)
                return GateDecision(False, cached_status, "runtime_not_ready")
            license_row = LicenseRepository(self.conn).current_usable(tenant_id)
            if license_row is None:
                if use_cache:
                    self.cache.set(tenant_id, "missing")
                return GateDecision(False, "missing", "license_not_found")
            starts = parse_utc(str(license_row["starts_at"]))
            if moment < starts:
                if use_cache:
                    self.cache.set(
                        tenant_id,
                        states.PENDING,
                        max_age_seconds=max(0.0, (starts - moment).total_seconds()),
                    )
                return GateDecision(False, states.PENDING, "license_not_started")
            effective = evaluate_license(license_row, now=moment)
            allowed = effective in (states.ACTIVE, states.GRACE)
            if use_cache:
                cutoff_raw = license_row.get("grace_until") or license_row.get("expires_at")
                cutoff = parse_utc(str(cutoff_raw))
                self.cache.set(
                    tenant_id,
                    effective,
                    max_age_seconds=max(0.0, (cutoff - moment).total_seconds()),
                )
            return GateDecision(allowed, effective, "license_evaluated")
        except Exception:
            return GateDecision(False, "error", "database_or_license_error")

    def is_allowed(self, tenant_id: int, *, now: Optional[datetime] = None) -> bool:
        return self.check(tenant_id, now=now).allowed

    def invalidate(self, tenant_id: int) -> None:
        self.cache.invalidate(int(tenant_id))
