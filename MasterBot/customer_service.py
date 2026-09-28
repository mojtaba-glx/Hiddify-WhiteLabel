"""Customer storefront and owner-side payment operations.

Every customer query is scoped by the Telegram actor id.  Prices and license
durations are always re-read from SQLite; callback payloads contain identifiers
only.  Multi-step balance/payment transitions use one IMMEDIATE transaction.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional

from Database.connection import transaction
from Database.repositories import AuditRepository, LicenseRepository, PlanRepository
from LicenseService.service import renew_license
from MasterBot.service import MasterService, NotFoundError
from MasterBot.platform_settings import PlatformSettingsService
from Provisioning.service import PreparedBot, ProvisioningResult
from Shared.access import require_master_admin
from Shared.timeutils import iso_utc, utcnow


class CustomerPortalError(RuntimeError):
    """A safe storefront error which contains no credentials."""


class CustomerBlockedError(CustomerPortalError):
    pass


class PaymentStateError(CustomerPortalError):
    pass


@dataclass(frozen=True)
class ProvisionedPurchase:
    provisioning: ProvisioningResult
    license: dict[str, Any]


def _clean_text(value: object, *, maximum: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError("required text is missing")
    if len(text) > int(maximum):
        raise ValueError("text is too long")
    return text


def _public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


class CustomerPortalService:
    """Role-separated customer commerce facade for the shared MasterBot."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        master_service: MasterService,
        cache_invalidator: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.conn = conn
        self.master = master_service
        self.master_admin_id = int(master_service.master_admin_id)
        self.cache_invalidator = cache_invalidator
        self.settings = PlatformSettingsService(conn)

    def storefront_settings(self) -> dict[str, Any]:
        return self.settings.all()

    def _customer(self, actor_id: int, *, active: bool = True) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM platform_customers WHERE telegram_user_id = ?",
            (int(actor_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("customer is not registered")
        result = dict(row)
        if active and result["status"] != "active":
            raise CustomerBlockedError("customer account is blocked")
        return result

    def register_customer(
        self, actor_id: int, *, display_name: str, username: Optional[str] = None
    ) -> dict[str, Any]:
        if int(actor_id) <= 0:
            raise ValueError("Telegram user id must be positive")
        name = _clean_text(display_name, maximum=120)
        clean_username = _clean_text(username, maximum=64, required=False) or None
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO platform_customers"
                " (telegram_user_id, display_name, username, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'active', ?, ?)"
                " ON CONFLICT(telegram_user_id) DO UPDATE SET"
                " display_name = excluded.display_name, username = excluded.username,"
                " updated_at = excluded.updated_at",
                (int(actor_id), name, clean_username, now, now),
            )
        return self._customer(int(actor_id), active=False)

    def list_public_plans(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM license_plans"
            " WHERE status = 'active' AND is_public = 1 ORDER BY price ASC, id ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_public_plan(self, plan_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM license_plans WHERE id = ? AND status = 'active' AND is_public = 1",
            (int(plan_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("public plan not found")
        return dict(row)

    def create_purchase_order(self, actor_id: int, plan_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        if not self.settings.get_bool("sales_enabled"):
            raise CustomerPortalError(self.settings.get("maintenance_message"))
        plan = self.get_public_plan(plan_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO customer_orders"
                " (public_id, customer_id, plan_id, tenant_id, kind, amount, currency, status, created_at, updated_at)"
                " VALUES (?, ?, ?, NULL, 'purchase', ?, ?, 'pending_payment', ?, ?)",
                (
                    _public_id("ord"), int(customer["id"]), int(plan["id"]),
                    int(plan["price"]), str(plan["currency"]), now, now,
                ),
            )
        return self.get_order(actor_id, int(cursor.lastrowid or 0))

    def create_renewal_order(
        self, actor_id: int, *, tenant_id: int, plan_id: int
    ) -> dict[str, Any]:
        customer = self._customer(actor_id)
        if not self.settings.get_bool("sales_enabled"):
            raise CustomerPortalError(self.settings.get("maintenance_message"))
        tenant = self.conn.execute(
            "SELECT * FROM tenants WHERE id = ? AND owner_telegram_id = ?",
            (int(tenant_id), int(actor_id)),
        ).fetchone()
        if tenant is None:
            raise NotFoundError("owned service not found")
        current = LicenseRepository(self.conn).latest_by_tenant(int(tenant_id))
        if current is None:
            raise CustomerPortalError("service has no renewable license")
        plan = self.get_public_plan(plan_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO customer_orders"
                " (public_id, customer_id, plan_id, tenant_id, kind, amount, currency, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'renewal', ?, ?, 'pending_payment', ?, ?)",
                (
                    _public_id("ren"), int(customer["id"]), int(plan["id"]),
                    int(tenant_id), int(plan["price"]), str(plan["currency"]), now, now,
                ),
            )
        return self.get_order(actor_id, int(cursor.lastrowid or 0))

    def create_wallet_topup(self, actor_id: int, *, currency: str, amount: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        code = _clean_text(currency, maximum=8).upper()
        if int(amount) <= 0:
            raise ValueError("top-up amount must be positive")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO customer_orders"
                " (public_id, customer_id, plan_id, tenant_id, kind, amount, currency, status, created_at, updated_at)"
                " VALUES (?, ?, NULL, NULL, 'wallet_topup', ?, ?, 'pending_payment', ?, ?)",
                (_public_id("top"), int(customer["id"]), int(amount), code, now, now),
            )
        return self.get_order(actor_id, int(cursor.lastrowid or 0))

    def get_order(self, actor_id: int, order_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT o.*, p.name AS plan_name, p.duration_days, p.trial_days"
            " FROM customer_orders AS o LEFT JOIN license_plans AS p ON p.id = o.plan_id"
            " WHERE o.id = ? AND o.customer_id = ?",
            (int(order_id), int(customer["id"])),
        ).fetchone()
        if row is None:
            raise NotFoundError("order not found")
        return dict(row)

    def list_orders(self, actor_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        customer = self._customer(actor_id)
        rows = self.conn.execute(
            "SELECT o.*, p.name AS plan_name FROM customer_orders AS o"
            " LEFT JOIN license_plans AS p ON p.id = o.plan_id"
            " WHERE o.customer_id = ? ORDER BY o.id DESC LIMIT ?",
            (int(customer["id"]), max(1, min(int(limit), 50))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_services(self, actor_id: int) -> list[dict[str, Any]]:
        self._customer(actor_id)
        rows = self.conn.execute(
            "SELECT t.*, l.id AS license_id, l.status AS license_status, l.expires_at,"
            " p.name AS plan_name FROM tenants AS t"
            " LEFT JOIN licenses AS l ON l.id ="
            "   (SELECT ll.id FROM licenses AS ll WHERE ll.tenant_id = t.id ORDER BY ll.id DESC LIMIT 1)"
            " LEFT JOIN license_plans AS p ON p.id = l.plan_id"
            " WHERE t.owner_telegram_id = ? ORDER BY t.id DESC",
            (int(actor_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_payment_methods(self, currency: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM payment_methods WHERE status = 'active' AND currency = ? ORDER BY id",
            (str(currency).upper(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_payment_method(self, method_id: int, *, currency: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM payment_methods WHERE id = ? AND status = 'active' AND currency = ?",
            (int(method_id), str(currency).upper()),
        ).fetchone()
        if row is None:
            raise NotFoundError("payment method not found")
        return dict(row)

    def submit_receipt(
        self,
        actor_id: int,
        *,
        order_id: int,
        payment_method_id: int,
        reference: Optional[str] = None,
        telegram_file_id: Optional[str] = None,
    ) -> dict[str, Any]:
        order = self.get_order(actor_id, order_id)
        if order["status"] != "pending_payment":
            raise PaymentStateError("order is not awaiting payment")
        method = self.get_payment_method(payment_method_id, currency=str(order["currency"]))
        clean_reference = _clean_text(reference, maximum=160, required=False) or None
        clean_file = _clean_text(telegram_file_id, maximum=256, required=False) or None
        if clean_reference is None and clean_file is None:
            raise ValueError("payment proof is required")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = 'payment_review', updated_at = ?"
                " WHERE id = ? AND customer_id = ? AND status = 'pending_payment'",
                (now, int(order_id), int(order["customer_id"])),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("order state changed")
            cursor = self.conn.execute(
                "INSERT INTO payment_receipts"
                " (order_id, payment_method_id, reference, telegram_file_id, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (int(order_id), int(method["id"]), clean_reference, clean_file, now),
            )
        row = self.conn.execute(
            "SELECT * FROM payment_receipts WHERE id = ?", (int(cursor.lastrowid or 0),)
        ).fetchone()
        assert row is not None
        return dict(row)

    def wallet_balance(self, actor_id: int, currency: str) -> int:
        customer = self._customer(actor_id)
        row = self.conn.execute(
            "SELECT balance FROM wallet_accounts WHERE customer_id = ? AND currency = ?",
            (int(customer["id"]), str(currency).upper()),
        ).fetchone()
        return int(row["balance"] if row else 0)

    def pay_order_from_wallet(self, actor_id: int, order_id: int) -> dict[str, Any]:
        order = self.get_order(actor_id, order_id)
        if order["status"] != "pending_payment" or order["kind"] == "wallet_topup":
            raise PaymentStateError("order cannot be paid from wallet")
        amount = int(order["amount"])
        currency = str(order["currency"])
        customer_id = int(order["customer_id"])
        now = iso_utc(utcnow())
        final_status = "paid"
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO wallet_accounts (customer_id, currency, balance, updated_at)"
                " VALUES (?, ?, 0, ?) ON CONFLICT(customer_id, currency) DO NOTHING",
                (customer_id, currency, now),
            )
            changed = self.conn.execute(
                "UPDATE wallet_accounts SET balance = balance - ?, updated_at = ?"
                " WHERE customer_id = ? AND currency = ? AND balance >= ?",
                (amount, now, customer_id, currency, amount),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("wallet balance is insufficient")
            balance = self.wallet_balance(actor_id, currency)
            self.conn.execute(
                "INSERT INTO wallet_transactions"
                " (customer_id, currency, amount, kind, order_id, idempotency_key, resulting_balance, created_at)"
                " VALUES (?, ?, ?, 'purchase', ?, ?, ?, ?)",
                (customer_id, currency, -amount, int(order_id), f"wallet-order:{order_id}", balance, now),
            )
            if order["kind"] == "renewal":
                plan = PlanRepository(self.conn).get_by_id(int(order["plan_id"]))
                tenant_id = int(order["tenant_id"] or 0)
                license_row = LicenseRepository(self.conn).latest_by_tenant(tenant_id)
                if plan is None or tenant_id <= 0 or license_row is None:
                    raise PaymentStateError("renewal target is unavailable")
                renew_license(
                    self.conn,
                    license_id=int(license_row["id"]),
                    tenant_id=tenant_id,
                    extra_days=int(plan["duration_days"]),
                    actor_id=int(actor_id),
                )
                final_status = "fulfilled"
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = ?, updated_at = ?"
                " WHERE id = ? AND customer_id = ? AND status = 'pending_payment'",
                (final_status, now, int(order_id), customer_id),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("order state changed")
        return self.get_order(actor_id, order_id)

    def claim_trial(self, actor_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        if not self.settings.get_bool("trial_enabled"):
            raise CustomerPortalError("trial is disabled")
        plan_row = self.conn.execute(
            "SELECT * FROM license_plans WHERE status = 'active' AND is_public = 1"
            " AND trial_days > 0 ORDER BY id LIMIT 1"
        ).fetchone()
        if plan_row is None:
            raise CustomerPortalError("trial is not configured")
        plan = dict(plan_row)
        now = iso_utc(utcnow())
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO customer_orders"
                    " (public_id, customer_id, plan_id, tenant_id, kind, amount, currency, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, NULL, 'trial', 0, ?, 'paid', ?, ?)",
                    (_public_id("try"), int(customer["id"]), int(plan["id"]), str(plan["currency"]), now, now),
                )
                order_id = int(cursor.lastrowid or 0)
                self.conn.execute(
                    "INSERT INTO trial_claims"
                    " (customer_id, plan_id, order_id, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'eligible', ?, ?)",
                    (int(customer["id"]), int(plan["id"]), order_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise CustomerPortalError("trial was already claimed") from exc
        return self.get_order(actor_id, order_id)

    def setup_candidates(self, actor_id: int) -> list[dict[str, Any]]:
        customer = self._customer(actor_id)
        rows = self.conn.execute(
            "SELECT o.*, p.name AS plan_name FROM customer_orders AS o"
            " JOIN license_plans AS p ON p.id = o.plan_id"
            " WHERE o.customer_id = ? AND o.tenant_id IS NULL"
            " AND o.kind IN ('purchase', 'trial') AND o.status = 'paid' ORDER BY o.id",
            (int(customer["id"]),),
        ).fetchall()
        return [dict(row) for row in rows]

    async def prepare_customer_bot(
        self, actor_id: int, *, order_id: int, role: str, plain_token: str
    ) -> PreparedBot:
        order = self.get_order(actor_id, order_id)
        if order["status"] != "paid" or order["tenant_id"] is not None:
            raise PaymentStateError("order is not ready for setup")
        # The privileged provisioner remains private behind this ownership and
        # paid-order gate; raw tokens are never persisted by the portal flow.
        return await self.master.prepare_tenant_bot(
            self.master_admin_id, role=role, plain_token=plain_token
        )

    def provision_paid_order(
        self,
        actor_id: int,
        *,
        order_id: int,
        name: str,
        slug: str,
        admin_bot: PreparedBot,
        user_bot: PreparedBot,
    ) -> ProvisionedPurchase:
        order = self.get_order(actor_id, order_id)
        if order["status"] != "paid" or order["tenant_id"] is not None:
            raise PaymentStateError("order is not ready for setup")
        plan = PlanRepository(self.conn).get_by_id(int(order["plan_id"]))
        if plan is None:
            raise NotFoundError("plan not found")
        duration = int(plan["duration_days"])
        if order["kind"] == "trial":
            duration = int(plan.get("trial_days") or 0)
            if duration <= 0:
                raise CustomerPortalError("trial is no longer configured")
        now_dt = utcnow()
        with transaction(self.conn):
            fresh = self.get_order(actor_id, order_id)
            if fresh["status"] != "paid" or fresh["tenant_id"] is not None:
                raise PaymentStateError("order state changed")
            result = self.master.provision_tenant_prepared(
                self.master_admin_id,
                name=_clean_text(name, maximum=120),
                slug=_clean_text(slug, maximum=64),
                owner_telegram_id=int(actor_id),
                admin_bot=admin_bot,
                user_bot=user_bot,
            )
            license_row = LicenseRepository(self.conn).create(
                tenant_id=int(result.tenant_id),
                plan_id=int(plan["id"]),
                status="active",
                starts_at=iso_utc(now_dt),
                expires_at=iso_utc(now_dt + timedelta(days=duration)),
            )
            changed = self.conn.execute(
                "UPDATE customer_orders SET tenant_id = ?, status = 'fulfilled', updated_at = ?"
                " WHERE id = ? AND customer_id = ? AND status = 'paid' AND tenant_id IS NULL",
                (int(result.tenant_id), iso_utc(utcnow()), int(order_id), int(order["customer_id"])),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("order state changed")
            if order["kind"] == "trial":
                self.conn.execute(
                    "UPDATE trial_claims SET tenant_id = ?, status = 'issued', updated_at = ?"
                    " WHERE order_id = ? AND customer_id = ? AND status = 'eligible'",
                    (int(result.tenant_id), iso_utc(utcnow()), int(order_id), int(order["customer_id"])),
                )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=int(result.tenant_id),
                action="customer.order_fulfill", entity_type="customer_order",
                entity_id=str(order_id), metadata={"kind": str(order["kind"])},
            )
        return ProvisionedPurchase(provisioning=result, license=license_row)

    # ---- owner-only commerce administration ----

    def add_payment_method(
        self,
        actor_id: int,
        *,
        kind: str,
        title: str,
        currency: str,
        destination: str,
        recipient: Optional[str] = None,
        network: Optional[str] = None,
        instructions: str = "",
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        if kind not in ("card", "crypto"):
            raise ValueError("invalid payment method kind")
        code = _clean_text(currency, maximum=8).upper()
        now = iso_utc(utcnow())
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO payment_methods"
                " (kind, title, currency, destination, recipient, network, instructions, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    kind, _clean_text(title, maximum=80), code,
                    _clean_text(destination, maximum=180),
                    _clean_text(recipient, maximum=100, required=False) or None,
                    _clean_text(network, maximum=40, required=False) or None,
                    _clean_text(instructions, maximum=500, required=False), now, now,
                ),
            )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment_method.create", entity_type="payment_method",
                entity_id=str(int(cursor.lastrowid or 0)),
                metadata={"kind": kind, "currency": code},
            )
        row = self.conn.execute("SELECT * FROM payment_methods WHERE id = ?", (cursor.lastrowid,)).fetchone()
        assert row is not None
        return dict(row)

    def configure_plan_commerce(
        self,
        actor_id: int,
        plan_id: int,
        *,
        currency: str,
        is_public: bool,
        trial_days: int,
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        code = _clean_text(currency, maximum=8).upper()
        if not 3 <= len(code) <= 8 or not code.isalnum():
            raise ValueError("currency must be 3-8 alphanumeric characters")
        if int(trial_days) < 0:
            raise ValueError("trial days must be non-negative")
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE license_plans SET currency = ?, is_public = ?, trial_days = ? WHERE id = ?",
                (code, 1 if is_public else 0, int(trial_days), int(plan_id)),
            )
            if changed.rowcount != 1:
                raise NotFoundError("plan not found")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="plan.commerce_configure", entity_type="license_plan",
                entity_id=str(int(plan_id)),
                metadata={"currency": code, "is_public": bool(is_public), "trial_days": int(trial_days)},
            )
        row = PlanRepository(self.conn).get_by_id(int(plan_id))
        assert row is not None
        return row

    def get_platform_settings(self, actor_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        return self.settings.all()

    def set_platform_text_setting(
        self, actor_id: int, key: str, value: str
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        limits = {
            "store_name": 80,
            "support_contact": 120,
            "maintenance_message": 500,
        }
        if key not in limits:
            raise ValueError("invalid text setting")
        clean_value = "" if key == "support_contact" and str(value).strip() == "-" else value
        result = self.settings.set_text(key, clean_value, maximum=limits[key])
        with transaction(self.conn):
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_setting.update", entity_type="platform_setting",
                entity_id=key, metadata={"configured": bool(result)},
            )
        return self.settings.all()

    def set_platform_bool_setting(
        self, actor_id: int, key: str, enabled: bool
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        self.settings.set_bool(key, bool(enabled))
        with transaction(self.conn):
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_setting.update", entity_type="platform_setting",
                entity_id=key, metadata={"enabled": bool(enabled)},
            )
        return self.settings.all()

    def get_payment_method_admin(self, actor_id: int, method_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        row = self.conn.execute(
            "SELECT * FROM payment_methods WHERE id = ?", (int(method_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError("payment method not found")
        return dict(row)

    def update_payment_method(
        self,
        actor_id: int,
        method_id: int,
        *,
        title: str,
        currency: str,
        destination: str,
        recipient: Optional[str] = None,
        network: Optional[str] = None,
        instructions: str = "",
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        current = self.get_payment_method_admin(actor_id, method_id)
        code = _clean_text(currency, maximum=8).upper()
        if not 3 <= len(code) <= 8 or not code.isalnum():
            raise ValueError("currency must be 3-8 alphanumeric characters")
        clean_recipient = (
            _clean_text(recipient, maximum=100, required=False) or None
            if current["kind"] == "card" else None
        )
        clean_network = (
            _clean_text(network, maximum=40, required=False) or None
            if current["kind"] == "crypto" else None
        )
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE payment_methods SET title = ?, currency = ?, destination = ?,"
                " recipient = ?, network = ?, instructions = ?, updated_at = ? WHERE id = ?",
                (
                    _clean_text(title, maximum=80), code,
                    _clean_text(destination, maximum=180), clean_recipient, clean_network,
                    _clean_text(instructions, maximum=500, required=False),
                    now, int(method_id),
                ),
            )
            if changed.rowcount != 1:
                raise NotFoundError("payment method not found")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment_method.update", entity_type="payment_method",
                entity_id=str(int(method_id)), metadata={"kind": str(current["kind"]), "currency": code},
            )
        return self.get_payment_method_admin(actor_id, method_id)

    def list_all_payment_methods(self, actor_id: int) -> list[dict[str, Any]]:
        require_master_admin(actor_id, self.master_admin_id)
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM payment_methods ORDER BY id DESC"
        ).fetchall()]

    def set_payment_method_status(
        self, actor_id: int, method_id: int, status: str
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        if status not in ("active", "disabled"):
            raise ValueError("invalid payment method status")
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE payment_methods SET status = ?, updated_at = ? WHERE id = ?",
                (status, iso_utc(utcnow()), int(method_id)),
            )
            if changed.rowcount != 1:
                raise NotFoundError("payment method not found")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment_method.status", entity_type="payment_method",
                entity_id=str(int(method_id)), metadata={"status": status},
            )
        row = self.conn.execute("SELECT * FROM payment_methods WHERE id = ?", (int(method_id),)).fetchone()
        assert row is not None
        return dict(row)

    def list_pending_receipts(self, actor_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
        require_master_admin(actor_id, self.master_admin_id)
        rows = self.conn.execute(
            "SELECT r.*, o.public_id, o.amount, o.currency, o.kind, o.customer_id,"
            " c.telegram_user_id, c.display_name, m.title AS method_title"
            " FROM payment_receipts AS r JOIN customer_orders AS o ON o.id = r.order_id"
            " JOIN platform_customers AS c ON c.id = o.customer_id"
            " JOIN payment_methods AS m ON m.id = r.payment_method_id"
            " WHERE r.status = 'pending' ORDER BY r.id LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def review_receipt(
        self, actor_id: int, receipt_id: int, *, approve: bool
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            receipt_row = self.conn.execute(
                "SELECT r.*, o.customer_id, o.tenant_id, o.plan_id, o.kind, o.status AS order_status,"
                " o.amount, o.currency FROM payment_receipts AS r"
                " JOIN customer_orders AS o ON o.id = r.order_id WHERE r.id = ?",
                (int(receipt_id),),
            ).fetchone()
            if receipt_row is None:
                raise NotFoundError("receipt not found")
            receipt = dict(receipt_row)
            if receipt["status"] != "pending" or receipt["order_status"] != "payment_review":
                raise PaymentStateError("receipt was already reviewed")
            receipt_status = "approved" if approve else "rejected"
            order_status = "paid" if approve else "rejected"
            changed = self.conn.execute(
                "UPDATE payment_receipts SET status = ?, reviewed_by = ?, reviewed_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (receipt_status, int(actor_id), now, int(receipt_id)),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("receipt state changed")
            if approve and receipt["kind"] == "wallet_topup":
                self._credit_wallet(
                    int(receipt["customer_id"]), str(receipt["currency"]),
                    int(receipt["amount"]), int(receipt["order_id"]), now,
                )
                order_status = "fulfilled"
            elif approve and receipt["kind"] == "renewal":
                plan = PlanRepository(self.conn).get_by_id(int(receipt["plan_id"]))
                license_row = LicenseRepository(self.conn).latest_by_tenant(int(receipt["tenant_id"]))
                if plan is None or license_row is None:
                    raise PaymentStateError("renewal target is unavailable")
                renew_license(
                    self.conn, license_id=int(license_row["id"]),
                    tenant_id=int(receipt["tenant_id"]), extra_days=int(plan["duration_days"]),
                    actor_id=int(actor_id),
                )
                order_status = "fulfilled"
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = ?, updated_at = ?"
                " WHERE id = ? AND status = 'payment_review'",
                (order_status, now, int(receipt["order_id"])),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("order state changed")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=(int(receipt["tenant_id"]) if receipt["tenant_id"] is not None else None),
                action="payment.receipt_review", entity_type="payment_receipt",
                entity_id=str(int(receipt_id)),
                metadata={"approved": bool(approve), "order_kind": str(receipt["kind"])},
            )
        row = self.conn.execute(
            "SELECT r.*, o.status AS order_status, c.telegram_user_id"
            " FROM payment_receipts AS r JOIN customer_orders AS o ON o.id = r.order_id"
            " JOIN platform_customers AS c ON c.id = o.customer_id WHERE r.id = ?",
            (int(receipt_id),),
        ).fetchone()
        assert row is not None
        return dict(row)

    def _credit_wallet(
        self, customer_id: int, currency: str, amount: int, order_id: int, now: str
    ) -> None:
        if int(amount) <= 0:
            raise ValueError("wallet credit must be positive")
        self.conn.execute(
            "INSERT INTO wallet_accounts (customer_id, currency, balance, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(customer_id, currency) DO UPDATE SET"
            " balance = wallet_accounts.balance + excluded.balance, updated_at = excluded.updated_at",
            (int(customer_id), str(currency), int(amount), now),
        )
        row = self.conn.execute(
            "SELECT balance FROM wallet_accounts WHERE customer_id = ? AND currency = ?",
            (int(customer_id), str(currency)),
        ).fetchone()
        assert row is not None
        self.conn.execute(
            "INSERT INTO wallet_transactions"
            " (customer_id, currency, amount, kind, order_id, idempotency_key, resulting_balance, created_at)"
            " VALUES (?, ?, ?, 'deposit', ?, ?, ?, ?)",
            (
                int(customer_id), str(currency), int(amount), int(order_id),
                f"receipt-order:{order_id}", int(row["balance"]), now,
            ),
        )
