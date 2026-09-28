"""Tenant-scoped business service used by each dedicated runtime bot.

The service is permanently bound to one tenant. Every read/write includes the
bound tenant id, so a callback id from another customer can never cross into
this tenant's data. Panel credentials are encrypted at rest and passed only at
the adapter boundary; concrete provider HTTP dialects remain separate.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from Database.connection import transaction
from Shared.crypto import TokenCipher, TokenCipherError, fingerprint_token
from Shared.timeutils import iso_utc, utcnow
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
    ) -> dict[str, Any]:
        self._admin(actor_id)
        panel_kind = str(panel_kind or "").strip().lower()
        if panel_kind not in ("manual", "hiddify", "xui"):
            raise ValueError("invalid panel kind")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_servers "
                "(tenant_id, label, panel_kind, endpoint, admin_path, user_path, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    self.tenant_id,
                    _text(label, 80),
                    panel_kind,
                    _text(endpoint, 250, required=False) or None,
                    _text(admin_path, 160, required=False) or None,
                    _text(user_path, 160, required=False) or None,
                    now,
                    now,
                ),
            )
        return self.server(int(cursor.lastrowid or 0))

    def server(self, server_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM tenant_servers WHERE id = ? AND tenant_id = ?", (int(server_id), self.tenant_id)).fetchone()
        if row is None:
            raise TenantBusinessError("server not found")
        return dict(row)

    def list_servers(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM tenant_servers WHERE tenant_id = ? ORDER BY id DESC", (self.tenant_id,)).fetchall()]

    def set_default_server(self, actor_id: int, *, server_id: int) -> dict[str, Any]:
        """Choose the only server automatic sales provisioning may target."""
        self._admin(actor_id)
        server = self.server(int(server_id))
        if server["status"] != "active" or server["panel_kind"] != "hiddify":
            raise TenantBusinessError("default provisioning server must be an active hiddify server")
        if not self.panel_status(int(server_id))["configured"]:
            raise TenantBusinessError("default provisioning server is not configured")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_servers SET is_default=0, updated_at=? WHERE tenant_id=? AND is_default=1",
                (now, self.tenant_id),
            )
            changed = self.conn.execute(
                "UPDATE tenant_servers SET is_default=1, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='active' AND panel_kind='hiddify'",
                (now, int(server_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("server is not eligible for automatic provisioning")
        return self.server(int(server_id))

    def _provisioning_server(self) -> dict[str, Any]:
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT s.* FROM tenant_servers s "
                "JOIN tenant_panel_credentials c ON c.server_id=s.id AND c.tenant_id=s.tenant_id "
                "WHERE s.tenant_id=? AND s.status='active' AND s.panel_kind='hiddify' "
                "ORDER BY s.is_default DESC, s.id ASC",
                (self.tenant_id,),
            ).fetchall()
        ]
        if not rows:
            raise TenantBusinessError("no configured hiddify server is available")
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
    def set_panel_credential(self, actor_id: int, *, server_id: int, secret: str) -> dict[str, Any]:
        """Replace one server secret, returning only safe configuration status."""
        self._admin(actor_id)
        server = self.server(server_id)
        if str(server["panel_kind"]) == "manual" or not str(server.get("endpoint") or "").strip():
            raise TenantBusinessError("server has no configured panel endpoint")
        if self.secret_cipher is None:
            raise TenantBusinessError("panel credential encryption is unavailable")
        try:
            encrypted = self.secret_cipher.encrypt_secret(secret)
            digest = fingerprint_token(secret)
        except TokenCipherError as exc:
            raise TenantBusinessError("invalid panel credential") from exc
        finally:
            secret = ""
        now = iso_utc(utcnow())
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO tenant_panel_credentials (server_id, tenant_id, encrypted_secret, secret_fingerprint, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_id) DO UPDATE SET encrypted_secret=excluded.encrypted_secret, secret_fingerprint=excluded.secret_fingerprint, updated_at=excluded.updated_at",
                    (int(server_id), self.tenant_id, encrypted, digest, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise TenantBusinessError("panel credential is already used by this tenant") from exc
        return {"server_id": int(server_id), "configured": True}

    def panel_status(self, server_id: int) -> dict[str, Any]:
        self.server(server_id)
        row = self.conn.execute(
            "SELECT 1 FROM tenant_panel_credentials WHERE server_id=? AND tenant_id=?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        return {"server_id": int(server_id), "configured": row is not None}

    def _panel_target(self, server: dict[str, Any]) -> PanelTarget:
        return PanelTarget(
            kind=str(server.get("panel_kind") or "").strip().lower(),
            endpoint=str(server.get("endpoint") or "").strip(),
            admin_path=str(server.get("admin_path") or "").strip(),
            user_path=str(server.get("user_path") or "").strip(),
        )

    def _panel_material(
        self, server_id: int
    ) -> tuple[dict[str, Any], PanelTarget, str]:
        server = self.server(server_id)
        endpoint = str(server.get("endpoint") or "").strip()
        if str(server["status"]) != "active" or not endpoint or str(server["panel_kind"]) == "manual":
            raise TenantBusinessError("server is not ready for panel provisioning")
        if self.secret_cipher is None:
            raise TenantBusinessError("panel credential encryption is unavailable")
        row = self.conn.execute(
            "SELECT encrypted_secret FROM tenant_panel_credentials WHERE server_id=? AND tenant_id=?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("panel credential is not configured")
        try:
            secret = self.secret_cipher.decrypt_secret(str(row["encrypted_secret"]))
            return server, self._panel_target(server), secret
        except TokenCipherError as exc:
            raise TenantBusinessError("panel credential cannot be decrypted") from exc

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
        return [dict(row) for row in self.conn.execute("SELECT * FROM tenant_nodes WHERE tenant_id = ? ORDER BY id DESC", (self.tenant_id,)).fetchall()]

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
        customer = self._customer(actor_id); plan = self.plan(plan_id, public=True); now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO tenant_orders (tenant_id, customer_id, plan_id, amount, currency, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?)",
                (self.tenant_id, int(customer["id"]), int(plan["id"]), int(plan["price"]), str(plan["currency"]), now, now),
            )
        return self.order(actor_id, int(cursor.lastrowid or 0))

    def order(self, actor_id: int, order_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, p.traffic_gb, p.duration_days FROM tenant_orders o JOIN tenant_sale_plans p ON p.id = o.plan_id"
            " WHERE o.id = ? AND o.tenant_id = ? AND o.customer_id = ?", (int(order_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None: raise TenantBusinessError("order not found")
        return dict(row)

    def list_orders_admin(self, actor_id: int) -> list[dict[str, Any]]:
        self._admin(actor_id)
        rows = self.conn.execute("SELECT o.*, c.display_name, p.name AS plan_name FROM tenant_orders o JOIN tenant_customers c ON c.id=o.customer_id JOIN tenant_sale_plans p ON p.id=o.plan_id WHERE o.tenant_id=? ORDER BY o.id DESC", (self.tenant_id,)).fetchall()
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
        """Review payment only; remote provisioning is deliberately outside this transaction."""
        self._admin(actor_id)
        now = iso_utc(utcnow())
        subscription_id: int | None = None
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT r.*, o.customer_id, o.plan_id, o.status AS order_status, p.traffic_gb, p.duration_days "
                "FROM tenant_receipts r JOIN tenant_orders o ON o.id=r.order_id "
                "JOIN tenant_sale_plans p ON p.id=o.plan_id "
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
                ("approved" if approve else "rejected", int(actor_id), now, int(receipt_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("receipt state changed")

            target = "rejected"
            if approve:
                expires = iso_utc(utcnow() + timedelta(days=int(receipt["duration_days"])))
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
                target = "paid"

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
        }
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
        """Provision a pending subscription, then atomically mark the paid order fulfilled.

        The provider call happens outside SQLite. Provider adapters receive a stable
        idempotency key, so a retry cannot intentionally create a second remote user.
        """
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions WHERE id=? AND tenant_id=? AND status='pending_provisioning'",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription is not awaiting provisioning")
        subscription = dict(row)
        plan = self.plan(int(subscription["plan_id"]), public=False)
        server, target, secret = self._panel_material(int(server_id))
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
                    idempotency_key=f"tenant:{self.tenant_id}:subscription:{int(subscription_id)}",
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
                "UPDATE tenant_subscriptions SET server_id=?, external_ref=?, status='active', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending_provisioning'",
                (int(server_id), external_ref, now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed during provisioning")
            if subscription.get("order_id") is not None:
                order_changed = self.conn.execute(
                    "UPDATE tenant_orders SET status='fulfilled', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status IN ('paid', 'fulfilled')",
                    (now, int(subscription["order_id"]), self.tenant_id),
                )
                if order_changed.rowcount != 1:
                    raise TenantBusinessError("order state changed during provisioning")
        return {
            "id": int(subscription_id),
            "order_id": int(subscription["order_id"]) if subscription.get("order_id") is not None else None,
            "status": "active",
            "external_ref": external_ref,
            "subscription_url": str(result.subscription_url or ""),
        }
    def sync_subscription_usage(self, actor_id: int, *, subscription_id: int) -> dict[str, Any]:
        """Read usage from a configured provider, then atomically persist it."""
        self._admin(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions WHERE id=? AND tenant_id=? AND status='active'",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if row is None or row["server_id"] is None or not row["external_ref"]:
            raise TenantBusinessError("subscription cannot be synchronized")
        subscription = dict(row)
        server, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            usage = self.panel_adapter.usage(
                target=target, secret=secret, external_ref=str(subscription["external_ref"])
            )
        except PanelError as exc:
            raise TenantBusinessError("panel usage synchronization failed") from exc
        finally:
            secret = ""
        if int(usage.usage_bytes) < 0:
            raise TenantBusinessError("panel returned invalid usage")
        now = iso_utc(utcnow())
        state = "active" if bool(usage.active) else "disabled"
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions SET usage_bytes=?, status=?, updated_at=? WHERE id=? AND tenant_id=? AND status='active'",
                (int(usage.usage_bytes), state, now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed during synchronization")
        return {"id": int(subscription_id), "usage_bytes": int(usage.usage_bytes), "status": state}

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
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        if int(traffic_gb) <= 0 or int(duration_days) <= 0:
            raise ValueError("invalid renewal values")
        traffic_bytes = int(traffic_gb) * 1024 * 1024 * 1024
        expires_at = iso_utc(utcnow() + timedelta(days=int(duration_days)))
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
                ),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel renewal failed") from exc
        finally:
            secret = ""
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions SET traffic_bytes=?, usage_bytes=?, expires_at=?, status='active', updated_at=? "
                "WHERE id=? AND tenant_id=? AND server_id=? AND external_ref=?",
                (
                    traffic_bytes,
                    max(0, int(user.usage_bytes)),
                    expires_at,
                    now,
                    int(subscription_id),
                    self.tenant_id,
                    int(subscription["server_id"]),
                    str(subscription["external_ref"]),
                ),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed during renewal")
        result = self._panel_user_dict(user)
        result.update(
            {
                "id": int(subscription_id),
                "status": "active",
                "traffic_bytes": traffic_bytes,
                "expires_at": expires_at,
            }
        )
        return result

    def set_subscription_enabled(
        self, actor_id: int, *, subscription_id: int, enabled: bool
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        _, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            user = self.panel_adapter.set_enabled(
                target=target,
                secret=secret,
                external_ref=str(subscription["external_ref"]),
                enabled=bool(enabled),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel account state change failed") from exc
        finally:
            secret = ""
        state = "active" if bool(enabled) else "disabled"
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                (state, now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
        result = self._panel_user_dict(user)
        result.update({"id": int(subscription_id), "status": state})
        return result

    def delete_subscription_from_panel(
        self, actor_id: int, *, subscription_id: int
    ) -> dict[str, Any]:
        subscription = self._admin_subscription(actor_id, subscription_id)
        if subscription["server_id"] is None or not subscription["external_ref"]:
            raise TenantBusinessError("subscription is not provisioned")
        _, target, secret = self._panel_material(int(subscription["server_id"]))
        try:
            self.panel_adapter.delete_user(
                target=target,
                secret=secret,
                external_ref=str(subscription["external_ref"]),
            )
        except PanelError as exc:
            raise TenantBusinessError("panel user deletion failed") from exc
        finally:
            secret = ""
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_subscriptions SET status='disabled', updated_at=? WHERE id=? AND tenant_id=?",
                (now, int(subscription_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise TenantBusinessError("subscription state changed")
        return {"id": int(subscription_id), "status": "disabled"}

    def subscription_link(self, actor_id: int, *, subscription_id: int) -> str:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT * FROM tenant_subscriptions WHERE id=? AND tenant_id=? AND customer_id=?",
            (int(subscription_id), self.tenant_id, int(customer["id"])),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("subscription not found")
        subscription = dict(row)
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
        return [dict(row) for row in self.conn.execute("SELECT * FROM tenant_smart_links WHERE tenant_id=? ORDER BY id DESC", (self.tenant_id,)).fetchall()]
