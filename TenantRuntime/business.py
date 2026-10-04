"""Tenant-scoped business service used by each dedicated runtime bot.

The service is permanently bound to one tenant. Every read/write includes the
bound tenant id, so a callback id from another customer can never cross into
this tenant's data. Panel credentials are encrypted at rest and passed only at
the adapter boundary; concrete provider HTTP dialects remain separate.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import uuid
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit

from Database.connection import transaction
from Shared.crypto import TokenCipher, TokenCipherError, fingerprint_token
from Shared.timeutils import iso_utc, parse_utc, utcnow
from TenantRuntime.payments import (
    begin_provider_payment,
    method_view as payment_method_view,
    payment_prompt,
    provider_for_key,
    registered_providers,
)
from TenantRuntime.panels import (
    PanelAdapter,
    PanelError,
    PanelTarget,
    ProvisionRequest,
    RenewRequest,
    UnconfiguredPanelAdapter,
)


class TenantBusinessError(RuntimeError):
    pass


DEFAULT_REFERRAL_INVITE_TEXT = (
    "🎁 دعوت دوستان\n"
    "❖ ◈━━━━━━━━━━━━━━━◈ ❖\n"
    "دوستت رو دعوت کن و از هر دعوت پاداش بگیر!\n\n"
    "🤝 پاداش تست دوستت برای تو: {trial_reward}\n"
    "🛒 پاداش اولین خرید دوستت: {purchase_reward}\n\n"
    "🔗 لینک دعوت:\n{invite_link}"
)


USERBOT_SETTING_DEFAULTS: dict[str, Any] = {
    # Purchase / renewal switches mirrored from Hiddify-SellBot.
    "enable_buy": True,
    "enable_renew": True,
    "show_renew_in_main_menu": True,
    "renew_policy": "advanced",
    "renew_volume_mode": "reset",
    "renew_time_mode": "reset",
    "renew_max_days": 3,
    "renew_max_remaining_gb": 3,
    "renew_unlimited_volume": False,
    "renew_unlimited_time": False,
    "renew_unlimited_volume_from_gb": 1000,
    "renew_unlimited_time_from_days": 365,

    # Subscription presentation.
    "show_user_page_link": True,
    "show_username": True,
    "shuffle_configs": True,
    "shuffle_server_layout": True,
    "shuffle_config_layout": True,
    "show_direct_config": True,
    "show_auto_sub_link": False,
    "show_sub_link": True,
    "show_sub_link_b64": False,
    "show_multi_server": False,
    "show_multi_server_b64": False,
    # Deprecated compatibility key from pre-v0.15 tenants. Runtime link
    # visibility uses the explicit SellBot-compatible keys above.
    "show_smart_link": False,
    "smart_base_url": "",

    # Texts editable from Tenant AdminBot. Keep these defaults aligned
    # with the proven Hiddify-SellBot UserBot so a new tenant has useful
    # output before the owner customizes anything.
    "welcome_message": "سلام {full_name} عزیز 👋\nبه ربات ما خوش آمدید.",
    "faq_text": (
        "❓ سوالات متداول\n\n"
        "1) لینک اشتراک را کجا بزنم؟\n"
        "از بخش «📊وضعیت اشتراک» وارد سرویس شوید و روی «لینک اشتراک» بزنید.\n\n"
        "2) اگر کانفیگ وصل نشد چه کنم؟\n"
        "اول اینترنت و تاریخ/ساعت گوشی را چک کنید، سپس دوباره وضعیت سرویس را بررسی کنید.\n\n"
        "3) چطور تمدید کنم؟\n"
        "از «♾تمدید اشتراک» سرویس را انتخاب کنید و پلن تمدید را بخرید.\n\n"
        "4) پشتیبانی از کجاست؟\n"
        "از دکمه «📩پشتیبانی» پیام خود را ارسال کنید."
    ),
    "guide_text": "انتخاب سیستم عامل ⬇️",
    "guide_android_text": (
        "📱 راهنمای اندروید\n\n"
        "1) Hiddify Next را نصب کنید.\n"
        "2) لینک اشتراک را Import کنید.\n"
        "3) پروفایل را انتخاب و Connect کنید."
    ),
    "guide_ios_text": (
        "📱 راهنمای iOS\n\n"
        "1) یک کلاینت سازگار مانند Streisand یا Hiddify نصب کنید.\n"
        "2) لینک اشتراک را Import کنید.\n"
        "3) اتصال را فعال کنید."
    ),
    "guide_windows_text": (
        "🖥️ راهنمای ویندوز\n\n"
        "1) Hiddify Next یا v2rayN را نصب کنید.\n"
        "2) لینک اشتراک را Paste/Import کنید.\n"
        "3) پروفایل را انتخاب و Connect کنید."
    ),
    "guide_mac_text": (
        "💻 راهنمای macOS\n\n"
        "1) یک کلاینت سازگار نصب کنید.\n"
        "2) لینک اشتراک را Import کنید.\n"
        "3) اتصال را فعال کنید."
    ),
    "guide_linux_text": (
        "🖥️ راهنمای Linux\n\n"
        "1) Hiddify Next یا یک کلاینت سازگار نصب کنید.\n"
        "2) لینک اشتراک را Import کنید.\n"
        "3) اتصال را فعال کنید."
    ),
    "servers_list_text": "📡 لیست سرورها\nلطفاً لوکیشن مورد نظر خود را انتخاب کنید:",
    "plans_list_text": "🛒 لطفاً پلن مورد نظر خود را انتخاب کنید:",
    "ticket_panel_text": "📩 برای ارتباط با پشتیبانی، پیام خود را ارسال کنید.",
    "invite_text": "💌 لینک دعوت شما:\n{invite_link}",
    "invite_info_text": "🎁 دوستان خود را دعوت کنید و از پاداش‌های فعال بهره‌مند شوید.",
    "invite_banner_text": (
        "🎁 بنر دعوت اختصاصی شما\n\n"
        "🔗 لینک دعوت شما:\n{invite_link}\n\n"
        "دوستانت را دعوت کن و از مزایای ویژه بهره‌مند شو."
    ),
    "invite_banner_photo_id": "",
    "force_join_help_text": (
        "برای فعال شدن عضویت اجباری، UserBot باید در کانال مقصد دسترسی "
        "لازم برای بررسی عضویت کاربران را داشته باشد."
    ),

    # Marketing / visibility.
    "show_gift_button": True,
    "show_user_status": True,
    "enable_discount_code": True,

    # Force join and event/channel settings.
    # Keep force_join_channel + event_channel_* as legacy compatibility keys.
    "force_join_enabled": False,
    "force_join_channel": "",
    "force_join_channel_id": "",
    "force_join_channel_username": "",
    "force_join_channel_link": "",
    "force_join_guide_text": (
        "🔒 برای استفاده از ربات، ابتدا در کانال پشتیبانی عضو شوید.\n"
        "پس از عضویت روی «✅ بررسی عضویت» بزنید."
    ),
    "event_channel_enabled": False,
    "event_channel_id": "",
    "purchase_event_channel_enabled": False,
    "purchase_event_channel_id": "",
    "payment_event_channel_enabled": False,
    "payment_event_channel_id": "",
    "system_event_channel_enabled": False,
    "system_event_channel_id": "",
    "auto_backup_enabled": True,
    "channel_id": "",

    # Telegram button styling.
    "button_theme": "smart",
    "colored_buttons": True,

    # Plan/server list layout and catalog.
    "plan_categories_enabled": True,
    "plan_sort_by_priority": True,
    "plan_sort_mode": "id",
    "plan_columns": 1,
    "server_columns": 1,

    # Subscription reminders.
    "reminder_enabled": True,
    "reminder_days": 3,
    "reminder_remaining_gb": 3,
}


def _text(value: object, maximum: int, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError("text is required")
    if len(result) > maximum:
        raise ValueError("text is too long")
    return result


def _currency_code(value: object, *, fallback: str = "") -> str:
    """Normalize an ISO-like wallet currency code and reject numeric pseudo-currencies."""
    clean = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9]{2,7}", clean):
        return clean
    backup = str(fallback or "").strip().upper()
    if backup and re.fullmatch(r"[A-Z][A-Z0-9]{2,7}", backup):
        return backup
    raise ValueError("invalid currency code")


class TenantBusinessService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: int,
        owner_telegram_id: int,
        secret_cipher: TokenCipher | None = None,
        panel_adapter: PanelAdapter | None = None,
    ) -> None:
        self.conn = conn
        self.tenant_id = int(tenant_id)
        self.owner_telegram_id = int(owner_telegram_id)
        if self.tenant_id <= 0 or self.owner_telegram_id <= 0:
            raise ValueError("invalid tenant scope")
        self.secret_cipher = secret_cipher
        self.panel_adapter = panel_adapter or UnconfiguredPanelAdapter()

    def _admin(self, actor_id: int) -> None:
        if int(actor_id) != self.owner_telegram_id:
            raise PermissionError("tenant admin access denied")

    def _customer(self, actor_id: int, *, active: bool = True) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tenant_customers WHERE tenant_id = ? AND telegram_user_id = ?",
            (self.tenant_id, int(actor_id)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("customer is not registered")
        result = dict(row)
        if active and result["status"] != "active":
            raise TenantBusinessError("customer is blocked")
        return result

    def register_customer(self, actor_id: int, *, display_name: str, username: str | None) -> dict[str, Any]:
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_customers (tenant_id, telegram_user_id, display_name, username, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'active', ?, ?)"
                " ON CONFLICT(tenant_id, telegram_user_id) DO UPDATE SET"
                " display_name = excluded.display_name, username = excluded.username, updated_at = excluded.updated_at",
                (self.tenant_id, int(actor_id), _text(display_name, 120), _text(username, 64, required=False) or None, now, now),
            )
        customer = self._customer(actor_id, active=False)
        if not str(customer.get("referral_code") or "").strip():
            customer = self._ensure_referral_code(int(customer["id"]))
        return customer

    def add_server(
        self,
        actor_id: int,
        *,
        label: str,
        panel_kind: str = "manual",
        endpoint: str = "",
        admin_path: str = "",
        user_path: str = "",
        xui_flavor: str = "",
        xui_inbound_ids: str = "",
        xui_public_origin: str = "",
        xui_sub_path: str = "",
        xnet_inbound_ids: str = "",
        xnet_public_origin: str = "",
        xnet_sub_port: int = 0,
        xnet_sub_path: str = "",
        xnet_api_url: str = "",
        users_limit: int = 0,
        priority: int = 0,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        provider_kind = str(panel_kind or "").strip().lower()
        if provider_kind not in ("manual", "hiddify", "xui", "xnet"):
            raise ValueError("invalid panel kind")
        # The original tenant_servers CHECK predates X-NET. Keep the legacy
        # value valid and route via provider_kind instead of rebuilding an FK
        # parent table during a production upgrade.
        legacy_kind = (
            provider_kind
            if provider_kind in ("manual", "hiddify", "xui")
            else "manual"
        )

        flavor = str(xui_flavor or "").strip().lower()
        if provider_kind == "xui" and flavor not in ("sanaei", "alireza"):
            raise ValueError("invalid X-UI flavor")
        if provider_kind != "xui":
            flavor = ""

        xui_ids = str(xui_inbound_ids or "").strip().replace("،", ",")
        if xui_ids:
            for part in xui_ids.replace(" ", ",").split(","):
                if not part:
                    continue
                try:
                    value = int(part)
                except ValueError as exc:
                    raise ValueError("invalid X-UI inbound ids") from exc
                if value < 0:
                    raise ValueError("invalid X-UI inbound ids")
        xui_path = str(xui_sub_path or "").strip()
        if provider_kind == "xui" and not xui_path:
            xui_path = "/sub/"

        xnet_ids = str(xnet_inbound_ids or "").strip().replace("،", ",")
        if provider_kind == "xnet":
            # X-NET inbound ids may be UUID-like strings (e.g. in-9457dabf),
            # while "0" means all active inbounds.
            if any(not part.strip() for part in xnet_ids.split(",")) and xnet_ids:
                raise ValueError("invalid X-NET inbound ids")
        else:
            xnet_ids = ""
        port = int(xnet_sub_port or 0)
        if port < 0 or port > 65535:
            raise ValueError("invalid X-NET subscription port")
        xnet_path = str(xnet_sub_path or "").strip()
        if provider_kind == "xnet" and not xnet_path:
            xnet_path = "sub"

        limit_value = int(users_limit or 0)
        priority_value = int(priority or 0)
        if limit_value < 0 or priority_value < 0:
            raise ValueError("users_limit/priority must be non-negative")

        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_servers "
                "(tenant_id, label, panel_kind, provider_kind, endpoint, "
                "admin_path, user_path, xui_flavor, xui_inbound_ids, "
                "xui_public_origin, xui_sub_path, xnet_inbound_ids, "
                "xnet_public_origin, xnet_sub_port, xnet_sub_path, "
                "users_limit, priority, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'active', ?, ?)",
                (
                    self.tenant_id,
                    _text(label, 80),
                    legacy_kind,
                    provider_kind,
                    _text(endpoint, 250, required=False) or None,
                    _text(admin_path, 160, required=False) or None,
                    _text(user_path, 160, required=False) or None,
                    flavor or None,
                    _text(xui_ids, 160, required=False) or None,
                    _text(xui_public_origin, 250, required=False) or None,
                    _text(xui_path, 160, required=False) or None,
                    _text(xnet_ids, 320, required=False) or None,
                    _text(xnet_public_origin, 250, required=False) or None,
                    port or None,
                    _text(xnet_path, 160, required=False) or None,
                    limit_value,
                    priority_value,
                    now,
                    now,
                ),
            )
            if xnet_api_url:
                from TenantRuntime.server_connections import url
                self.conn.execute(
                    "UPDATE tenant_servers SET xnet_api_url=? WHERE id=? AND tenant_id=?",
                    (url(xnet_api_url), int(cursor.lastrowid or 0), self.tenant_id),
                )
        return self.server(int(cursor.lastrowid or 0))
    @staticmethod
    def _server_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        legacy = str(result.get("panel_kind") or "manual").strip().lower()
        provider = str(result.get("provider_kind") or legacy).strip().lower()
        result["legacy_panel_kind"] = legacy
        result["panel_kind"] = provider
        return result

    def server(self, server_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tenant_servers WHERE id = ? AND tenant_id = ?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("server not found")
        return self._server_dict(row)
    def list_servers(self) -> list[dict[str, Any]]:
        return [
            self._server_dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_servers "
                "WHERE tenant_id = ? ORDER BY id DESC",
                (self.tenant_id,),
            ).fetchall()
        ]
    def list_purchase_servers(self) -> list[dict[str, Any]]:
        """Servers that can safely receive a newly purchased subscription."""
        rows = self.conn.execute(
            "SELECT DISTINCT s.* FROM tenant_servers s "
            "JOIN tenant_panel_credentials c "
            "ON c.server_id=s.id AND c.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? AND s.status='active' "
            "AND (COALESCE(s.provider_kind,s.panel_kind)='hiddify' OR "
            "(COALESCE(s.provider_kind,s.panel_kind)='xui' "
            "AND s.xui_flavor IN ('sanaei','alireza')) OR "
            "COALESCE(s.provider_kind,s.panel_kind)='xnet') "
            "ORDER BY s.priority DESC, s.id ASC",
            (self.tenant_id,),
        ).fetchall()
        return [self._server_dict(row) for row in rows]

    def _purchase_server(self, server_id: int) -> dict[str, Any]:
        target_id = int(server_id)
        for server in self.list_purchase_servers():
            if int(server["id"]) == target_id:
                return server
        raise TenantBusinessError("purchase server is unavailable")

    def update_server(
        self,
        actor_id: int,
        *,
        server_id: int,
        **changes: Any,
    ) -> dict[str, Any]:
        """Update non-secret server metadata without crossing tenant scope."""
        self._admin(actor_id)
        current = self.server(int(server_id))
        allowed = {
            "label",
            "endpoint",
            "admin_path",
            "user_path",
            "xui_inbound_ids",
            "xui_public_origin",
            "xui_sub_path",
            "xnet_inbound_ids",
            "xnet_public_origin",
            "xnet_sub_port",
            "xnet_sub_path",
            "xnet_api_url",
            "users_limit",
            "priority",
            "status",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported server field")
        if not changes:
            return current

        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "label":
                normalized[key] = _text(value, 80)
            elif key in ("endpoint", "xui_public_origin", "xnet_public_origin", "xnet_api_url"):
                normalized[key] = _text(value, 250, required=False) or None
            elif key in ("admin_path", "user_path", "xui_inbound_ids", "xui_sub_path", "xnet_sub_path"):
                normalized[key] = _text(value, 160, required=False) or None
            elif key == "xnet_inbound_ids":
                normalized[key] = _text(value, 320, required=False) or None
            elif key == "xnet_sub_port":
                port = int(value or 0)
                if port < 0 or port > 65535:
                    raise ValueError("invalid X-NET subscription port")
                normalized[key] = port or None
            elif key in ("users_limit", "priority"):
                number = int(value or 0)
                if number < 0:
                    raise ValueError("server number must be non-negative")
                normalized[key] = number
            elif key == "status":
                status = str(value or "").strip().lower()
                if status not in ("active", "offline", "disabled"):
                    raise ValueError("invalid server status")
                normalized[key] = status

        assignments = ", ".join(f"{key}=?" for key in normalized)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                f"UPDATE tenant_servers SET {assignments}, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (*normalized.values(), now, int(server_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("server not found")
        return self.server(int(server_id))

    def server_admin_summary(
        self, actor_id: int, *, server_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        server = self.server(int(server_id))
        users_row = self.conn.execute(
            "SELECT COUNT(DISTINCT subscription_id) AS total FROM ("
            " SELECT subscription_id FROM tenant_subscription_nodes "
            " WHERE tenant_id=? AND server_id=? AND external_ref IS NOT NULL"
            " UNION "
            " SELECT id AS subscription_id FROM tenant_subscriptions "
            " WHERE tenant_id=? AND server_id=? AND external_ref IS NOT NULL"
            ")",
            (self.tenant_id, int(server_id), self.tenant_id, int(server_id)),
        ).fetchone()
        plans_row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM tenant_sale_plans "
            "WHERE tenant_id=? AND status!='archived' AND is_dynamic=0 AND (server_id IS NULL OR server_id=?) AND name NOT GLOB '__WHITELABEL_*'",
            (self.tenant_id,int(server_id)),
        ).fetchone()
        nodes_row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM tenant_nodes "
            "WHERE tenant_id=? AND (parent_server_id=? OR "
            "(parent_server_id IS NULL AND server_id!=?))",
            (self.tenant_id, int(server_id), int(server_id)),
        ).fetchone()
        frozen_row = self.conn.execute(
            "SELECT COUNT(DISTINCT subscription_id) AS total "
            "FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND server_id=? "
            "AND (frozen_at IS NOT NULL OR fail_count>0 OR last_error IS NOT NULL)",
            (self.tenant_id, int(server_id)),
        ).fetchone()
        result = dict(server)
        result.update(
            {
                "users_count": int(users_row["total"] if users_row else 0),
                "plans_count": int(plans_row["total"] if plans_row else 0),
                "nodes_count": int(nodes_row["total"] if nodes_row else 0),
                "frozen_count": int(frozen_row["total"] if frozen_row else 0),
                "credential_configured": bool(
                    self.panel_status(int(server_id))["configured"]
                ),
            }
        )
        return result

    def _update_server_daily_traffic(
        self,
        *,
        server_id: int,
        total_gb: float,
        day: str | None = None,
    ) -> float:
        """SellBot-compatible X-UI daily traffic baseline, Tenant-scoped."""
        sid = int(server_id)
        self.server(sid)
        try:
            total = max(0.0, float(total_gb or 0.0))
        except (TypeError, ValueError):
            total = 0.0
        day_key = str(day or utcnow().date().isoformat())
        now = iso_utc(utcnow())
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT baseline_gb,last_total_gb "
                "FROM tenant_server_traffic_daily "
                "WHERE tenant_id=? AND server_id=? AND day=?",
                (self.tenant_id, sid, day_key),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO tenant_server_traffic_daily "
                    "(tenant_id,server_id,day,baseline_gb,last_total_gb,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.tenant_id, sid, day_key, total, total, now),
                )
                return 0.0
            baseline = max(0.0, float(row["baseline_gb"] or 0.0))
            last_total = max(0.0, float(row["last_total_gb"] or 0.0))
            if total < last_total:
                baseline = total
            used = max(0.0, total - baseline)
            self.conn.execute(
                "UPDATE tenant_server_traffic_daily "
                "SET baseline_gb=?,last_total_gb=?,updated_at=? "
                "WHERE tenant_id=? AND server_id=? AND day=?",
                (baseline, total, now, self.tenant_id, sid, day_key),
            )
            return used

    def server_status_admin(
        self,
        actor_id: int,
        *,
        server_id: int,
    ) -> dict[str, Any]:
        """Return the live server-status contract used by SellBot AdminBot."""
        self._admin(actor_id)
        server, target, secret = self._panel_material(int(server_id))
        defaults: dict[str, Any] = {
            "cpu_percent": 0.0,
            "cpu_cores": 1,
            "ram_used": 0.0,
            "ram_total": 1.0,
            "disk_used": 0.0,
            "disk_total": 20.0,
            "users_total": 0,
            "users_online": 0,
            "users_today": 0,
            "users_month": 0,
            "usage_today_gb": 0.0,
            "usage_30days_gb": 0.0,
            "traffic_dl": 0.0,
            "traffic_ul": 0.0,
            "now_net_recv_mb": 0.0,
            "now_net_sent_mb": 0.0,
        }
        try:
            method = getattr(self.panel_adapter, "server_stats", None)
            if callable(method):
                try:
                    stats = method(target=target, secret=secret)
                    if isinstance(stats, dict):
                        defaults.update(stats)
                except PanelError:
                    pass
            else:
                try:
                    users = self.panel_adapter.list_users(
                        target=target, secret=secret
                    )
                except PanelError:
                    users = []
                now = utcnow()
                total_usage = 0
                online = today = month = 0
                for user in users:
                    if not isinstance(user, dict):
                        continue
                    total_usage += max(0, int(user.get("usage_bytes") or 0))
                    if bool(user.get("online")):
                        online += 1
                    raw_last = user.get("last_online")
                    if not raw_last:
                        continue
                    try:
                        seen = parse_utc(str(raw_last))
                    except (TypeError, ValueError):
                        continue
                    age = max(0.0, (now - seen).total_seconds())
                    if age <= 86400:
                        today += 1
                    if age <= 30 * 86400:
                        month += 1
                defaults.update(
                    users_total=len(users),
                    users_online=online,
                    users_today=max(today, online),
                    users_month=max(month, online),
                    usage_30days_gb=total_usage / float(1024 ** 3),
                )
        finally:
            secret = ""

        if str(server.get("panel_kind") or "").strip().lower() == "xui":
            defaults["usage_today_gb"] = self._update_server_daily_traffic(
                server_id=int(server_id),
                total_gb=float(defaults.get("usage_30days_gb") or 0.0),
            )
        defaults["server_id"] = int(server_id)
        defaults["server_label"] = str(
            server.get("label") or f"سرور #{int(server_id)}"
        ).strip()
        defaults["panel_kind"] = str(server.get("panel_kind") or "").strip().lower()
        return defaults

    def server_subscriptions(
        self, actor_id: int, *, server_id: int, query: str = ""
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        self.server(int(server_id))
        needle = str(query or "").strip()
        like = f"%{needle}%"
        sql = (
            "SELECT DISTINCT s.*, c.display_name, c.username, "
            "p.name AS plan_name FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id "
            "LEFT JOIN tenant_subscription_nodes n "
            "ON n.tenant_id=s.tenant_id AND n.subscription_id=s.id "
            "WHERE s.tenant_id=? AND (s.server_id=? OR n.server_id=?)"
        )
        args: list[Any] = [self.tenant_id, int(server_id), int(server_id)]
        if needle:
            sql += (
                " AND (c.display_name LIKE ? OR c.username LIKE ? "
                "OR CAST(c.telegram_user_id AS TEXT) LIKE ? "
                "OR CAST(s.id AS TEXT) LIKE ?)"
            )
            args.extend([like, like, like, like])
        sql += " ORDER BY s.id DESC LIMIT 100"
        return [dict(row) for row in self.conn.execute(sql, tuple(args)).fetchall()]

    def server_frozen_subscriptions(
        self, actor_id: int, *, server_id: int
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        self.server(int(server_id))
        rows = self.conn.execute(
            "SELECT n.*, s.status AS subscription_status, "
            "c.display_name, p.name AS plan_name "
            "FROM tenant_subscription_nodes n "
            "JOIN tenant_subscriptions s ON s.id=n.subscription_id "
            "AND s.tenant_id=n.tenant_id "
            "JOIN tenant_customers c ON c.id=s.customer_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id "
            "WHERE n.tenant_id=? AND n.server_id=? "
            "AND (n.frozen_at IS NOT NULL OR n.fail_count>0 OR n.last_error IS NOT NULL) "
            "ORDER BY n.id DESC LIMIT 100",
            (self.tenant_id, int(server_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def sync_server_subscriptions(
        self, actor_id: int, *, server_id: int
    ) -> dict[str, int]:
        self._admin(actor_id)
        rows = self.server_subscriptions(actor_id, server_id=int(server_id))
        synced = expired = errors = 0
        for row in rows:
            if str(row.get("status") or "") not in ("active", "disabled"):
                continue
            try:
                result = self.sync_subscription_usage(
                    actor_id, subscription_id=int(row["id"])
                )
                synced += 1
                expired += int(str(result.get("status") or "") == "expired")
            except TenantBusinessError:
                errors += 1
        return {"synced": synced, "expired": expired, "errors": errors}

    def delete_server(self, actor_id: int, *, server_id: int) -> dict[str, Any]:
        """Delete configuration only when no live subscription mapping depends on it."""
        self._admin(actor_id)
        server = self.server(int(server_id))
        primary = self.conn.execute(
            "SELECT COUNT(*) AS total FROM tenant_subscriptions "
            "WHERE tenant_id=? AND server_id=?",
            (self.tenant_id, int(server_id)),
        ).fetchone()
        mapped = self.conn.execute(
            "SELECT COUNT(*) AS total FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND server_id=? AND external_ref IS NOT NULL",
            (self.tenant_id, int(server_id)),
        ).fetchone()
        pending_orders = self.conn.execute(
            "SELECT COUNT(*) AS total FROM tenant_orders "
            "WHERE tenant_id=? AND selected_server_id=? "
            "AND status IN ('pending_payment','payment_review','paid')",
            (self.tenant_id, int(server_id)),
        ).fetchone()
        if int(primary["total"] if primary else 0) or int(mapped["total"] if mapped else 0):
            raise TenantBusinessError(
                "server still has subscription mappings; move or remove them first"
            )
        if int(pending_orders["total"] if pending_orders else 0):
            raise TenantBusinessError(
                "server is selected by unfinished purchase orders"
            )
        inventory=self.conn.execute("SELECT 1 FROM tenant_panel_users WHERE tenant_id=? AND server_id=? AND state!='deleted' LIMIT 1",
                                    (self.tenant_id,int(server_id))).fetchone()
        if inventory:
            raise TenantBusinessError("server still has panel users")
        with transaction(self.conn):
            self.conn.execute("UPDATE tenant_sale_plans SET status='archived',server_id=NULL WHERE tenant_id=? AND server_id=?",(self.tenant_id,int(server_id)))
            self.conn.execute("DELETE FROM tenant_panel_user_nodes WHERE tenant_id=? AND (server_id=? OR source_user_id IN (SELECT id FROM tenant_panel_users WHERE tenant_id=? AND server_id=?))",
                              (self.tenant_id,int(server_id),self.tenant_id,int(server_id)))
            for table in ("tenant_server_domains","tenant_server_sales_settings","tenant_panel_users"):
                self.conn.execute(f"DELETE FROM {table} WHERE tenant_id=? AND server_id=?",(self.tenant_id,int(server_id)))
            self.conn.execute(
                "DELETE FROM tenant_nodes WHERE tenant_id=? "
                "AND (server_id=? OR parent_server_id=?)",
                (self.tenant_id, int(server_id), int(server_id)),
            )
            self.conn.execute(
                "DELETE FROM tenant_panel_credentials "
                "WHERE tenant_id=? AND server_id=?",
                (self.tenant_id, int(server_id)),
            )
            changed = self.conn.execute(
                "DELETE FROM tenant_servers WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(server_id)),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("server not found")
        return {"id": int(server_id), "label": str(server["label"])}

    def set_default_server(self, actor_id: int, *, server_id: int) -> dict[str, Any]:
        """Choose the only server automatic sales provisioning may target."""
        self._admin(actor_id)
        server = self.server(int(server_id))
        kind = str(server["panel_kind"])
        if server["status"] != "active" or kind not in (
            "hiddify",
            "xui",
            "xnet",
        ):
            raise TenantBusinessError(
                "default provisioning server must be an active supported panel"
            )
        if kind == "xui" and str(server.get("xui_flavor") or "") not in (
            "sanaei",
            "alireza",
        ):
            raise TenantBusinessError("X-UI flavor is not configured")
        if not self.panel_status(int(server_id))["configured"]:
            raise TenantBusinessError("default provisioning server is not configured")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_servers SET is_default=0, updated_at=? "
                "WHERE tenant_id=? AND is_default=1",
                (now, self.tenant_id),
            )
            changed = self.conn.execute(
                "UPDATE tenant_servers SET is_default=1, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='active' "
                "AND COALESCE(provider_kind, panel_kind) "
                "IN ('hiddify','xui','xnet')",
                (now, int(server_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError(
                    "server is not eligible for automatic provisioning"
                )
        return self.server(int(server_id))
    def _provisioning_server(self) -> dict[str, Any]:
        rows = [
            self._server_dict(row)
            for row in self.conn.execute(
                "SELECT s.* FROM tenant_servers s "
                "JOIN tenant_panel_credentials c "
                "ON c.server_id=s.id AND c.tenant_id=s.tenant_id "
                "WHERE s.tenant_id=? AND s.status='active' "
                "AND (COALESCE(s.provider_kind,s.panel_kind)='hiddify' OR "
                "(COALESCE(s.provider_kind,s.panel_kind)='xui' "
                "AND s.xui_flavor IN ('sanaei','alireza')) OR "
                "COALESCE(s.provider_kind,s.panel_kind)='xnet') "
                "ORDER BY s.is_default DESC, s.priority DESC, s.id ASC",
                (self.tenant_id,),
            ).fetchall()
        ]
        if not rows:
            raise TenantBusinessError("no configured provisioning server is available")
        defaults = [row for row in rows if int(row.get("is_default") or 0) == 1]
        if len(defaults) == 1:
            return defaults[0]
        if len(rows) == 1:
            return rows[0]
        raise TenantBusinessError("default provisioning server is required")
    def provision_pending_subscription(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        """Provision to the server selected at purchase, with legacy fallback."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT s.order_id, o.selected_server_id "
            "FROM tenant_subscriptions s "
            "LEFT JOIN tenant_orders o "
            "ON o.id=s.order_id AND o.tenant_id=s.tenant_id "
            "WHERE s.id=? AND s.tenant_id=? "
            "AND s.status='pending_provisioning'",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription is not awaiting provisioning")
        if row["selected_server_id"] is not None:
            server = self._purchase_server(int(row["selected_server_id"]))
        else:
            # Backward compatibility for old orders and free-trial orders.
            server = self._provisioning_server()
        return self.activate_subscription(
            actor_id,
            subscription_id=int(subscription_id),
            server_id=int(server["id"]),
        )
    def _store_panel_secret(
        self, *, server_id: int, secret: str
    ) -> dict[str, Any]:
        if self.secret_cipher is None:
            raise TenantBusinessError("panel credential encryption is unavailable")
        raw = str(secret or "")
        if not raw.strip():
            raise TenantBusinessError("panel credential is empty")
        try:
            encrypted = self.secret_cipher.encrypt_secret(raw)
            digest = fingerprint_token(raw)
        except TokenCipherError as exc:
            raise TenantBusinessError("invalid panel credential") from exc
        finally:
            raw = ""
            secret = ""
        now = iso_utc(utcnow())
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO tenant_panel_credentials "
                    "(server_id, tenant_id, encrypted_secret, secret_fingerprint, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_id) DO UPDATE SET "
                    "encrypted_secret=excluded.encrypted_secret, "
                    "secret_fingerprint=excluded.secret_fingerprint, "
                    "updated_at=excluded.updated_at",
                    (
                        int(server_id),
                        self.tenant_id,
                        encrypted,
                        digest,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise TenantBusinessError(
                "panel credential is already used by this tenant"
            ) from exc
        return {"server_id": int(server_id), "configured": True}

    def set_panel_credential(
        self, actor_id: int, *, server_id: int, secret: str
    ) -> dict[str, Any]:
        """Replace one encrypted panel secret (Hiddify/backward-compatible)."""
        self._admin(actor_id)
        server = self.server(server_id)
        if (
            str(server["panel_kind"]) == "manual"
            or not str(server.get("endpoint") or "").strip()
        ):
            raise TenantBusinessError("server has no configured panel endpoint")
        return self._store_panel_secret(server_id=int(server_id), secret=secret)

    def set_xui_credential(
        self,
        actor_id: int,
        *,
        server_id: int,
        api_token: str = "",
        username: str = "",
        password: str = "",
        secret_header: str = "",
    ) -> dict[str, Any]:
        """Store flavor-specific X-UI credentials as one encrypted JSON blob."""
        self._admin(actor_id)
        server = self.server(server_id)
        if str(server["panel_kind"]) != "xui":
            raise TenantBusinessError("server is not X-UI")
        if not str(server.get("endpoint") or "").strip():
            raise TenantBusinessError("server has no configured panel endpoint")
        flavor = str(server.get("xui_flavor") or "").strip().lower()
        from TenantRuntime.server_connections import credential
        serialized = credential("xui", flavor=flavor, values={
            "api_token": api_token, "username": username,
            "password": password, "secret_header": secret_header,
        })
        return self._store_panel_secret(server_id=int(server_id), secret=serialized)

    def panel_status(self, server_id: int) -> dict[str, Any]:
        self.server(server_id)
        row = self.conn.execute(
            "SELECT 1 FROM tenant_panel_credentials WHERE server_id=? AND tenant_id=?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        return {"server_id": int(server_id), "configured": row is not None}

    def set_xnet_credential(
        self,
        actor_id: int,
        *,
        server_id: int,
        api_token: str = "",
        username: str = "admin",
        password: str = "",
    ) -> dict[str, Any]:
        """Store X-NET API token and optional JWT-fallback login encrypted."""
        self._admin(actor_id)
        server = self.server(server_id)
        if str(server["panel_kind"]) != "xnet":
            raise TenantBusinessError("server is not X-NET")
        if not str(server.get("endpoint") or "").strip():
            raise TenantBusinessError("server has no configured panel endpoint")
        token = str(api_token or "").strip()
        user = str(username or "admin").strip() or "admin"
        passwd = str(password or "").strip()
        if not token and not passwd:
            raise TenantBusinessError(
                "X-NET API token or fallback password is required"
            )
        material = {
            "version": 1,
            "provider": "xnet",
            "api_token": token,
            "username": user,
            "password": passwd,
        }
        serialized = json.dumps(
            material, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        try:
            return self._store_panel_secret(
                server_id=int(server_id), secret=serialized
            )
        finally:
            serialized = ""
            material.clear()

    def _panel_target(self, server: dict[str, Any]) -> PanelTarget:
        row = self.conn.execute(
            "SELECT origin FROM tenant_server_domains WHERE tenant_id=? AND server_id=? AND is_primary=1",
            (self.tenant_id,int(server.get("id") or 0))).fetchone()
        public_origin = str(row["origin"]) if row else ""
        return PanelTarget(
            public_origin=public_origin,
            kind=str(server.get("panel_kind") or "").strip().lower(),
            endpoint=str(server.get("endpoint") or "").strip(),
            admin_path=str(server.get("admin_path") or "").strip(),
            user_path=str(server.get("user_path") or "").strip(),
            xui_flavor=str(server.get("xui_flavor") or "").strip().lower(),
            xui_inbound_ids=str(server.get("xui_inbound_ids") or "").strip(),
            xui_public_origin=public_origin or str(server.get("xui_public_origin") or "").strip(),
            xui_sub_path=str(server.get("xui_sub_path") or "").strip(),
            xnet_inbound_ids=str(server.get("xnet_inbound_ids") or "").strip(),
            xnet_public_origin=public_origin or str(server.get("xnet_public_origin") or "").strip(),
            xnet_sub_port=int(server.get("xnet_sub_port") or 0),
            xnet_sub_path=str(server.get("xnet_sub_path") or "").strip(),
            xnet_api_url=str(server.get("xnet_api_url") or "").strip(),
        )
    def _panel_material(
        self, server_id: int
    ) -> tuple[dict[str, Any], PanelTarget, str]:
        server = self.server(server_id)
        endpoint = str(server.get("endpoint") or "").strip()
        kind = str(server["panel_kind"])
        if (
            str(server["status"]) != "active"
            or not endpoint
            or kind == "manual"
        ):
            raise TenantBusinessError("server is not ready for panel provisioning")
        if kind == "xui" and str(
            server.get("xui_flavor") or ""
        ) not in ("sanaei", "alireza"):
            raise TenantBusinessError("X-UI flavor is not configured")
        if kind not in ("hiddify", "xui", "xnet"):
            raise TenantBusinessError("panel provider is not supported")
        if self.secret_cipher is None:
            raise TenantBusinessError("panel credential encryption is unavailable")
        row = self.conn.execute(
            "SELECT encrypted_secret FROM tenant_panel_credentials "
            "WHERE server_id=? AND tenant_id=?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("panel credential is not configured")
        try:
            secret = self.secret_cipher.decrypt_secret(
                str(row["encrypted_secret"])
            )
            return server, self._panel_target(server), secret
        except TokenCipherError as exc:
            raise TenantBusinessError(
                "panel credential cannot be decrypted"
            ) from exc
    def prepare_server_backup(
        self,
        actor_id: int,
        *,
        server_id: int,
    ) -> tuple[dict[str, Any], PanelTarget, str]:
        """Resolve one tenant-owned panel backup without exposing other tenants."""
        self._admin(actor_id)
        return self._panel_material(int(server_id))

    def prepare_server_connection(
        self, actor_id: int, *, settings: dict | None = None,
        credentials: dict | None = None, server_id: int | None = None,
    ) -> tuple[dict, PanelTarget, str]:
        """Prepare a tenant-owned candidate without modifying working settings."""
        self._admin(actor_id)
        from TenantRuntime.server_connections import normalize_settings, credential
        candidate = self.server(server_id) if server_id is not None else {}
        candidate.update(settings or {})
        candidate = normalize_settings(candidate)
        previous = ""
        if server_id is not None:
            if self.secret_cipher is None:
                raise TenantBusinessError("panel credential encryption is unavailable")
            row = self.conn.execute(
                "SELECT encrypted_secret FROM tenant_panel_credentials "
                "WHERE tenant_id=? AND server_id=?", (self.tenant_id, int(server_id)),
            ).fetchone()
            if row is not None:
                try:
                    previous = self.secret_cipher.decrypt_secret(str(row["encrypted_secret"]))
                except TokenCipherError as exc:
                    raise TenantBusinessError("panel credential cannot be decrypted") from exc
        if credentials is None:
            if not previous:
                raise TenantBusinessError("panel credential is not configured")
            secret = previous
        else:
            values = {}
            if previous and candidate["panel_kind"] != "hiddify":
                try:
                    values = json.loads(previous)
                except ValueError:
                    values = {"api_token": previous}
                if not isinstance(values, dict):
                    values = {}
            values.update(credentials)
            secret = credential(candidate["panel_kind"],
                flavor=str(candidate.get("xui_flavor") or ""), values=values)
        return candidate, self._panel_target(candidate), secret

    def commit_server_connection(
        self, actor_id: int, *, candidate: dict, secret: str,
        server_id: int | None = None,
    ) -> dict:
        """Commit metadata and encrypted access together after the read-only probe."""
        self._admin(actor_id)
        from TenantRuntime.server_connections import normalize_settings
        candidate = normalize_settings(candidate)
        fields = {key: candidate[key] for key in (
            "label", "endpoint", "admin_path", "user_path", "xui_inbound_ids",
            "xui_public_origin", "xui_sub_path", "xnet_inbound_ids",
            "xnet_public_origin", "xnet_sub_port", "xnet_sub_path", "xnet_api_url",
            "users_limit", "priority",
        ) if key in candidate}
        with transaction(self.conn):
            if server_id is None:
                server = self.add_server(actor_id, panel_kind=candidate["panel_kind"],
                    xui_flavor=str(candidate.get("xui_flavor") or ""), **fields)
                server_id = int(server["id"])
            else:
                self.update_server(actor_id, server_id=server_id, **fields)
            self._store_panel_secret(server_id=server_id, secret=secret)
        return self.server(server_id)

    def add_node(
        self,
        actor_id: int,
        *,
        label: str,
        server_id: int | None = None,
        location: str = "",
        parent_server_id: int | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        if server_id is not None:
            self.server(int(server_id))
        if parent_server_id is not None:
            self.server(int(parent_server_id))
        if (
            server_id is not None
            and parent_server_id is not None
            and int(server_id) == int(parent_server_id)
        ):
            raise TenantBusinessError("a server cannot be its own node")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_nodes "
                "(tenant_id, server_id, parent_server_id, label, location, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    self.tenant_id,
                    int(server_id) if server_id else None,
                    int(parent_server_id) if parent_server_id else None,
                    _text(label, 80),
                    _text(location, 80, required=False) or None,
                    now,
                    now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM tenant_nodes WHERE id=? AND tenant_id=?",
            (int(cursor.lastrowid or 0), self.tenant_id),
        ).fetchone()
        assert row is not None
        return dict(row)

    def list_nodes(
        self, *, parent_server_id: int | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT n.*, s.label AS server_label, "
            "COALESCE(s.provider_kind,s.panel_kind) AS provider_kind, "
            "p.label AS parent_server_label "
            "FROM tenant_nodes n "
            "LEFT JOIN tenant_servers s "
            "ON s.id=n.server_id AND s.tenant_id=n.tenant_id "
            "LEFT JOIN tenant_servers p "
            "ON p.id=n.parent_server_id AND p.tenant_id=n.tenant_id "
            "WHERE n.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if parent_server_id is not None:
            self.server(int(parent_server_id))
            query += " AND (n.parent_server_id=? OR n.parent_server_id IS NULL)"
            args.append(int(parent_server_id))
        query += " ORDER BY n.id DESC"
        return [
            dict(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]

    def delete_node(
        self, actor_id: int, *, node_id: int, parent_server_id: int | None = None
    ) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_nodes WHERE id=? AND tenant_id=?",
            (int(node_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("node not found")
        item = dict(row)
        if (
            parent_server_id is not None
            and item.get("parent_server_id") is not None
            and int(item["parent_server_id"]) != int(parent_server_id)
        ):
            raise TenantBusinessError("node does not belong to this server")
        with transaction(self.conn):
            changed = self.conn.execute(
                "DELETE FROM tenant_nodes WHERE id=? AND tenant_id=?",
                (int(node_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("node not found")
        return item

    def _desired_subscription_servers(
        self, primary_server_id: int
    ) -> list[dict[str, Any]]:
        rows = [
            self._server_dict(row)
            for row in self.conn.execute(
                "SELECT DISTINCT s.* FROM tenant_servers s "
                "JOIN tenant_panel_credentials c "
                "ON c.server_id=s.id AND c.tenant_id=s.tenant_id "
                "LEFT JOIN tenant_nodes n "
                "ON n.server_id=s.id AND n.tenant_id=s.tenant_id "
                "AND n.status='active' "
                "AND (n.parent_server_id=? OR n.parent_server_id IS NULL) "
                "WHERE s.tenant_id=? AND s.status='active' "
                "AND (s.id=? OR n.id IS NOT NULL) "
                "AND COALESCE(s.provider_kind,s.panel_kind) "
                "IN ('hiddify','xui','xnet') "
                "ORDER BY CASE WHEN s.id=? THEN 0 ELSE 1 END, s.id",
                (
                    int(primary_server_id),
                    self.tenant_id,
                    int(primary_server_id),
                    int(primary_server_id),
                ),
            ).fetchall()
        ]
        return rows

    def _upsert_subscription_node(
        self,
        *,
        subscription_id: int,
        server_id: int,
        external_ref: str | None,
        is_primary: bool,
        status: str,
        usage_bytes: int = 0,
        last_online: str | None = None,
        last_error: str | None = None,
        reset_usage_offset: bool = False,
        usage_is_logical: bool = False,
    ) -> None:
        existing = self.conn.execute(
            "SELECT usage_offset_bytes FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
            (self.tenant_id, int(subscription_id), int(server_id))).fetchone()
        offset = 0 if reset_usage_offset or existing is None else int(existing["usage_offset_bytes"] or 0)
        effective_usage = max(0, int(usage_bytes)) + (offset if not last_error and not usage_is_logical else 0)
        now = iso_utc(utcnow())
        self.conn.execute(
            "INSERT INTO tenant_subscription_nodes "
            "(tenant_id, subscription_id, server_id, external_ref, is_primary, "
            "status, usage_bytes, last_online, last_error, created_at, updated_at, usage_offset_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, subscription_id, server_id) DO UPDATE SET "
            "external_ref=excluded.external_ref, "
            "is_primary=excluded.is_primary, status=excluded.status, "
            "usage_bytes=excluded.usage_bytes, usage_offset_bytes=excluded.usage_offset_bytes, last_online=excluded.last_online, "
            "last_error=excluded.last_error, "
            "fail_count=CASE WHEN excluded.last_error IS NULL THEN 0 ELSE fail_count END, "
            "frozen_at=CASE WHEN excluded.last_error IS NULL THEN NULL ELSE frozen_at END, "
            "updated_at=excluded.updated_at",
            (
                self.tenant_id,
                int(subscription_id),
                int(server_id),
                _text(external_ref, 255, required=False) or None,
                1 if is_primary else 0,
                str(status),
                effective_usage,
                _text(last_online, 80, required=False) or None,
                _text(last_error, 300, required=False) or None,
                now,
                now,
                offset,
            ),
        )

    def _ensure_primary_subscription_node(
        self, subscription: dict[str, Any]
    ) -> None:
        if not subscription.get("server_id") or not subscription.get("external_ref"):
            return
        existing = self.conn.execute(
            "SELECT id FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
            (
                self.tenant_id,
                int(subscription["id"]),
                int(subscription["server_id"]),
            ),
        ).fetchone()
        if existing is None:
            with transaction(self.conn):
                self._upsert_subscription_node(
                    subscription_id=int(subscription["id"]),
                    server_id=int(subscription["server_id"]),
                    external_ref=str(subscription["external_ref"]),
                    is_primary=True,
                    status=(
                        "active"
                        if str(subscription["status"]) == "active"
                        else str(subscription["status"])
                    ),
                    usage_bytes=int(subscription.get("usage_bytes") or 0),
                    last_online=subscription.get("last_online"),
                )

    def subscription_nodes_admin(
        self, actor_id: int, *, subscription_id: int
    ) -> list[dict[str, Any]]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        self._ensure_primary_subscription_node(subscription)
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT m.*, s.label AS server_label, "
                "COALESCE(s.provider_kind,s.panel_kind) AS provider_kind "
                "FROM tenant_subscription_nodes m "
                "JOIN tenant_servers s ON s.id=m.server_id AND s.tenant_id=m.tenant_id "
                "WHERE m.tenant_id=? AND m.subscription_id=? "
                "ORDER BY m.is_primary DESC, m.id",
                (self.tenant_id, int(subscription_id)),
            ).fetchall()
        ]

    def _ensure_subscription_smart_link(
        self, *, subscription_id: int, label: str = ""
    ) -> dict[str, Any]:
        target = f"subscription:{int(subscription_id)}"
        row = self.conn.execute(
            "SELECT * FROM tenant_smart_links "
            "WHERE tenant_id=? AND target=? ORDER BY id LIMIT 1",
            (self.tenant_id, target),
        ).fetchone()
        if row is not None:
            return dict(row)
        now = iso_utc(utcnow())
        code = secrets.token_urlsafe(24)
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO tenant_smart_links "
                    "(tenant_id, code, label, target, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                    (
                        self.tenant_id,
                        code,
                        _text(label or f"Subscription {int(subscription_id)}", 80),
                        target,
                        now,
                        now,
                    ),
                )
            link_id = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT * FROM tenant_smart_links "
                "WHERE tenant_id=? AND target=? ORDER BY id LIMIT 1",
                (self.tenant_id, target),
            ).fetchone()
            if row is None:
                raise
            return dict(row)
        row = self.conn.execute(
            "SELECT * FROM tenant_smart_links WHERE id=? AND tenant_id=?",
            (link_id, self.tenant_id),
        ).fetchone()
        assert row is not None
        return dict(row)

    def _ensure_panel_user_smart_link(
        self, *, panel_user_id: int, label: str = ""
    ) -> dict[str, Any]:
        uid = int(panel_user_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_panel_users "
            "WHERE id=? AND tenant_id=? AND state!='deleted'",
            (uid, self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("panel user not found")
        target = f"paneluser:{uid}"
        existing = self.conn.execute(
            "SELECT * FROM tenant_smart_links "
            "WHERE tenant_id=? AND target=? ORDER BY id LIMIT 1",
            (self.tenant_id, target),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = iso_utc(utcnow())
        code = secrets.token_urlsafe(24)
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO tenant_smart_links "
                    "(tenant_id, code, label, target, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                    (
                        self.tenant_id,
                        code,
                        _text(label or str(row["name"] or f"Panel user {uid}"), 80),
                        target,
                        now,
                        now,
                    ),
                )
            link_id = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                "SELECT * FROM tenant_smart_links "
                "WHERE tenant_id=? AND target=? ORDER BY id LIMIT 1",
                (self.tenant_id, target),
            ).fetchone()
            if existing is None:
                raise
            return dict(existing)
        created = self.conn.execute(
            "SELECT * FROM tenant_smart_links WHERE id=? AND tenant_id=?",
            (link_id, self.tenant_id),
        ).fetchone()
        assert created is not None
        return dict(created)

    def _panel_smart_url(
        self,
        *,
        panel_user_id: int,
        label: str = "",
        base64_output: bool = False,
    ) -> str:
        link = self._ensure_panel_user_smart_link(
            panel_user_id=int(panel_user_id), label=label
        )
        settings = self.runtime_userbot_settings()
        public_base = str(
            settings.get("smart_base_url")
            or os.getenv("SMART_SUB_PUBLIC_BASE_URL", "")
            or ""
        ).strip()
        if not public_base:
            return ""
        from TenantRuntime.smart_subscription import smart_subscription_url
        return smart_subscription_url(
            public_base,
            str(link["code"]),
            base64_output=bool(base64_output),
        )

    def _smart_url(
        self,
        *,
        subscription_id: int,
        label: str = "",
        base64_output: bool = True,
    ) -> str:
        link = self._ensure_subscription_smart_link(
            subscription_id=int(subscription_id), label=label
        )
        settings = self.runtime_userbot_settings()
        public_base = str(
            settings.get("smart_base_url")
            or os.getenv("SMART_SUB_PUBLIC_BASE_URL", "")
            or ""
        ).strip()
        if not public_base:
            return ""
        from TenantRuntime.smart_subscription import smart_subscription_url

        return smart_subscription_url(
            public_base,
            str(link["code"]),
            base64_output=bool(base64_output),
        )

    def repair_subscription_nodes(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, int]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        self._require_completed_rotation(subscription_id)
        if subscription["status"] not in ("active", "disabled"):
            raise TenantBusinessError("subscription nodes cannot be repaired")
        if not subscription.get("server_id") or not subscription.get("external_ref"):
            raise TenantBusinessError("subscription is not provisioned")
        self._ensure_primary_subscription_node(subscription)
        plan = self.plan(int(subscription["plan_id"]), public=False)
        desired = self._desired_subscription_servers(int(subscription["server_id"]))
        desired_ids = {int(server["id"]) for server in desired}
        created = restored = disabled = errors = 0

        for server in desired:
            server_id = int(server["id"])
            if server_id == int(subscription["server_id"]):
                continue
            mapping = self.conn.execute(
                "SELECT * FROM tenant_subscription_nodes "
                "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
                (self.tenant_id, int(subscription_id), server_id),
            ).fetchone()
            secret = ""
            try:
                _, target, secret = self._panel_material(server_id)
                if mapping is not None and mapping["external_ref"]:
                    user = self.panel_adapter.get_user(
                        target=target,
                        secret=secret,
                        external_ref=str(mapping["external_ref"]),
                    )
                    if subscription["status"] == "active" and not user.active:
                        user = self.panel_adapter.set_enabled(
                            target=target,
                            secret=secret,
                            external_ref=str(mapping["external_ref"]),
                            enabled=True,
                        )
                    with transaction(self.conn):
                        self._upsert_subscription_node(
                            subscription_id=int(subscription_id),
                            server_id=server_id,
                            external_ref=user.external_ref,
                            is_primary=False,
                            status=(
                                "active"
                                if subscription["status"] == "active"
                                else "disabled"
                            ),
                            usage_bytes=int(user.usage_bytes),
                            last_online=user.last_online,
                        )
                    restored += 1
                    continue

                result = self.panel_adapter.provision(
                    target=target,
                    secret=secret,
                    request=ProvisionRequest(
                        tenant_id=self.tenant_id,
                        server_id=server_id,
                        subscription_id=int(subscription_id),
                        customer_id=int(subscription["customer_id"]),
                        traffic_bytes=int(subscription["traffic_bytes"]),
                        duration_days=int(plan["duration_days"]),
                        expires_at=str(subscription["expires_at"]),
                        idempotency_key=(
                            f"tenant:{self.tenant_id}:subscription:"
                            f"{int(subscription_id)}:server:{server_id}"
                        ),
                        name=str(subscription.get("service_name") or ""),
                    ),
                )
                if subscription["status"] == "disabled":
                    self.panel_adapter.set_enabled(
                        target=target,
                        secret=secret,
                        external_ref=result.external_ref,
                        enabled=False,
                    )
                with transaction(self.conn):
                    self._upsert_subscription_node(
                        subscription_id=int(subscription_id),
                        server_id=server_id,
                        external_ref=result.external_ref,
                        is_primary=False,
                        status=str(subscription["status"]),
                    )
                created += 1
            except PanelError:
                with transaction(self.conn):
                    self._upsert_subscription_node(
                        subscription_id=int(subscription_id),
                        server_id=server_id,
                        external_ref=(
                            str(mapping["external_ref"])
                            if mapping is not None and mapping["external_ref"]
                            else None
                        ),
                        is_primary=False,
                        status="error",
                        last_error="provider operation failed",
                    )
                errors += 1
            finally:
                secret = ""

        stale = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND is_primary=0",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        for row in stale:
            if int(row["server_id"]) in desired_ids or row["status"] == "disabled":
                continue
            secret = ""
            try:
                if row["external_ref"]:
                    _, target, secret = self._panel_material(int(row["server_id"]))
                    self.panel_adapter.set_enabled(
                        target=target,
                        secret=secret,
                        external_ref=str(row["external_ref"]),
                        enabled=False,
                    )
                with transaction(self.conn):
                    self._upsert_subscription_node(
                        subscription_id=int(subscription_id),
                        server_id=int(row["server_id"]),
                        external_ref=row["external_ref"],
                        is_primary=False,
                        status="disabled",
                        usage_bytes=int(row["usage_bytes"] or 0),
                        usage_is_logical=True,
                        last_online=row["last_online"],
                    )
                disabled += 1
            except (PanelError, TenantBusinessError):
                errors += 1
            finally:
                secret = ""

        return {
            "created": created,
            "restored": restored,
            "disabled": disabled,
            "errors": errors,
        }

    def _ensure_growth_settings(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tenant_sales_growth_settings WHERE tenant_id=?",
            (self.tenant_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO tenant_sales_growth_settings "
                "(tenant_id, updated_at) VALUES (?, ?)",
                (self.tenant_id, now),
            )
        row = self.conn.execute(
            "SELECT * FROM tenant_sales_growth_settings WHERE tenant_id=?",
            (self.tenant_id,),
        ).fetchone()
        assert row is not None
        return dict(row)

    def growth_settings(self, actor_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        return self._ensure_growth_settings()

    def update_growth_settings(
        self,
        actor_id: int,
        *,
        referral_enabled: bool | None = None,
        referral_trial_reward_enabled: bool | None = None,
        referral_trial_reward: int | None = None,
        referral_purchase_reward_enabled: bool | None = None,
        referral_purchase_reward: int | None = None,
        referral_min_purchase: int | None = None,
        referral_max_rewards: int | None = None,
        referral_currency: str | None = None,
        referral_invite_text: str | None = None,
        trial_enabled: bool | None = None,
        trial_announce_enabled: bool | None = None,
        trial_traffic_gb: int | None = None,
        trial_duration_days: int | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        current = self._ensure_growth_settings()
        values = {
            "referral_enabled": int(
                bool(current["referral_enabled"])
                if referral_enabled is None
                else bool(referral_enabled)
            ),
            "referral_trial_reward_enabled": int(
                bool(current["referral_trial_reward_enabled"])
                if referral_trial_reward_enabled is None
                else bool(referral_trial_reward_enabled)
            ),
            "referral_trial_reward": int(
                current["referral_trial_reward"]
                if referral_trial_reward is None
                else referral_trial_reward
            ),
            "referral_purchase_reward_enabled": int(
                bool(current["referral_purchase_reward_enabled"])
                if referral_purchase_reward_enabled is None
                else bool(referral_purchase_reward_enabled)
            ),
            "referral_purchase_reward": int(
                current["referral_purchase_reward"]
                if referral_purchase_reward is None
                else referral_purchase_reward
            ),
            "referral_min_purchase": int(
                current["referral_min_purchase"]
                if referral_min_purchase is None
                else referral_min_purchase
            ),
            "referral_max_rewards": int(
                current["referral_max_rewards"]
                if referral_max_rewards is None
                else referral_max_rewards
            ),
            "referral_currency": str(
                current["referral_currency"]
                if referral_currency is None
                else referral_currency
            ).strip().upper(),
            "referral_invite_text": str(
                current["referral_invite_text"]
                if referral_invite_text is None
                else referral_invite_text
            ).strip(),
            "trial_enabled": int(
                bool(current["trial_enabled"])
                if trial_enabled is None
                else bool(trial_enabled)
            ),
            "trial_announce_enabled": int(
                bool(current["trial_announce_enabled"])
                if trial_announce_enabled is None
                else bool(trial_announce_enabled)
            ),
            "trial_traffic_gb": int(
                current["trial_traffic_gb"]
                if trial_traffic_gb is None
                else trial_traffic_gb
            ),
            "trial_duration_days": int(
                current["trial_duration_days"]
                if trial_duration_days is None
                else trial_duration_days
            ),
        }
        if (
            min(
                values["referral_trial_reward"],
                values["referral_purchase_reward"],
                values["referral_min_purchase"],
                values["referral_max_rewards"],
            )
            < 0
            or values["trial_traffic_gb"] <= 0
            or values["trial_duration_days"] <= 0
            or not 3 <= len(values["referral_currency"]) <= 8
            or len(values["referral_invite_text"]) > 3000
        ):
            raise ValueError("invalid sales growth settings")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_sales_growth_settings SET "
                "referral_enabled=?, referral_trial_reward_enabled=?, "
                "referral_trial_reward=?, referral_purchase_reward_enabled=?, "
                "referral_purchase_reward=?, referral_min_purchase=?, "
                "referral_max_rewards=?, referral_currency=?, "
                "referral_invite_text=?, trial_enabled=?, "
                "trial_announce_enabled=?, trial_traffic_gb=?, "
                "trial_duration_days=?, updated_at=? "
                "WHERE tenant_id=?",
                (
                    values["referral_enabled"],
                    values["referral_trial_reward_enabled"],
                    values["referral_trial_reward"],
                    values["referral_purchase_reward_enabled"],
                    values["referral_purchase_reward"],
                    values["referral_min_purchase"],
                    values["referral_max_rewards"],
                    values["referral_currency"],
                    values["referral_invite_text"],
                    values["trial_enabled"],
                    values["trial_announce_enabled"],
                    values["trial_traffic_gb"],
                    values["trial_duration_days"],
                    now,
                    self.tenant_id,
                ),
            )
        return self._ensure_growth_settings()

    def _ensure_referral_code(self, customer_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tenant_customers WHERE id=? AND tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("customer not found")
        if str(row["referral_code"] or "").strip():
            return dict(row)
        for _ in range(12):
            code = secrets.token_hex(5)
            try:
                with transaction(self.conn):
                    changed = self.conn.execute(
                        "UPDATE tenant_customers SET referral_code=?, updated_at=? "
                        "WHERE id=? AND tenant_id=? "
                        "AND (referral_code IS NULL OR referral_code='')",
                        (
                            code,
                            iso_utc(utcnow()),
                            int(customer_id),
                            self.tenant_id,
                        ),
                    )
                if changed.rowcount == 1:
                    break
            except sqlite3.IntegrityError:
                continue
        row = self.conn.execute(
            "SELECT * FROM tenant_customers WHERE id=? AND tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if row is None or not str(row["referral_code"] or "").strip():
            raise TenantBusinessError("referral code could not be created")
        return dict(row)

    def referral_summary(self, actor_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        customer = self._ensure_referral_code(int(customer["id"]))
        settings = self._ensure_growth_settings()
        referred = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_referrals "
            "WHERE tenant_id=? AND inviter_customer_id=? AND status='active'",
            (self.tenant_id, int(customer["id"])),
        ).fetchone()
        totals: dict[tuple[str, str], dict[str, Any]] = {}
        auto_rewards = self.conn.execute(
            "SELECT reward_type, currency, COUNT(*) AS count, "
            "COALESCE(SUM(amount),0) AS amount "
            "FROM tenant_referral_rewards "
            "WHERE tenant_id=? AND inviter_customer_id=? AND status='paid' "
            "GROUP BY reward_type, currency",
            (self.tenant_id, int(customer["id"])),
        ).fetchall()
        manual_rewards = self.conn.execute(
            "SELECT 'manual' AS reward_type, currency, COUNT(*) AS count, "
            "COALESCE(SUM(amount),0) AS amount "
            "FROM tenant_referral_manual_rewards "
            "WHERE tenant_id=? AND customer_id=? GROUP BY currency",
            (self.tenant_id, int(customer["id"])),
        ).fetchall()
        for row in [*auto_rewards, *manual_rewards]:
            key = (str(row["reward_type"]), str(row["currency"]))
            bucket = totals.setdefault(
                key,
                {
                    "reward_type": key[0],
                    "currency": key[1],
                    "count": 0,
                    "amount": 0,
                },
            )
            bucket["count"] += int(row["count"] or 0)
            bucket["amount"] += int(row["amount"] or 0)
        return {
            "referral_code": str(customer["referral_code"]),
            "referred_count": int(referred[0] or 0),
            "rewards": sorted(
                totals.values(),
                key=lambda item: (str(item["reward_type"]), str(item["currency"])),
            ),
            "settings": settings,
        }

    def register_referral(
        self,
        actor_id: int,
        *,
        referral_code: str,
    ) -> dict[str, Any]:
        invitee = self._customer(actor_id)
        code = str(referral_code or "").strip().lower()
        if not code or len(code) > 40:
            raise TenantBusinessError("invalid referral code")
        settings = self._ensure_growth_settings()
        if not bool(settings["referral_enabled"]):
            raise TenantBusinessError("referral program is disabled")
        existing = self.conn.execute(
            "SELECT * FROM tenant_referrals "
            "WHERE tenant_id=? AND invitee_customer_id=?",
            (self.tenant_id, int(invitee["id"])),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        inviter = self.conn.execute(
            "SELECT * FROM tenant_customers "
            "WHERE tenant_id=? AND referral_code=? AND status='active'",
            (self.tenant_id, code),
        ).fetchone()
        if inviter is None:
            raise TenantBusinessError("referral code not found")
        if int(inviter["id"]) == int(invitee["id"]):
            raise TenantBusinessError("self referral is not allowed")
        paid_before = self.conn.execute(
            "SELECT 1 FROM tenant_orders "
            "WHERE tenant_id=? AND customer_id=? "
            "AND order_kind IN ('purchase','renewal') "
            "AND status IN ('paid','fulfilled') LIMIT 1",
            (self.tenant_id, int(invitee["id"])),
        ).fetchone()
        qualified = 0 if paid_before is not None else 1
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_referrals "
                "(tenant_id, inviter_customer_id, invitee_customer_id, "
                "invited_by_code, qualified, fraud_flag, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?)",
                (
                    self.tenant_id,
                    int(inviter["id"]),
                    int(invitee["id"]),
                    code,
                    qualified,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE tenant_customers SET invited_by_customer_id=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    int(inviter["id"]),
                    now,
                    int(invitee["id"]),
                    self.tenant_id,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM tenant_referrals WHERE id=? AND tenant_id=?",
            (int(cursor.lastrowid or 0), self.tenant_id),
        ).fetchone()
        assert row is not None
        return dict(row)

    def _wallet_change_tx(
        self,
        *,
        customer_id: int,
        currency: str,
        amount: int,
        kind: str,
        idempotency_key: str,
        order_id: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        currency_code = _currency_code(currency)
        delta = int(amount)
        if delta == 0:
            raise ValueError("wallet amount must be non-zero")
        existing = self.conn.execute(
            "SELECT * FROM tenant_wallet_transactions WHERE idempotency_key=?",
            (str(idempotency_key),),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        row = self.conn.execute(
            "SELECT balance FROM tenant_wallet_accounts "
            "WHERE tenant_id=? AND customer_id=? AND currency=?",
            (self.tenant_id, int(customer_id), currency_code),
        ).fetchone()
        balance = int(row["balance"] or 0) if row is not None else 0
        resulting = balance + delta
        if resulting < 0:
            raise TenantBusinessError("insufficient wallet balance")
        now = iso_utc(utcnow())
        self.conn.execute(
            "INSERT INTO tenant_wallet_accounts "
            "(tenant_id, customer_id, currency, balance, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, customer_id, currency) DO UPDATE SET "
            "balance=excluded.balance, updated_at=excluded.updated_at",
            (
                self.tenant_id,
                int(customer_id),
                currency_code,
                resulting,
                now,
            ),
        )
        cursor = self.conn.execute(
            "INSERT INTO tenant_wallet_transactions "
            "(tenant_id, customer_id, currency, amount, kind, order_id, "
            "idempotency_key, note, resulting_balance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.tenant_id,
                int(customer_id),
                currency_code,
                delta,
                str(kind),
                int(order_id) if order_id is not None else None,
                str(idempotency_key),
                _text(note, 240, required=False) or None,
                resulting,
                now,
            ),
        )
        tx = self.conn.execute(
            "SELECT * FROM tenant_wallet_transactions WHERE id=?",
            (int(cursor.lastrowid or 0),),
        ).fetchone()
        assert tx is not None
        return dict(tx)

    def _preferred_wallet_currency(self, customer_id: int) -> str:
        cid = int(customer_id)
        accounts = [
            dict(row)
            for row in self.conn.execute(
                "SELECT currency,balance FROM tenant_wallet_accounts "
                "WHERE tenant_id=? AND customer_id=? ORDER BY balance DESC,currency",
                (self.tenant_id, cid),
            ).fetchall()
        ]
        valid_accounts = [
            row for row in accounts
            if re.fullmatch(r"[A-Z][A-Z0-9]{2,7}", str(row.get("currency") or "").upper())
        ]
        if valid_accounts:
            for preferred in ("IRR", "IRT"):
                hit = next(
                    (row for row in valid_accounts if str(row["currency"]).upper() == preferred),
                    None,
                )
                if hit is not None and int(hit.get("balance") or 0) > 0:
                    return preferred
            return str(valid_accounts[0]["currency"]).upper()

        row = self.conn.execute(
            "SELECT currency FROM tenant_orders "
            "WHERE tenant_id=? AND customer_id=? "
            "ORDER BY id DESC LIMIT 1",
            (self.tenant_id, cid),
        ).fetchone()
        if row is not None:
            try:
                return _currency_code(row["currency"])
            except ValueError:
                pass

        row = self.conn.execute(
            "SELECT currency FROM tenant_sale_plans "
            "WHERE tenant_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (self.tenant_id,),
        ).fetchone()
        if row is not None:
            try:
                return _currency_code(row["currency"])
            except ValueError:
                pass

        row = self.conn.execute(
            "SELECT currency FROM tenant_payment_methods "
            "WHERE tenant_id=? AND status='active' "
            "ORDER BY priority,id LIMIT 1",
            (self.tenant_id,),
        ).fetchone()
        if row is not None:
            try:
                return _currency_code(row["currency"])
            except ValueError:
                pass
        return "IRR"

    def wallet_summary(self, actor_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        accounts = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_wallet_accounts "
                "WHERE tenant_id=? AND customer_id=? ORDER BY currency",
                (self.tenant_id, int(customer["id"])),
            ).fetchall()
        ]
        history = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_wallet_transactions "
                "WHERE tenant_id=? AND customer_id=? ORDER BY id DESC LIMIT 15",
                (self.tenant_id, int(customer["id"])),
            ).fetchall()
        ]
        gifts = [
            {
                "id": int(row["id"]),
                "tenant_id": self.tenant_id,
                "customer_id": int(customer["id"]),
                "currency": str(row["currency"]),
                "amount": int(row["amount"]),
                "kind": "gift",
                "order_id": None,
                "idempotency_key": f"gift:{int(row['id'])}",
                "note": f"gift voucher {row['code']}",
                "resulting_balance": int(row["resulting_balance"]),
                "created_at": str(row["redeemed_at"]),
            }
            for row in self.conn.execute(
                "SELECT r.*, v.code FROM tenant_gift_redemptions r "
                "JOIN tenant_gift_vouchers v "
                "ON v.id=r.voucher_id AND v.tenant_id=r.tenant_id "
                "WHERE r.tenant_id=? AND r.customer_id=? "
                "ORDER BY r.id DESC LIMIT 15",
                (self.tenant_id, int(customer["id"])),
            ).fetchall()
        ]
        history = sorted(
            history + gifts,
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:15]
        primary_currency = self._preferred_wallet_currency(int(customer["id"]))
        primary_account = next(
            (
                row for row in accounts
                if str(row.get("currency") or "").upper() == primary_currency
            ),
            {
                "tenant_id": self.tenant_id,
                "customer_id": int(customer["id"]),
                "currency": primary_currency,
                "balance": 0,
            },
        )
        return {
            "accounts": accounts,
            "history": history,
            "primary_currency": primary_currency,
            "primary_account": dict(primary_account),
            "customer_status": str(customer.get("status") or "active"),
        }

    def create_wallet_topup(
        self,
        actor_id: int,
        *,
        amount: int,
        currency: str,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        value = int(amount)
        currency_code = _currency_code(currency)
        if value <= 0:
            raise ValueError("invalid wallet topup amount")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_wallet_topups "
                "(tenant_id, customer_id, amount, currency, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending_payment', ?, ?)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    value,
                    currency_code,
                    now,
                    now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM tenant_wallet_topups WHERE id=? AND tenant_id=?",
            (int(cursor.lastrowid or 0), self.tenant_id),
        ).fetchone()
        assert row is not None
        return dict(row)

    def wallet_topup(self, actor_id: int, topup_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT * FROM tenant_wallet_topups "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(topup_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("wallet topup not found")
        return dict(row)

    def submit_wallet_topup_receipt(
        self,
        actor_id: int,
        *,
        topup_id: int,
        method_id: int,
        reference: str | None = None,
        telegram_file_id: str | None = None,
    ) -> dict[str, Any]:
        topup = self.wallet_topup(actor_id, int(topup_id))
        if topup["status"] != "pending_payment":
            raise TenantBusinessError("wallet topup is not awaiting payment")
        method = self.method(int(method_id), currency=str(topup["currency"]))
        ref = _text(reference, 160, required=False) or None
        file_id = _text(telegram_file_id, 256, required=False) or None
        if bool(method.get("requires_receipt", True)) and not ref and not file_id:
            raise ValueError("receipt is required")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_wallet_topups SET status='payment_review', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending_payment'",
                (now, int(topup_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("wallet topup state changed")
            cursor = self.conn.execute(
                "INSERT INTO tenant_wallet_topup_receipts "
                "(tenant_id, topup_id, payment_method_id, reference, telegram_file_id, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    self.tenant_id,
                    int(topup_id),
                    int(method["id"]),
                    ref,
                    file_id,
                    now,
                ),
            )
            receipt_id = int(cursor.lastrowid or 0)
            self._record_payment_event_tx(
                source="wallet_topup",
                payment_ref_id=receipt_id,
                provider_key=str(method.get("provider_key") or method.get("kind") or ""),
                event_type="submitted",
                actor_id=actor_id,
                idempotency_key=(
                    f"tenant:{self.tenant_id}:payment:wallet_topup:"
                    f"{receipt_id}:submitted"
                ),
            )
        return {
            "id": receipt_id,
            "topup_id": int(topup_id),
            "payment_key": f"wallet_topup:{receipt_id}",
            "status": "pending",
        }

    def list_wallet_topups_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT w.*, c.display_name, c.telegram_user_id "
            "FROM tenant_wallet_topups w "
            "JOIN tenant_customers c "
            "ON c.id=w.customer_id AND c.tenant_id=w.tenant_id "
            "WHERE w.tenant_id=? ORDER BY w.id DESC LIMIT 100",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_wallet_topup_receipts_admin(
        self, actor_id: int
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT r.*, w.customer_id, w.amount, w.currency, "
            "w.status AS topup_status, c.display_name, c.telegram_user_id "
            "FROM tenant_wallet_topup_receipts r "
            "JOIN tenant_wallet_topups w "
            "ON w.id=r.topup_id AND w.tenant_id=r.tenant_id "
            "JOIN tenant_customers c "
            "ON c.id=w.customer_id AND c.tenant_id=w.tenant_id "
            "WHERE r.tenant_id=? AND r.status='pending' "
            "ORDER BY r.id",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def review_wallet_topup_receipt(
        self,
        actor_id: int,
        *,
        receipt_id: int,
        approve: bool,
        note: str = "",
        external_event_id: str | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT r.*, w.customer_id, w.amount, w.currency, "
                "w.status AS topup_status "
                "FROM tenant_wallet_topup_receipts r "
                "JOIN tenant_wallet_topups w "
                "ON w.id=r.topup_id AND w.tenant_id=r.tenant_id "
                "WHERE r.id=? AND r.tenant_id=?",
                (int(receipt_id), self.tenant_id),
            ).fetchone()
            if row is None:
                raise TenantBusinessError("wallet topup receipt not found")
            receipt = dict(row)
            if receipt["status"] != "pending" or receipt["topup_status"] != "payment_review":
                raise TenantBusinessError("wallet topup receipt was already reviewed")
            changed = self.conn.execute(
                "UPDATE tenant_wallet_topup_receipts "
                "SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, "
                "provider_event_id=COALESCE(?, provider_event_id) "
                "WHERE id=? AND tenant_id=? AND status='pending'",
                (
                    "approved" if approve else "rejected",
                    int(actor_id),
                    now,
                    _text(note, 500, required=False),
                    _text(external_event_id, 180, required=False) or None,
                    int(receipt_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("wallet topup receipt state changed")
            target = "paid" if approve else "rejected"
            changed = self.conn.execute(
                "UPDATE tenant_wallet_topups SET status=?, "
                "paid_at=CASE WHEN ?='paid' THEN COALESCE(paid_at, ?) ELSE paid_at END, "
                "updated_at=? WHERE id=? AND tenant_id=? AND status='payment_review'",
                (
                    target,
                    target,
                    now,
                    now,
                    int(receipt["topup_id"]),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("wallet topup state changed")
            wallet_tx = None
            if approve:
                wallet_tx = self._wallet_change_tx(
                    customer_id=int(receipt["customer_id"]),
                    currency=str(receipt["currency"]),
                    amount=int(receipt["amount"]),
                    kind="topup",
                    idempotency_key=(
                        f"tenant:{self.tenant_id}:wallet-topup:"
                        f"{int(receipt['topup_id'])}"
                    ),
                    note=f"wallet topup #{int(receipt['topup_id'])}",
                )
            method_row = self.conn.execute(
                "SELECT kind, provider_key FROM tenant_payment_methods "
                "WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(receipt["payment_method_id"])),
            ).fetchone()
            provider_key = (
                str(method_row["provider_key"] or method_row["kind"])
                if method_row is not None else "unknown"
            )
            self._record_payment_event_tx(
                source="wallet_topup",
                payment_ref_id=int(receipt_id),
                provider_key=provider_key,
                event_type="approved" if approve else "rejected",
                actor_id=actor_id,
                external_event_id=external_event_id,
                idempotency_key=(
                    f"tenant:{self.tenant_id}:payment:wallet_topup:"
                    f"{int(receipt_id)}:{'approved' if approve else 'rejected'}"
                ),
            )
        return {
            "topup_id": int(receipt["topup_id"]),
            "status": target,
            "customer_id": int(receipt["customer_id"]),
            "amount": int(receipt["amount"]),
            "currency": str(receipt["currency"]),
            "wallet_transaction": wallet_tx,
            "payment_key": f"wallet_topup:{int(receipt_id)}",
        }

    def adjust_wallet_admin(
        self,
        actor_id: int,
        *,
        customer_id: int,
        currency: str,
        amount: int,
        note: str = "",
    ) -> dict[str, Any]:
        self._admin(actor_id)
        owned = self.conn.execute(
            "SELECT 1 FROM tenant_customers WHERE id=? AND tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if owned is None:
            raise TenantBusinessError("customer not found")
        delta = int(amount)
        if delta == 0:
            raise ValueError("wallet amount must be non-zero")
        nonce = secrets.token_hex(8)
        with transaction(self.conn):
            return self._wallet_change_tx(
                customer_id=int(customer_id),
                currency=currency,
                amount=delta,
                kind="admin_credit" if delta > 0 else "admin_debit",
                idempotency_key=(
                    f"tenant:{self.tenant_id}:admin-wallet:"
                    f"{int(customer_id)}:{nonce}"
                ),
                note=note,
            )

    def add_gift_voucher_admin(
        self,
        actor_id: int,
        *,
        code: str,
        amount: int,
        currency: str = "IRR",
        max_uses: int = 1,
        expires_at: str = "",
    ) -> dict[str, Any]:
        self._admin(actor_id)
        clean_code = str(code or "").strip().upper()
        value = int(amount)
        uses = int(max_uses)
        clean_currency = _text(currency, 8).upper()
        if not clean_code or len(clean_code) > 48 or value <= 0 or uses <= 0:
            raise ValueError("invalid gift voucher")
        expiry = _text(expires_at, 80, required=False) or None
        if expiry:
            parse_utc(expiry)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_gift_vouchers "
                "(tenant_id, code, amount, currency, max_uses, used_count, "
                "expires_at, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, 'active', ?, ?)",
                (
                    self.tenant_id,
                    clean_code,
                    value,
                    clean_currency,
                    uses,
                    expiry,
                    now,
                    now,
                ),
            )
        return self.gift_voucher_admin(
            actor_id, voucher_id=int(cursor.lastrowid or 0)
        )

    def gift_voucher_admin(
        self, actor_id: int, *, voucher_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_gift_vouchers "
            "WHERE tenant_id=? AND id=?",
            (self.tenant_id, int(voucher_id)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("gift voucher not found")
        return dict(row)

    def list_gift_vouchers_admin(
        self, actor_id: int
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT * FROM tenant_gift_vouchers "
            "WHERE tenant_id=? ORDER BY id DESC",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def gift_redemptions_admin(
        self, actor_id: int, *, voucher_id: int | None = None
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        sql = (
            "SELECT r.*, v.code, c.display_name, c.username, "
            "c.telegram_user_id FROM tenant_gift_redemptions r "
            "JOIN tenant_gift_vouchers v "
            "ON v.id=r.voucher_id AND v.tenant_id=r.tenant_id "
            "JOIN tenant_customers c "
            "ON c.id=r.customer_id AND c.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if voucher_id is not None:
            sql += " AND r.voucher_id=?"
            args.append(int(voucher_id))
        sql += " ORDER BY r.id DESC LIMIT 500"
        return [
            dict(row)
            for row in self.conn.execute(sql, tuple(args)).fetchall()
        ]

    def set_gift_voucher_status_admin(
        self, actor_id: int, *, voucher_id: int, enabled: bool
    ) -> dict[str, Any]:
        self._admin(actor_id)
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET status=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (
                    "active" if enabled else "disabled",
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(voucher_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")
        return self.gift_voucher_admin(actor_id, voucher_id=int(voucher_id))

    def rename_gift_voucher_admin(
        self, actor_id: int, *, voucher_id: int, code: str
    ) -> dict[str, Any]:
        self._admin(actor_id)
        clean_code = str(code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{4,48}", clean_code):
            raise ValueError("invalid gift voucher code")
        duplicate = self.conn.execute(
            "SELECT 1 FROM tenant_gift_vouchers "
            "WHERE tenant_id=? AND code=? AND id<>?",
            (self.tenant_id, clean_code, int(voucher_id)),
        ).fetchone()
        if duplicate is not None:
            raise TenantBusinessError("gift voucher code already exists")
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET code=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (
                    clean_code,
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(voucher_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")
        return self.gift_voucher_admin(actor_id, voucher_id=int(voucher_id))

    def set_gift_voucher_amount_admin(
        self, actor_id: int, *, voucher_id: int, amount: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        value = int(amount)
        if value <= 0:
            raise ValueError("invalid gift voucher amount")
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET amount=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (
                    value,
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(voucher_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")
        return self.gift_voucher_admin(actor_id, voucher_id=int(voucher_id))

    def set_gift_voucher_max_uses_admin(
        self, actor_id: int, *, voucher_id: int, max_uses: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        limit = int(max_uses)
        if limit <= 0:
            raise ValueError("invalid gift voucher usage limit")
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET max_uses=?, "
                "status=CASE WHEN used_count>=? THEN 'disabled' ELSE 'active' END, "
                "updated_at=? WHERE tenant_id=? AND id=?",
                (
                    limit,
                    limit,
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(voucher_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")
        return self.gift_voucher_admin(actor_id, voucher_id=int(voucher_id))

    def set_gift_voucher_expiry_hours_admin(
        self, actor_id: int, *, voucher_id: int, hours: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        duration = int(hours)
        if duration < 0:
            raise ValueError("invalid gift voucher expiry")
        expiry = (
            None
            if duration == 0
            else iso_utc(utcnow() + timedelta(hours=duration))
        )
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET expires_at=?, "
                "status=CASE WHEN used_count>=max_uses THEN 'disabled' ELSE 'active' END, "
                "updated_at=? WHERE tenant_id=? AND id=?",
                (
                    expiry,
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(voucher_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")
        return self.gift_voucher_admin(actor_id, voucher_id=int(voucher_id))

    def deactivate_unusable_gift_vouchers_admin(self, actor_id: int) -> int:
        self._admin(actor_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers SET status='disabled', updated_at=? "
                "WHERE tenant_id=? AND status='active' "
                "AND (used_count>=max_uses OR "
                "(expires_at IS NOT NULL AND expires_at<=?))",
                (now, self.tenant_id, now),
            )
        return max(0, int(changed.rowcount or 0))

    def create_gift_vouchers_bulk_admin(
        self,
        actor_id: int,
        *,
        prefix: str,
        count: int,
        amount: int,
        currency: str = "IRR",
        expiry_hours: int = 0,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        clean_prefix = str(prefix or "").strip().upper()
        total = int(count)
        value = int(amount)
        hours = int(expiry_hours)
        clean_currency = _text(currency, 8).upper()
        if (
            not re.fullmatch(r"[A-Z0-9_-]{2,24}", clean_prefix)
            or not 1 <= total <= 200
            or value <= 0
            or hours < 0
        ):
            raise ValueError("invalid bulk gift voucher request")
        expiry = (
            None
            if hours == 0
            else iso_utc(utcnow() + timedelta(hours=hours))
        )
        now = iso_utc(utcnow())
        created_ids: list[int] = []
        with transaction(self.conn):
            for _ in range(total):
                for _attempt in range(30):
                    code = f"{clean_prefix}-{secrets.token_hex(4).upper()}"
                    try:
                        cursor = self.conn.execute(
                            "INSERT INTO tenant_gift_vouchers "
                            "(tenant_id, code, amount, currency, max_uses, used_count, "
                            "expires_at, status, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, 1, 0, ?, 'active', ?, ?)",
                            (
                                self.tenant_id,
                                code,
                                value,
                                clean_currency,
                                expiry,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    created_ids.append(int(cursor.lastrowid or 0))
                    break
                else:
                    raise TenantBusinessError(
                        "unique gift voucher code could not be generated"
                    )
        if not created_ids:
            return []
        placeholders = ",".join("?" for _ in created_ids)
        rows = self.conn.execute(
            f"SELECT * FROM tenant_gift_vouchers "
            f"WHERE tenant_id=? AND id IN ({placeholders}) ORDER BY id",
            (self.tenant_id, *created_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_gift_voucher_admin(
        self, actor_id: int, *, voucher_id: int
    ) -> None:
        self._admin(actor_id)
        used = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_gift_redemptions "
            "WHERE tenant_id=? AND voucher_id=?",
            (self.tenant_id, int(voucher_id)),
        ).fetchone()
        if int(used[0] or 0) > 0:
            raise TenantBusinessError(
                "used gift voucher cannot be deleted; disable it instead"
            )
        with transaction(self.conn):
            changed = self.conn.execute(
                "DELETE FROM tenant_gift_vouchers "
                "WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(voucher_id)),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher not found")

    def redeem_gift_voucher(
        self, actor_id: int, *, code: str
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        clean_code = str(code or "").strip().upper()
        if not clean_code:
            raise TenantBusinessError("gift voucher not found")
        now_dt = utcnow()
        now = iso_utc(now_dt)
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM tenant_gift_vouchers "
                "WHERE tenant_id=? AND code=? AND status='active'",
                (self.tenant_id, clean_code),
            ).fetchone()
            if row is None:
                raise TenantBusinessError("gift voucher not found")
            voucher = dict(row)
            if voucher.get("expires_at") and parse_utc(
                str(voucher["expires_at"])
            ) <= now_dt:
                raise TenantBusinessError("gift voucher expired")
            if int(voucher["used_count"] or 0) >= int(voucher["max_uses"]):
                raise TenantBusinessError("gift voucher is fully used")
            previous = self.conn.execute(
                "SELECT 1 FROM tenant_gift_redemptions "
                "WHERE tenant_id=? AND voucher_id=? AND customer_id=?",
                (
                    self.tenant_id,
                    int(voucher["id"]),
                    int(customer["id"]),
                ),
            ).fetchone()
            if previous is not None:
                raise TenantBusinessError("gift voucher already used")
            balance_row = self.conn.execute(
                "SELECT balance FROM tenant_wallet_accounts "
                "WHERE tenant_id=? AND customer_id=? AND currency=?",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    str(voucher["currency"]),
                ),
            ).fetchone()
            current = int(balance_row["balance"] or 0) if balance_row else 0
            resulting = current + int(voucher["amount"])
            self.conn.execute(
                "INSERT INTO tenant_wallet_accounts "
                "(tenant_id, customer_id, currency, balance, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, customer_id, currency) DO UPDATE SET "
                "balance=excluded.balance, updated_at=excluded.updated_at",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    str(voucher["currency"]),
                    resulting,
                    now,
                ),
            )
            cursor = self.conn.execute(
                "INSERT INTO tenant_gift_redemptions "
                "(tenant_id, voucher_id, customer_id, amount, currency, "
                "resulting_balance, redeemed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.tenant_id,
                    int(voucher["id"]),
                    int(customer["id"]),
                    int(voucher["amount"]),
                    str(voucher["currency"]),
                    resulting,
                    now,
                ),
            )
            changed = self.conn.execute(
                "UPDATE tenant_gift_vouchers "
                "SET used_count=used_count+1, "
                "status=CASE WHEN used_count+1>=max_uses THEN 'disabled' ELSE status END, "
                "updated_at=? "
                "WHERE tenant_id=? AND id=? AND status='active' "
                "AND used_count<max_uses",
                (now, self.tenant_id, int(voucher["id"])),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("gift voucher state changed")
        return {
            "id": int(cursor.lastrowid or 0),
            "code": clean_code,
            "amount": int(voucher["amount"]),
            "currency": str(voucher["currency"]),
            "resulting_balance": resulting,
        }

    def add_coupon(
        self,
        actor_id: int,
        *,
        code: str,
        discount_kind: str,
        value: int,
        currency: str = "",
        min_amount: int = 0,
        max_discount: int = 0,
        max_uses: int = 0,
        per_customer_limit: int = 1,
        expires_at: str = "",
    ) -> dict[str, Any]:
        self._admin(actor_id)
        coupon_code = str(code or "").strip().upper()
        kind = str(discount_kind or "").strip().lower()
        if (
            not coupon_code
            or len(coupon_code) > 40
            or kind not in ("percent", "fixed")
            or int(value) <= 0
            or (kind == "percent" and int(value) > 100)
            or min(int(min_amount), int(max_discount), int(max_uses), int(per_customer_limit)) < 0
        ):
            raise ValueError("invalid coupon")
        currency_code = str(currency or "").strip().upper()
        if kind == "fixed" and not currency_code:
            raise ValueError("fixed coupon requires currency")
        expiry = _text(expires_at, 80, required=False) or None
        if expiry:
            parse_utc(expiry)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_coupons "
                "(tenant_id, code, discount_kind, value, currency, min_amount, "
                "max_discount, max_uses, used_count, per_customer_limit, starts_at, "
                "expires_at, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, 'active', ?, ?)",
                (
                    self.tenant_id,
                    coupon_code,
                    kind,
                    int(value),
                    currency_code or None,
                    int(min_amount),
                    int(max_discount),
                    int(max_uses),
                    int(per_customer_limit),
                    expiry,
                    now,
                    now,
                ),
            )
        return self.coupon(int(cursor.lastrowid or 0))

    def coupon(self, coupon_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tenant_coupons WHERE id=? AND tenant_id=?",
            (int(coupon_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("coupon not found")
        return dict(row)

    def list_coupons_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_coupons "
                "WHERE tenant_id=? ORDER BY id DESC",
                (self.tenant_id,),
            ).fetchall()
        ]

    def set_coupon_status_admin(
        self,
        actor_id: int,
        *,
        coupon_id: int,
        enabled: bool,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_coupons SET status=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    "active" if enabled else "disabled",
                    now,
                    int(coupon_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("coupon not found")
        return self.coupon(int(coupon_id))

    def apply_coupon(
        self,
        actor_id: int,
        *,
        order_id: int,
        code: str,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        coupon_code = str(code or "").strip().upper()
        if not coupon_code:
            raise TenantBusinessError("coupon not found")
        now_dt = utcnow()
        now = iso_utc(now_dt)
        with transaction(self.conn):
            order_row = self.conn.execute(
                "SELECT * FROM tenant_orders "
                "WHERE id=? AND tenant_id=? AND customer_id=?",
                (int(order_id), self.tenant_id, int(customer["id"])),
            ).fetchone()
            if order_row is None:
                raise TenantBusinessError("order not found")
            order = dict(order_row)
            if order["status"] != "pending_payment":
                raise TenantBusinessError("order is not awaiting payment")
            if order.get("coupon_id") is not None:
                raise TenantBusinessError("coupon already applied")

            coupon_row = self.conn.execute(
                "SELECT * FROM tenant_coupons "
                "WHERE tenant_id=? AND code=? AND status='active'",
                (self.tenant_id, coupon_code),
            ).fetchone()
            if coupon_row is None:
                raise TenantBusinessError("coupon not found")
            coupon = dict(coupon_row)
            if coupon.get("starts_at") and parse_utc(
                str(coupon["starts_at"])
            ) > now_dt:
                raise TenantBusinessError("coupon is not active yet")
            if coupon.get("expires_at") and parse_utc(
                str(coupon["expires_at"])
            ) <= now_dt:
                raise TenantBusinessError("coupon expired")
            if (
                int(coupon["max_uses"] or 0) > 0
                and int(coupon["used_count"] or 0)
                >= int(coupon["max_uses"])
            ):
                raise TenantBusinessError("coupon usage limit reached")
            per_limit = int(coupon["per_customer_limit"] or 0)
            if per_limit > 0:
                used = self.conn.execute(
                    "SELECT COUNT(*) FROM tenant_coupon_redemptions "
                    "WHERE tenant_id=? AND coupon_id=? AND customer_id=?",
                    (
                        self.tenant_id,
                        int(coupon["id"]),
                        int(customer["id"]),
                    ),
                ).fetchone()
                if int(used[0] or 0) >= per_limit:
                    raise TenantBusinessError("coupon customer limit reached")

            original = int(order.get("original_amount") or order["amount"])
            if original < int(coupon["min_amount"] or 0):
                raise TenantBusinessError("order is below coupon minimum")
            if coupon["discount_kind"] == "fixed":
                if str(coupon.get("currency") or "") != str(order["currency"]):
                    raise TenantBusinessError("coupon currency mismatch")
                discount = int(coupon["value"])
            else:
                discount = (original * int(coupon["value"])) // 100
            max_discount = int(coupon["max_discount"] or 0)
            if max_discount > 0:
                discount = min(discount, max_discount)
            discount = min(original, max(0, discount))
            if discount <= 0:
                raise TenantBusinessError("coupon has no discount")
            final_amount = original - discount

            changed = self.conn.execute(
                "UPDATE tenant_orders SET amount=?, discount_amount=?, coupon_id=?, "
                "updated_at=? WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND status='pending_payment' AND coupon_id IS NULL",
                (
                    final_amount,
                    discount,
                    int(coupon["id"]),
                    now,
                    int(order_id),
                    self.tenant_id,
                    int(customer["id"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
            self.conn.execute(
                "INSERT INTO tenant_coupon_redemptions "
                "(tenant_id, coupon_id, customer_id, order_id, discount_amount, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.tenant_id,
                    int(coupon["id"]),
                    int(customer["id"]),
                    int(order_id),
                    discount,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE tenant_coupons SET used_count=used_count+1, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (now, int(coupon["id"]), self.tenant_id),
            )
        return self.order(actor_id, int(order_id))

    def _grant_referral_reward_tx(
        self,
        *,
        invitee_customer_id: int,
        reward_type: str,
        order_id: int | None = None,
    ) -> dict[str, Any] | None:
        settings = self._ensure_growth_settings()
        if not bool(settings["referral_enabled"]):
            return None
        if reward_type not in ("trial", "purchase"):
            raise ValueError("invalid referral reward")
        if (
            reward_type == "trial"
            and not bool(settings["referral_trial_reward_enabled"])
        ):
            return None
        if (
            reward_type == "purchase"
            and not bool(settings["referral_purchase_reward_enabled"])
        ):
            return None
        amount = int(
            settings["referral_trial_reward"]
            if reward_type == "trial"
            else settings["referral_purchase_reward"]
        )
        if amount <= 0:
            return None
        referral = self.conn.execute(
            "SELECT * FROM tenant_referrals "
            "WHERE tenant_id=? AND invitee_customer_id=? "
            "AND status='active' AND qualified=1 AND fraud_flag=0",
            (self.tenant_id, int(invitee_customer_id)),
        ).fetchone()
        if referral is None:
            return None
        ref = dict(referral)
        existing = self.conn.execute(
            "SELECT * FROM tenant_referral_rewards "
            "WHERE tenant_id=? AND referral_id=? AND reward_type=?",
            (self.tenant_id, int(ref["id"]), reward_type),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        if reward_type == "trial":
            paid = self.conn.execute(
                "SELECT 1 FROM tenant_orders "
                "WHERE tenant_id=? AND customer_id=? "
                "AND order_kind IN ('purchase','renewal') "
                "AND status IN ('paid','fulfilled') LIMIT 1",
                (self.tenant_id, int(invitee_customer_id)),
            ).fetchone()
            if paid is not None:
                return None
        else:
            if order_id is None:
                return None
            order = self.conn.execute(
                "SELECT * FROM tenant_orders "
                "WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND order_kind='purchase' AND status IN ('paid','fulfilled')",
                (int(order_id), self.tenant_id, int(invitee_customer_id)),
            ).fetchone()
            if order is None or int(order["amount"] or 0) < int(settings["referral_min_purchase"] or 0):
                return None
            first = self.conn.execute(
                "SELECT id FROM tenant_orders "
                "WHERE tenant_id=? AND customer_id=? AND order_kind='purchase' "
                "AND status IN ('paid','fulfilled') ORDER BY paid_at, id LIMIT 1",
                (self.tenant_id, int(invitee_customer_id)),
            ).fetchone()
            if first is None or int(first["id"]) != int(order_id):
                return None
        max_rewards = int(settings["referral_max_rewards"] or 0)
        if max_rewards > 0:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM tenant_referral_rewards "
                "WHERE tenant_id=? AND inviter_customer_id=? "
                "AND reward_type=? AND status='paid'",
                (
                    self.tenant_id,
                    int(ref["inviter_customer_id"]),
                    reward_type,
                ),
            ).fetchone()
            if int(count[0] or 0) >= max_rewards:
                return None
        currency = str(settings["referral_currency"])
        now = iso_utc(utcnow())
        self._wallet_change_tx(
            customer_id=int(ref["inviter_customer_id"]),
            currency=currency,
            amount=amount,
            kind=(
                "referral_trial"
                if reward_type == "trial"
                else "referral_purchase"
            ),
            order_id=int(order_id) if order_id is not None else None,
            idempotency_key=(
                f"tenant:{self.tenant_id}:referral:{int(ref['id'])}:"
                f"{reward_type}"
            ),
            note=f"referral {reward_type} reward",
        )
        cursor = self.conn.execute(
            "INSERT INTO tenant_referral_rewards "
            "(tenant_id, referral_id, inviter_customer_id, invitee_customer_id, "
            "reward_type, amount, currency, order_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'paid', ?)",
            (
                self.tenant_id,
                int(ref["id"]),
                int(ref["inviter_customer_id"]),
                int(invitee_customer_id),
                reward_type,
                amount,
                currency,
                int(order_id) if order_id is not None else None,
                now,
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM tenant_referral_rewards WHERE id=?",
            (int(cursor.lastrowid or 0),),
        ).fetchone()
        return dict(row) if row is not None else None

    def _create_paid_subscription_tx(
        self,
        *,
        order: dict[str, Any],
        now: str,
    ) -> int | None:
        if str(order.get("order_kind") or "purchase") == "renewal":
            linked = self.conn.execute(
                "SELECT subscription_id FROM tenant_renewal_orders "
                "WHERE order_id=? AND tenant_id=?",
                (int(order["id"]), self.tenant_id),
            ).fetchone()
            if linked is None:
                raise TenantBusinessError("renewal subscription is unavailable")
            return int(linked["subscription_id"])
        existing = self.conn.execute(
            "SELECT id FROM tenant_subscriptions "
            "WHERE tenant_id=? AND order_id=?",
            (self.tenant_id, int(order["id"])),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        plan = self.plan(int(order["plan_id"]), public=False)
        expires = iso_utc(utcnow() + timedelta(days=int(plan["duration_days"])))
        cursor = self.conn.execute(
            "INSERT INTO tenant_subscriptions "
            "(tenant_id, customer_id, plan_id, order_id, server_id, status, "
            "usage_bytes, traffic_bytes, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 'pending_provisioning', 0, ?, ?, ?, ?)",
            (
                self.tenant_id,
                int(order["customer_id"]),
                int(order["plan_id"]),
                int(order["id"]),
                int(plan["traffic_gb"]) * 1024 * 1024 * 1024,
                expires,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    def pay_order_with_wallet(
        self,
        actor_id: int,
        *,
        order_id: int,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        order = self.order(actor_id, int(order_id))
        if order["status"] != "pending_payment":
            raise TenantBusinessError("order is not awaiting payment")
        due = int(order["amount"] or 0)
        now = iso_utc(utcnow())
        subscription_id: int | None = None
        with transaction(self.conn):
            if due > 0:
                self._wallet_change_tx(
                    customer_id=int(customer["id"]),
                    currency=str(order["currency"]),
                    amount=-due,
                    kind="purchase",
                    order_id=int(order_id),
                    idempotency_key=(
                        f"tenant:{self.tenant_id}:wallet-order:{int(order_id)}"
                    ),
                    note=f"wallet payment for order #{int(order_id)}",
                )
            changed = self.conn.execute(
                "UPDATE tenant_orders SET status='paid', wallet_amount=?, "
                "paid_at=COALESCE(paid_at, ?), updated_at=? "
                "WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND status='pending_payment'",
                (
                    due,
                    now,
                    now,
                    int(order_id),
                    self.tenant_id,
                    int(customer["id"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
            paid_order = dict(
                self.conn.execute(
                    "SELECT * FROM tenant_orders WHERE id=? AND tenant_id=?",
                    (int(order_id), self.tenant_id),
                ).fetchone()
            )
            subscription_id = self._create_paid_subscription_tx(
                order=paid_order,
                now=now,
            )
            if str(paid_order.get("order_kind") or "") == "purchase":
                self._grant_referral_reward_tx(
                    invitee_customer_id=int(customer["id"]),
                    reward_type="purchase",
                    order_id=int(order_id),
                )
            self._record_payment_event_tx(
                source="wallet_order",
                payment_ref_id=int(order_id),
                provider_key="wallet",
                event_type="approved",
                actor_id=actor_id,
                idempotency_key=(
                    f"tenant:{self.tenant_id}:payment:wallet_order:"
                    f"{int(order_id)}:approved"
                ),
            )
        result: dict[str, Any] = {
            "order_id": int(order_id),
            "status": "paid",
            "subscription_id": subscription_id,
        }
        try:
            fulfilled = self.fulfill_paid_order(
                self.owner_telegram_id, order_id=int(order_id)
            )
            result.update(fulfilled)
        except TenantBusinessError:
            result["fulfillment_pending"] = True
        return result

    def retry_own_paid_order(
        self,
        actor_id: int,
        *,
        order_id: int,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT 1 FROM tenant_orders "
            "WHERE id=? AND tenant_id=? AND customer_id=? AND status='paid'",
            (int(order_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order is not awaiting fulfillment")
        return self.fulfill_paid_order(
            self.owner_telegram_id, order_id=int(order_id)
        )

    def _trial_plan(self, settings: dict[str, Any]) -> dict[str, Any]:
        name = "__WHITELABEL_FREE_TRIAL__"
        row = self.conn.execute(
            "SELECT * FROM tenant_sale_plans WHERE tenant_id=? AND name=?",
            (self.tenant_id, name),
        ).fetchone()
        now = iso_utc(utcnow())
        if row is None:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO tenant_sale_plans "
                    "(tenant_id, name, traffic_gb, duration_days, price, currency, "
                    "status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, 'archived', ?, ?)",
                    (
                        self.tenant_id,
                        name,
                        int(settings["trial_traffic_gb"]),
                        int(settings["trial_duration_days"]),
                        str(settings["referral_currency"]),
                        now,
                        now,
                    ),
                )
            row = self.conn.execute(
                "SELECT * FROM tenant_sale_plans WHERE id=?",
                (int(cursor.lastrowid or 0),),
            ).fetchone()
        elif (
            int(row["traffic_gb"]) != int(settings["trial_traffic_gb"])
            or int(row["duration_days"]) != int(settings["trial_duration_days"])
        ):
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_sale_plans SET traffic_gb=?, duration_days=?, "
                    "currency=?, updated_at=? WHERE id=? AND tenant_id=?",
                    (
                        int(settings["trial_traffic_gb"]),
                        int(settings["trial_duration_days"]),
                        str(settings["referral_currency"]),
                        now,
                        int(row["id"]),
                        self.tenant_id,
                    ),
                )
            row = self.conn.execute(
                "SELECT * FROM tenant_sale_plans WHERE id=?",
                (int(row["id"]),),
            ).fetchone()
        assert row is not None
        return dict(row)

    def free_trial_state(self, actor_id: int) -> dict[str, Any]:
        """Return SellBot-style free-trial eligibility without creating anything."""
        customer = self._customer(actor_id, active=False)
        settings = self._ensure_growth_settings()
        existing = self.conn.execute(
            "SELECT * FROM tenant_trial_claims "
            "WHERE tenant_id=? AND customer_id=?",
            (self.tenant_id, int(customer["id"])),
        ).fetchone()
        existing_dict = dict(existing) if existing is not None else None
        used = bool(customer.get("trial_used_at")) or bool(
            existing_dict and str(existing_dict.get("status") or "") == "issued"
        )
        retryable = bool(
            existing_dict
            and str(existing_dict.get("status") or "") in ("pending", "failed")
            and not used
        )
        return {
            "enabled": bool(settings.get("trial_enabled")),
            "used": used,
            "retryable": retryable,
            "customer_id": int(customer["id"]),
            "claim": existing_dict,
            "traffic_gb": int(settings.get("trial_traffic_gb") or 1),
            "duration_days": int(settings.get("trial_duration_days") or 1),
            "announce_enabled": bool(
                settings.get("trial_announce_enabled", True)
            ),
        }

    def claim_free_trial(
        self,
        actor_id: int,
        *,
        server_id: int | None = None,
        service_name: str = "",
    ) -> dict[str, Any]:
        """Issue one free trial using the server/name selected in UserBot.

        SellBot lets the customer choose a location and service name before
        provisioning.  Keeping that selection on the trial order also avoids
        the old WhiteLabel failure where a multi-server tenant required an
        unrelated default server.
        """
        customer = self._customer(actor_id)
        settings = self._ensure_growth_settings()
        if not bool(settings["trial_enabled"]):
            raise TenantBusinessError("free trial is disabled")

        clean_name = str(service_name or "").strip()
        if clean_name:
            if not 1 <= len(clean_name) <= 64 or any(
                unicodedata.category(char).startswith("C")
                for char in clean_name
            ):
                raise ValueError("invalid free trial service name")

        selected_server_id: int | None = None
        if server_id is not None:
            selected = self._purchase_server(int(server_id))
            selected_server_id = int(selected["id"])

        existing = self.conn.execute(
            "SELECT * FROM tenant_trial_claims "
            "WHERE tenant_id=? AND customer_id=?",
            (self.tenant_id, int(customer["id"])),
        ).fetchone()
        existing_dict = dict(existing) if existing is not None else None

        if bool(customer.get("trial_used_at")):
            raise TenantBusinessError("free trial already used")

        if existing_dict is not None:
            status = str(existing_dict.get("status") or "")
            if status not in ("pending", "failed"):
                raise TenantBusinessError("free trial already used")
            order_id = int(existing_dict["order_id"])
            subscription_id = int(existing_dict.get("subscription_id") or 0)
            now_retry = iso_utc(utcnow())
            with transaction(self.conn):
                if selected_server_id is not None:
                    self.conn.execute(
                        "UPDATE tenant_orders SET selected_server_id=?, updated_at=? "
                        "WHERE id=? AND tenant_id=? AND status='paid'",
                        (
                            selected_server_id,
                            now_retry,
                            order_id,
                            self.tenant_id,
                        ),
                    )
                if clean_name and subscription_id > 0:
                    self.conn.execute(
                        "UPDATE tenant_subscriptions SET service_name=?, updated_at=? "
                        "WHERE id=? AND tenant_id=? "
                        "AND status='pending_provisioning'",
                        (
                            clean_name,
                            now_retry,
                            subscription_id,
                            self.tenant_id,
                        ),
                    )
            try:
                result = self.fulfill_paid_order(
                    self.owner_telegram_id,
                    order_id=order_id,
                )
            except TenantBusinessError as exc:
                with transaction(self.conn):
                    self.conn.execute(
                        "UPDATE tenant_trial_claims "
                        "SET status='failed', updated_at=? "
                        "WHERE id=? AND tenant_id=?",
                        (iso_utc(utcnow()), int(existing_dict["id"]), self.tenant_id),
                    )
                raise TenantBusinessError(
                    "free trial provisioning is still pending"
                ) from exc
            with transaction(self.conn):
                now_done = iso_utc(utcnow())
                self.conn.execute(
                    "UPDATE tenant_trial_claims "
                    "SET status='issued', updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (now_done, int(existing_dict["id"]), self.tenant_id),
                )
                self.conn.execute(
                    "UPDATE tenant_customers SET trial_used_at=?, updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (
                        now_done,
                        now_done,
                        int(customer["id"]),
                        self.tenant_id,
                    ),
                )
                self._grant_referral_reward_tx(
                    invitee_customer_id=int(customer["id"]),
                    reward_type="trial",
                )
            result.update(
                {
                    "order_kind": "trial",
                    "order_id": order_id,
                    "service_name": clean_name,
                    "selected_server_id": selected_server_id,
                }
            )
            return result

        plan = self._trial_plan(settings)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_orders "
                "(tenant_id, customer_id, plan_id, selected_server_id, "
                "amount, currency, status, created_at, updated_at, paid_at, "
                "order_kind, original_amount, discount_amount, wallet_amount) "
                "VALUES (?, ?, ?, ?, 0, ?, 'paid', ?, ?, ?, "
                "'trial', 0, 0, 0)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    int(plan["id"]),
                    selected_server_id,
                    str(plan["currency"]),
                    now,
                    now,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid or 0)
            paid_order = dict(
                self.conn.execute(
                    "SELECT * FROM tenant_orders WHERE id=? AND tenant_id=?",
                    (order_id, self.tenant_id),
                ).fetchone()
            )
            subscription_id = self._create_paid_subscription_tx(
                order=paid_order, now=now
            )
            if clean_name and subscription_id:
                self.conn.execute(
                    "UPDATE tenant_subscriptions SET service_name=?, updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (
                        clean_name,
                        now,
                        int(subscription_id),
                        self.tenant_id,
                    ),
                )
            self.conn.execute(
                "INSERT INTO tenant_trial_claims "
                "(tenant_id, customer_id, order_id, subscription_id, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    order_id,
                    int(subscription_id or 0),
                    now,
                    now,
                ),
            )
        try:
            result = self.fulfill_paid_order(
                self.owner_telegram_id, order_id=order_id
            )
        except TenantBusinessError:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_trial_claims SET status='failed', updated_at=? "
                    "WHERE tenant_id=? AND customer_id=? AND order_id=?",
                    (
                        iso_utc(utcnow()),
                        self.tenant_id,
                        int(customer["id"]),
                        order_id,
                    ),
                )
            raise
        with transaction(self.conn):
            now_done = iso_utc(utcnow())
            self.conn.execute(
                "UPDATE tenant_trial_claims SET status='issued', updated_at=? "
                "WHERE tenant_id=? AND customer_id=? AND order_id=?",
                (
                    now_done,
                    self.tenant_id,
                    int(customer["id"]),
                    order_id,
                ),
            )
            self.conn.execute(
                "UPDATE tenant_customers SET trial_used_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    now_done,
                    now_done,
                    int(customer["id"]),
                    self.tenant_id,
                ),
            )
            self._grant_referral_reward_tx(
                invitee_customer_id=int(customer["id"]),
                reward_type="trial",
            )
        result.update(
            {
                "order_kind": "trial",
                "order_id": order_id,
                "service_name": clean_name,
                "selected_server_id": selected_server_id,
            }
        )
        return result

    def list_plan_categories(
        self, *, public: bool = True
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM tenant_sale_plans p "
            " WHERE p.tenant_id=c.tenant_id AND p.category_id=c.id "
            " AND p.status!='archived') AS plan_count "
            "FROM tenant_plan_categories c WHERE c.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if public:
            query += " AND c.status='active'"
        query += " ORDER BY c.priority ASC, c.id ASC"
        return [
            dict(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]

    def plan_category(
        self, category_id: int, *, public: bool = True
    ) -> dict[str, Any]:
        query = (
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM tenant_sale_plans p "
            " WHERE p.tenant_id=c.tenant_id AND p.category_id=c.id "
            " AND p.status!='archived') AS plan_count "
            "FROM tenant_plan_categories c "
            "WHERE c.id=? AND c.tenant_id=?"
        )
        args: list[Any] = [int(category_id), self.tenant_id]
        if public:
            query += " AND status='active'"
        row = self.conn.execute(query, tuple(args)).fetchone()
        if row is None:
            raise TenantBusinessError("plan category not found")
        return dict(row)

    def add_plan_category_admin(
        self,
        actor_id: int,
        *,
        title: str,
        priority: int = 0,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        now = iso_utc(utcnow())
        clean_priority = int(priority)
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_plan_categories "
                "(tenant_id,title,priority,status,created_at,updated_at) "
                "VALUES (?,?,?,'active',?,?)",
                (
                    self.tenant_id,
                    _text(title, 80),
                    clean_priority,
                    now,
                    now,
                ),
            )
        return self.plan_category(int(cursor.lastrowid or 0), public=False)

    def update_plan_category_admin(
        self,
        actor_id: int,
        *,
        category_id: int,
        title: str | None = None,
        priority: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        current = self.plan_category(int(category_id), public=False)
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = _text(title, 80)
        if priority is not None:
            updates["priority"] = int(priority)
        if status is not None:
            clean_status = str(status).strip().lower()
            if clean_status not in ("active", "disabled"):
                raise ValueError("invalid plan category status")
            updates["status"] = clean_status
        if not updates:
            return current
        updates["updated_at"] = iso_utc(utcnow())
        assignments = ", ".join(f"{key}=?" for key in updates)
        with transaction(self.conn):
            changed = self.conn.execute(
                f"UPDATE tenant_plan_categories SET {assignments} "
                "WHERE id=? AND tenant_id=?",
                (*updates.values(), int(category_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("plan category not found")
        return self.plan_category(int(category_id), public=False)

    def assign_plan_category_admin(
        self,
        actor_id: int,
        *,
        plan_id: int,
        category_id: int | None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        self.plan(int(plan_id), public=False)
        clean_category: int | None = None
        if category_id is not None:
            clean_category = int(category_id)
            self.plan_category(clean_category, public=False)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_sale_plans SET category_id=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (clean_category, now, int(plan_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("plan not found")
        return self.plan(int(plan_id), public=False)

    def set_plan_priority_admin(
        self, actor_id: int, *, plan_id: int, priority: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        self.plan(int(plan_id), public=False)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_sale_plans SET priority=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    int(priority),
                    iso_utc(utcnow()),
                    int(plan_id),
                    self.tenant_id,
                ),
            )
        return self.plan(int(plan_id), public=False)

    def add_plan(
        self,
        actor_id: int,
        *,
        name: str,
        traffic_gb: int,
        duration_days: int,
        price: int,
        currency: str = "IRR",
        category_id: int | None = None,
        priority: int = 0,
        server_id: int | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        if min(int(traffic_gb), int(duration_days)) <= 0 or int(price) < 0:
            raise ValueError("invalid plan values")
        if server_id is not None:
            self.server(int(server_id))
        clean_category: int | None = None
        if category_id is not None:
            clean_category = int(category_id)
            self.plan_category(clean_category, public=False)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_sale_plans "
                "(tenant_id,name,traffic_gb,duration_days,price,currency,status,"
                "category_id,priority,created_at,updated_at,server_id) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
                (
                    self.tenant_id,
                    _text(name, 80),
                    int(traffic_gb),
                    int(duration_days),
                    int(price),
                    _text(currency, 8).upper(),
                    clean_category,
                    int(priority),
                    now,
                    now,
                    int(server_id) if server_id is not None else None,
                ),
            )
        return self.plan(int(cursor.lastrowid or 0), public=False)

    def plan(self, plan_id: int, *, public: bool = True) -> dict[str, Any]:
        query = (
            "SELECT p.*, c.title AS category_title, "
            "c.priority AS category_priority, c.status AS category_status "
            "FROM tenant_sale_plans p "
            "LEFT JOIN tenant_plan_categories c "
            "ON c.id=p.category_id AND c.tenant_id=p.tenant_id "
            "WHERE p.id=? AND p.tenant_id=?"
        )
        args: list[Any] = [int(plan_id), self.tenant_id]
        if public:
            query += (
                " AND p.status='active' "
                "AND (p.category_id IS NULL OR c.status='active')"
            )
        row = self.conn.execute(query, tuple(args)).fetchone()
        if row is None:
            raise TenantBusinessError("plan not found")
        return dict(row)

    def list_plans(
        self,
        *,
        public: bool = True,
        category_id: int | None = None,
        server_id: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT p.*, c.title AS category_title, "
            "c.priority AS category_priority, c.status AS category_status "
            "FROM tenant_sale_plans p "
            "LEFT JOIN tenant_plan_categories c "
            "ON c.id=p.category_id AND c.tenant_id=p.tenant_id "
            "WHERE p.tenant_id=? AND p.is_dynamic=0"
        )
        args: list[Any] = [self.tenant_id]
        if public:
            query += (
                " AND p.status='active' "
                "AND (p.category_id IS NULL OR c.status='active')"
            )
        if public:
            query += " AND (p.server_id IS NULL OR NOT EXISTS (SELECT 1 FROM tenant_server_sales_settings ss WHERE ss.tenant_id=p.tenant_id AND ss.server_id=p.server_id AND json_extract(ss.settings_json,'$.mode')='dynamic'))"
        if server_id is not None:
            self.server(int(server_id))
            if public:
                settings=self.conn.execute("SELECT settings_json FROM tenant_server_sales_settings WHERE tenant_id=? AND server_id=?",(self.tenant_id,int(server_id))).fetchone()
                if settings and json.loads(settings["settings_json"]).get("mode")=="dynamic": return []
            query += " AND (p.server_id IS NULL OR p.server_id=?)"
            args.append(int(server_id))
        if category_id is not None:
            query += " AND p.category_id=?"
            args.append(int(category_id))
        query += " ORDER BY p.priority ASC, p.price ASC, p.id ASC"
        return [
            dict(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]

    @staticmethod
    def _payment_method_view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return payment_method_view(dict(row))

    def available_payment_providers_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        return [
            {
                "key": spec.key,
                "title": spec.title,
                "icon": spec.icon,
                "legacy_kind": spec.legacy_kind,
                "requires_network": spec.requires_network,
                "requires_receipt": spec.requires_receipt,
                "manual_review": spec.manual_review,
            }
            for spec in registered_providers(user_selectable=True)
        ]

    def add_payment_method(
        self,
        actor_id: int,
        *,
        kind: str = "",
        provider_key: str = "",
        title: str,
        currency: str,
        destination: str,
        network: str = "",
        instructions: str = "",
        priority: int = 100,
        provider_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        clean_kind = str(kind or "").strip().lower()
        clean_provider = str(provider_key or "").strip().lower()
        if not clean_provider:
            if clean_kind not in ("card", "crypto"):
                raise ValueError("invalid payment kind")
            clean_provider = "card_manual" if clean_kind == "card" else "crypto_manual"
        spec = provider_for_key(clean_provider, legacy_kind=clean_kind)
        if clean_kind and clean_kind != spec.legacy_kind:
            raise ValueError("payment provider family mismatch")
        if int(priority) < 0:
            raise ValueError("invalid payment priority")
        clean_network = _text(network, 40, required=False)
        options = provider_options or {}
        if not isinstance(options, dict):
            raise ValueError("invalid provider options")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_payment_methods "
                "(tenant_id, kind, provider_key, title, currency, destination, "
                "network, instructions, priority, provider_options_json, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    self.tenant_id,
                    spec.legacy_kind,
                    spec.key,
                    _text(title, 80),
                    _text(currency, 8).upper(),
                    _text(destination, 180),
                    clean_network or None,
                    _text(instructions, 500, required=False),
                    int(priority),
                    json.dumps(options, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return self.method(int(cursor.lastrowid or 0), currency=None)

    def method(self, method_id: int, *, currency: str | None) -> dict[str, Any]:
        query = (
            "SELECT * FROM tenant_payment_methods "
            "WHERE id = ? AND tenant_id = ? AND status = 'active'"
        )
        args: list[Any] = [int(method_id), self.tenant_id]
        if currency is not None:
            query += " AND currency = ?"
            args.append(str(currency).upper())
        row = self.conn.execute(query, tuple(args)).fetchone()
        if row is None:
            raise TenantBusinessError("payment method not found")
        return self._payment_method_view(row)

    def payment_method_admin(self, actor_id: int, *, method_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_payment_methods WHERE id=? AND tenant_id=?",
            (int(method_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("payment method not found")
        return self._payment_method_view(row)

    def list_methods(self, *, currency: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM tenant_payment_methods "
            "WHERE tenant_id = ? AND status = 'active'"
        )
        args: list[Any] = [self.tenant_id]
        if currency:
            query += " AND currency = ?"
            args.append(str(currency).upper())
        rows = self.conn.execute(
            query + " ORDER BY priority ASC, id ASC", tuple(args)
        ).fetchall()
        return [self._payment_method_view(row) for row in rows]

    def payment_method_prompt(
        self, method: dict[str, Any], *, amount: int | None = None
    ) -> str:
        return payment_prompt(method, amount=amount)

    def begin_order_payment(
        self,
        actor_id: int,
        *,
        order_id: int,
        method_id: int,
    ) -> dict[str, Any]:
        order = self.order(actor_id, int(order_id))
        if order["status"] != "pending_payment":
            raise TenantBusinessError("order is not awaiting payment")
        method = self.method(int(method_id), currency=str(order["currency"]))
        result = begin_provider_payment(
            method,
            context={
                "tenant_id": self.tenant_id,
                "actor_id": int(actor_id),
                "source": "order",
                "subject_id": int(order_id),
                "amount": int(order["amount"]),
                "currency": str(order["currency"]),
            },
        )
        return {
            "action": result.action,
            "message": result.message,
            "checkout_url": result.checkout_url,
            "external_reference": result.external_reference,
            "method": method,
            "order": order,
        }

    def begin_wallet_topup_payment(
        self,
        actor_id: int,
        *,
        topup_id: int,
        method_id: int,
    ) -> dict[str, Any]:
        topup = self.wallet_topup(actor_id, int(topup_id))
        if topup["status"] != "pending_payment":
            raise TenantBusinessError("wallet topup is not awaiting payment")
        method = self.method(int(method_id), currency=str(topup["currency"]))
        result = begin_provider_payment(
            method,
            context={
                "tenant_id": self.tenant_id,
                "actor_id": int(actor_id),
                "source": "wallet_topup",
                "subject_id": int(topup_id),
                "amount": int(topup["amount"]),
                "currency": str(topup["currency"]),
            },
        )
        return {
            "action": result.action,
            "message": result.message,
            "checkout_url": result.checkout_url,
            "external_reference": result.external_reference,
            "method": method,
            "topup": topup,
        }

    def update_payment_method_admin(
        self,
        actor_id: int,
        *,
        method_id: int,
        title: str | None = None,
        currency: str | None = None,
        destination: str | None = None,
        network: str | None = None,
        instructions: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        current = self.payment_method_admin(actor_id, method_id=int(method_id))
        updates: list[str] = []
        args: list[Any] = []
        if title is not None:
            updates.append("title=?")
            args.append(_text(title, 80))
        if currency is not None:
            updates.append("currency=?")
            args.append(_text(currency, 8).upper())
        if destination is not None:
            updates.append("destination=?")
            args.append(_text(destination, 180))
        if network is not None:
            clean_network = _text(network, 40, required=False)
            updates.append("network=?")
            args.append(clean_network or None)
        if instructions is not None:
            updates.append("instructions=?")
            args.append(_text(instructions, 500, required=False))
        if priority is not None:
            clean_priority = int(priority)
            if clean_priority < 0:
                raise ValueError("invalid payment priority")
            updates.append("priority=?")
            args.append(clean_priority)
        if not updates:
            return current
        updates.append("updated_at=?")
        args.append(iso_utc(utcnow()))
        args.extend([self.tenant_id, int(method_id)])
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_payment_methods SET " + ", ".join(updates)
                + " WHERE tenant_id=? AND id=?",
                tuple(args),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("payment method not found")
        return self.payment_method_admin(actor_id, method_id=int(method_id))

    def remove_payment_method_admin(
        self, actor_id: int, *, method_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        current = self.payment_method_admin(actor_id, method_id=int(method_id))
        refs = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM tenant_receipts "
            " WHERE tenant_id=? AND payment_method_id=?) + "
            "(SELECT COUNT(*) FROM tenant_wallet_topup_receipts "
            " WHERE tenant_id=? AND payment_method_id=?) AS total",
            (
                self.tenant_id,
                int(method_id),
                self.tenant_id,
                int(method_id),
            ),
        ).fetchone()
        total = int(refs["total"] or 0) if refs is not None else 0
        with transaction(self.conn):
            if total:
                self.conn.execute(
                    "UPDATE tenant_payment_methods SET status='disabled', "
                    "updated_at=? WHERE tenant_id=? AND id=?",
                    (iso_utc(utcnow()), self.tenant_id, int(method_id)),
                )
                result = self.payment_method_admin(
                    actor_id, method_id=int(method_id)
                )
                result["removed"] = False
                return result
            self.conn.execute(
                "DELETE FROM tenant_payment_methods WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(method_id)),
            )
        result = dict(current)
        result["removed"] = True
        return result

    def create_order(
        self,
        actor_id: int,
        plan_id: int,
        *,
        server_id: int | None = None,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        plan = self.plan(plan_id, public=True)
        if plan.get("server_id") is not None:
            if server_id is not None and int(server_id)!=int(plan["server_id"]):
                raise TenantBusinessError("plan does not belong to this server")
            server_id=int(plan["server_id"])
        selected_server_id: int | None = None
        if server_id is not None:
            selected = self._purchase_server(int(server_id))
            selected_server_id = int(selected["id"])
        if selected_server_id is not None and not plan.get("is_dynamic"):
            row=self.conn.execute("SELECT settings_json FROM tenant_server_sales_settings WHERE tenant_id=? AND server_id=?",(self.tenant_id,selected_server_id)).fetchone()
            if row and json.loads(row["settings_json"]).get("mode")=="dynamic":
                raise TenantBusinessError("server only accepts dynamic plans")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_orders "
                "(tenant_id, customer_id, plan_id, selected_server_id, "
                "amount, currency, status, created_at, updated_at, order_kind, "
                "original_amount, discount_amount, wallet_amount) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending_payment', ?, ?, "
                "'purchase', ?, 0, 0)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    int(plan["id"]),
                    selected_server_id,
                    int(plan["price"]),
                    str(plan["currency"]),
                    now,
                    now,
                    int(plan["price"]),
                ),
            )
        return self.order(actor_id, int(cursor.lastrowid or 0))

    def change_purchase_order_server(
        self,
        actor_id: int,
        *,
        order_id: int,
        server_id: int,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        selected = self._purchase_server(int(server_id))
        current = self.order(actor_id,int(order_id))
        plan = self.plan(int(current["plan_id"]),public=False)
        if plan.get("server_id") is not None and int(plan["server_id"])!=int(server_id):
            raise TenantBusinessError("plan does not belong to this server")
        sales = self.conn.execute("SELECT settings_json FROM tenant_server_sales_settings WHERE tenant_id=? AND server_id=?", (self.tenant_id, int(server_id))).fetchone()
        if sales and json.loads(sales["settings_json"]).get("mode")=="dynamic" and not plan.get("is_dynamic"):
            raise TenantBusinessError("fixed plans are disabled on this server")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_orders SET selected_server_id=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND order_kind='purchase' AND status='pending_payment'",
                (
                    int(selected["id"]),
                    now,
                    int(order_id),
                    self.tenant_id,
                    int(customer["id"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("purchase order server cannot be changed")
        return self.order(actor_id, int(order_id))

    @staticmethod
    def _renew_modes_from_policy(policy: str) -> tuple[str, str]:
        normalized = str(policy or "").strip().lower()
        if normalized == "fair":
            return "add", "add"
        if normalized == "default":
            return "add", "reset"
        return "reset", "reset"

    def set_renewal_policy_admin(
        self, actor_id: int, *, policy: str
    ) -> dict[str, Any]:
        self._admin(actor_id)
        normalized = str(policy or "").strip().lower()
        if normalized not in ("advanced", "default", "fair"):
            raise ValueError("invalid renewal policy")
        volume_mode, time_mode = self._renew_modes_from_policy(normalized)
        now = iso_utc(utcnow())
        values = {
            "renew_policy": normalized,
            "renew_volume_mode": volume_mode,
            "renew_time_mode": time_mode,
        }
        with transaction(self.conn):
            for key, value in values.items():
                self.conn.execute(
                    "INSERT INTO tenant_userbot_settings "
                    "(tenant_id,key,value,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(tenant_id,key) DO UPDATE SET "
                    "value=excluded.value,updated_at=excluded.updated_at",
                    (
                        self.tenant_id,
                        key,
                        json.dumps(value, ensure_ascii=False),
                        now,
                    ),
                )
        return self.runtime_userbot_settings()

    def set_renewal_rollover_admin(
        self,
        actor_id: int,
        *,
        kind: str,
        mode: str,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        clean_kind = str(kind or "").strip().lower()
        clean_mode = str(mode or "").strip().lower()
        if clean_kind not in ("volume", "time"):
            raise ValueError("invalid renewal rollover kind")
        if clean_mode not in ("add", "reset"):
            raise ValueError("invalid renewal rollover mode")
        return self.set_userbot_setting_admin(
            actor_id,
            key=(
                "renew_volume_mode"
                if clean_kind == "volume"
                else "renew_time_mode"
            ),
            value=clean_mode,
        )

    def renewal_eligibility(
        self,
        actor_id: int,
        *,
        subscription_id: int,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND customer_id=? "
            "AND status IN ('active','disabled','expired')",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription cannot be renewed")
        subscription = dict(row)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")

        # Match the proven SellBot behavior: make the renewal-window decision
        # from fresh panel usage when possible. Provider outages fall back to
        # the last safely persisted counters instead of opening the policy.
        if str(subscription.get("status") or "") in ("active", "disabled"):
            try:
                self.sync_subscription_usage(
                    self.owner_telegram_id,
                    subscription_id=int(subscription_id),
                )
            except (TenantBusinessError, PanelError):
                pass
            refreshed = self.conn.execute(
                "SELECT * FROM tenant_subscriptions "
                "WHERE id=? AND tenant_id=? AND customer_id=?",
                (int(subscription_id), self.tenant_id, int(customer["id"])),
            ).fetchone()
            if refreshed is not None:
                subscription = dict(refreshed)

        settings = self.runtime_userbot_settings()
        if not bool(settings.get("enable_renew", True)):
            return {
                "allowed": False,
                "reason": "disabled",
                "policy": str(settings.get("renew_policy") or "advanced"),
            }

        policy = str(settings.get("renew_policy") or "advanced").strip().lower()
        if policy not in ("advanced", "default", "fair"):
            policy = "advanced"

        now = utcnow()
        remaining_seconds: float | None = None
        try:
            expires_at = parse_utc(str(subscription["expires_at"]))
            remaining_seconds = (expires_at - now).total_seconds()
            days_left = int(remaining_seconds // 86400)
        except Exception:
            days_left = None

        traffic_bytes = max(0, int(subscription.get("traffic_bytes") or 0))
        usage_bytes = max(0, int(subscription.get("usage_bytes") or 0))
        remaining_bytes = max(0, traffic_bytes - usage_bytes)
        remaining_gb = remaining_bytes / (1024 ** 3)

        max_days = max(1, int(settings.get("renew_max_days") or 3))
        max_remaining_gb = max(
            1, int(settings.get("renew_max_remaining_gb") or 3)
        )
        days_ok = (
            remaining_seconds is not None
            and remaining_seconds < (max_days * 86400)
        )
        usage_ok = traffic_bytes > 0 and remaining_gb < max_remaining_gb
        # SellBot applies the same renewal window in every policy profile.
        # The policy only controls rollover semantics (add/reset), not whether
        # a subscription may bypass the remaining-time/volume thresholds.
        allowed = days_ok or usage_ok
        return {
            "allowed": bool(allowed),
            "reason": "" if allowed else "advanced_limits",
            "policy": policy,
            "days_left": days_left,
            "remaining_gb": remaining_gb,
            "max_days": max_days,
            "max_remaining_gb": max_remaining_gb,
            "volume_mode": str(
                settings.get("renew_volume_mode") or "reset"
            ),
            "time_mode": str(settings.get("renew_time_mode") or "reset"),
        }

    def renewal_not_allowed_text(self) -> str:
        settings = self.runtime_userbot_settings()
        max_days = max(1, int(settings.get("renew_max_days") or 3))
        max_remaining_gb = max(
            1, int(settings.get("renew_max_remaining_gb") or 3)
        )
        return (
            "🛑 در حال حاضر شما امکان تمدید اشتراک خود را ندارید.\n"
            f"1- کمتر از {max_days} روز تا اتمام اشتراک شما باقی مانده باشد.\n"
            f"2- حجم باقی مانده اشتراک شما کمتر از {max_remaining_gb} گیگابایت باشد."
        )

    def create_renewal_order(
        self, actor_id: int, *, subscription_id: int, plan_id: int
    ) -> dict[str, Any]:
        """Create a payable renewal with a snapshot of rollover behavior."""
        customer = self._customer(actor_id)
        self._require_completed_rotation(subscription_id)
        plan = self.plan(plan_id, public=True)
        try:
            subscription = self.customer_subscription_status(actor_id,subscription_id=subscription_id,refresh=False)
        except TenantBusinessError as exc:
            raise TenantBusinessError("subscription cannot be renewed") from exc
        if plan.get("server_id") is not None and int(plan["server_id"])!=int(subscription.get("server_id") or 0):
            raise TenantBusinessError("renewal plan does not belong to this server")
        eligibility = self.renewal_eligibility(
            actor_id, subscription_id=int(subscription_id)
        )
        if not bool(eligibility.get("allowed")):
            raise TenantBusinessError("renewal policy does not allow renewal")

        settings = self.runtime_userbot_settings()
        volume_mode = str(settings.get("renew_volume_mode") or "reset").lower()
        time_mode = str(settings.get("renew_time_mode") or "reset").lower()
        if volume_mode not in ("add", "reset"):
            volume_mode = "reset"
        if time_mode not in ("add", "reset"):
            time_mode = "reset"

        now = iso_utc(utcnow())
        with transaction(self.conn):
            subscription = self.conn.execute(
                "SELECT * FROM tenant_subscriptions "
                "WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND status IN ('active','disabled','expired')",
                (int(subscription_id), self.tenant_id, int(customer["id"])),
            ).fetchone()
            if subscription is None:
                raise TenantBusinessError("subscription cannot be renewed")
            if subscription["server_id"] is None or not subscription["external_ref"]:
                raise TenantBusinessError("subscription is not provisioned")
            pending = self.conn.execute(
                "SELECT 1 FROM tenant_renewal_orders ro "
                "JOIN tenant_orders o ON o.id=ro.order_id "
                "WHERE ro.tenant_id=? AND ro.subscription_id=? "
                "AND o.status IN ('pending_payment','payment_review','paid') LIMIT 1",
                (self.tenant_id, int(subscription_id)),
            ).fetchone()
            if pending is not None:
                raise TenantBusinessError("renewal already pending")
            cursor = self.conn.execute(
                "INSERT INTO tenant_orders "
                "(tenant_id, customer_id, plan_id, amount, currency, status, "
                "created_at, updated_at, order_kind, original_amount, "
                "discount_amount, wallet_amount) "
                "VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?, 'renewal', ?, 0, 0)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    int(plan["id"]),
                    int(plan["price"]),
                    str(plan["currency"]),
                    now,
                    now,
                    int(plan["price"]),
                ),
            )
            order_id = int(cursor.lastrowid or 0)
            self.conn.execute(
                "INSERT INTO tenant_renewal_orders "
                "(order_id, tenant_id, subscription_id, renew_volume_mode, "
                "renew_time_mode, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    self.tenant_id,
                    int(subscription_id),
                    volume_mode,
                    time_mode,
                    now,
                ),
            )
        return self.order(actor_id, order_id)
    def order(self, actor_id: int, order_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, p.traffic_gb, p.duration_days, "
            "p.category_id, c.title AS category_title, "
            "srv.label AS selected_server_label, "
            "o.order_kind AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_plan_categories c "
            "ON c.id=p.category_id AND c.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_servers srv "
            "ON srv.id=o.selected_server_id AND srv.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.id=? AND o.tenant_id=? AND o.customer_id=?",
            (int(order_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order not found")
        return dict(row)
    def list_orders_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT o.*, c.display_name, p.name AS plan_name, "
            "pc.title AS category_title, srv.label AS selected_server_label, "
            "o.order_kind AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_plan_categories pc "
            "ON pc.id=p.category_id AND pc.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_servers srv "
            "ON srv.id=o.selected_server_id AND srv.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? ORDER BY o.id DESC",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_fulfillment_pending_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT o.id, o.customer_id, o.plan_id, o.status, c.display_name, p.name AS plan_name, "
            "o.order_kind AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.status='paid' ORDER BY o.id",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    def cancel_order(self, actor_id: int, *, order_id: int) -> dict[str, Any]:
        """Cancel one unpaid owned order and release any reserved coupon use."""
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT * FROM tenant_orders "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(order_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order not found")
        order = dict(row)
        if order["status"] != "pending_payment":
            raise TenantBusinessError("order cannot be cancelled")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_orders SET status='cancelled', updated_at=? "
                "WHERE id=? AND tenant_id=? AND customer_id=? "
                "AND status='pending_payment'",
                (
                    now,
                    int(order_id),
                    self.tenant_id,
                    int(customer["id"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
            coupon_id = order.get("coupon_id")
            if coupon_id is not None:
                self.conn.execute(
                    "DELETE FROM tenant_coupon_redemptions "
                    "WHERE tenant_id=? AND order_id=?",
                    (self.tenant_id, int(order_id)),
                )
                self.conn.execute(
                    "UPDATE tenant_coupons "
                    "SET used_count=MAX(0, used_count-1), updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (now, int(coupon_id), self.tenant_id),
                )
        return self.order(actor_id, int(order_id))

    def _record_payment_event_tx(
        self,
        *,
        source: str,
        payment_ref_id: int,
        provider_key: str,
        event_type: str,
        actor_id: int | None,
        external_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO tenant_payment_events "
            "(tenant_id,payment_source,payment_ref_id,provider_key,event_type,"
            "actor_id,external_event_id,payload_json,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.tenant_id,
                source,
                int(payment_ref_id),
                str(provider_key or "unknown"),
                event_type,
                int(actor_id) if actor_id is not None else None,
                _text(external_event_id, 180, required=False) or None,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                idempotency_key,
                iso_utc(utcnow()),
            ),
        )

    def submit_receipt(
        self,
        actor_id: int,
        *,
        order_id: int,
        method_id: int,
        reference: str | None = None,
        telegram_file_id: str | None = None,
    ) -> dict[str, Any]:
        order = self.order(actor_id, order_id)
        if order["status"] != "pending_payment":
            raise TenantBusinessError("order is not awaiting payment")
        method = self.method(method_id, currency=str(order["currency"]))
        ref = _text(reference, 160, required=False) or None
        file_id = _text(telegram_file_id, 256, required=False) or None
        if bool(method.get("requires_receipt", True)) and not ref and not file_id:
            raise ValueError("receipt is required")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_orders SET status='payment_review', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending_payment'",
                (now, int(order_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
            cursor = self.conn.execute(
                "INSERT INTO tenant_receipts "
                "(tenant_id,order_id,payment_method_id,reference,telegram_file_id,"
                "status,created_at) VALUES (?,?,?,?,?,'pending',?)",
                (
                    self.tenant_id,
                    int(order_id),
                    int(method["id"]),
                    ref,
                    file_id,
                    now,
                ),
            )
            receipt_id = int(cursor.lastrowid or 0)
            self._record_payment_event_tx(
                source="order",
                payment_ref_id=receipt_id,
                provider_key=str(method.get("provider_key") or method.get("kind") or ""),
                event_type="submitted",
                actor_id=actor_id,
                idempotency_key=(
                    f"tenant:{self.tenant_id}:payment:order:{receipt_id}:submitted"
                ),
            )
        return {
            "id": receipt_id,
            "order_id": int(order_id),
            "payment_key": f"order:{receipt_id}",
            "status": "pending",
        }

    def review_receipt(
        self,
        actor_id: int,
        receipt_id: int,
        *,
        approve: bool,
        note: str = "",
        external_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Approve money first; fulfillment and referral credit stay retry-safe."""
        self._admin(actor_id)
        now = iso_utc(utcnow())
        subscription_id: int | None = None
        operation = "purchase"
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT r.*, o.customer_id, o.plan_id, o.amount, o.order_kind, "
                "o.status AS order_status, "
                "ro.subscription_id AS renewal_subscription_id "
                "FROM tenant_receipts r "
                "JOIN tenant_orders o ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
                "LEFT JOIN tenant_renewal_orders ro "
                "ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
                "WHERE r.id=? AND r.tenant_id=?",
                (int(receipt_id), self.tenant_id),
            ).fetchone()
            if row is None:
                raise TenantBusinessError("receipt not found")
            receipt = dict(row)
            if receipt["status"] != "pending" or receipt["order_status"] != "payment_review":
                raise TenantBusinessError("receipt was already reviewed")

            receipt_status = "approved" if approve else "rejected"
            changed = self.conn.execute(
                "UPDATE tenant_receipts SET status=?, reviewed_by=?, reviewed_at=?, "
                "review_note=?, provider_event_id=COALESCE(?, provider_event_id) "
                "WHERE id=? AND tenant_id=? AND status='pending'",
                (
                    receipt_status,
                    int(actor_id),
                    now,
                    _text(note, 500, required=False),
                    _text(external_event_id, 180, required=False) or None,
                    int(receipt_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("receipt state changed")

            if not approve:
                changed = self.conn.execute(
                    "UPDATE tenant_orders SET status='rejected', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status='payment_review'",
                    (now, int(receipt["order_id"]), self.tenant_id),
                )
                if changed.rowcount != 1:
                    raise TenantBusinessError("order state changed")
                coupon_id = self.conn.execute(
                    "SELECT coupon_id FROM tenant_orders WHERE id=? AND tenant_id=?",
                    (int(receipt["order_id"]), self.tenant_id),
                ).fetchone()
                if coupon_id is not None and coupon_id["coupon_id"] is not None:
                    self.conn.execute(
                        "UPDATE tenant_coupons SET used_count=MAX(0, used_count-1), "
                        "updated_at=? WHERE id=? AND tenant_id=?",
                        (now, int(coupon_id["coupon_id"]), self.tenant_id),
                    )
                    self.conn.execute(
                        "DELETE FROM tenant_coupon_redemptions "
                        "WHERE tenant_id=? AND order_id=?",
                        (self.tenant_id, int(receipt["order_id"])),
                    )
                method_row = self.conn.execute(
                    "SELECT kind, provider_key FROM tenant_payment_methods "
                    "WHERE tenant_id=? AND id=?",
                    (self.tenant_id, int(receipt["payment_method_id"])),
                ).fetchone()
                provider_key = (
                    str(method_row["provider_key"] or method_row["kind"])
                    if method_row is not None else "unknown"
                )
                self._record_payment_event_tx(
                    source="order",
                    payment_ref_id=int(receipt_id),
                    provider_key=provider_key,
                    event_type="rejected",
                    actor_id=actor_id,
                    external_event_id=external_event_id,
                    idempotency_key=(
                        f"tenant:{self.tenant_id}:payment:order:{int(receipt_id)}:rejected"
                    ),
                )
                return {
                    "order_id": int(receipt["order_id"]),
                    "status": "rejected",
                    "customer_id": int(receipt["customer_id"]),
                    "subscription_id": None,
                    "operation": str(receipt.get("order_kind") or "purchase"),
                }

            changed = self.conn.execute(
                "UPDATE tenant_orders SET status='paid', "
                "paid_at=COALESCE(paid_at, ?), updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='payment_review'",
                (
                    now,
                    now,
                    int(receipt["order_id"]),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
            paid_order_row = self.conn.execute(
                "SELECT * FROM tenant_orders WHERE id=? AND tenant_id=?",
                (int(receipt["order_id"]), self.tenant_id),
            ).fetchone()
            assert paid_order_row is not None
            paid_order = dict(paid_order_row)
            operation = str(paid_order.get("order_kind") or "purchase")
            subscription_id = self._create_paid_subscription_tx(
                order=paid_order,
                now=now,
            )
            if operation == "purchase":
                self._grant_referral_reward_tx(
                    invitee_customer_id=int(receipt["customer_id"]),
                    reward_type="purchase",
                    order_id=int(receipt["order_id"]),
                )
            method_row = self.conn.execute(
                "SELECT kind, provider_key FROM tenant_payment_methods "
                "WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(receipt["payment_method_id"])),
            ).fetchone()
            provider_key = (
                str(method_row["provider_key"] or method_row["kind"])
                if method_row is not None else "unknown"
            )
            self._record_payment_event_tx(
                source="order",
                payment_ref_id=int(receipt_id),
                provider_key=provider_key,
                event_type="approved",
                actor_id=actor_id,
                external_event_id=external_event_id,
                idempotency_key=(
                    f"tenant:{self.tenant_id}:payment:order:{int(receipt_id)}:approved"
                ),
            )
        return {
            "order_id": int(receipt["order_id"]),
            "status": "paid",
            "customer_id": int(receipt["customer_id"]),
            "subscription_id": subscription_id,
            "operation": operation,
        }

    def fulfill_paid_order(self, actor_id: int, *, order_id: int) -> dict[str, Any]:
        """Fulfill one approved purchase or renewal without losing retryability."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.traffic_gb, p.duration_days, "
            "ro.subscription_id AS renewal_subscription_id, "
            "ro.renew_volume_mode, ro.renew_time_mode "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.id=? AND o.tenant_id=? AND o.status='paid'",
            (int(order_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order is not awaiting fulfillment")
        order = dict(row)
        if order["renewal_subscription_id"] is not None:
            result = self.renew_subscription(
                actor_id,
                subscription_id=int(order["renewal_subscription_id"]),
                traffic_gb=int(order["traffic_gb"]),
                duration_days=int(order["duration_days"]),
                plan_id=int(order["plan_id"]),
                volume_mode=str(order.get("renew_volume_mode") or "reset"),
                time_mode=str(order.get("renew_time_mode") or "reset"),
                idempotency_key=f"tenant:{self.tenant_id}:renewal-order:{int(order_id)}",
                fulfillment_order_id=int(order_id),
            )
            result.update({"operation": "renewal", "order_id": int(order_id)})
            return result

        subscription = self.conn.execute(
            "SELECT id FROM tenant_subscriptions "
            "WHERE tenant_id=? AND order_id=? AND status='pending_provisioning'",
            (self.tenant_id, int(order_id)),
        ).fetchone()
        if subscription is None:
            raise TenantBusinessError("purchase subscription is unavailable")
        result = self.provision_pending_subscription(
            actor_id, subscription_id=int(subscription["id"])
        )
        result.update({"operation": "purchase", "order_id": int(order_id)})
        return result
    def list_receipts_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT r.*, o.amount, o.currency, c.display_name FROM tenant_receipts r"
            " JOIN tenant_orders o ON o.id=r.order_id JOIN tenant_customers c ON c.id=o.customer_id"
            " WHERE r.tenant_id=? AND r.status='pending' ORDER BY r.id",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _customer_subscription_rows(
        self,
        customer_id: int,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        subscription_id: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT s.*, p.name AS plan_name, "
            "p.traffic_gb AS plan_traffic_gb, "
            "p.duration_days AS plan_duration_days, "
            "p.price AS plan_price, p.currency AS plan_currency, "
            "c.username AS customer_username, "
            "c.display_name AS customer_display_name, "
            "srv.label AS server_label, "
            "COALESCE((SELECT o.amount FROM tenant_orders o "
            "JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE ro.tenant_id=s.tenant_id AND ro.subscription_id=s.id "
            "AND o.status='fulfilled' ORDER BY o.id DESC LIMIT 1), "
            "(SELECT o.amount FROM tenant_orders o WHERE o.id=s.order_id "
            "AND o.tenant_id=s.tenant_id)) AS subscription_price, "
            "COALESCE((SELECT o.currency FROM tenant_orders o "
            "JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE ro.tenant_id=s.tenant_id AND ro.subscription_id=s.id "
            "AND o.status='fulfilled' ORDER BY o.id DESC LIMIT 1), "
            "(SELECT o.currency FROM tenant_orders o WHERE o.id=s.order_id "
            "AND o.tenant_id=s.tenant_id)) AS subscription_currency, "
            "EXISTS(SELECT 1 FROM tenant_subscription_rotations r "
            "WHERE r.tenant_id=s.tenant_id AND r.subscription_id=s.id) AS rotation_pending "
            "FROM tenant_subscriptions s "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? AND s.customer_id=?"
        )
        args: list[Any] = [self.tenant_id, int(customer_id)]
        if status is not None:
            query += " AND s.status=?"
            args.append(str(status))
        if subscription_id is not None:
            query += " AND s.id=?"
            args.append(int(subscription_id))
        query += " ORDER BY s.id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        return [
            dict(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]

    def list_subscriptions(
        self,
        actor_id: int,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        customer = self._customer(actor_id, active=False)
        return self._customer_subscription_rows(
            int(customer["id"]),
            status=status,
            limit=limit,
            offset=offset,
        )

    def customer_subscription_status(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        refresh: bool = True,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT id, status, server_id, external_ref "
            "FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")

        sync_failed = False
        if (
            bool(refresh)
            and str(row["status"]) in ("active", "disabled")
            and row["server_id"] is not None
            and str(row["external_ref"] or "").strip()
        ):
            try:
                synced = self.sync_subscription_usage(
                    self.owner_telegram_id,
                    subscription_id=int(subscription_id),
                )
                sync_failed = bool(synced.get("node_errors"))
            except TenantBusinessError:
                # Status pages must remain readable during a temporary panel
                # outage. The last persisted snapshot is safer than inventing
                # a new state.
                sync_failed = True

        items = self._customer_subscription_rows(
            int(customer["id"]),
            limit=1,
            subscription_id=subscription_id,
        )
        for item in items:
            if int(item["id"]) == int(subscription_id):
                item["sync_failed"] = sync_failed
                return item
        raise TenantBusinessError("subscription not found")

    def rename_customer_subscription(self, actor_id: int, *,
                                     subscription_id: int, name: str) -> dict[str, Any]:
        self._customer(actor_id)
        self.customer_subscription_status(actor_id, subscription_id=subscription_id,
                                          refresh=False)
        clean = str(name).strip()
        if not 3 <= len(clean) <= 64 or any(
            unicodedata.category(char).startswith("C") for char in clean
        ):
            raise ValueError("subscription name must contain 3 to 64 printable characters")
        # This customer-facing alias is independent of panel email/traffic keys.
        # Renaming an email in X-UI would lose the accounting identity.
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_subscriptions SET service_name=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (clean, iso_utc(utcnow()), self.tenant_id, int(subscription_id)))
        return self.customer_subscription_status(actor_id,
            subscription_id=subscription_id, refresh=False)

    def rotate_customer_subscription(self, actor_id: int, *,
                                     subscription_id: int) -> dict[str, Any]:
        self._customer(actor_id)
        subscription = self.customer_subscription_status(actor_id,
            subscription_id=subscription_id, refresh=False)
        if not subscription.get("server_id") or not subscription.get("external_ref"):
            raise TenantBusinessError("subscription is not provisioned")
        method = getattr(self.panel_adapter, "rotate_identity", None)
        if not callable(method):
            raise TenantBusinessError("panel cannot rotate credentials")
        if not subscription.get("rotation_pending") and subscription["status"] in ("active", "disabled"):
            self.sync_subscription_usage(self.owner_telegram_id, subscription_id=subscription_id)
            subscription = self.customer_subscription_status(actor_id,
                subscription_id=subscription_id, refresh=False)
        self._ensure_primary_subscription_node(subscription)
        pending = self.conn.execute(
            "SELECT new_ref FROM tenant_subscription_rotations "
            "WHERE tenant_id=? AND subscription_id=?",
            (self.tenant_id, int(subscription_id))).fetchone()
        validator = getattr(self.panel_adapter, "validate_identity_rotation", None)
        if pending is None and callable(validator):
            # Validate every node before invalidating any existing link.
            for node in self.conn.execute(
                "SELECT server_id,external_ref FROM tenant_subscription_nodes "
                "WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL",
                (self.tenant_id, int(subscription_id))).fetchall():
                secret = ""
                try:
                    _, target, secret = self._panel_material(int(node["server_id"]))
                    validator(target=target, secret=secret, external_ref=str(node["external_ref"]))
                except (PanelError, TenantBusinessError) as exc:
                    raise TenantBusinessError("credential rotation is unavailable") from exc
                finally:
                    secret = ""
        new_ref = str(pending["new_ref"]) if pending else str(uuid.uuid4())
        # Journal the desired identity before any remote mutation. A retry uses
        # the same UUID and resumes unfinished nodes, including lost responses.
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO tenant_subscription_rotations "
                "(tenant_id,subscription_id,new_ref,created_at) VALUES (?,?,?,?)",
                (self.tenant_id, int(subscription_id), new_ref, iso_utc(utcnow())))
            self.conn.execute(
                "UPDATE tenant_smart_links SET code=?, updated_at=? "
                "WHERE tenant_id=? AND target=?",
                (secrets.token_urlsafe(24), iso_utc(utcnow()), self.tenant_id,
                 f"subscription:{int(subscription_id)}"))
        nodes = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes WHERE tenant_id=? "
            "AND subscription_id=? AND external_ref IS NOT NULL ORDER BY is_primary DESC,id",
            (self.tenant_id, int(subscription_id))).fetchall()
        for node in nodes:
            if str(node["external_ref"]) == new_ref:
                continue
            secret = ""
            try:
                _, target, secret = self._panel_material(int(node["server_id"]))
                result = method(target=target, secret=secret,
                    external_ref=str(node["external_ref"]), new_ref=new_ref)
                if result.external_ref != new_ref:
                    raise PanelError("credential rotation was not verified")
            except (PanelError, TenantBusinessError) as exc:
                raise TenantBusinessError("credential rotation is pending; retry to continue") from exc
            finally:
                secret = ""
            # Commit each verified node immediately; a later outage must not
            # leave the database pointing at already-invalid credentials.
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_subscription_nodes SET external_ref=?,usage_offset_bytes=?,updated_at=? "
                    "WHERE tenant_id=? AND id=?",
                    (new_ref, max(int(node["usage_offset_bytes"] or 0),
                                  int(node["usage_bytes"] or 0) - int(result.usage_bytes)),
                     iso_utc(utcnow()), self.tenant_id, int(node["id"])))
                if node["is_primary"]:
                    self.conn.execute(
                        "UPDATE tenant_subscriptions SET external_ref=?,updated_at=? "
                        "WHERE tenant_id=? AND id=?",
                        (new_ref, iso_utc(utcnow()), self.tenant_id, int(subscription_id)))
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM tenant_subscription_rotations WHERE tenant_id=? AND subscription_id=?",
                (self.tenant_id, int(subscription_id)))
        return self.customer_subscription_status(actor_id,
            subscription_id=subscription_id, refresh=False)

    def _require_completed_rotation(self, subscription_id: int) -> None:
        if self.conn.execute(
            "SELECT 1 FROM tenant_subscription_rotations WHERE tenant_id=? AND subscription_id=?",
            (self.tenant_id, int(subscription_id))).fetchone():
            raise TenantBusinessError("credential rotation is pending")

    def refresh_customer_subscription_statuses(
        self,
        actor_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        customer = self._customer(actor_id, active=False)
        rows = self._customer_subscription_rows(
            int(customer["id"]),
            limit=max(1, min(int(limit), 100)),
        )
        for item in rows:
            if (
                str(item.get("status") or "") in ("active", "disabled")
                and item.get("server_id") is not None
                and str(item.get("external_ref") or "").strip()
            ):
                try:
                    self.sync_subscription_usage(
                        self.owner_telegram_id,
                        subscription_id=int(item["id"]),
                    )
                except TenantBusinessError:
                    continue
        return self._customer_subscription_rows(
            int(customer["id"]),
            limit=max(1, min(int(limit), 100)),
        )

    def list_subscriptions_admin(self, actor_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
        self._admin(actor_id)
        query = (
            "SELECT s.*, c.display_name, p.name AS plan_name FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id WHERE s.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if status is not None:
            query += " AND s.status=?"
            args.append(str(status))
        rows = self.conn.execute(query + " ORDER BY s.id DESC", tuple(args)).fetchall()
        return [dict(row) for row in rows]

    def activate_subscription(self, actor_id: int, *, subscription_id: int, server_id: int) -> dict[str, Any]:
        """Provision the required primary, then fan out to active tenant nodes."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND status='pending_provisioning'",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription is not awaiting provisioning")
        subscription = dict(row)
        plan = self.plan(int(subscription["plan_id"]), public=False)
        if plan.get("server_id") is not None and int(plan["server_id"])!=int(server_id):
            raise TenantBusinessError("plan does not belong to this server")
        _, target, secret = self._panel_material(int(server_id))
        try:
            result = self.panel_adapter.provision(
                target=target,
                secret=secret,
                request=ProvisionRequest(
                    tenant_id=self.tenant_id,
                    server_id=int(server_id),
                    subscription_id=int(subscription_id),
                    customer_id=int(subscription["customer_id"]),
                    traffic_bytes=int(subscription["traffic_bytes"]),
                    duration_days=int(plan["duration_days"]),
                    expires_at=str(subscription["expires_at"]),
                    idempotency_key=(
                        f"tenant:{self.tenant_id}:subscription:{int(subscription_id)}"
                    ),
                    name=str(subscription.get("service_name") or ""),
                ),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel provisioning failed") from exc
        finally:
            secret = ""

        external_ref = _text(result.external_ref, 255)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET server_id=?, external_ref=?, status='active', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending_provisioning'",
                (
                    int(server_id),
                    external_ref,
                    now,
                    int(subscription_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError(
                    "subscription state changed during provisioning"
                )
            self._upsert_subscription_node(
                subscription_id=int(subscription_id),
                server_id=int(server_id),
                external_ref=external_ref,
                is_primary=True,
                status="active",
            )
            if subscription.get("order_id") is not None:
                order_changed = self.conn.execute(
                    "UPDATE tenant_orders SET status='fulfilled', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status IN ('paid', 'fulfilled')",
                    (now, int(subscription["order_id"]), self.tenant_id),
                )
                if order_changed.rowcount != 1:
                    raise TenantBusinessError(
                        "order state changed during provisioning"
                    )
        link = self._ensure_subscription_smart_link(
            subscription_id=int(subscription_id),
            label=str(plan.get("name") or f"Subscription {int(subscription_id)}"),
        )
        try:
            nodes = self.repair_subscription_nodes(
                actor_id, subscription_id=int(subscription_id)
            )
        except TenantBusinessError:
            nodes = {"created": 0, "restored": 0, "disabled": 0, "errors": 1}
        smart_url = self._smart_url(
            subscription_id=int(subscription_id),
            label=str(plan.get("name") or ""),
            base64_output=False,
        )
        return {
            "id": int(subscription_id),
            "order_id": (
                int(subscription["order_id"])
                if subscription.get("order_id") is not None
                else None
            ),
            "status": "active",
            "external_ref": external_ref,
            "smart_code": str(link["code"]),
            "node_report": nodes,
            "subscription_url": str(result.subscription_url or ""),
            "smart_subscription_url": smart_url,
        }
    def _mark_subscription_node_runtime_failure(
        self,
        *,
        subscription_id: int,
        mapping: sqlite3.Row | dict[str, Any],
        error: str,
        freeze_after: int = 3,
    ) -> None:
        row = dict(mapping)
        fail_count = max(0, int(row.get("fail_count") or 0)) + 1
        now = iso_utc(utcnow())
        frozen_at = (
            str(row.get("frozen_at") or "").strip()
            or (now if fail_count >= max(1, int(freeze_after)) else "")
        )
        self.conn.execute(
            "UPDATE tenant_subscription_nodes "
            "SET fail_count=?, frozen_at=?, last_error=?, updated_at=? "
            "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
            (
                fail_count,
                frozen_at or None,
                _text(error, 300, required=False) or "provider operation failed",
                now,
                self.tenant_id,
                int(subscription_id),
                int(row["server_id"]),
            ),
        )

    def enforce_subscription_batch(
        self,
        actor_id: int,
        *,
        limit: int = 30,
        hot_usage_ratio: float = 0.85,
        cursor: int = 0,
        freeze_after: int = 3,
    ) -> dict[str, int]:
        """Bounded global-enforcer pass with hot-first + round-robin selection."""
        self._admin(actor_id)
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_subscriptions "
                "WHERE tenant_id=? AND status IN ('active','disabled') "
                "AND server_id IS NOT NULL AND external_ref IS NOT NULL "
                "ORDER BY id",
                (self.tenant_id,),
            ).fetchall()
        ]
        if not rows:
            return {
                "scanned": 0,
                "synced": 0,
                "expired": 0,
                "pending": 0,
                "errors": 0,
                "next_cursor": 0,
            }
        ratio = min(max(float(hot_usage_ratio), 0.5), 1.0)
        now = utcnow()
        hot: list[dict[str, Any]] = []
        normal: list[dict[str, Any]] = []
        for row in rows:
            usage = max(0, int(row.get("usage_bytes") or 0))
            traffic = max(0, int(row.get("traffic_bytes") or 0))
            near_usage = traffic > 0 and usage >= int(traffic * ratio)
            try:
                near_time = parse_utc(str(row["expires_at"])) <= (
                    now + timedelta(days=1)
                )
            except Exception:
                near_time = True
            if (
                int(row.get("enforcement_pending") or 0) == 1
                or near_usage
                or near_time
            ):
                hot.append(row)
            else:
                normal.append(row)

        batch = max(1, min(int(limit), 500))
        next_cursor = max(0, int(cursor))
        if len(hot) >= batch:
            start = next_cursor % len(hot)
            ordered_hot = hot[start:] + hot[:start]
            selected = ordered_hot[:batch]
            next_cursor = (start + len(selected)) % len(hot)
        else:
            selected = list(hot)
            if normal:
                start = next_cursor % len(normal)
                ordered = normal[start:] + normal[:start]
                picked = ordered[: batch - len(selected)]
                selected.extend(picked)
                next_cursor = (start + len(picked)) % len(normal)

        synced = expired = pending = errors = 0
        for row in selected:
            try:
                result = self.sync_subscription_usage(
                    actor_id,
                    subscription_id=int(row["id"]),
                    freeze_after=freeze_after,
                )
                synced += 1
                expired += int(result["status"] == "expired")
                pending += int(bool(result.get("enforcement_pending")))
            except TenantBusinessError:
                errors += 1
        return {
            "scanned": len(selected),
            "synced": synced,
            "expired": expired,
            "pending": pending,
            "errors": errors,
            "next_cursor": next_cursor,
        }

    def sync_subscription_usage(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        freeze_after: int = 3,
    ) -> dict[str, Any]:
        """Aggregate usage safely and enforce expiry across every current node."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND status IN ('active','disabled')",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None or row["server_id"] is None or not row["external_ref"]:
            raise TenantBusinessError("subscription cannot be synchronized")
        subscription = dict(row)
        self._ensure_primary_subscription_node(subscription)
        desired_ids = {
            int(server["id"])
            for server in self._desired_subscription_servers(
                int(subscription["server_id"])
            )
        }
        all_mappings = [
            row
            for row in self.conn.execute(
                "SELECT * FROM tenant_subscription_nodes "
                "WHERE tenant_id=? AND subscription_id=? "
                "AND external_ref IS NOT NULL "
                "ORDER BY is_primary DESC, id",
                (self.tenant_id, int(subscription_id)),
            ).fetchall()
        ]
        mappings = [row for row in all_mappings if int(row["server_id"]) in desired_ids]
        if not mappings:
            raise TenantBusinessError("subscription has no panel targets")

        snapshots: list[tuple[sqlite3.Row, Any]] = []
        failed_mappings: list[sqlite3.Row] = []
        primary_usage = None
        # Disabled attachments retain their last measured consumption. Otherwise
        # disabling a node would restore quota the customer already consumed.
        total_usage = sum(max(0, int(row["usage_bytes"] or 0)) for row in all_mappings
                          if int(row["server_id"]) not in desired_ids)
        node_errors = 0
        last_online_values: list[str] = []

        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                usage = self.panel_adapter.usage(
                    target=target,
                    secret=secret,
                    external_ref=str(mapping["external_ref"]),
                )
                usage = replace(usage, usage_bytes=max(0, int(usage.usage_bytes))
                                + int(mapping["usage_offset_bytes"] or 0))
                usage_bytes = max(0, int(usage.usage_bytes))
                total_usage += usage_bytes
                snapshots.append((mapping, usage))
                if usage.last_online:
                    last_online_values.append(str(usage.last_online))
                if int(mapping["is_primary"] or 0) == 1:
                    primary_usage = usage
            except (PanelError, TenantBusinessError):
                # Never drop a node's last known usage from the global total.
                # This prevents quota bypass while a provider is unreachable.
                total_usage += max(0, int(mapping["usage_bytes"] or 0))
                failed_mappings.append(mapping)
                node_errors += 1
                with transaction(self.conn):
                    self._mark_subscription_node_runtime_failure(
                        subscription_id=int(subscription_id),
                        mapping=mapping,
                        error="usage sync failed",
                        freeze_after=freeze_after,
                    )
            finally:
                secret = ""

        last_online = max(last_online_values) if last_online_values else (
            subscription.get("last_online")
        )
        now_dt = utcnow()
        due = self._subscription_is_due(
            subscription, now=now_dt, usage_bytes=total_usage
        )
        enforcement_pending = False
        enforcement_error = None

        disabled_server_ids: set[int] = set()
        if due:
            disable_failures = 0
            # Enforce every mapping, including those whose usage read failed.
            for mapping in mappings:
                secret = ""
                try:
                    _, target, secret = self._panel_material(int(mapping["server_id"]))
                    self.panel_adapter.set_enabled(
                        target=target,
                        secret=secret,
                        external_ref=str(mapping["external_ref"]),
                        enabled=False,
                    )
                    disabled_server_ids.add(int(mapping["server_id"]))
                except (PanelError, TenantBusinessError):
                    disable_failures += 1
                    with transaction(self.conn):
                        self._mark_subscription_node_runtime_failure(
                            subscription_id=int(subscription_id),
                            mapping=mapping,
                            error="disable pending",
                            freeze_after=freeze_after,
                        )
                finally:
                    secret = ""
            if disable_failures:
                enforcement_pending = True
                enforcement_error = (
                    f"disable pending on {disable_failures} node(s)"
                )

        now = iso_utc(now_dt)
        if due and not enforcement_pending:
            state = "expired"
        elif primary_usage is not None:
            state = "active" if bool(primary_usage.active) else "disabled"
        else:
            # A transient primary outage must not invent a new service state.
            state = str(subscription["status"])
        expired_at = now if state == "expired" else None

        with transaction(self.conn):
            if due and disabled_server_ids:
                placeholders = ",".join("?" for _ in disabled_server_ids)
                node_state = "expired" if state == "expired" else "disabled"
                self.conn.execute(
                    "UPDATE tenant_subscription_nodes "
                    f"SET status=?, updated_at=? WHERE tenant_id=? "
                    f"AND subscription_id=? AND server_id IN ({placeholders})",
                    (
                        node_state,
                        now,
                        self.tenant_id,
                        int(subscription_id),
                        *sorted(disabled_server_ids),
                    ),
                )
            for mapping, usage in snapshots:
                self.conn.execute(
                    "UPDATE tenant_subscription_nodes "
                    "SET status=?, usage_bytes=?, last_online=?, fail_count=0, "
                    "frozen_at=NULL, last_error=NULL, updated_at=? "
                    "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
                    (
                        (
                            "expired"
                            if state == "expired"
                            else (
                                "disabled"
                                if due and int(mapping["server_id"]) in disabled_server_ids
                                else ("active" if bool(usage.active) else "disabled")
                            )
                        ),
                        max(0, int(usage.usage_bytes)),
                        usage.last_online,
                        now,
                        self.tenant_id,
                        int(subscription_id),
                        int(mapping["server_id"]),
                    ),
                )
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET usage_bytes=?, status=?, last_online=?, last_synced_at=?, "
                "expired_at=?, enforcement_pending=?, enforcement_error=?, "
                "enforced_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status IN ('active','disabled')",
                (
                    total_usage,
                    state,
                    last_online,
                    now,
                    expired_at,
                    1 if enforcement_pending else 0,
                    enforcement_error,
                    now if due and not enforcement_pending else None,
                    now,
                    int(subscription_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError(
                    "subscription state changed during synchronization"
                )

        return {
            "id": int(subscription_id),
            "usage_bytes": total_usage,
            "status": state,
            "last_online": last_online,
            "last_synced_at": now,
            "node_errors": node_errors,
            "enforcement_pending": enforcement_pending,
        }
    def sync_all_subscriptions(self, actor_id: int, *, limit: int = 250) -> dict[str, int]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT id FROM tenant_subscriptions "
            "WHERE tenant_id=? AND status IN ('active','disabled') "
            "AND server_id IS NOT NULL AND external_ref IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (self.tenant_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        synced = expired = errors = 0
        for row in rows:
            try:
                result = self.sync_subscription_usage(
                    actor_id, subscription_id=int(row["id"])
                )
                synced += 1
                expired += int(result["status"] == "expired")
            except TenantBusinessError:
                errors += 1
        return {"synced": synced, "expired": expired, "errors": errors}
    def _admin_subscription(self, actor_id: int, subscription_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions WHERE id=? AND tenant_id=?",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        return dict(row)

    @staticmethod
    def _subscription_is_due(
        subscription: dict[str, Any], *, now=None, usage_bytes: int | None = None
    ) -> bool:
        current = now or utcnow()
        try:
            time_due = parse_utc(str(subscription["expires_at"])) <= current
        except (TypeError, ValueError):
            return True
        used = (
            int(usage_bytes)
            if usage_bytes is not None
            else int(subscription.get("usage_bytes") or 0)
        )
        limit = int(subscription.get("traffic_bytes") or 0)
        return time_due or (limit > 0 and used >= limit)

    def expire_subscription(self, actor_id: int, *, subscription_id: int) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["status"] == "expired":
            return {"id": int(subscription_id), "status": "expired"}
        if subscription["status"] not in ("active", "disabled"):
            raise TenantBusinessError(
                "subscription cannot expire from its current state"
            )
        if not self._subscription_is_due(subscription):
            raise TenantBusinessError("subscription is not expired")
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        self._ensure_primary_subscription_node(subscription)
        mappings = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL "
            "ORDER BY is_primary DESC, id",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                self.panel_adapter.set_enabled(
                    target=target,
                    secret=secret,
                    external_ref=str(mapping["external_ref"]),
                    enabled=False,
                )
            except (PanelError, TenantBusinessError) as exc:
                raise TenantBusinessError(
                    "panel expiry enforcement failed"
                ) from exc
            finally:
                secret = ""
        now = iso_utc(utcnow())
        with transaction(self.conn):
            for mapping in mappings:
                self._upsert_subscription_node(
                    subscription_id=int(subscription_id),
                    server_id=int(mapping["server_id"]),
                    external_ref=str(mapping["external_ref"]),
                    is_primary=bool(mapping["is_primary"]),
                    status="expired",
                    usage_bytes=int(mapping["usage_bytes"] or 0),
                    usage_is_logical=True,
                    last_online=mapping["last_online"],
                )
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET status='expired', expired_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status IN ('active','disabled')",
                (now, now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError(
                    "subscription state changed during expiry"
                )
        return {"id": int(subscription_id), "status": "expired"}
    def expire_due_subscriptions(self, actor_id: int, *, limit: int = 250) -> dict[str, int]:
        self._admin(actor_id)
        now = iso_utc(utcnow())
        rows = self.conn.execute(
            "SELECT id FROM tenant_subscriptions "
            "WHERE tenant_id=? AND status IN ('active','disabled') "
            "AND server_id IS NOT NULL AND external_ref IS NOT NULL "
            "AND (expires_at<=? OR usage_bytes>=traffic_bytes) "
            "ORDER BY id LIMIT ?",
            (self.tenant_id, now, max(1, min(int(limit), 1000))),
        ).fetchall()
        expired = errors = 0
        for row in rows:
            try:
                self.expire_subscription(actor_id, subscription_id=int(row["id"]))
                expired += 1
            except TenantBusinessError:
                errors += 1
        return {"expired": expired, "errors": errors}

    @staticmethod
    def _panel_user_dict(user) -> dict[str, Any]:
        return {
            "external_ref": str(user.external_ref),
            "usage_bytes": int(user.usage_bytes),
            "traffic_bytes": (
                int(user.traffic_bytes) if user.traffic_bytes is not None else None
            ),
            "active": bool(user.active),
            "expires_at": user.expires_at,
            "last_online": user.last_online,
            "subscription_url": str(user.subscription_url or ""),
        }

    def get_subscription_panel_user(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        _, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            user = self.panel_adapter.get_user(
                target=target,
                secret=secret,
                external_ref=str(subscription["external_ref"]),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel user lookup failed") from exc
        finally:
            secret = ""
        return self._panel_user_dict(user)

    def renew_subscription(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        traffic_gb: int,
        duration_days: int,
        plan_id: int | None = None,
        volume_mode: str = "reset",
        time_mode: str = "reset",
        idempotency_key: str = "",
        fulfillment_order_id: int | None = None,
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        self._require_completed_rotation(subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        if int(traffic_gb) <= 0 or int(duration_days) <= 0:
            raise ValueError("invalid renewal values")
        self._ensure_primary_subscription_node(subscription)
        next_plan_id = int(plan_id or subscription["plan_id"])
        if plan_id is not None:
            renewal_plan=self.plan(next_plan_id,public=False)
            if renewal_plan.get("server_id") is not None and int(renewal_plan["server_id"])!=int(subscription["server_id"]):
                raise TenantBusinessError("renewal plan does not belong to this server")
        clean_volume_mode = str(volume_mode or "reset").strip().lower()
        clean_time_mode = str(time_mode or "reset").strip().lower()
        if clean_volume_mode not in ("add", "reset"):
            raise ValueError("invalid renewal volume mode")
        if clean_time_mode not in ("add", "reset"):
            raise ValueError("invalid renewal time mode")

        plan_traffic_bytes = int(traffic_gb) * 1024 * 1024 * 1024
        current_traffic_bytes = max(
            0, int(subscription.get("traffic_bytes") or 0)
        )
        current_usage_bytes = max(
            0, int(subscription.get("usage_bytes") or 0)
        )
        remaining_traffic_bytes = max(
            0, current_traffic_bytes - current_usage_bytes
        )
        traffic_bytes = (
            remaining_traffic_bytes + plan_traffic_bytes
            if clean_volume_mode == "add"
            else plan_traffic_bytes
        )

        # SellBot starts every renewed period with zero usage. In add mode,
        # only the unused remainder is carried into the new package.
        reset_usage = True

        now_dt = utcnow()
        remaining_days = 0
        if clean_time_mode == "add":
            try:
                current_expiry = parse_utc(str(subscription["expires_at"]))
                remaining_days = max(
                    0,
                    (current_expiry.date() - now_dt.date()).days,
                )
            except Exception:
                remaining_days = 0
        panel_duration_days = max(
            1,
            int(duration_days) + (
                remaining_days if clean_time_mode == "add" else 0
            ),
        )
        target_expiry = now_dt + timedelta(days=panel_duration_days)
        expires_at = iso_utc(target_expiry)
        base_key = str(idempotency_key or "")
        _, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            user = self.panel_adapter.renew(
                target=target,
                secret=secret,
                external_ref=str(subscription["external_ref"]),
                request=RenewRequest(
                    traffic_bytes=traffic_bytes,
                    duration_days=panel_duration_days,
                    expires_at=expires_at,
                    reset_usage=reset_usage,
                    reset_time=True,
                    idempotency_key=base_key,
                ),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel renewal failed") from exc
        finally:
            secret = ""

        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET plan_id=?, traffic_bytes=?, usage_bytes=?, expires_at=?, "
                "status='active', expired_at=NULL, enforcement_pending=0, "
                "enforcement_error=NULL, enforced_at=NULL, "
                "last_online=?, last_synced_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND server_id=? AND external_ref=?",
                (
                    next_plan_id,
                    traffic_bytes,
                    max(0, int(user.usage_bytes)),
                    expires_at,
                    user.last_online,
                    now,
                    now,
                    int(subscription_id),
                    self.tenant_id,
                    int(subscription["server_id"]),
                    str(subscription["external_ref"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError(
                    "subscription state changed during renewal"
                )
            self._upsert_subscription_node(
                subscription_id=int(subscription_id),
                server_id=int(subscription["server_id"]),
                external_ref=user.external_ref,
                is_primary=True,
                status="active",
                usage_bytes=max(0, int(user.usage_bytes)),
                last_online=user.last_online,
                reset_usage_offset=True,
            )
            if fulfillment_order_id is not None:
                order_changed = self.conn.execute(
                    "UPDATE tenant_orders SET status='fulfilled', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status IN ('paid','fulfilled')",
                    (now, int(fulfillment_order_id), self.tenant_id),
                )
                if order_changed.rowcount != 1:
                    raise TenantBusinessError("renewal order state changed")

        secondary_errors = 0
        rows = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND is_primary=0 "
            "AND external_ref IS NOT NULL "
            "AND status IN ('active','disabled','expired','error')",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        for row in rows:
            node_secret = ""
            try:
                _, node_target, node_secret = self._panel_material(
                    int(row["server_id"])
                )
                node_user = self.panel_adapter.renew(
                    target=node_target,
                    secret=node_secret,
                    external_ref=str(row["external_ref"]),
                    request=RenewRequest(
                        traffic_bytes=traffic_bytes,
                        duration_days=panel_duration_days,
                        expires_at=expires_at,
                        reset_usage=reset_usage,
                        reset_time=True,
                        idempotency_key=(
                            f"{base_key}:server:{int(row['server_id'])}"
                            if base_key
                            else (
                                f"tenant:{self.tenant_id}:subscription:"
                                f"{int(subscription_id)}:renew:server:"
                                f"{int(row['server_id'])}:{expires_at}"
                            )
                        ),
                    ),
                )
                with transaction(self.conn):
                    self._upsert_subscription_node(
                        subscription_id=int(subscription_id),
                        server_id=int(row["server_id"]),
                        external_ref=node_user.external_ref,
                        is_primary=False,
                        status="active",
                        usage_bytes=max(0, int(node_user.usage_bytes)),
                        last_online=node_user.last_online,
                        reset_usage_offset=True,
                    )
            except (PanelError, TenantBusinessError):
                with transaction(self.conn):
                    self._upsert_subscription_node(
                        subscription_id=int(subscription_id),
                        server_id=int(row["server_id"]),
                        external_ref=str(row["external_ref"]),
                        is_primary=False,
                        status="error",
                        usage_bytes=int(row["usage_bytes"] or 0),
                        usage_is_logical=True,
                        last_online=row["last_online"],
                        last_error="renewal failed",
                    )
                secondary_errors += 1
            finally:
                node_secret = ""

        try:
            repair = self.repair_subscription_nodes(
                actor_id, subscription_id=int(subscription_id)
            )
            secondary_errors += int(repair.get("errors") or 0)
        except TenantBusinessError:
            secondary_errors += 1

        result = self._panel_user_dict(user)
        result.update(
            {
                "id": int(subscription_id),
                "status": "active",
                "traffic_bytes": traffic_bytes,
                "expires_at": expires_at,
                "renew_volume_mode": clean_volume_mode,
                "renew_time_mode": clean_time_mode,
                "reset_usage": reset_usage,
                "node_errors": secondary_errors,
                "subscription_url": str(user.subscription_url or ""),
                "smart_subscription_url": self._smart_url(
                    subscription_id=int(subscription_id),
                    base64_output=False,
                ),
            }
        )
        return result
    def set_subscription_enabled(
        self, actor_id: int, *, subscription_id: int, enabled: bool
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        self._require_completed_rotation(subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        due = self._subscription_is_due(subscription)
        if bool(enabled) and (subscription["status"] == "expired" or due):
            raise TenantBusinessError("expired subscription requires renewal")
        self._ensure_primary_subscription_node(subscription)
        mappings = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL "
            "ORDER BY is_primary DESC, id",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        results: list[tuple[sqlite3.Row, Any]] = []
        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                user = self.panel_adapter.set_enabled(
                    target=target,
                    secret=secret,
                    external_ref=str(mapping["external_ref"]),
                    enabled=bool(enabled),
                )
                results.append((mapping, user))
            except (PanelError, TenantBusinessError) as exc:
                raise TenantBusinessError(
                    "panel account state change failed"
                ) from exc
            finally:
                secret = ""
        primary_user = next(
            (
                user
                for mapping, user in results
                if int(mapping["is_primary"] or 0) == 1
            ),
            None,
        )
        if primary_user is None:
            raise TenantBusinessError("primary panel target is unavailable")
        state = (
            "expired"
            if subscription["status"] == "expired" or due
            else ("active" if bool(enabled) else "disabled")
        )
        now = iso_utc(utcnow())
        with transaction(self.conn):
            for mapping, user in results:
                self._upsert_subscription_node(
                    subscription_id=int(subscription_id),
                    server_id=int(mapping["server_id"]),
                    external_ref=user.external_ref,
                    is_primary=bool(mapping["is_primary"]),
                    status=state,
                    usage_bytes=max(0, int(user.usage_bytes)),
                    last_online=user.last_online,
                )
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET status=?, expired_at=?, last_online=?, last_synced_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    state,
                    now if state == "expired" else None,
                    primary_user.last_online,
                    now,
                    now,
                    int(subscription_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
        result = self._panel_user_dict(primary_user)
        result.update({"id": int(subscription_id), "status": state})
        return result
    def delete_subscription_from_panel(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        self._require_completed_rotation(subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        self._ensure_primary_subscription_node(subscription)
        mappings = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL "
            "ORDER BY is_primary DESC, id",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                self.panel_adapter.delete_user(
                    target=target,
                    secret=secret,
                    external_ref=str(mapping["external_ref"]),
                )
            except (PanelError, TenantBusinessError) as exc:
                raise TenantBusinessError("panel user deletion failed") from exc
            finally:
                secret = ""
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_subscription_nodes "
                "SET status='disabled', updated_at=? "
                "WHERE tenant_id=? AND subscription_id=?",
                (now, self.tenant_id, int(subscription_id)),
            )
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions "
                "SET status='disabled', updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
        return {"id": int(subscription_id), "status": "disabled"}
    def subscription_configs(
        self, actor_id: int, *, subscription_id: int
    ) -> list[dict[str, str]]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        subscription = dict(row)
        self._require_completed_rotation(subscription_id)
        if subscription["status"] != "active" or self._subscription_is_due(subscription):
            raise TenantBusinessError("subscription is not active")
        self._ensure_primary_subscription_node(subscription)
        mappings = self.conn.execute(
            "SELECT n.*, s.label AS server_label "
            "FROM tenant_subscription_nodes n "
            "JOIN tenant_servers s ON s.id=n.server_id AND s.tenant_id=n.tenant_id "
            "WHERE n.tenant_id=? AND n.subscription_id=? "
            "AND n.external_ref IS NOT NULL AND n.status='active' "
            "ORDER BY n.is_primary DESC, n.id",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        results: list[dict[str, str]] = []
        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                content = str(
                    self.panel_adapter.subscription_content(
                        target=target,
                        secret=secret,
                        external_ref=str(mapping["external_ref"]),
                    )
                    or ""
                ).strip()
                if content:
                    results.append(
                        {
                            "server": str(mapping["server_label"]),
                            "content": content,
                        }
                    )
            except PanelError:
                continue
            finally:
                secret = ""
        if not results:
            raise TenantBusinessError("subscription configs are unavailable")
        return results

    def _customer_subscription_for_link(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        subscription = dict(row)
        self._require_completed_rotation(subscription_id)
        if subscription["status"] != "active" or self._subscription_is_due(
            subscription
        ):
            raise TenantBusinessError("subscription is not active")
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        return subscription

    def panel_user_page_link(
        self, actor_id: int, *, subscription_id: int
    ) -> str:
        subscription = self._customer_subscription_for_link(
            actor_id, subscription_id=int(subscription_id)
        )
        server = self.server(int(subscription["server_id"]))
        target = self._panel_target(server)
        if str(target.kind or "").lower() != "hiddify":
            raise TenantBusinessError("panel user page is unavailable")

        base = str(target.public_origin or target.endpoint or "").strip().rstrip("/")
        parsed = urlsplit(base)
        if (
            str(parsed.scheme or "").lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise TenantBusinessError("panel user page is unavailable")

        path = str(target.user_path or "user").strip().strip("/") or "user"
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise TenantBusinessError("panel user page is unavailable")
        ref = quote(str(subscription["external_ref"]), safe="-._~")
        if not ref:
            raise TenantBusinessError("panel user page is unavailable")
        return f"{base}/{path}/{ref}"

    def subscription_link(self, actor_id: int, *, subscription_id: int) -> str:
        """Return the panel/native Subscription URL only; never Smart Link."""
        subscription = self._customer_subscription_for_link(
            actor_id, subscription_id=int(subscription_id)
        )
        server = self.server(int(subscription["server_id"]))
        try:
            return self.panel_adapter.subscription_link(
                target=self._panel_target(server),
                external_ref=str(subscription["external_ref"]),
            )
        except PanelError as exc:
            raise TenantBusinessError(
                "subscription link is unavailable"
            ) from exc

    def automatic_subscription_link(
        self, actor_id: int, *, subscription_id: int
    ) -> str:
        """Hiddify automatic subscription URL, separate from ordinary sub."""
        base = self.panel_user_page_link(
            actor_id, subscription_id=int(subscription_id)
        )
        return f"{base.rstrip('/')}/sub/?asn=unknown"

    def subscription_link_b64(
        self, actor_id: int, *, subscription_id: int
    ) -> str:
        link = self.subscription_link(
            actor_id, subscription_id=int(subscription_id)
        )
        separator = "&" if "?" in link else "?"
        return f"{link}{separator}base64=1"

    def smart_subscription_link(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        base64_output: bool = False,
    ) -> str:
        self._customer_subscription_for_link(
            actor_id, subscription_id=int(subscription_id)
        )
        link = self._smart_url(
            subscription_id=int(subscription_id),
            base64_output=bool(base64_output),
        )
        if not link:
            raise TenantBusinessError("smart subscription link is unavailable")
        return link
    @staticmethod
    def _report_period(
        days: int,
        *,
        now: datetime | None = None,
        tz_name: str | None = None,
    ) -> tuple[str | None, str | None, str]:
        span = int(days)
        if span < 0:
            raise ValueError("invalid report period")
        timezone_name = str(
            tz_name or os.getenv("DISPLAY_TIMEZONE", "Asia/Tehran") or "Asia/Tehran"
        ).strip()
        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = ZoneInfo("Asia/Tehran")
            timezone_name = "Asia/Tehran"
        moment = now or utcnow()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local_now = moment.astimezone(zone)
        if span == 0:
            return None, None, f"همه زمان‌ها · {timezone_name}"
        start_local = datetime.combine(
            local_now.date() - timedelta(days=span - 1),
            datetime.min.time(),
            tzinfo=zone,
        )
        end_local = datetime.combine(
            local_now.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=zone,
        )
        return (
            iso_utc(start_local.astimezone(timezone.utc)),
            iso_utc(end_local.astimezone(timezone.utc)),
            (
                "امروز"
                if span == 1
                else f"{span} روز اخیر"
            )
            + f" · {timezone_name}",
        )

    @staticmethod
    def _money_totals(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [
            {
                "currency": str(row["currency"]),
                "count": int(row["count"] or 0),
                "amount": int(row["amount"] or 0),
            }
            for row in rows
        ]

    def subscription_admin(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT s.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, p.duration_days AS plan_duration_days, "
            "p.traffic_gb AS plan_traffic_gb, srv.label AS server_label "
            "FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "WHERE s.id=? AND s.tenant_id=?",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        return dict(row)

    def search_subscriptions_admin(
        self, actor_id: int, query: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        term = _text(query, 200)
        plain = term.strip().lstrip("@").strip()
        uuid_match = re.search(
            r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            plain,
        )
        # Hiddify-SellBot accepts a full config/subscription link.  When the
        # message contains a UUID, search the UUID itself rather than requiring
        # the entire URL to equal a stored panel reference.
        search_term = uuid_match.group(1) if uuid_match else plain
        like = f"%{search_term}%"
        args: list[Any] = [
            self.tenant_id,
            like,
            like,
            like,
            like,
        ]
        numeric = 0
        try:
            numeric = int(search_term.lstrip("#"))
        except (TypeError, ValueError):
            numeric = 0
        id_clause = ""
        if numeric > 0:
            id_clause = (
                " OR s.id=? OR c.id=? OR c.telegram_user_id=? "
                "OR o.id=?"
            )
            args.extend([numeric, numeric, numeric, numeric])
        args.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(
            "SELECT s.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, srv.label AS server_label "
            "FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_orders o ON o.id=s.order_id AND o.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? AND ("
            "c.display_name LIKE ? COLLATE NOCASE OR "
            "COALESCE(c.username,'') LIKE ? COLLATE NOCASE OR "
            "COALESCE(s.external_ref,'') LIKE ? COLLATE NOCASE OR "
            "COALESCE(srv.label,'') LIKE ? COLLATE NOCASE"
            + id_clause
            + ") ORDER BY CASE s.status WHEN 'active' THEN 0 "
            "WHEN 'disabled' THEN 1 WHEN 'pending_provisioning' THEN 2 "
            "ELSE 3 END, s.id DESC LIMIT ?",
            tuple(args),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_panel_users_admin(
        self,
        actor_id: int,
        query: str = "",
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Search the tenant's real panel inventory, matching SellBot smart search.

        This intentionally includes AdminBot-created panel users which do not
        have a customer/order/subscription row.  Linked UserBot subscriptions
        are enriched with customer identifiers so the same search also accepts
        Telegram names/IDs.
        """
        self._admin(actor_id)
        raw = _text(query, 200, required=False)
        needle = raw.strip().lstrip("@").casefold()
        uuid_match = re.search(
            r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            raw,
        )
        uuid_term = uuid_match.group(1).casefold() if uuid_match else ""
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in (
            "active", "disabled", "expired", "pending"
        ):
            raise ValueError("invalid panel user status")

        # Local import avoids a module-level circular dependency:
        # server_admin imports TenantBusinessError from this module.
        from TenantRuntime.server_admin import user_status

        rows = self.conn.execute(
            "SELECT u.*, srv.label AS server_label "
            "FROM tenant_panel_users u "
            "JOIN tenant_servers srv ON srv.id=u.server_id AND srv.tenant_id=u.tenant_id "
            "WHERE u.tenant_id=? AND u.state!='deleted' "
            "ORDER BY u.id DESC LIMIT 2000",
            (self.tenant_id,),
        ).fetchall()

        found: list[dict[str, Any]] = []
        for raw_row in rows:
            item = dict(raw_row)
            linked = self.conn.execute(
                "SELECT s.id AS subscription_id, c.display_name AS customer_display_name, "
                "c.username AS customer_username, c.telegram_user_id AS customer_telegram_user_id "
                "FROM tenant_subscriptions s "
                "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
                "LEFT JOIN tenant_subscription_nodes n "
                "ON n.subscription_id=s.id AND n.tenant_id=s.tenant_id "
                "WHERE s.tenant_id=? AND ("
                "(s.server_id=? AND s.external_ref=?) OR "
                "(n.server_id=? AND n.external_ref=?)) "
                "ORDER BY s.id DESC LIMIT 1",
                (
                    self.tenant_id,
                    int(item["server_id"]),
                    str(item["external_ref"]),
                    int(item["server_id"]),
                    str(item["external_ref"]),
                ),
            ).fetchone()
            if linked is not None:
                item.update(dict(linked))
            else:
                item.update(
                    subscription_id=None,
                    customer_display_name=None,
                    customer_username=None,
                    customer_telegram_user_id=None,
                )

            extra: dict[str, Any] = {}
            try:
                parsed = json.loads(item.get("extra_json") or "{}")
                if isinstance(parsed, dict):
                    extra = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                extra = {}
            item["legacy_service_id"] = extra.get("legacy_service_id")

            service_code = ""
            for part in str(item.get("comment") or "").split("|"):
                if ":" not in part:
                    continue
                key, value = part.split(":", 1)
                if key.strip().casefold() == "code":
                    service_code = value.strip()
                    break
            item["service_code"] = service_code
            item["status"] = user_status(item)

            if normalized_status and item["status"] != normalized_status:
                continue

            if needle:
                values = (
                    item.get("name"),
                    item.get("external_ref"),
                    item.get("comment"),
                    item.get("id"),
                    item.get("server_label"),
                    item.get("subscription_id"),
                    item.get("customer_display_name"),
                    item.get("customer_username"),
                    item.get("customer_telegram_user_id"),
                    item.get("service_code"),
                    item.get("legacy_service_id"),
                )
                direct_match = any(
                    needle in str(value or "").casefold() for value in values
                )
                embedded_uuid_match = bool(
                    uuid_term
                    and uuid_term == str(item.get("external_ref") or "").casefold()
                )
                config_contains_ref = bool(
                    item.get("external_ref")
                    and str(item["external_ref"]).casefold() in raw.casefold()
                )
                if not (direct_match or embedded_uuid_match or config_contains_ref):
                    continue

            found.append(item)
            if len(found) >= max(1, min(int(limit), 500)):
                break
        return found

    def list_subscriptions_tracking_admin(
        self,
        actor_id: int,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        query = (
            "SELECT s.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, srv.label AS server_label "
            "FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        normalized = None
        if status is not None:
            normalized = str(status).strip().lower()
            if normalized not in ("active", "disabled", "expired", "pending_provisioning"):
                raise ValueError("invalid subscription status")
            if normalized == "expired":
                # SellBot treats a service as expired when its effective time
                # or volume is exhausted even if a stale DB status still says
                # active/disabled. Keep pending provisioning out of this list.
                query += (
                    " AND (s.status='expired' OR "
                    "(s.status IN ('active','disabled') AND ("
                    "(s.expires_at IS NOT NULL AND s.expires_at<=?) OR "
                    "(s.traffic_bytes IS NOT NULL AND s.traffic_bytes>0 "
                    "AND s.usage_bytes>=s.traffic_bytes))))"
                )
                args.append(iso_utc(utcnow()))
            else:
                query += " AND s.status=?"
                args.append(normalized)
        query += " ORDER BY s.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        result = [
            dict(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]
        if normalized == "expired":
            for row in result:
                row["status"] = "expired"
        return result

    def review_old_subscriptions_admin(
        self,
        actor_id: int,
        *,
        kind: str,
        now: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        moment = now or utcnow()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        normalized = str(kind or "").strip().lower()
        base = (
            "SELECT DISTINCT s.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, srv.label AS server_label "
            "FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_subscription_nodes n "
            "ON n.subscription_id=s.id AND n.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? "
        )
        args: list[Any] = [self.tenant_id]
        if normalized == "stale_zero":
            base += (
                "AND ((s.status IN ('active','disabled') AND s.expires_at<=?) "
                "OR s.enforcement_pending=1 OR n.status='error' "
                "OR n.fail_count>0 OR n.frozen_at IS NOT NULL) "
            )
            args.append(iso_utc(moment))
        elif normalized == "unstarted":
            base += (
                "AND s.status='pending_provisioning' AND s.created_at<=? "
            )
            args.append(iso_utc(moment - timedelta(days=1)))
        else:
            raise ValueError("invalid review kind")
        base += "ORDER BY s.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [
            dict(row)
            for row in self.conn.execute(base, tuple(args)).fetchall()
        ]

    def admin_subscription_link(
        self, actor_id: int, *, subscription_id: int
    ) -> str:
        """Admin view of the panel/native Subscription URL, never Smart Link."""
        subscription = self.subscription_admin(
            actor_id, subscription_id=int(subscription_id)
        )
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        server = self.server(int(subscription["server_id"]))
        try:
            return self.panel_adapter.subscription_link(
                target=self._panel_target(server),
                external_ref=str(subscription["external_ref"]),
            )
        except PanelError as exc:
            raise TenantBusinessError("subscription link is unavailable") from exc

    def admin_smart_subscription_link(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        base64_output: bool = False,
    ) -> str:
        self.subscription_admin(actor_id, subscription_id=int(subscription_id))
        link = self._smart_url(
            subscription_id=int(subscription_id),
            base64_output=bool(base64_output),
        )
        if not link:
            raise TenantBusinessError("smart subscription link is unavailable")
        return link

    def admin_panel_smart_subscription_link(
        self,
        actor_id: int,
        *,
        panel_user_id: int,
        base64_output: bool = False,
    ) -> str:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_panel_users "
            "WHERE id=? AND tenant_id=? AND state!='deleted'",
            (int(panel_user_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("panel user not found")
        link = self._panel_smart_url(
            panel_user_id=int(panel_user_id),
            label=str(row["name"] or ""),
            base64_output=bool(base64_output),
        )
        if not link:
            raise TenantBusinessError("smart subscription link is unavailable")
        return link

    def cleanup_unstarted_subscription_admin(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        subscription = self.subscription_admin(
            actor_id, subscription_id=int(subscription_id)
        )
        if (
            subscription["status"] != "pending_provisioning"
            or subscription.get("server_id") is not None
            or str(subscription.get("external_ref") or "").strip()
        ):
            raise TenantBusinessError(
                "only an unprovisioned pending subscription can be cleaned"
            )
        order_id = int(subscription.get("order_id") or 0)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_trial_claims SET subscription_id=NULL, status='failed', updated_at=? "
                "WHERE tenant_id=? AND subscription_id=?",
                (iso_utc(utcnow()), self.tenant_id, int(subscription_id)),
            )
            self.conn.execute(
                "DELETE FROM tenant_smart_links "
                "WHERE tenant_id=? AND target=?",
                (self.tenant_id, f"subscription:{int(subscription_id)}"),
            )
            changed = self.conn.execute(
                "DELETE FROM tenant_subscriptions WHERE id=? AND tenant_id=? "
                "AND status='pending_provisioning' AND server_id IS NULL "
                "AND external_ref IS NULL",
                (int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
            if order_id > 0:
                self.conn.execute(
                    "UPDATE tenant_orders SET status='cancelled', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status='paid'",
                    (iso_utc(utcnow()), order_id, self.tenant_id),
                )
        return {
            "id": int(subscription_id),
            "order_id": order_id,
            "status": "removed",
        }

    def edit_subscription_terms_admin(
        self,
        actor_id: int,
        *,
        subscription_id: int,
        traffic_gb: int | None = None,
        duration_days: int | None = None,
        reset_usage: bool = False,
        reset_days: bool = False,
    ) -> dict[str, Any]:
        subscription = self.subscription_admin(
            actor_id, subscription_id=int(subscription_id)
        )
        self._require_completed_rotation(subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        if subscription["status"] == "expired" and duration_days is None and not reset_days:
            raise TenantBusinessError("expired subscription requires a new duration")

        next_traffic = int(subscription["traffic_bytes"])
        if traffic_gb is not None:
            if int(traffic_gb) <= 0:
                raise ValueError("traffic must be positive")
            next_traffic = int(traffic_gb) * 1024**3

        now_dt = utcnow()
        current_expiry = parse_utc(str(subscription["expires_at"]))
        next_expiry = current_expiry
        if duration_days is not None:
            if int(duration_days) <= 0:
                raise ValueError("duration must be positive")
            next_expiry = now_dt + timedelta(days=int(duration_days))
        elif reset_days:
            plan = self.plan(int(subscription["plan_id"]), public=False)
            next_expiry = now_dt + timedelta(days=int(plan["duration_days"]))

        remaining_seconds = max(
            86400,
            int((next_expiry - now_dt).total_seconds()),
        )
        request_days = max(1, (remaining_seconds + 86399) // 86400)

        self._ensure_primary_subscription_node(subscription)
        mappings = self.conn.execute(
            "SELECT * FROM tenant_subscription_nodes "
            "WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL "
            "ORDER BY is_primary DESC, id",
            (self.tenant_id, int(subscription_id)),
        ).fetchall()
        if not mappings:
            raise TenantBusinessError("subscription has no panel targets")

        results: list[tuple[sqlite3.Row, Any]] = []
        original_disabled = str(subscription["status"]) == "disabled"
        for mapping in mappings:
            secret = ""
            try:
                _, target, secret = self._panel_material(int(mapping["server_id"]))
                user = self.panel_adapter.renew(
                    target=target,
                    secret=secret,
                    external_ref=str(mapping["external_ref"]),
                    request=RenewRequest(
                        traffic_bytes=next_traffic,
                        duration_days=int(request_days),
                        expires_at=iso_utc(next_expiry),
                        reset_usage=bool(reset_usage),
                        idempotency_key=(
                            f"tenant:{self.tenant_id}:admin-edit:"
                            f"{int(subscription_id)}:{int(mapping['server_id'])}:"
                            f"{iso_utc(now_dt)}"
                        ),
                    ),
                )
                if original_disabled:
                    user = self.panel_adapter.set_enabled(
                        target=target,
                        secret=secret,
                        external_ref=str(mapping["external_ref"]),
                        enabled=False,
                    )
                results.append((mapping, user))
            except PanelError as exc:
                raise TenantBusinessError("panel user update failed") from exc
            finally:
                secret = ""

        primary_user = next(
            (
                user
                for mapping, user in results
                if int(mapping["is_primary"] or 0) == 1
            ),
            None,
        )
        if primary_user is None:
            raise TenantBusinessError("primary panel target is unavailable")

        state = "disabled" if original_disabled else "active"
        now_text = iso_utc(now_dt)
        with transaction(self.conn):
            for mapping, user in results:
                self._upsert_subscription_node(
                    subscription_id=int(subscription_id),
                    server_id=int(mapping["server_id"]),
                    external_ref=user.external_ref,
                    is_primary=bool(mapping["is_primary"]),
                    status=state,
                    usage_bytes=max(0, int(user.usage_bytes)),
                    last_online=user.last_online,
                    reset_usage_offset=bool(reset_usage),
                )
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions SET traffic_bytes=?, usage_bytes=?, "
                "expires_at=?, status=?, expired_at=NULL, enforcement_pending=0, "
                "enforcement_error=NULL, last_online=?, last_synced_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    next_traffic,
                    int(self.conn.execute(
                        "SELECT COALESCE(SUM(usage_bytes),0) FROM tenant_subscription_nodes "
                        "WHERE tenant_id=? AND subscription_id=?",
                        (self.tenant_id, int(subscription_id))).fetchone()[0]),
                    iso_utc(next_expiry),
                    state,
                    primary_user.last_online,
                    now_text,
                    now_text,
                    int(subscription_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
        return self.subscription_admin(
            actor_id, subscription_id=int(subscription_id)
        )

    def daily_admin_report(
        self,
        actor_id: int,
        *,
        now: datetime | None = None,
        tz_name: str = "Asia/Tehran",
    ) -> dict[str, Any]:
        """Completed previous-day accounting, mirroring SellBot's daily report semantics."""
        self._admin(actor_id)
        try:
            zone = ZoneInfo(str(tz_name or "Asia/Tehran"))
        except Exception:
            zone = ZoneInfo("Asia/Tehran")
            tz_name = "Asia/Tehran"
        moment = now or utcnow()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local_now = moment.astimezone(zone)
        report_day = local_now.date() - timedelta(days=1)
        start_local = datetime.combine(
            report_day, datetime.min.time(), tzinfo=zone
        )
        end_local = start_local + timedelta(days=1)
        start = iso_utc(start_local.astimezone(timezone.utc))
        end = iso_utc(end_local.astimezone(timezone.utc))

        cash_rows = self.conn.execute(
            "SELECT currency, COUNT(*) AS count, COALESCE(SUM(amount),0) AS amount "
            "FROM ("
            " SELECT o.currency AS currency, o.amount AS amount "
            " FROM tenant_receipts r JOIN tenant_orders o "
            " ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
            " WHERE r.tenant_id=? AND r.status='approved' "
            " AND r.reviewed_at>=? AND r.reviewed_at<? "
            " UNION ALL "
            " SELECT w.currency AS currency, w.amount AS amount "
            " FROM tenant_wallet_topups w "
            " WHERE w.tenant_id=? AND w.status='paid' "
            " AND w.paid_at>=? AND w.paid_at<?"
            ") GROUP BY currency ORDER BY currency",
            (self.tenant_id, start, end, self.tenant_id, start, end),
        ).fetchall()
        service_rows = self.conn.execute(
            "SELECT o.currency, "
            "SUM(CASE WHEN o.order_kind='purchase' THEN 1 ELSE 0 END) AS buy_count, "
            "COALESCE(SUM(CASE WHEN o.order_kind='purchase' THEN o.amount ELSE 0 END),0) AS buy_amount, "
            "SUM(CASE WHEN o.order_kind='renewal' THEN 1 ELSE 0 END) AS renew_count, "
            "COALESCE(SUM(CASE WHEN o.order_kind='renewal' THEN o.amount ELSE 0 END),0) AS renew_amount, "
            "SUM(CASE WHEN o.wallet_amount>0 THEN 1 ELSE 0 END) AS wallet_count, "
            "COALESCE(SUM(o.wallet_amount),0) AS wallet_amount "
            "FROM tenant_orders o WHERE o.tenant_id=? "
            "AND o.status IN ('paid','fulfilled') "
            "AND o.order_kind IN ('purchase','renewal') "
            "AND o.paid_at>=? AND o.paid_at<? "
            "GROUP BY o.currency ORDER BY o.currency",
            (self.tenant_id, start, end),
        ).fetchall()
        receipt_counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
            "FROM tenant_receipts WHERE tenant_id=? "
            "AND reviewed_at>=? AND reviewed_at<?",
            (self.tenant_id, start, end),
        ).fetchone()
        topup_counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
            "FROM tenant_wallet_topups WHERE tenant_id=? "
            "AND updated_at>=? AND updated_at<?",
            (self.tenant_id, start, end),
        ).fetchone()
        created = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_customers "
            "WHERE tenant_id=? AND created_at>=? AND created_at<?",
            (self.tenant_id, start, end),
        ).fetchone()
        return {
            "report_day": report_day.isoformat(),
            "timezone": str(tz_name),
            "cash": self._money_totals(cash_rows),
            "services": [
                {
                    "currency": str(row["currency"]),
                    "buy_count": int(row["buy_count"] or 0),
                    "buy_amount": int(row["buy_amount"] or 0),
                    "renew_count": int(row["renew_count"] or 0),
                    "renew_amount": int(row["renew_amount"] or 0),
                    "wallet_count": int(row["wallet_count"] or 0),
                    "wallet_amount": int(row["wallet_amount"] or 0),
                }
                for row in service_rows
            ],
            "approved_receipts": int(receipt_counts["approved"] or 0),
            "rejected_receipts": int(receipt_counts["rejected"] or 0),
            "approved_topups": int(topup_counts["approved"] or 0),
            "rejected_topups": int(topup_counts["rejected"] or 0),
            "new_customers": int(created[0] or 0),
        }

    def sales_report(
        self,
        actor_id: int,
        *,
        days: int = 1,
        now: datetime | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        """Tenant-scoped financial + operational report using immutable paid_at."""
        self._admin(actor_id)
        start, end, label = self._report_period(
            int(days), now=now, tz_name=tz_name
        )
        paid_where = (
            "o.tenant_id=? AND o.status IN ('paid','fulfilled') "
            "AND o.order_kind IN ('purchase','renewal') "
            "AND o.paid_at IS NOT NULL"
        )
        paid_args: list[Any] = [self.tenant_id]
        reviewed_where = "r.tenant_id=?"
        reviewed_args: list[Any] = [self.tenant_id]
        customer_where = "tenant_id=?"
        customer_args: list[Any] = [self.tenant_id]
        if start is not None and end is not None:
            paid_where += " AND o.paid_at>=? AND o.paid_at<?"
            paid_args.extend([start, end])
            reviewed_where += " AND r.reviewed_at>=? AND r.reviewed_at<?"
            reviewed_args.extend([start, end])
            customer_where += " AND created_at>=? AND created_at<?"
            customer_args.extend([start, end])

        totals = self._money_totals(
            self.conn.execute(
                "SELECT o.currency, COUNT(*) AS count, "
                "COALESCE(SUM(o.amount),0) AS amount "
                "FROM tenant_orders o "
                f"WHERE {paid_where} GROUP BY o.currency ORDER BY o.currency",
                tuple(paid_args),
            ).fetchall()
        )
        operation_rows = self.conn.execute(
            "SELECT o.currency, "
            "SUM(CASE WHEN o.order_kind='purchase' THEN 1 ELSE 0 END) AS purchase_count, "
            "COALESCE(SUM(CASE WHEN o.order_kind='purchase' THEN o.amount ELSE 0 END),0) "
            "AS purchase_amount, "
            "SUM(CASE WHEN o.order_kind='renewal' THEN 1 ELSE 0 END) AS renewal_count, "
            "COALESCE(SUM(CASE WHEN o.order_kind='renewal' THEN o.amount ELSE 0 END),0) "
            "AS renewal_amount, "
            "COALESCE(SUM(p.traffic_gb),0) AS traffic_gb "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_renewal_orders ro "
            "ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            f"WHERE {paid_where} GROUP BY o.currency ORDER BY o.currency",
            tuple(paid_args),
        ).fetchall()
        operations = [
            {
                "currency": str(row["currency"]),
                "purchase_count": int(row["purchase_count"] or 0),
                "purchase_amount": int(row["purchase_amount"] or 0),
                "renewal_count": int(row["renewal_count"] or 0),
                "renewal_amount": int(row["renewal_amount"] or 0),
                "traffic_gb": int(row["traffic_gb"] or 0),
            }
            for row in operation_rows
        ]
        unique_customers = self.conn.execute(
            "SELECT COUNT(DISTINCT o.customer_id) "
            "FROM tenant_orders o "
            f"WHERE {paid_where}",
            tuple(paid_args),
        ).fetchone()
        payment_counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN r.status='approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN r.status='rejected' THEN 1 ELSE 0 END) AS rejected "
            "FROM tenant_receipts r "
            f"WHERE {reviewed_where}",
            tuple(reviewed_args),
        ).fetchone()
        new_customers = self.conn.execute(
            f"SELECT COUNT(*) FROM tenant_customers WHERE {customer_where}",
            tuple(customer_args),
        ).fetchone()

        current = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM tenant_customers c "
            " WHERE c.tenant_id=? AND c.status='active') AS customers_active, "
            "(SELECT COUNT(*) FROM tenant_customers c "
            " WHERE c.tenant_id=?) AS customers_total, "
            "(SELECT COUNT(*) FROM tenant_receipts r "
            " WHERE r.tenant_id=? AND r.status='pending') AS receipts_pending, "
            "(SELECT COUNT(*) FROM tenant_orders o "
            " WHERE o.tenant_id=? AND o.status='paid') AS fulfillment_pending, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status='active') AS subs_active, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status='disabled') AS subs_disabled, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status='expired') AS subs_expired, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status='pending_provisioning') AS subs_pending, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.enforcement_pending=1) AS enforcement_pending, "
            "(SELECT COUNT(*) FROM tenant_subscription_nodes n "
            " WHERE n.tenant_id=? AND "
            " (n.status='error' OR n.fail_count>0 OR n.frozen_at IS NOT NULL)) AS node_attention, "
            "(SELECT COUNT(*) FROM tenant_tickets t "
            " WHERE t.tenant_id=? AND t.status='open') AS tickets_open, "
            "(SELECT COUNT(*) FROM tenant_servers srv "
            " WHERE srv.tenant_id=? AND srv.status='active') AS servers_active, "
            "(SELECT COUNT(*) FROM tenant_nodes n "
            " WHERE n.tenant_id=? AND n.status='active') AS nodes_active, "
            "(SELECT COALESCE(SUM(s.usage_bytes),0) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status IN ('active','disabled')) AS usage_bytes, "
            "(SELECT COALESCE(SUM(s.traffic_bytes),0) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=? AND s.status IN ('active','disabled')) AS traffic_bytes",
            (self.tenant_id,) * 15,
        ).fetchone()
        current_data = dict(current)
        reference_now = now or utcnow()
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=timezone.utc)
        expiring = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_subscriptions "
            "WHERE tenant_id=? AND status='active' "
            "AND expires_at>? AND expires_at<=?",
            (
                self.tenant_id,
                iso_utc(reference_now),
                iso_utc(reference_now + timedelta(days=1)),
            ),
        ).fetchone()
        current_data["expiring_24h"] = int(expiring[0] or 0)

        return {
            "period_days": int(days),
            "period_label": label,
            "start": start,
            "end": end,
            "totals": totals,
            "operations": operations,
            "paid_orders": sum(item["count"] for item in totals),
            "unique_customers": int(unique_customers[0] or 0),
            "approved_receipts": int(payment_counts["approved"] or 0),
            "rejected_receipts": int(payment_counts["rejected"] or 0),
            "new_customers": int(new_customers[0] or 0),
            "current": current_data,
        }

    def dashboard_summary(
        self,
        actor_id: int,
        *,
        now: datetime | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return self.sales_report(
            actor_id, days=1, now=now, tz_name=tz_name
        )

    def search_customers_admin(
        self,
        actor_id: int,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        term = _text(query, 120)
        like = f"%{term.lstrip('@')}%"
        args: list[Any] = [
            self.tenant_id,
            like,
            like,
        ]
        id_clause = ""
        numeric = 0
        try:
            numeric = int(term)
        except (TypeError, ValueError):
            numeric = 0
        if numeric > 0:
            id_clause = " OR c.telegram_user_id=? OR c.id=?"
            args.extend([numeric, numeric])
        args.append(max(1, min(int(limit), 50)))
        rows = self.conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='active') AS active_subscriptions, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='expired') AS expired_subscriptions, "
            "(SELECT MAX(o.paid_at) FROM tenant_orders o "
            " WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id "
            " AND o.status IN ('paid','fulfilled')) AS last_paid_at "
            "FROM tenant_customers c "
            "WHERE c.tenant_id=? AND "
            "(c.display_name LIKE ? COLLATE NOCASE "
            "OR COALESCE(c.username,'') LIKE ? COLLATE NOCASE"
            + id_clause
            + ") ORDER BY active_subscriptions DESC, c.id DESC LIMIT ?",
            tuple(args),
        ).fetchall()
        return [dict(row) for row in rows]

    def customer_profile_admin(
        self,
        actor_id: int,
        *,
        customer_id: int,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id) AS subscriptions_total, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='active') AS subscriptions_active, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='disabled') AS subscriptions_disabled, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='expired') AS subscriptions_expired, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id "
            " AND s.status='pending_provisioning') AS subscriptions_pending, "
            "(SELECT COUNT(*) FROM tenant_tickets t "
            " WHERE t.tenant_id=c.tenant_id AND t.customer_id=c.id "
            " AND t.status='open') AS tickets_open "
            "FROM tenant_customers c WHERE c.id=? AND c.tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("customer not found")
        result = dict(row)
        result["paid_totals"] = self._money_totals(
            self.conn.execute(
                "SELECT currency, COUNT(*) AS count, "
                "COALESCE(SUM(amount),0) AS amount "
                "FROM tenant_orders WHERE tenant_id=? AND customer_id=? "
                "AND order_kind IN ('purchase','renewal') "
                "AND status IN ('paid','fulfilled') AND paid_at IS NOT NULL "
                "GROUP BY currency ORDER BY currency",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        )
        result["recent_orders"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT o.*, p.name AS plan_name, "
                "o.order_kind AS operation "
                "FROM tenant_orders o "
                "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
                "LEFT JOIN tenant_renewal_orders ro "
                "ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
                "WHERE o.tenant_id=? AND o.customer_id=? "
                "ORDER BY o.id DESC LIMIT 5",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]
        payments = self._payment_rows(customer_id=int(customer_id))
        order_stats = self.conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "COALESCE(SUM(p.traffic_gb),0) AS gb, "
            "COALESCE(SUM(o.amount),0) AS price "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p "
            "ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.customer_id=? "
            "AND o.order_kind IN ('purchase','renewal')",
            (self.tenant_id, int(customer_id)),
        ).fetchone()
        order_totals = [
            dict(item)
            for item in self.conn.execute(
                "SELECT o.currency, COUNT(*) AS count, "
                "COALESCE(SUM(o.amount),0) AS amount "
                "FROM tenant_orders o "
                "WHERE o.tenant_id=? AND o.customer_id=? "
                "AND o.order_kind IN ('purchase','renewal') "
                "GROUP BY o.currency ORDER BY o.currency",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]
        result["full_stats"] = {
            "subs_bought": int(result.get("subscriptions_total") or 0),
            # SellBot treats every stored service as a connected service in
            # the profile counter, regardless of its current expiry status.
            "subs_connected": int(result.get("subscriptions_total") or 0),
            "tx_total": len(payments),
            "tx_approved": sum(
                1 for item in payments
                if str(item.get("status") or "").lower() == "approved"
            ),
            "orders_count": int(order_stats["cnt"] or 0) if order_stats else 0,
            "orders_gb": float(order_stats["gb"] or 0) if order_stats else 0.0,
            "orders_price": int(order_stats["price"] or 0) if order_stats else 0,
            "orders_by_currency": order_totals,
        }
        return result

    def set_customer_status_admin(
        self,
        actor_id: int,
        *,
        customer_id: int,
        status: str,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        normalized = str(status or "").strip().lower()
        if normalized not in ("active", "blocked"):
            raise ValueError("invalid customer status")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_customers SET status=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    normalized,
                    now,
                    int(customer_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("customer not found")
        return self.customer_profile_admin(actor_id, customer_id=int(customer_id))

    def subscriptions_for_customer_admin(
        self,
        actor_id: int,
        *,
        customer_id: int,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        owned = self.conn.execute(
            "SELECT 1 FROM tenant_customers WHERE id=? AND tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if owned is None:
            raise TenantBusinessError("customer not found")
        rows = self.conn.execute(
            "SELECT s.*, p.name AS plan_name, srv.label AS server_label "
            "FROM tenant_subscriptions s "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? AND s.customer_id=? ORDER BY s.id DESC",
            (self.tenant_id, int(customer_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def service_attention_admin(
        self,
        actor_id: int,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT s.*, c.display_name, c.telegram_user_id, p.name AS plan_name, "
            "srv.label AS server_label, "
            "(SELECT COUNT(*) FROM tenant_subscription_nodes n "
            " WHERE n.tenant_id=s.tenant_id AND n.subscription_id=s.id "
            " AND (n.status='error' OR n.fail_count>0 OR n.frozen_at IS NOT NULL)) "
            "AS node_errors "
            "FROM tenant_subscriptions s "
            "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
            "LEFT JOIN tenant_servers srv ON srv.id=s.server_id AND srv.tenant_id=s.tenant_id "
            "WHERE s.tenant_id=? AND "
            "(s.status IN ('pending_provisioning','expired','disabled') "
            " OR s.enforcement_pending=1 "
            " OR EXISTS (SELECT 1 FROM tenant_subscription_nodes n2 "
            "   WHERE n2.tenant_id=s.tenant_id AND n2.subscription_id=s.id "
            "   AND (n2.status='error' OR n2.fail_count>0 OR n2.frozen_at IS NOT NULL))) "
            "ORDER BY s.enforcement_pending DESC, "
            "CASE s.status WHEN 'pending_provisioning' THEN 0 WHEN 'disabled' THEN 1 "
            "WHEN 'expired' THEN 2 ELSE 3 END, s.id DESC LIMIT ?",
            (self.tenant_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def runtime_userbot_settings(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT key, value FROM tenant_userbot_settings WHERE tenant_id=?",
            (self.tenant_id,),
        ).fetchall()
        result = dict(USERBOT_SETTING_DEFAULTS)
        stored_keys: set[str] = set()
        for row in rows:
            try:
                key = str(row["key"])
                result[key] = json.loads(str(row["value"]))
                stored_keys.add(key)
            except Exception:
                continue

        # Pre-v0.15 WhiteLabel used one ambiguous show_smart_link switch.
        # Preserve an explicitly stored tenant choice until the tenant saves
        # the new independent show_multi_server setting.
        if (
            "show_smart_link" in stored_keys
            and "show_multi_server" not in stored_keys
        ):
            result["show_multi_server"] = bool(result.get("show_smart_link"))

        # Phase 14 compatibility: old v0.50 tenants had one force-join target
        # and one generic event channel. Preserve those choices until each new
        # SellBot-compatible setting is explicitly saved by the tenant.
        legacy_force = str(result.get("force_join_channel") or "").strip()
        if legacy_force:
            if (
                "force_join_channel_username" not in stored_keys
                and legacy_force.startswith("@")
            ):
                result["force_join_channel_username"] = legacy_force.lstrip("@")
            if (
                "force_join_channel_id" not in stored_keys
                and legacy_force.lstrip("-").isdigit()
            ):
                result["force_join_channel_id"] = legacy_force
            if (
                "force_join_channel_link" not in stored_keys
                and legacy_force.startswith("@")
            ):
                result["force_join_channel_link"] = (
                    "https://t.me/" + legacy_force.lstrip("@")
                )

        if "purchase_event_channel_enabled" not in stored_keys:
            result["purchase_event_channel_enabled"] = bool(
                result.get("event_channel_enabled", False)
            )
        if "purchase_event_channel_id" not in stored_keys:
            result["purchase_event_channel_id"] = str(
                result.get("event_channel_id") or ""
            ).strip()
        return result

    def userbot_settings_admin(self, actor_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        return self.runtime_userbot_settings()

    def set_userbot_setting_admin(
        self, actor_id: int, *, key: str, value: Any
    ) -> dict[str, Any]:
        self._admin(actor_id)
        name = str(key or "").strip()
        if name not in USERBOT_SETTING_DEFAULTS:
            raise ValueError("unsupported UserBot setting")
        if isinstance(USERBOT_SETTING_DEFAULTS[name], bool):
            clean: Any = bool(value)
        elif isinstance(USERBOT_SETTING_DEFAULTS[name], int):
            clean = int(value)
            if clean < 1:
                raise ValueError("setting must be positive")
            if name in ("plan_columns", "server_columns") and clean > 3:
                raise ValueError("layout columns out of range")
            if name == "reminder_days" and clean > 30:
                raise ValueError("reminder days out of range")
            if name == "reminder_remaining_gb" and clean > 1000:
                raise ValueError("reminder volume out of range")
            if name == "renew_max_days" and clean > 3650:
                raise ValueError("renewal day limit out of range")
            if name == "renew_max_remaining_gb" and clean > 100000:
                raise ValueError("renewal volume limit out of range")
            if name == "renew_unlimited_volume_from_gb" and clean > 1000000:
                raise ValueError("unlimited volume threshold out of range")
            if name == "renew_unlimited_time_from_days" and clean > 36500:
                raise ValueError("unlimited time threshold out of range")
        else:
            clean = str(value or "").strip()
            text_reset_keys = {
                "welcome_message",
                "faq_text",
                "guide_text",
                "guide_android_text",
                "guide_ios_text",
                "guide_windows_text",
                "guide_mac_text",
                "guide_linux_text",
                "servers_list_text",
                "plans_list_text",
                "ticket_panel_text",
                "invite_text",
                "invite_info_text",
                "invite_banner_text",
            }
            if name in text_reset_keys and clean in ("0", "-", "—"):
                clean = str(USERBOT_SETTING_DEFAULTS[name])
            elif name == "invite_banner_photo_id" and clean in ("0", "-", "—"):
                clean = ""
            if len(clean) > 4000:
                raise ValueError("setting text is too long")
            if name == "button_theme" and clean not in (
                "smart", "shop", "pro", "minimal"
            ):
                raise ValueError("invalid button theme")
            if name == "plan_sort_mode" and clean not in (
                "id", "price_asc", "price_desc", "traffic_asc", "traffic_desc"
            ):
                raise ValueError("invalid plan sort mode")
            if name == "renew_policy" and clean not in (
                "advanced", "default", "fair"
            ):
                raise ValueError("invalid renewal policy")
            if name in ("renew_volume_mode", "renew_time_mode") and clean not in (
                "add", "reset"
            ):
                raise ValueError("invalid renewal rollover mode")
            if name == "smart_base_url" and clean:
                parsed = urlsplit(clean)
                if (
                    str(parsed.scheme or "").lower() not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("invalid smart subscription base url")
                clean = clean.rstrip("/")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_userbot_settings "
                "(tenant_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (
                    self.tenant_id,
                    name,
                    json.dumps(clean, ensure_ascii=False),
                    now,
                ),
            )
        return self.runtime_userbot_settings()

    def toggle_userbot_setting_admin(
        self, actor_id: int, *, key: str
    ) -> dict[str, Any]:
        current = self.userbot_settings_admin(actor_id)
        if key not in USERBOT_SETTING_DEFAULTS or not isinstance(
            USERBOT_SETTING_DEFAULTS[key], bool
        ):
            raise ValueError("setting is not boolean")
        return self.set_userbot_setting_admin(
            actor_id, key=key, value=not bool(current[key])
        )

    def list_customers_admin(
        self,
        actor_id: int,
        *,
        query: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        term = str(query or "").strip().lstrip("@")
        sql = (
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id) AS subscriptions_total, "
            "(SELECT COUNT(*) FROM tenant_orders o "
            " WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id) AS orders_count, "
            "(SELECT COUNT(*) FROM tenant_orders o "
            " WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id "
            " AND o.status IN ('paid','fulfilled')) AS paid_orders "
            "FROM tenant_customers c WHERE c.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if term:
            like = f"%{term}%"
            sql += (
                " AND (c.display_name LIKE ? COLLATE NOCASE "
                "OR COALESCE(c.username,'') LIKE ? COLLATE NOCASE"
            )
            args.extend([like, like])
            try:
                numeric = int(term)
            except (TypeError, ValueError):
                numeric = 0
            if numeric > 0:
                sql += " OR c.telegram_user_id=? OR c.id=?"
                args.extend([numeric, numeric])
            sql += ")"
        sql += " ORDER BY c.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return [
            dict(row)
            for row in self.conn.execute(sql, tuple(args)).fetchall()
        ]

    def customer_wallet_admin(
        self, actor_id: int, *, customer_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        owned = self.conn.execute(
            "SELECT 1 FROM tenant_customers WHERE tenant_id=? AND id=?",
            (self.tenant_id, int(customer_id)),
        ).fetchone()
        if owned is None:
            raise TenantBusinessError("customer not found")
        accounts = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_wallet_accounts "
                "WHERE tenant_id=? AND customer_id=? ORDER BY currency",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]
        history = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_wallet_transactions "
                "WHERE tenant_id=? AND customer_id=? ORDER BY id DESC LIMIT 30",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]
        primary_currency = self._preferred_wallet_currency(int(customer_id))
        primary_account = next(
            (
                row for row in accounts
                if str(row.get("currency") or "").upper() == primary_currency
            ),
            {
                "tenant_id": self.tenant_id,
                "customer_id": int(customer_id),
                "currency": primary_currency,
                "balance": 0,
            },
        )
        return {
            "accounts": accounts,
            "history": history,
            "primary_currency": primary_currency,
            "primary_account": dict(primary_account),
        }

    def reset_customer_trial_admin(
        self, actor_id: int, *, customer_id: int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_customers WHERE tenant_id=? AND id=?",
            (self.tenant_id, int(customer_id)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("customer not found")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM tenant_trial_claims "
                "WHERE tenant_id=? AND customer_id=?",
                (self.tenant_id, int(customer_id)),
            )
            self.conn.execute(
                "UPDATE tenant_customers SET trial_used_at=NULL, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (now, self.tenant_id, int(customer_id)),
            )
        return self.customer_profile_admin(actor_id, customer_id=int(customer_id))

    def reset_all_customer_trials_admin(self, actor_id: int) -> int:
        """Reset free-trial eligibility for every customer in this tenant only."""
        self._admin(actor_id)
        now = iso_utc(utcnow())
        row = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_customers c "
            "WHERE c.tenant_id=? AND ("
            "c.trial_used_at IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM tenant_trial_claims tc "
            "WHERE tc.tenant_id=c.tenant_id AND tc.customer_id=c.id"
            "))",
            (self.tenant_id,),
        ).fetchone()
        affected = int(row[0] or 0) if row is not None else 0
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM tenant_trial_claims WHERE tenant_id=?",
                (self.tenant_id,),
            )
            self.conn.execute(
                "UPDATE tenant_customers SET trial_used_at=NULL, updated_at=? "
                "WHERE tenant_id=? AND trial_used_at IS NOT NULL",
                (now, self.tenant_id),
            )
        return affected

    def customer_orders_admin(
        self, actor_id: int, *, customer_id: int
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT o.*, p.name AS plan_name, o.order_kind AS operation "
                "FROM tenant_orders o JOIN tenant_sale_plans p "
                "ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
                "WHERE o.tenant_id=? AND o.customer_id=? ORDER BY o.id DESC",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]

    def customer_receipts_admin(
        self, actor_id: int, *, customer_id: int
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT r.*, o.amount, o.currency, o.order_kind AS operation "
                "FROM tenant_receipts r JOIN tenant_orders o "
                "ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
                "WHERE r.tenant_id=? AND o.customer_id=? ORDER BY r.id DESC",
                (self.tenant_id, int(customer_id)),
            ).fetchall()
        ]

    def order_admin(self, actor_id: int, *, order_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT o.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, p.traffic_gb, p.duration_days, "
            "pc.title AS category_title, srv.label AS selected_server_label, "
            "o.order_kind AS operation, ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_plan_categories pc "
            "ON pc.id=p.category_id AND pc.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_servers srv "
            "ON srv.id=o.selected_server_id AND srv.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_renewal_orders ro "
            "ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.id=?",
            (self.tenant_id, int(order_id)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order not found")
        return dict(row)

    def search_orders_admin(
        self, actor_id: int, query: str
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        term = str(query or "").strip().lstrip("#")
        like = f"%{term}%"
        args: list[Any] = [self.tenant_id, like, like, like]
        extra = ""
        try:
            numeric = int(term)
        except (TypeError, ValueError):
            numeric = 0
        if numeric > 0:
            extra = " OR o.id=? OR c.telegram_user_id=? OR c.id=?"
            args.extend([numeric, numeric, numeric])
        rows = self.conn.execute(
            "SELECT o.*, c.display_name, c.username, c.telegram_user_id, "
            "p.name AS plan_name, pc.title AS category_title, "
            "srv.label AS selected_server_label, o.order_kind AS operation "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_plan_categories pc "
            "ON pc.id=p.category_id AND pc.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_servers srv "
            "ON srv.id=o.selected_server_id AND srv.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND ("
            "c.display_name LIKE ? COLLATE NOCASE OR "
            "COALESCE(c.username,'') LIKE ? COLLATE NOCASE OR "
            "p.name LIKE ? COLLATE NOCASE"
            + extra
            + ") ORDER BY o.id DESC LIMIT 100",
            tuple(args),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_receipts_history_admin(
        self,
        actor_id: int,
        *,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        sql = (
            "SELECT r.*, o.amount, o.currency, o.customer_id, "
            "o.order_kind AS operation, c.display_name, c.username, "
            "c.telegram_user_id, m.kind AS payment_kind, m.title AS payment_title "
            "FROM tenant_receipts r "
            "JOIN tenant_orders o ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "JOIN tenant_payment_methods m ON m.id=r.payment_method_id "
            "AND m.tenant_id=r.tenant_id WHERE r.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if status is not None:
            normalized = str(status).strip().lower()
            if normalized not in ("pending", "approved", "rejected"):
                raise ValueError("invalid receipt status")
            sql += " AND r.status=?"
            args.append(normalized)
        if kind is not None:
            sql += " AND m.kind=?"
            args.append(str(kind).strip().lower())
        sql += " ORDER BY r.id DESC LIMIT 300"
        return [
            dict(row)
            for row in self.conn.execute(sql, tuple(args)).fetchall()
        ]

    def receipt_admin(self, actor_id: int, *, receipt_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT r.*, o.amount, o.currency, o.customer_id, "
            "o.order_kind AS operation, o.status AS order_status, "
            "c.display_name, c.username, c.telegram_user_id, "
            "m.kind AS payment_kind, m.title AS payment_title "
            "FROM tenant_receipts r "
            "JOIN tenant_orders o ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "JOIN tenant_payment_methods m ON m.id=r.payment_method_id "
            "AND m.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=? AND r.id=?",
            (self.tenant_id, int(receipt_id)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("receipt not found")
        return dict(row)

    def _payment_rows(
        self, *, customer_id: int | None = None
    ) -> list[dict[str, Any]]:
        customer_filter = ""
        args_order: list[Any] = [self.tenant_id]
        args_topup: list[Any] = [self.tenant_id]
        args_wallet: list[Any] = [self.tenant_id]
        if customer_id is not None:
            customer_filter = " AND c.id=?"
            args_order.append(int(customer_id))
            args_topup.append(int(customer_id))
            args_wallet.append(int(customer_id))

        order_rows = self.conn.execute(
            "SELECT r.id, r.order_id AS subject_id, r.payment_method_id, "
            "r.reference, r.telegram_file_id, r.status, r.reviewed_by, "
            "r.review_note, r.provider_event_id, r.created_at, r.reviewed_at, "
            "o.amount, o.currency, o.customer_id, o.order_kind AS operation, "
            "o.status AS subject_status, c.display_name, c.username, "
            "c.telegram_user_id, m.kind AS payment_kind, m.title AS payment_title, "
            "m.provider_key, m.network, m.destination "
            "FROM tenant_receipts r "
            "JOIN tenant_orders o ON o.id=r.order_id AND o.tenant_id=r.tenant_id "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "JOIN tenant_payment_methods m ON m.id=r.payment_method_id "
            "AND m.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=?" + customer_filter + " ORDER BY r.id DESC LIMIT 400",
            tuple(args_order),
        ).fetchall()

        topup_rows = self.conn.execute(
            "SELECT r.id, r.topup_id AS subject_id, r.payment_method_id, "
            "r.reference, r.telegram_file_id, r.status, r.reviewed_by, "
            "r.review_note, r.provider_event_id, r.created_at, r.reviewed_at, "
            "w.amount, w.currency, w.customer_id, 'wallet_topup' AS operation, "
            "w.status AS subject_status, c.display_name, c.username, "
            "c.telegram_user_id, m.kind AS payment_kind, m.title AS payment_title, "
            "m.provider_key, m.network, m.destination "
            "FROM tenant_wallet_topup_receipts r "
            "JOIN tenant_wallet_topups w ON w.id=r.topup_id AND w.tenant_id=r.tenant_id "
            "JOIN tenant_customers c ON c.id=w.customer_id AND c.tenant_id=w.tenant_id "
            "JOIN tenant_payment_methods m ON m.id=r.payment_method_id "
            "AND m.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=?" + customer_filter + " ORDER BY r.id DESC LIMIT 400",
            tuple(args_topup),
        ).fetchall()

        wallet_rows = self.conn.execute(
            "SELECT o.id, o.id AS subject_id, NULL AS payment_method_id, "
            "NULL AS reference, NULL AS telegram_file_id, 'approved' AS status, "
            "NULL AS reviewed_by, '' AS review_note, NULL AS provider_event_id, "
            "COALESCE(o.paid_at,o.updated_at,o.created_at) AS created_at, "
            "o.paid_at AS reviewed_at, o.wallet_amount AS amount, o.currency, "
            "o.customer_id, o.order_kind AS operation, o.status AS subject_status, "
            "c.display_name, c.username, c.telegram_user_id, 'wallet' AS payment_kind, "
            "'کیف پول' AS payment_title, 'wallet' AS provider_key, "
            "NULL AS network, NULL AS destination "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id AND c.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.wallet_amount>0 "
            "AND o.status IN ('paid','fulfilled')" + customer_filter
            + " ORDER BY o.id DESC LIMIT 400",
            tuple(args_wallet),
        ).fetchall()

        items: list[dict[str, Any]] = []
        for source, rows in (
            ("order", order_rows),
            ("wallet_topup", topup_rows),
            ("wallet_order", wallet_rows),
        ):
            for row in rows:
                item = dict(row)
                item["source"] = source
                item["payment_key"] = f"{source}:{int(item['id'])}"
                if source == "wallet_order":
                    item["provider_title"] = "کیف پول"
                    item["provider_icon"] = "💰"
                else:
                    view = payment_method_view(
                        {
                            "kind": item.get("payment_kind"),
                            "provider_key": item.get("provider_key"),
                        }
                    )
                    item["provider_key"] = view["provider_key"]
                    item["provider_title"] = view["provider_title"]
                    item["provider_icon"] = view["provider_icon"]
                items.append(item)
        items.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )
        return items

    def list_payments_admin(
        self,
        actor_id: int,
        *,
        status: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        provider_key: str | None = None,
        customer_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        items = self._payment_rows(customer_id=customer_id)
        if status is not None:
            normalized = str(status or "").strip().lower()
            if normalized not in ("pending", "approved", "rejected"):
                raise ValueError("invalid payment status")
            items = [item for item in items if item.get("status") == normalized]
        if kind is not None:
            normalized_kind = str(kind or "").strip().lower()
            items = [
                item for item in items
                if str(item.get("payment_kind") or "").lower() == normalized_kind
            ]
        if source is not None:
            clean_source = str(source or "").strip().lower()
            items = [item for item in items if item.get("source") == clean_source]
        if provider_key is not None:
            clean_provider = str(provider_key or "").strip().lower()
            items = [
                item for item in items
                if str(item.get("provider_key") or "").lower() == clean_provider
            ]
        return items[:500]

    def customer_payment_history(self, actor_id: int) -> list[dict[str, Any]]:
        customer = self._customer(actor_id, active=False)
        return self._payment_rows(customer_id=int(customer["id"]))[:50]

    def attach_payment_receipt_media(
        self,
        actor_id: int,
        *,
        payment_key: str,
        media: bytes,
        mime_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        source, receipt_id = self._parse_payment_key(payment_key)
        if source not in ("order", "wallet_topup"):
            raise TenantBusinessError("payment does not accept receipt media")
        owned = next(
            (
                item for item in self._payment_rows(customer_id=int(customer["id"]))
                if item["payment_key"] == f"{source}:{receipt_id}"
            ),
            None,
        )
        if owned is None:
            raise TenantBusinessError("payment not found")
        payload = bytes(media or b"")
        if not payload or len(payload) > 8 * 1024 * 1024:
            raise ValueError("invalid receipt media")
        clean_mime = _text(mime_type, 80, required=False) or "image/jpeg"
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_payment_receipt_media "
                "(tenant_id,payment_source,receipt_id,mime_type,media,created_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,payment_source,receipt_id) DO UPDATE SET "
                "mime_type=excluded.mime_type, media=excluded.media, "
                "created_at=excluded.created_at",
                (
                    self.tenant_id,
                    source,
                    int(receipt_id),
                    clean_mime,
                    sqlite3.Binary(payload),
                    now,
                ),
            )
        return {
            "payment_key": f"{source}:{receipt_id}",
            "size": len(payload),
            "mime_type": clean_mime,
        }

    def payment_receipt_media_admin(
        self, actor_id: int, *, payment_key: str | int
    ) -> dict[str, Any] | None:
        self._admin(actor_id)
        source, receipt_id = self._parse_payment_key(payment_key)
        if source not in ("order", "wallet_topup"):
            return None
        row = self.conn.execute(
            "SELECT mime_type, media, created_at "
            "FROM tenant_payment_receipt_media "
            "WHERE tenant_id=? AND payment_source=? AND receipt_id=?",
            (self.tenant_id, source, int(receipt_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "mime_type": str(row["mime_type"] or "image/jpeg"),
            "media": bytes(row["media"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _parse_payment_key(payment_key: str | int) -> tuple[str, int]:
        raw = str(payment_key or "").strip()
        if raw.isdigit():
            return "order", int(raw)
        if ":" not in raw:
            raise ValueError("invalid payment key")
        source, raw_id = raw.split(":", 1)
        if source not in ("order", "wallet_topup", "wallet_order"):
            raise ValueError("invalid payment source")
        payment_id = int(raw_id)
        if payment_id <= 0:
            raise ValueError("invalid payment id")
        return source, payment_id

    def payment_admin(
        self, actor_id: int, *, payment_key: str | int
    ) -> dict[str, Any]:
        self._admin(actor_id)
        source, payment_id = self._parse_payment_key(payment_key)
        key = f"{source}:{payment_id}"
        item = next(
            (row for row in self._payment_rows() if row["payment_key"] == key),
            None,
        )
        if item is None:
            raise TenantBusinessError("payment not found")
        return item

    def review_payment_admin(
        self,
        actor_id: int,
        *,
        payment_key: str | int,
        approve: bool,
        note: str = "",
        external_event_id: str | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        source, payment_id = self._parse_payment_key(payment_key)
        if source == "order":
            result = self.review_receipt(
                actor_id,
                payment_id,
                approve=approve,
                note=note,
                external_event_id=external_event_id,
            )
        elif source == "wallet_topup":
            result = self.review_wallet_topup_receipt(
                actor_id,
                receipt_id=payment_id,
                approve=approve,
                note=note,
                external_event_id=external_event_id,
            )
        else:
            raise TenantBusinessError("wallet payment is already final")
        result["payment_key"] = f"{source}:{payment_id}"
        result["source"] = source
        return result

    def search_payments_admin(
        self, actor_id: int, query: str
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        term = str(query or "").strip().lower().lstrip("#")
        if not term:
            return []
        matches: list[dict[str, Any]] = []
        for item in self._payment_rows():
            haystack = " ".join(
                str(item.get(key) or "")
                for key in (
                    "payment_key", "id", "display_name", "username",
                    "telegram_user_id", "reference", "amount", "payment_title",
                    "provider_title",
                )
            ).lower()
            if term in haystack:
                matches.append(item)
        return matches[:100]

    def update_coupon_admin(
        self,
        actor_id: int,
        *,
        coupon_id: int,
        code: str | None = None,
        value: int | None = None,
        max_uses: int | None = None,
        per_customer_limit: int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        current = self.coupon(int(coupon_id))
        updates: list[str] = []
        args: list[Any] = []

        if code is not None:
            clean_code = str(code or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9_-]{3,48}", clean_code):
                raise ValueError("invalid coupon code")
            duplicate = self.conn.execute(
                "SELECT 1 FROM tenant_coupons "
                "WHERE tenant_id=? AND code=? AND id<>?",
                (self.tenant_id, clean_code, int(coupon_id)),
            ).fetchone()
            if duplicate is not None:
                raise TenantBusinessError("coupon code already exists")
            updates.append("code=?")
            args.append(clean_code)

        if value is not None:
            clean_value = int(value)
            if clean_value <= 0:
                raise ValueError("invalid coupon value")
            if (
                str(current["discount_kind"]) == "percent"
                and clean_value > 100
            ):
                raise ValueError("percent coupon cannot exceed 100")
            updates.append("value=?")
            args.append(clean_value)

        if max_uses is not None:
            clean_max_uses = int(max_uses)
            if clean_max_uses < 0:
                raise ValueError("invalid coupon usage limit")
            updates.append("max_uses=?")
            args.append(clean_max_uses)

        if per_customer_limit is not None:
            clean_customer_limit = int(per_customer_limit)
            if clean_customer_limit < 0:
                raise ValueError("invalid coupon customer limit")
            updates.append("per_customer_limit=?")
            args.append(clean_customer_limit)

        if expires_at is not None:
            clean_expiry = str(expires_at or "").strip()
            if clean_expiry in {"", "0", "-", "—"}:
                normalized_expiry = None
            else:
                normalized_expiry = iso_utc(parse_utc(clean_expiry))
            updates.append("expires_at=?")
            args.append(normalized_expiry)

        if not updates:
            return current

        updates.append("updated_at=?")
        args.append(iso_utc(utcnow()))
        args.extend([self.tenant_id, int(coupon_id)])
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_coupons SET " + ", ".join(updates) +
                " WHERE tenant_id=? AND id=?",
                tuple(args),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("coupon not found")
        return self.coupon(int(coupon_id))

    def list_coupon_redemptions_admin(
        self, actor_id: int, *, coupon_id: int | None = None
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        sql = (
            "SELECT r.*, cp.code, c.display_name, c.telegram_user_id "
            "FROM tenant_coupon_redemptions r "
            "JOIN tenant_coupons cp ON cp.id=r.coupon_id AND cp.tenant_id=r.tenant_id "
            "JOIN tenant_customers c ON c.id=r.customer_id AND c.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if coupon_id is not None:
            sql += " AND r.coupon_id=?"
            args.append(int(coupon_id))
        sql += " ORDER BY r.id DESC LIMIT 300"
        return [
            dict(row)
            for row in self.conn.execute(sql, tuple(args)).fetchall()
        ]

    def delete_coupon_admin(self, actor_id: int, *, coupon_id: int) -> None:
        self._admin(actor_id)
        uses = self.conn.execute(
            "SELECT COUNT(*) FROM tenant_coupon_redemptions "
            "WHERE tenant_id=? AND coupon_id=?",
            (self.tenant_id, int(coupon_id)),
        ).fetchone()
        if int(uses[0] or 0) > 0:
            raise TenantBusinessError(
                "used coupon cannot be deleted; disable it instead"
            )
        with transaction(self.conn):
            changed = self.conn.execute(
                "DELETE FROM tenant_coupons WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(coupon_id)),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("coupon not found")

    def referrals_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT r.*, "
            "i.display_name AS inviter_name, i.telegram_user_id AS inviter_telegram_id, "
            "e.display_name AS invitee_name, e.telegram_user_id AS invitee_telegram_id "
            "FROM tenant_referrals r "
            "JOIN tenant_customers i ON i.id=r.inviter_customer_id AND i.tenant_id=r.tenant_id "
            "JOIN tenant_customers e ON e.id=r.invitee_customer_id AND e.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=? ORDER BY r.id DESC LIMIT 500",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def referral_rewards_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT * FROM ("
            "SELECT rw.id AS id, rw.referral_id AS referral_id, "
            "rw.inviter_customer_id AS inviter_customer_id, "
            "rw.invitee_customer_id AS invitee_customer_id, "
            "rw.reward_type AS reward_type, rw.amount AS amount, "
            "rw.currency AS currency, rw.order_id AS order_id, "
            "rw.status AS status, rw.created_at AS created_at, "
            "'automatic' AS source, i.display_name AS inviter_name, "
            "e.display_name AS invitee_name "
            "FROM tenant_referral_rewards rw "
            "JOIN tenant_customers i ON i.id=rw.inviter_customer_id "
            "AND i.tenant_id=rw.tenant_id "
            "JOIN tenant_customers e ON e.id=rw.invitee_customer_id "
            "AND e.tenant_id=rw.tenant_id "
            "WHERE rw.tenant_id=? "
            "UNION ALL "
            "SELECT mr.id AS id, NULL AS referral_id, "
            "mr.customer_id AS inviter_customer_id, "
            "0 AS invitee_customer_id, 'manual' AS reward_type, "
            "mr.amount AS amount, mr.currency AS currency, "
            "NULL AS order_id, 'paid' AS status, mr.created_at AS created_at, "
            "'manual' AS source, c.display_name AS inviter_name, "
            "NULL AS invitee_name "
            "FROM tenant_referral_manual_rewards mr "
            "JOIN tenant_customers c ON c.id=mr.customer_id "
            "AND c.tenant_id=mr.tenant_id "
            "WHERE mr.tenant_id=?"
            ") ORDER BY created_at DESC, id DESC LIMIT 500",
            (self.tenant_id, self.tenant_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def referral_admin_stats(self, actor_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        referrals = self.referrals_admin(actor_id)
        rewards = self.referral_rewards_admin(actor_id)
        paid = [x for x in rewards if str(x.get("status") or "") == "paid"]
        automatic = [x for x in paid if str(x.get("source") or "") == "automatic"]
        purchase_referrals = {
            int(x["referral_id"])
            for x in automatic
            if x.get("referral_id") is not None
            and str(x.get("reward_type") or "") == "purchase"
        }
        return {
            "total_referrals": len(referrals),
            "active_referrals": sum(
                1 for x in referrals if str(x.get("status") or "") == "active"
            ),
            "qualified_referrals": sum(
                1 for x in referrals if int(x.get("qualified") or 0) == 1
            ),
            "fraud_flagged": sum(
                1 for x in referrals if int(x.get("fraud_flag") or 0) == 1
            ),
            "successful_referrals": len(purchase_referrals),
            "trial_rewards_count": sum(
                1 for x in automatic if str(x.get("reward_type") or "") == "trial"
            ),
            "trial_rewards_amount": sum(
                int(x.get("amount") or 0)
                for x in automatic
                if str(x.get("reward_type") or "") == "trial"
            ),
            "purchase_rewards_count": sum(
                1 for x in automatic
                if str(x.get("reward_type") or "") == "purchase"
            ),
            "purchase_rewards_amount": sum(
                int(x.get("amount") or 0)
                for x in automatic
                if str(x.get("reward_type") or "") == "purchase"
            ),
            "manual_rewards_count": sum(
                1 for x in paid if str(x.get("source") or "") == "manual"
            ),
            "manual_rewards_amount": sum(
                int(x.get("amount") or 0)
                for x in paid if str(x.get("source") or "") == "manual"
            ),
            "total_reward_cost": sum(int(x.get("amount") or 0) for x in paid),
        }

    def grant_manual_referral_reward_admin(
        self,
        actor_id: int,
        *,
        customer_id: int,
        amount: int,
        currency: str = "IRR",
        note: str = "",
    ) -> dict[str, Any]:
        self._admin(actor_id)
        value = int(amount)
        if value <= 0:
            raise ValueError("manual referral reward must be positive")
        clean_currency = _text(currency, 8).upper()
        customer = self.conn.execute(
            "SELECT * FROM tenant_customers WHERE tenant_id=? AND id=?",
            (self.tenant_id, int(customer_id)),
        ).fetchone()
        if customer is None:
            raise TenantBusinessError("customer not found")
        clean_note = _text(note, 240, required=False) or "manual referral reward"
        nonce = secrets.token_hex(8)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            wallet_tx = self._wallet_change_tx(
                customer_id=int(customer_id),
                currency=clean_currency,
                amount=value,
                kind="admin_credit",
                idempotency_key=(
                    f"tenant:{self.tenant_id}:manual-referral:"
                    f"{int(customer_id)}:{nonce}"
                ),
                note=f"manual referral reward: {clean_note}",
            )
            cursor = self.conn.execute(
                "INSERT INTO tenant_referral_manual_rewards "
                "(tenant_id, customer_id, amount, currency, "
                "wallet_transaction_id, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.tenant_id,
                    int(customer_id),
                    value,
                    clean_currency,
                    int(wallet_tx["id"]),
                    clean_note,
                    now,
                ),
            )
        return {
            "id": int(cursor.lastrowid or 0),
            "customer_id": int(customer_id),
            "customer_name": str(customer["display_name"] or ""),
            "amount": value,
            "currency": clean_currency,
            "wallet_transaction_id": int(wallet_tx["id"]),
            "reward_type": "manual",
            "source": "manual",
            "status": "paid",
            "created_at": now,
        }

    def list_payment_methods_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        return [
            self._payment_method_view(row)
            for row in self.conn.execute(
                "SELECT * FROM tenant_payment_methods "
                "WHERE tenant_id=? ORDER BY priority ASC, id ASC",
                (self.tenant_id,),
            ).fetchall()
        ]

    def set_payment_method_status_admin(
        self, actor_id: int, *, method_id: int, enabled: bool
    ) -> dict[str, Any]:
        self._admin(actor_id)
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_payment_methods SET status=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (
                    "active" if enabled else "disabled",
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(method_id),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("payment method not found")
        row = self.conn.execute(
            "SELECT * FROM tenant_payment_methods WHERE tenant_id=? AND id=?",
            (self.tenant_id, int(method_id)),
        ).fetchone()
        assert row is not None
        return dict(row)

    def _broadcast_users_snapshot_admin(
        self, actor_id: int
    ) -> list[dict[str, Any]]:
        """SellBot-compatible, tenant-scoped broadcast segmentation snapshot."""
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT c.id, c.telegram_user_id, c.display_name, "
            "(SELECT COUNT(*) FROM tenant_orders o "
            " WHERE o.tenant_id=c.tenant_id AND o.customer_id=c.id) AS orders_count, "
            "(SELECT COUNT(*) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id) AS services_count, "
            "(SELECT MAX(s.expires_at) FROM tenant_subscriptions s "
            " WHERE s.tenant_id=c.tenant_id AND s.customer_id=c.id) AS max_expires_at "
            "FROM tenant_customers c "
            "WHERE c.tenant_id=? AND c.telegram_user_id IS NOT NULL "
            "ORDER BY c.id DESC",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _broadcast_row_matches(
        row: dict[str, Any], segment: str, *, now: datetime
    ) -> bool:
        normalized = str(segment or "all").strip().lower()
        allowed = {
            "all", "expired_all", "no_order",
            "expired_1w", "expired_2w", "expired_4w", "expired_8w",
        }
        if normalized not in allowed:
            raise ValueError("invalid broadcast segment")
        if normalized == "all":
            return True
        if normalized == "no_order":
            return int(row.get("orders_count") or 0) <= 0

        if int(row.get("services_count") or 0) <= 0:
            return False
        raw_expiry = str(row.get("max_expires_at") or "").strip()
        if not raw_expiry:
            return False
        try:
            expiry = parse_utc(raw_expiry)
        except Exception:
            return False

        if normalized == "expired_all":
            return expiry <= now
        thresholds = {
            "expired_1w": 7,
            "expired_2w": 14,
            "expired_4w": 28,
            "expired_8w": 56,
        }
        return expiry <= now - timedelta(days=thresholds[normalized])

    def broadcast_targets_admin(
        self, actor_id: int, *, segment: str
    ) -> list[dict[str, Any]]:
        normalized = str(segment or "").strip().lower()
        # Validate before querying so copied/forged callbacks fail fast.
        if normalized not in {
            "all", "expired_all", "no_order",
            "expired_1w", "expired_2w", "expired_4w", "expired_8w",
        }:
            raise ValueError("invalid broadcast segment")
        now = utcnow()
        rows = self._broadcast_users_snapshot_admin(actor_id)
        seen: set[int] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            if not self._broadcast_row_matches(row, normalized, now=now):
                continue
            telegram_id = int(row.get("telegram_user_id") or 0)
            if telegram_id <= 0 or telegram_id in seen:
                continue
            seen.add(telegram_id)
            result.append(row)
        return result

    def broadcast_stats_admin(self, actor_id: int) -> dict[str, int]:
        rows = self._broadcast_users_snapshot_admin(actor_id)
        now = utcnow()

        def count(segment: str) -> int:
            return sum(
                1
                for row in rows
                if self._broadcast_row_matches(row, segment, now=now)
            )

        return {
            "total_users": count("all"),
            "expired_users": count("expired_all"),
            "no_order_users": count("no_order"),
            "expired_1w_users": count("expired_1w"),
            "expired_2w_users": count("expired_2w"),
            "expired_4w_users": count("expired_4w"),
            "expired_8w_users": count("expired_8w"),
        }

    def start_broadcast_run_admin(
        self,
        actor_id: int,
        *,
        segment: str,
        message_kind: str,
        target_count: int,
        buttons_count: int = 0,
    ) -> int:
        self._admin(actor_id)
        kind = str(message_kind or "").strip().lower()
        if kind not in {"text", "photo", "video", "document"}:
            raise ValueError("invalid broadcast message kind")
        targets = max(0, int(target_count))
        buttons = max(0, min(int(buttons_count), 8))
        now = iso_utc(utcnow())
        cursor = self.conn.execute(
            "INSERT INTO tenant_broadcast_runs "
            "(tenant_id, segment, message_kind, target_count, sent_count, "
            "failed_count, buttons_count, recovered_count, unreachable_count, "
            "temporary_count, telegram_error_count, other_error_count, created_at) "
            "VALUES (?, ?, ?, ?, 0, 0, ?, 0, 0, 0, 0, 0, ?)",
            (
                self.tenant_id,
                str(segment),
                kind,
                targets,
                buttons,
                now,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def finish_broadcast_run_admin(
        self,
        actor_id: int,
        *,
        run_id: int,
        sent: int,
        failed: int,
        recovered: int = 0,
        unreachable: int = 0,
        temporary: int = 0,
        telegram_error: int = 0,
        other: int = 0,
    ) -> None:
        self._admin(actor_id)
        changed = self.conn.execute(
            "UPDATE tenant_broadcast_runs SET "
            "sent_count=?, failed_count=?, recovered_count=?, "
            "unreachable_count=?, temporary_count=?, telegram_error_count=?, "
            "other_error_count=?, finished_at=? "
            "WHERE id=? AND tenant_id=?",
            (
                max(0, int(sent)),
                max(0, int(failed)),
                max(0, int(recovered)),
                max(0, int(unreachable)),
                max(0, int(temporary)),
                max(0, int(telegram_error)),
                max(0, int(other)),
                iso_utc(utcnow()),
                int(run_id),
                self.tenant_id,
            ),
        )
        self.conn.commit()
        if changed.rowcount != 1:
            raise TenantBusinessError("broadcast run not found")


    def customer_account_summary(
        self,
        actor_id: int,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        if bool(refresh):
            self.refresh_customer_subscription_statuses(actor_id)
        counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN status='disabled' THEN 1 ELSE 0 END) AS disabled, "
            "SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired, "
            "SUM(CASE WHEN status='pending_provisioning' THEN 1 ELSE 0 END) AS pending "
            "FROM tenant_subscriptions WHERE tenant_id=? AND customer_id=?",
            (self.tenant_id, int(customer["id"])),
        ).fetchone()
        paid_totals = self._money_totals(
            self.conn.execute(
                "SELECT currency, COUNT(*) AS count, COALESCE(SUM(amount),0) AS amount "
                "FROM tenant_orders WHERE tenant_id=? AND customer_id=? "
                "AND order_kind IN ('purchase','renewal') "
                "AND status IN ('paid','fulfilled') AND paid_at IS NOT NULL "
                "GROUP BY currency ORDER BY currency",
                (self.tenant_id, int(customer["id"])),
            ).fetchall()
        )
        order_counts = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('pending_payment','payment_review','paid') "
            "THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN status='fulfilled' THEN 1 ELSE 0 END) AS fulfilled, "
            "SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled "
            "FROM tenant_orders WHERE tenant_id=? AND customer_id=?",
            (self.tenant_id, int(customer["id"])),
        ).fetchone()
        return {
            "customer": dict(customer),
            "subscriptions": {
                "active": int(counts["active"] or 0),
                "disabled": int(counts["disabled"] or 0),
                "expired": int(counts["expired"] or 0),
                "pending": int(counts["pending"] or 0),
            },
            "paid_totals": paid_totals,
            "pending_orders": int(order_counts["pending"] or 0),
            "orders_total": int(order_counts["total"] or 0),
            "fulfilled_orders": int(order_counts["fulfilled"] or 0),
            "cancelled_orders": int(order_counts["cancelled"] or 0),
        }

    def list_customer_orders(
        self,
        actor_id: int,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        customer = self._customer(actor_id, active=False)
        rows = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, "
            "p.traffic_gb AS plan_traffic_gb, "
            "p.duration_days AS plan_duration_days, "
            "o.order_kind AS operation "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "LEFT JOIN tenant_renewal_orders ro "
            "ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.customer_id=? "
            "ORDER BY o.id DESC LIMIT ?",
            (
                self.tenant_id,
                int(customer["id"]),
                max(1, min(int(limit), 50)),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def customer_order(
        self,
        actor_id: int,
        *,
        order_id: int,
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, "
            "p.traffic_gb AS plan_traffic_gb, "
            "p.duration_days AS plan_duration_days, "
            "o.order_kind AS operation "
            "FROM tenant_orders o "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id AND p.tenant_id=o.tenant_id "
            "WHERE o.id=? AND o.tenant_id=? AND o.customer_id=?",
            (int(order_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("order not found")
        return dict(row)

    @staticmethod
    def _ticket_message_view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["has_media"] = item.get("media") is not None
        item.pop("media", None)
        return item

    def _ticket_owned(self, actor_id: int, ticket_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        row = self.conn.execute(
            "SELECT t.*, c.display_name, c.username, c.telegram_user_id "
            "FROM tenant_tickets t "
            "JOIN tenant_customers c ON c.id=t.customer_id AND c.tenant_id=t.tenant_id "
            "WHERE t.tenant_id=? AND t.id=? AND t.customer_id=?",
            (self.tenant_id, int(ticket_id), int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("ticket not found")
        return dict(row)

    def _add_ticket_message_tx(
        self,
        *,
        ticket_id: int,
        sender_type: str,
        sender_name: str,
        message_text: str = "",
        media: bytes | None = None,
        media_mime: str = "",
        created_at: str | None = None,
    ) -> int:
        kind = str(sender_type or "").strip().lower()
        if kind not in {"user", "admin"}:
            raise ValueError("invalid ticket sender")
        text = _text(message_text, 4000, required=False)
        payload = bytes(media or b"")
        if not text and not payload:
            raise ValueError("ticket message is empty")
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("ticket media is too large")
        mime = _text(media_mime, 80, required=False)
        now = created_at or iso_utc(utcnow())
        cursor = self.conn.execute(
            "INSERT INTO tenant_ticket_messages "
            "(tenant_id,ticket_id,sender_type,sender_name,message_text,"
            "media_mime,media,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.tenant_id,
                int(ticket_id),
                kind,
                _text(sender_name, 120, required=False),
                text,
                mime,
                sqlite3.Binary(payload) if payload else None,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    def create_ticket(
        self,
        actor_id: int,
        *,
        subject: str,
        body: str,
        media: bytes | None = None,
        media_mime: str = "",
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        clean_subject = _text(subject, 100)
        clean_body = _text(body, 4000)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_tickets "
                "(tenant_id,customer_id,subject,body,status,created_at,updated_at) "
                "VALUES (?,?,?,?, 'open', ?, ?)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    clean_subject,
                    clean_body,
                    now,
                    now,
                ),
            )
            ticket_id = int(cursor.lastrowid or 0)
            self._add_ticket_message_tx(
                ticket_id=ticket_id,
                sender_type="user",
                sender_name=str(customer.get("display_name") or customer.get("username") or actor_id),
                message_text=clean_body,
                media=media,
                media_mime=media_mime,
                created_at=now,
            )
        return self._ticket_owned(actor_id, ticket_id)

    def ticket(self, actor_id: int, *, ticket_id: int) -> dict[str, Any]:
        return self._ticket_owned(actor_id, int(ticket_id))

    def list_tickets(self, actor_id: int) -> list[dict[str, Any]]:
        customer = self._customer(actor_id, active=False)
        rows = self.conn.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM tenant_ticket_messages m "
            " WHERE m.tenant_id=t.tenant_id AND m.ticket_id=t.id) AS message_count "
            "FROM tenant_tickets t WHERE t.tenant_id=? AND t.customer_id=? "
            "ORDER BY t.updated_at DESC, t.id DESC LIMIT 100",
            (self.tenant_id, int(customer["id"])),
        ).fetchall()
        return [dict(row) for row in rows]

    def ticket_messages(self, actor_id: int, *, ticket_id: int) -> list[dict[str, Any]]:
        self._ticket_owned(actor_id, int(ticket_id))
        rows = self.conn.execute(
            "SELECT * FROM tenant_ticket_messages "
            "WHERE tenant_id=? AND ticket_id=? ORDER BY id ASC",
            (self.tenant_id, int(ticket_id)),
        ).fetchall()
        return [self._ticket_message_view(row) for row in rows]

    def ticket_message_media(
        self,
        actor_id: int,
        *,
        ticket_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        self._ticket_owned(actor_id, int(ticket_id))
        row = self.conn.execute(
            "SELECT media_mime, media FROM tenant_ticket_messages "
            "WHERE tenant_id=? AND ticket_id=? AND id=?",
            (self.tenant_id, int(ticket_id), int(message_id)),
        ).fetchone()
        if row is None or row["media"] is None:
            return None
        return {
            "media_mime": str(row["media_mime"] or "image/jpeg"),
            "media": bytes(row["media"]),
        }

    def reply_ticket(
        self,
        actor_id: int,
        *,
        ticket_id: int,
        reply: str,
        media: bytes | None = None,
        media_mime: str = "",
    ) -> dict[str, Any]:
        customer = self._customer(actor_id, active=False)
        ticket = self._ticket_owned(actor_id, int(ticket_id))
        if str(ticket.get("status") or "") == "closed":
            raise TenantBusinessError("ticket is closed")
        text = _text(reply, 4000)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self._add_ticket_message_tx(
                ticket_id=int(ticket_id),
                sender_type="user",
                sender_name=str(customer.get("display_name") or customer.get("username") or actor_id),
                message_text=text,
                media=media,
                media_mime=media_mime,
                created_at=now,
            )
            self.conn.execute(
                "UPDATE tenant_tickets SET status='open', updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (now, self.tenant_id, int(ticket_id)),
            )
        return self._ticket_owned(actor_id, int(ticket_id))

    def close_ticket(
        self, actor_id: int, *, ticket_id: int
    ) -> dict[str, Any]:
        self._ticket_owned(actor_id, int(ticket_id))
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_tickets SET status='closed', updated_at=? "
                "WHERE tenant_id=? AND id=? AND status IN ('open','answered')",
                (now, self.tenant_id, int(ticket_id)),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("ticket cannot be closed")
        return self._ticket_owned(actor_id, int(ticket_id))

    def reopen_ticket(
        self, actor_id: int, *, ticket_id: int
    ) -> dict[str, Any]:
        self._ticket_owned(actor_id, int(ticket_id))
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_tickets SET status='open', updated_at=? "
                "WHERE tenant_id=? AND id=? AND status='closed'",
                (now, self.tenant_id, int(ticket_id)),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("ticket cannot be reopened")
        return self._ticket_owned(actor_id, int(ticket_id))

    def list_tickets_admin(
        self,
        actor_id: int,
        *,
        status: str | None = None,
        customer_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self._admin(actor_id)
        sql = (
            "SELECT t.*, c.display_name, c.telegram_user_id, c.username, "
            "(SELECT COUNT(*) FROM tenant_ticket_messages m "
            " WHERE m.tenant_id=t.tenant_id AND m.ticket_id=t.id) AS message_count "
            "FROM tenant_tickets t "
            "JOIN tenant_customers c ON c.id=t.customer_id AND c.tenant_id=t.tenant_id "
            "WHERE t.tenant_id=?"
        )
        args: list[Any] = [self.tenant_id]
        if status is not None:
            clean = str(status or "").strip().lower()
            mapping = {"pending": "open", "open": "answered", "closed": "closed"}
            if clean not in mapping:
                raise ValueError("invalid ticket status")
            sql += " AND t.status=?"
            args.append(mapping[clean])
        if customer_id is not None:
            sql += " AND t.customer_id=?"
            args.append(int(customer_id))
        sql += " ORDER BY t.updated_at DESC, t.id DESC"
        return [dict(row) for row in self.conn.execute(sql, tuple(args)).fetchall()]

    def ticket_admin(self, actor_id: int, *, ticket_id: int) -> dict[str, Any]:
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT t.*, c.display_name, c.telegram_user_id, c.username, "
            "(SELECT COUNT(*) FROM tenant_ticket_messages m "
            " WHERE m.tenant_id=t.tenant_id AND m.ticket_id=t.id) AS message_count "
            "FROM tenant_tickets t "
            "JOIN tenant_customers c ON c.id=t.customer_id AND c.tenant_id=t.tenant_id "
            "WHERE t.id=? AND t.tenant_id=?",
            (int(ticket_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("ticket not found")
        return dict(row)

    def ticket_messages_admin(
        self, actor_id: int, *, ticket_id: int
    ) -> list[dict[str, Any]]:
        self.ticket_admin(actor_id, ticket_id=int(ticket_id))
        rows = self.conn.execute(
            "SELECT * FROM tenant_ticket_messages "
            "WHERE tenant_id=? AND ticket_id=? ORDER BY id ASC",
            (self.tenant_id, int(ticket_id)),
        ).fetchall()
        return [self._ticket_message_view(row) for row in rows]

    def ticket_message_media_admin(
        self,
        actor_id: int,
        *,
        ticket_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        self.ticket_admin(actor_id, ticket_id=int(ticket_id))
        row = self.conn.execute(
            "SELECT media_mime, media FROM tenant_ticket_messages "
            "WHERE tenant_id=? AND ticket_id=? AND id=?",
            (self.tenant_id, int(ticket_id), int(message_id)),
        ).fetchone()
        if row is None or row["media"] is None:
            return None
        return {
            "media_mime": str(row["media_mime"] or "image/jpeg"),
            "media": bytes(row["media"]),
        }

    def reply_ticket_admin(
        self,
        actor_id: int,
        *,
        ticket_id: int,
        reply: str,
        media: bytes | None = None,
        media_mime: str = "",
        admin_name: str = "پشتیبانی",
    ) -> dict[str, Any]:
        self._admin(actor_id)
        ticket = self.ticket_admin(actor_id, ticket_id=int(ticket_id))
        if str(ticket.get("status") or "") == "closed":
            raise TenantBusinessError("ticket is closed")
        message = _text(reply, 4000)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self._add_ticket_message_tx(
                ticket_id=int(ticket_id),
                sender_type="admin",
                sender_name=_text(admin_name, 120, required=False) or "پشتیبانی",
                message_text=message,
                media=media,
                media_mime=media_mime,
                created_at=now,
            )
            changed = self.conn.execute(
                "UPDATE tenant_tickets SET admin_reply=?, status='answered', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status IN ('open','answered')",
                (message, now, int(ticket_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("ticket cannot be answered")
        return self.ticket_admin(actor_id, ticket_id=int(ticket_id))

    def set_ticket_status_admin(
        self,
        actor_id: int,
        *,
        ticket_id: int,
        status: str,
    ) -> dict[str, Any]:
        self._admin(actor_id)
        clean = str(status or "").strip().lower()
        if clean not in {"open", "closed"}:
            raise ValueError("invalid ticket status")
        self.ticket_admin(actor_id, ticket_id=int(ticket_id))
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_tickets SET status=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (clean, now, int(ticket_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("ticket state changed")
        return self.ticket_admin(actor_id, ticket_id=int(ticket_id))

    def close_ticket_admin(
        self,
        actor_id: int,
        *,
        ticket_id: int,
    ) -> dict[str, Any]:
        return self.set_ticket_status_admin(
            actor_id, ticket_id=int(ticket_id), status="closed"
        )


    def create_smart_link(self, actor_id: int, *, label: str, target: str) -> dict[str, Any]:
        self._admin(actor_id); now = iso_utc(utcnow()); code = secrets.token_urlsafe(7)
        with transaction(self.conn):
            cursor = self.conn.execute("INSERT INTO tenant_smart_links (tenant_id, code, label, target, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", (self.tenant_id, code, _text(label, 80), _text(target, 250), now, now))
        return {"id": int(cursor.lastrowid or 0), "code": code, "label": _text(label, 80), "target": _text(target, 250)}

    def list_smart_links(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        settings = self.runtime_userbot_settings()
        public_base = str(
            settings.get("smart_base_url")
            or os.getenv("SMART_SUB_PUBLIC_BASE_URL", "")
            or ""
        ).strip()
        from TenantRuntime.smart_subscription import smart_subscription_url

        items: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT * FROM tenant_smart_links "
            "WHERE tenant_id=? ORDER BY id DESC",
            (self.tenant_id,),
        ).fetchall():
            item = dict(row)
            if str(item["target"]).startswith(("subscription:", "paneluser:")):
                item["public_url"] = smart_subscription_url(
                    public_base,
                    str(item["code"]),
                    base64_output=False,
                )
                item["public_url_b64"] = smart_subscription_url(
                    public_base,
                    str(item["code"]),
                    base64_output=True,
                )
            else:
                item["public_url"] = ""
                item["public_url_b64"] = ""
            items.append(item)
        return items
