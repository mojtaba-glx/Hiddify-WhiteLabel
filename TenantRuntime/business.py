"""Tenant-scoped business service used by each dedicated runtime bot.

The service is permanently bound to one tenant. Every read/write includes the
bound tenant id, so a callback id from another customer can never cross into
this tenant's data. Panel credentials are encrypted at rest and passed only at
the adapter boundary; concrete provider HTTP dialects remain separate.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from Database.connection import transaction
from Shared.crypto import TokenCipher, TokenCipherError, fingerprint_token
from Shared.timeutils import iso_utc, parse_utc, utcnow
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


def _text(value: object, maximum: int, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError("text is required")
    if len(result) > maximum:
        raise ValueError("text is too long")
    return result


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
        return self._customer(actor_id, active=False)

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

        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_servers "
                "(tenant_id, label, panel_kind, provider_kind, endpoint, "
                "admin_path, user_path, xui_flavor, xui_inbound_ids, "
                "xui_public_origin, xui_sub_path, xnet_inbound_ids, "
                "xnet_public_origin, xnet_sub_port, xnet_sub_path, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
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
                    now,
                    now,
                ),
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
                "ORDER BY s.is_default DESC, s.id ASC",
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
    def provision_pending_subscription(self, actor_id: int, *, subscription_id: int) -> dict[str, Any]:
        """Provision through the tenant's deterministic sales server selection."""
        self._admin(actor_id)
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
        if flavor == "sanaei":
            token = str(api_token or "").strip()
            if not token:
                raise TenantBusinessError("Sanaei API token is required")
            material = {
                "version": 1,
                "flavor": "sanaei",
                "api_token": token,
            }
        elif flavor == "alireza":
            user = str(username or "").strip()
            passwd = str(password or "").strip()
            if not user or not passwd:
                raise TenantBusinessError(
                    "Alireza username and password are required"
                )
            material = {
                "version": 1,
                "flavor": "alireza",
                "username": user,
                "password": passwd,
                "secret_header": str(secret_header or "").strip(),
            }
        else:
            raise TenantBusinessError("X-UI flavor is not configured")
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
        return PanelTarget(
            kind=str(server.get("panel_kind") or "").strip().lower(),
            endpoint=str(server.get("endpoint") or "").strip(),
            admin_path=str(server.get("admin_path") or "").strip(),
            user_path=str(server.get("user_path") or "").strip(),
            xui_flavor=str(server.get("xui_flavor") or "").strip().lower(),
            xui_inbound_ids=str(server.get("xui_inbound_ids") or "").strip(),
            xui_public_origin=str(server.get("xui_public_origin") or "").strip(),
            xui_sub_path=str(server.get("xui_sub_path") or "").strip(),
            xnet_inbound_ids=str(server.get("xnet_inbound_ids") or "").strip(),
            xnet_public_origin=str(server.get("xnet_public_origin") or "").strip(),
            xnet_sub_port=int(server.get("xnet_sub_port") or 0),
            xnet_sub_path=str(server.get("xnet_sub_path") or "").strip(),
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
    def add_node(self, actor_id: int, *, label: str, server_id: int | None = None, location: str = "") -> dict[str, Any]:
        self._admin(actor_id)
        if server_id is not None:
            self.server(int(server_id))
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_nodes (tenant_id, server_id, label, location, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (self.tenant_id, int(server_id) if server_id else None, _text(label, 80), _text(location, 80, required=False) or None, now, now),
            )
        row = self.conn.execute("SELECT * FROM tenant_nodes WHERE id = ? AND tenant_id = ?", (int(cursor.lastrowid or 0), self.tenant_id)).fetchone()
        assert row is not None
        return dict(row)

    def list_nodes(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT n.*, s.label AS server_label, "
                "COALESCE(s.provider_kind,s.panel_kind) AS provider_kind "
                "FROM tenant_nodes n "
                "LEFT JOIN tenant_servers s "
                "ON s.id=n.server_id AND s.tenant_id=n.tenant_id "
                "WHERE n.tenant_id=? ORDER BY n.id DESC",
                (self.tenant_id,),
            ).fetchall()
        ]
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
                "WHERE s.tenant_id=? AND s.status='active' "
                "AND (s.id=? OR n.id IS NOT NULL) "
                "AND COALESCE(s.provider_kind,s.panel_kind) "
                "IN ('hiddify','xui','xnet') "
                "ORDER BY CASE WHEN s.id=? THEN 0 ELSE 1 END, s.id",
                (
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
    ) -> None:
        now = iso_utc(utcnow())
        self.conn.execute(
            "INSERT INTO tenant_subscription_nodes "
            "(tenant_id, subscription_id, server_id, external_ref, is_primary, "
            "status, usage_bytes, last_online, last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, subscription_id, server_id) DO UPDATE SET "
            "external_ref=excluded.external_ref, "
            "is_primary=excluded.is_primary, status=excluded.status, "
            "usage_bytes=excluded.usage_bytes, last_online=excluded.last_online, "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (
                self.tenant_id,
                int(subscription_id),
                int(server_id),
                _text(external_ref, 255, required=False) or None,
                1 if is_primary else 0,
                str(status),
                max(0, int(usage_bytes)),
                _text(last_online, 80, required=False) or None,
                _text(last_error, 300, required=False) or None,
                now,
                now,
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

    def _smart_url(self, *, subscription_id: int, label: str = "") -> str:
        link = self._ensure_subscription_smart_link(
            subscription_id=int(subscription_id), label=label
        )
        public_base = str(
            os.getenv("SMART_SUB_PUBLIC_BASE_URL", "") or ""
        ).strip()
        if not public_base:
            return ""
        from TenantRuntime.smart_subscription import smart_subscription_url

        return smart_subscription_url(public_base, str(link["code"]))

    def repair_subscription_nodes(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, int]:
        subscription = self._admin_subscription(actor_id, subscription_id)
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

    def add_plan(self, actor_id: int, *, name: str, traffic_gb: int, duration_days: int, price: int, currency: str = "IRR") -> dict[str, Any]:
        self._admin(actor_id)
        if min(int(traffic_gb), int(duration_days)) <= 0 or int(price) < 0:
            raise ValueError("invalid plan values")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_sale_plans (tenant_id, name, traffic_gb, duration_days, price, currency, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (self.tenant_id, _text(name, 80), int(traffic_gb), int(duration_days), int(price), _text(currency, 8).upper(), now, now),
            )
        return self.plan(int(cursor.lastrowid or 0), public=False)

    def plan(self, plan_id: int, *, public: bool = True) -> dict[str, Any]:
        query = "SELECT * FROM tenant_sale_plans WHERE id = ? AND tenant_id = ?"
        args: list[Any] = [int(plan_id), self.tenant_id]
        if public:
            query += " AND status = 'active'"
        row = self.conn.execute(query, tuple(args)).fetchone()
        if row is None:
            raise TenantBusinessError("plan not found")
        return dict(row)

    def list_plans(self, *, public: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM tenant_sale_plans WHERE tenant_id = ?"
        if public:
            query += " AND status = 'active'"
        query += " ORDER BY price, id"
        return [dict(row) for row in self.conn.execute(query, (self.tenant_id,)).fetchall()]

    def add_payment_method(self, actor_id: int, *, kind: str, title: str, currency: str, destination: str, network: str = "", instructions: str = "") -> dict[str, Any]:
        self._admin(actor_id)
        if kind not in ("card", "crypto"):
            raise ValueError("invalid payment kind")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_payment_methods (tenant_id, kind, title, currency, destination, network, instructions, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (self.tenant_id, kind, _text(title, 80), _text(currency, 8).upper(), _text(destination, 180), _text(network, 40, required=False) or None, _text(instructions, 500, required=False), now, now),
            )
        return self.method(int(cursor.lastrowid or 0), currency=None)

    def method(self, method_id: int, *, currency: str | None) -> dict[str, Any]:
        query = "SELECT * FROM tenant_payment_methods WHERE id = ? AND tenant_id = ? AND status = 'active'"
        args: list[Any] = [int(method_id), self.tenant_id]
        if currency is not None:
            query += " AND currency = ?"; args.append(str(currency).upper())
        row = self.conn.execute(query, tuple(args)).fetchone()
        if row is None:
            raise TenantBusinessError("payment method not found")
        return dict(row)

    def list_methods(self, *, currency: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tenant_payment_methods WHERE tenant_id = ? AND status = 'active'"; args: list[Any] = [self.tenant_id]
        if currency:
            query += " AND currency = ?"; args.append(str(currency).upper())
        return [dict(row) for row in self.conn.execute(query + " ORDER BY id", tuple(args)).fetchall()]

    def create_order(self, actor_id: int, plan_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        plan = self.plan(plan_id, public=True)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_orders (tenant_id, customer_id, plan_id, amount, currency, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    int(plan["id"]),
                    int(plan["price"]),
                    str(plan["currency"]),
                    now,
                    now,
                ),
            )
        return self.order(actor_id, int(cursor.lastrowid or 0))

    def create_renewal_order(
        self, actor_id: int, *, subscription_id: int, plan_id: int
    ) -> dict[str, Any]:
        """Create a normal payable order linked to one existing subscription."""
        customer = self._customer(actor_id)
        plan = self.plan(plan_id, public=True)
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
                "(tenant_id, customer_id, plan_id, amount, currency, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?)",
                (
                    self.tenant_id,
                    int(customer["id"]),
                    int(plan["id"]),
                    int(plan["price"]),
                    str(plan["currency"]),
                    now,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid or 0)
            self.conn.execute(
                "INSERT INTO tenant_renewal_orders (order_id, tenant_id, subscription_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (order_id, self.tenant_id, int(subscription_id), now),
            )
        return self.order(actor_id, order_id)
    def order(self, actor_id: int, order_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, p.traffic_gb, p.duration_days, "
            "CASE WHEN ro.order_id IS NULL THEN 'purchase' ELSE 'renewal' END AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o JOIN tenant_sale_plans p ON p.id=o.plan_id "
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
            "CASE WHEN ro.order_id IS NULL THEN 'purchase' ELSE 'renewal' END AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? ORDER BY o.id DESC",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_fulfillment_pending_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute(
            "SELECT o.id, o.customer_id, o.plan_id, o.status, c.display_name, p.name AS plan_name, "
            "CASE WHEN ro.order_id IS NULL THEN 'purchase' ELSE 'renewal' END AS operation, "
            "ro.subscription_id AS renewal_subscription_id "
            "FROM tenant_orders o "
            "JOIN tenant_customers c ON c.id=o.customer_id "
            "JOIN tenant_sale_plans p ON p.id=o.plan_id "
            "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
            "WHERE o.tenant_id=? AND o.status='paid' ORDER BY o.id",
            (self.tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    def submit_receipt(self, actor_id: int, *, order_id: int, method_id: int, reference: str | None = None, telegram_file_id: str | None = None) -> dict[str, Any]:
        order = self.order(actor_id, order_id)
        if order["status"] != "pending_payment": raise TenantBusinessError("order is not awaiting payment")
        method = self.method(method_id, currency=str(order["currency"]))
        ref = _text(reference, 160, required=False) or None; file_id = _text(telegram_file_id, 256, required=False) or None
        if not ref and not file_id: raise ValueError("receipt is required")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute("UPDATE tenant_orders SET status='payment_review', updated_at=? WHERE id=? AND tenant_id=? AND status='pending_payment'", (now, int(order_id), self.tenant_id))
            if changed.rowcount != 1: raise TenantBusinessError("order state changed")
            cursor = self.conn.execute("INSERT INTO tenant_receipts (tenant_id, order_id, payment_method_id, reference, telegram_file_id, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)", (self.tenant_id, int(order_id), int(method["id"]), ref, file_id, now))
        return {"id": int(cursor.lastrowid or 0), "order_id": int(order_id)}

    def review_receipt(self, actor_id: int, receipt_id: int, *, approve: bool) -> dict[str, Any]:
        """Approve money first; purchase/renewal fulfillment remains retryable."""
        self._admin(actor_id)
        now = iso_utc(utcnow())
        subscription_id: int | None = None
        operation = "purchase"
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT r.*, o.customer_id, o.plan_id, o.status AS order_status, "
                "p.traffic_gb, p.duration_days, ro.subscription_id AS renewal_subscription_id "
                "FROM tenant_receipts r "
                "JOIN tenant_orders o ON o.id=r.order_id "
                "JOIN tenant_sale_plans p ON p.id=o.plan_id "
                "LEFT JOIN tenant_renewal_orders ro ON ro.order_id=o.id AND ro.tenant_id=o.tenant_id "
                "WHERE r.id=? AND r.tenant_id=?",
                (int(receipt_id), self.tenant_id),
            ).fetchone()
            if row is None:
                raise TenantBusinessError("receipt not found")
            receipt = dict(row)
            if receipt["status"] != "pending" or receipt["order_status"] != "payment_review":
                raise TenantBusinessError("receipt was already reviewed")
            changed = self.conn.execute(
                "UPDATE tenant_receipts SET status=?, reviewed_by=?, reviewed_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending'",
                (
                    "approved" if approve else "rejected",
                    int(actor_id),
                    now,
                    int(receipt_id),
                    self.tenant_id,
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("receipt state changed")

            target = "rejected"
            if approve:
                target = "paid"
                if receipt["renewal_subscription_id"] is not None:
                    operation = "renewal"
                    subscription_id = int(receipt["renewal_subscription_id"])
                    owned = self.conn.execute(
                        "SELECT 1 FROM tenant_subscriptions "
                        "WHERE id=? AND tenant_id=? AND customer_id=?",
                        (
                            subscription_id,
                            self.tenant_id,
                            int(receipt["customer_id"]),
                        ),
                    ).fetchone()
                    if owned is None:
                        raise TenantBusinessError("renewal subscription is unavailable")
                else:
                    expires = iso_utc(
                        utcnow() + timedelta(days=int(receipt["duration_days"]))
                    )
                    cursor = self.conn.execute(
                        "INSERT INTO tenant_subscriptions "
                        "(tenant_id, customer_id, plan_id, order_id, server_id, status, usage_bytes, traffic_bytes, expires_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, NULL, 'pending_provisioning', 0, ?, ?, ?, ?)",
                        (
                            self.tenant_id,
                            int(receipt["customer_id"]),
                            int(receipt["plan_id"]),
                            int(receipt["order_id"]),
                            int(receipt["traffic_gb"]) * 1024 * 1024 * 1024,
                            expires,
                            now,
                            now,
                        ),
                    )
                    subscription_id = int(cursor.lastrowid or 0)

            changed = self.conn.execute(
                "UPDATE tenant_orders SET status=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='payment_review'",
                (target, now, int(receipt["order_id"]), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("order state changed")
        return {
            "order_id": int(receipt["order_id"]),
            "status": target,
            "customer_id": int(receipt["customer_id"]),
            "subscription_id": subscription_id,
            "operation": operation,
        }

    def fulfill_paid_order(self, actor_id: int, *, order_id: int) -> dict[str, Any]:
        """Fulfill one approved purchase or renewal without losing retryability."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.traffic_gb, p.duration_days, "
            "ro.subscription_id AS renewal_subscription_id "
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

    def list_subscriptions(self, actor_id: int) -> list[dict[str, Any]]:
        customer = self._customer(actor_id)
        rows = self.conn.execute("SELECT s.*, p.name AS plan_name FROM tenant_subscriptions s JOIN tenant_sale_plans p ON p.id=s.plan_id WHERE s.tenant_id=? AND s.customer_id=? ORDER BY s.id DESC", (self.tenant_id, int(customer["id"]))).fetchall()
        return [dict(row) for row in rows]

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
            "subscription_url": smart_url or str(result.subscription_url or ""),
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
        selected: list[dict[str, Any]] = hot[:batch]
        next_cursor = max(0, int(cursor))
        if len(selected) < batch and normal:
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
        mappings = [
            row
            for row in self.conn.execute(
                "SELECT * FROM tenant_subscription_nodes "
                "WHERE tenant_id=? AND subscription_id=? "
                "AND external_ref IS NOT NULL "
                "ORDER BY is_primary DESC, id",
                (self.tenant_id, int(subscription_id)),
            ).fetchall()
            if int(row["server_id"]) in desired_ids
        ]
        if not mappings:
            raise TenantBusinessError("subscription has no panel targets")

        snapshots: list[tuple[sqlite3.Row, Any]] = []
        failed_mappings: list[sqlite3.Row] = []
        primary_usage = None
        total_usage = 0
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
                            else ("active" if bool(usage.active) else "disabled")
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
        idempotency_key: str = "",
        fulfillment_order_id: int | None = None,
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        if int(traffic_gb) <= 0 or int(duration_days) <= 0:
            raise ValueError("invalid renewal values")
        self._ensure_primary_subscription_node(subscription)
        next_plan_id = int(plan_id or subscription["plan_id"])
        if plan_id is not None:
            self.plan(next_plan_id, public=False)
        traffic_bytes = int(traffic_gb) * 1024 * 1024 * 1024
        expires_at = iso_utc(utcnow() + timedelta(days=int(duration_days)))
        base_key = str(idempotency_key or "")
        _, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            user = self.panel_adapter.renew(
                target=target,
                secret=secret,
                external_ref=str(subscription["external_ref"]),
                request=RenewRequest(
                    traffic_bytes=traffic_bytes,
                    duration_days=int(duration_days),
                    expires_at=expires_at,
                    reset_usage=True,
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
                "status='active', expired_at=NULL, last_online=?, last_synced_at=?, updated_at=? "
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
                        duration_days=int(duration_days),
                        expires_at=expires_at,
                        reset_usage=True,
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
                "node_errors": secondary_errors,
                "subscription_url": self._smart_url(
                    subscription_id=int(subscription_id)
                )
                or str(user.subscription_url or ""),
            }
        )
        return result
    def set_subscription_enabled(
        self, actor_id: int, *, subscription_id: int, enabled: bool
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
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
    def subscription_link(self, actor_id: int, *, subscription_id: int) -> str:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        subscription = dict(row)
        if subscription["status"] != "active" or self._subscription_is_due(
            subscription
        ):
            raise TenantBusinessError("subscription is not active")
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        smart_url = self._smart_url(subscription_id=int(subscription_id))
        if smart_url:
            return smart_url
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
    def create_ticket(self, actor_id: int, *, subject: str, body: str) -> dict[str, Any]:
        customer = self._customer(actor_id); now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute("INSERT INTO tenant_tickets (tenant_id, customer_id, subject, body, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)", (self.tenant_id, int(customer["id"]), _text(subject, 100), _text(body, 2000), now, now))
        return {"id": int(cursor.lastrowid or 0), "status": "open"}

    def list_tickets_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute("SELECT t.*, c.display_name FROM tenant_tickets t JOIN tenant_customers c ON c.id=t.customer_id WHERE t.tenant_id=? ORDER BY t.id DESC", (self.tenant_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_smart_link(self, actor_id: int, *, label: str, target: str) -> dict[str, Any]:
        self._admin(actor_id); now = iso_utc(utcnow()); code = secrets.token_urlsafe(7)
        with transaction(self.conn):
            cursor = self.conn.execute("INSERT INTO tenant_smart_links (tenant_id, code, label, target, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", (self.tenant_id, code, _text(label, 80), _text(target, 250), now, now))
        return {"id": int(cursor.lastrowid or 0), "code": code, "label": _text(label, 80), "target": _text(target, 250)}

    def list_smart_links(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        public_base = str(
            os.getenv("SMART_SUB_PUBLIC_BASE_URL", "") or ""
        ).strip()
        from TenantRuntime.smart_subscription import smart_subscription_url

        items: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT * FROM tenant_smart_links "
            "WHERE tenant_id=? ORDER BY id DESC",
            (self.tenant_id,),
        ).fetchall():
            item = dict(row)
            item["public_url"] = (
                smart_subscription_url(public_base, str(item["code"]))
                if str(item["target"]).startswith("subscription:")
                else ""
            )
            items.append(item)
        return items
