"""Durable runtime state scoped to exactly one tenant, bot role and user."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from Gateway.routing import tenant_state_key
from Shared.redaction import redact_text
from Shared.timeutils import iso_utc, utcnow

MAX_STATE_BYTES = 32 * 1024
_SENSITIVE = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "privatekey",
    "encryptionkey",
)


class RuntimeStateError(ValueError):
    """State is invalid, unsafe, oversized or outside the fixed scope."""


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _validate_value(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if any(part in normalized for part in _SENSITIVE):
                raise RuntimeStateError("runtime state must not contain credentials")
            _validate_value(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_value(nested)
        return
    if isinstance(value, str):
        if redact_text(value) != value:
            raise RuntimeStateError("runtime state must not contain credentials")
        return
    if value is None or type(value) in (bool, int, float):
        return
    raise RuntimeStateError("runtime state must contain only JSON-safe values")


def encode_state(state: dict[str, Any]) -> str:
    if not isinstance(state, dict):
        raise RuntimeStateError("runtime state must be an object")
    _validate_value(state)
    try:
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeStateError("runtime state is not JSON-serializable") from exc
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise RuntimeStateError("runtime state exceeds the size limit")
    return payload


@dataclass(frozen=True)
class StateScope:
    tenant_id: int
    bot_role: str

    def __post_init__(self) -> None:
        if int(self.tenant_id) <= 0:
            raise RuntimeStateError("tenant_id must be positive")
        if self.bot_role not in ("admin", "user"):
            raise RuntimeStateError("bot_role must be admin or user")

    def key(self, telegram_user_id: int) -> str:
        return tenant_state_key(
            tenant_id=self.tenant_id,
            bot_role=self.bot_role,
            telegram_user_id=int(telegram_user_id),
        )


class TenantStateStore:
    """A store instance is permanently bound to one tenant and one bot role."""

    def __init__(self, conn: sqlite3.Connection, *, scope: StateScope) -> None:
        self.conn = conn
        self.scope = scope

    @staticmethod
    def _user_id(telegram_user_id: int) -> int:
        value = int(telegram_user_id)
        if value <= 0:
            raise RuntimeStateError("telegram_user_id must be positive")
        return value

    def load(self, telegram_user_id: int) -> dict[str, Any]:
        user_id = self._user_id(telegram_user_id)
        row = self.conn.execute(
            "SELECT state_json FROM tenant_user_state"
            " WHERE tenant_id = ? AND bot_role = ? AND telegram_user_id = ?",
            (self.scope.tenant_id, self.scope.bot_role, user_id),
        ).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(str(row["state_json"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeStateError("stored runtime state is invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeStateError("stored runtime state is not an object")
        _validate_value(value)
        return value

    def save(self, telegram_user_id: int, state: dict[str, Any]) -> None:
        user_id = self._user_id(telegram_user_id)
        payload = encode_state(state)
        self.conn.execute(
            "INSERT INTO tenant_user_state"
            " (tenant_id, bot_role, telegram_user_id, state_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(tenant_id, bot_role, telegram_user_id) DO UPDATE SET"
            " state_json = excluded.state_json, updated_at = excluded.updated_at",
            (
                self.scope.tenant_id,
                self.scope.bot_role,
                user_id,
                payload,
                iso_utc(utcnow()),
            ),
        )

    def clear(self, telegram_user_id: int) -> bool:
        user_id = self._user_id(telegram_user_id)
        cursor = self.conn.execute(
            "DELETE FROM tenant_user_state"
            " WHERE tenant_id = ? AND bot_role = ? AND telegram_user_id = ?",
            (self.scope.tenant_id, self.scope.bot_role, user_id),
        )
        return cursor.rowcount == 1

    def state_key(self, telegram_user_id: int) -> str:
        return self.scope.key(self._user_id(telegram_user_id))
