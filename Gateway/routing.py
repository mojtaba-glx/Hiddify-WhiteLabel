"""Gateway helpers: token->tenant routing and per-update isolation.

Phase 5 wires these to real polling; Phase 1 provides the pure,
tested primitives: fingerprint routing (never plain-token compare),
state keys and cross-tenant guards.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from Database.repositories import BotRepository


class CrossTenantError(PermissionError):
    """Raised when an operation crosses tenant boundaries."""


def resolve_tenant_by_fingerprint(
    conn: sqlite3.Connection, *, token_fingerprint: str
) -> Optional[dict[str, Any]]:
    """Return the tenant_bot row for a fingerprint, or None (unknown token)."""
    fingerprint = str(token_fingerprint or "").strip()
    if not fingerprint:
        return None
    return BotRepository(conn).get_by_fingerprint(fingerprint)


def tenant_state_key(*, tenant_id: int, bot_role: str, telegram_user_id: int) -> str:
    """Isolated state key: tenant_id + bot_role + telegram_user_id."""
    role = str(bot_role or "").strip().lower()
    if role not in ("admin", "user"):
        raise ValueError("bot_role must be admin or user")
    tenant = int(tenant_id)
    user = int(telegram_user_id)
    if tenant <= 0 or user <= 0:
        raise ValueError("tenant_id and telegram_user_id must be positive")
    return f"{tenant}:{role}:{user}"


def assert_same_tenant(first_tenant_id: int, second_tenant_id: int) -> None:
    """Raise CrossTenantError unless both ids match (and are positive)."""
    if int(first_tenant_id) <= 0 or int(second_tenant_id) <= 0:
        raise CrossTenantError("unknown tenant")
    if int(first_tenant_id) != int(second_tenant_id):
        raise CrossTenantError("cross-tenant access denied")


def assert_bot_usable(bot_row: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Fail-closed gate for a tenant bot row (missing/disabled/revoked -> error)."""
    if not bot_row or str(bot_row.get("status") or "") != "active":
        raise CrossTenantError("bot is not usable")
    return bot_row
