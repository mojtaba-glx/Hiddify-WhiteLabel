"""Persistent, non-secret storefront settings for MasterBot."""

from __future__ import annotations

import sqlite3
from typing import Any

from Database.connection import transaction
from Shared.timeutils import iso_utc, utcnow


DEFAULTS: dict[str, str] = {
    "store_name": "فروش ربات اختصاصی",
    "support_contact": "",
    "sales_enabled": "1",
    "trial_enabled": "1",
    "trial_plan_id": "",
    "maintenance_message": "فروش موقتاً غیرفعال است. لطفاً کمی بعد دوباره تلاش کنید.",
}

EDITABLE_KEYS = frozenset(DEFAULTS)


class PlatformSettingsService:
    """Small typed facade around non-secret platform settings."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> str:
        name = str(key or "").strip()
        if name not in EDITABLE_KEYS:
            raise ValueError("unknown platform setting")
        row = self.conn.execute(
            "SELECT value FROM platform_settings WHERE key = ?", (name,)
        ).fetchone()
        return str(row["value"]) if row is not None else DEFAULTS[name]

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def all(self) -> dict[str, Any]:
        return {
            "store_name": self.get("store_name"),
            "support_contact": self.get("support_contact"),
            "sales_enabled": self.get_bool("sales_enabled"),
            "trial_enabled": self.get_bool("trial_enabled"),
            "trial_plan_id": (
                int(self.get("trial_plan_id")) if self.get("trial_plan_id").isdigit() else None
            ),
            "maintenance_message": self.get("maintenance_message"),
        }

    def set_text(self, key: str, value: str, *, maximum: int) -> str:
        name = str(key or "").strip()
        if name not in EDITABLE_KEYS or name.endswith("_enabled"):
            raise ValueError("setting is not a text field")
        clean = str(value or "").strip()
        if name not in {"support_contact", "trial_plan_id"} and not clean:
            raise ValueError("setting value is required")
        if len(clean) > int(maximum):
            raise ValueError("setting value is too long")
        self._write(name, clean)
        return clean

    def set_bool(self, key: str, enabled: bool) -> bool:
        name = str(key or "").strip()
        if name not in {"sales_enabled", "trial_enabled"}:
            raise ValueError("setting is not a boolean field")
        self._write(name, "1" if bool(enabled) else "0")
        return bool(enabled)

    def _write(self, key: str, value: str) -> None:
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO platform_settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, now),
            )
