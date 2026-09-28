"""Access control helpers. Phase 2 wires these into every handler."""

from __future__ import annotations


def is_master_admin(user_id: int | None, master_admin_id: int) -> bool:
    """True only for the single platform-owner id (strict int compare)."""
    try:
        return int(user_id or 0) == int(master_admin_id) and int(master_admin_id) > 0
    except (TypeError, ValueError):
        return False


class AccessDenied(PermissionError):
    """Raised when a non-owner attempts a management operation."""


def require_master_admin(user_id: int | None, master_admin_id: int) -> None:
    """Raise AccessDenied unless user is the platform owner."""
    if not is_master_admin(user_id, master_admin_id):
        raise AccessDenied("access denied")
