"""Customer storefront and owner-side payment operations.

Every customer query is scoped by the Telegram actor id.  Prices and license
durations are always re-read from SQLite; callback payloads contain identifiers
only.  Multi-step balance/payment transitions use one IMMEDIATE transaction.
"""

from __future__ import annotations

import os
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timezone
import stat
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from Database.connection import transaction
from Database.repositories import AuditRepository, LicenseRepository, PlanRepository
from LicenseService.service import renew_license
from MasterBot.service import MasterService, NotFoundError, Page
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


def normalize_currency_code(currency: object) -> str:
    cleaned = str(currency or "").strip().upper()
    if cleaned in ("تومان", "TOMAN"):
        return "TOMAN"
    return cleaned


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
        self._init_sms_tables()

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
            raise CustomerBlockedError("⛔ حساب شما غیرفعال است.")
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
            raise CustomerPortalError("این سرویس در حال حاضر قابل تمدید نیست.")
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
        code = normalize_currency_code(_clean_text(currency, maximum=8))
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

    def cancel_order(self, actor_id: int, order_id: int) -> dict[str, Any]:
        order = self.get_order(actor_id, int(order_id))
        if order["status"] != "pending_payment":
            raise PaymentStateError("فقط سفارش در انتظار پرداخت قابل لغو است.")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = 'cancelled', updated_at = ?"
                " WHERE id = ? AND customer_id = ? AND status = 'pending_payment'",
                (now, int(order_id), int(order["customer_id"])),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=(
                    int(order["tenant_id"]) if order.get("tenant_id") is not None else None
                ),
                action="customer.order_cancel",
                entity_type="customer_order",
                entity_id=str(int(order_id)),
                metadata={"kind": str(order["kind"]), "public_id": str(order["public_id"])},
            )
        return self.get_order(actor_id, int(order_id))

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
        code = normalize_currency_code(currency)
        if code in ("TOMAN", "IRT"):
            rows = self.conn.execute(
                "SELECT * FROM payment_methods WHERE status = 'active' AND currency IN ('TOMAN', 'IRT') ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM payment_methods WHERE status = 'active' AND currency = ? ORDER BY id",
                (code,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_payment_method(self, method_id: int, *, currency: str) -> dict[str, Any]:
        code = normalize_currency_code(currency)
        if code in ("TOMAN", "IRT"):
            row = self.conn.execute(
                "SELECT * FROM payment_methods WHERE id = ? AND status = 'active' AND currency IN ('TOMAN', 'IRT')",
                (int(method_id),),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM payment_methods WHERE id = ? AND status = 'active' AND currency = ?",
                (int(method_id), code),
            ).fetchone()
        if row is None:
            raise NotFoundError("payment method not found")
        return dict(row)

    def list_available_currencies(self, actor_id: int) -> list[str]:
        self._customer(actor_id)
        plan_rows = self.conn.execute(
            "SELECT DISTINCT currency FROM license_plans WHERE is_public = 1"
        ).fetchall()
        method_rows = self.conn.execute(
            "SELECT DISTINCT currency FROM payment_methods WHERE status = 'active'"
        ).fetchall()
        customer = self._customer(actor_id)
        wallet_rows = self.conn.execute(
            "SELECT DISTINCT currency FROM wallet_accounts WHERE customer_id = ? AND balance > 0",
            (int(customer["id"]),),
        ).fetchall()

        found = set()
        for r in plan_rows:
            if r["currency"]:
                found.add(normalize_currency_code(r["currency"]))
        for r in method_rows:
            if r["currency"]:
                found.add(normalize_currency_code(r["currency"]))
        for r in wallet_rows:
            if r["currency"]:
                found.add(normalize_currency_code(r["currency"]))

        if "IRT" in found and "TOMAN" in found:
            found.remove("IRT")
        if not (found & {"TOMAN", "IRT"}):
            found.add("TOMAN")
        if not (found - {"TOMAN", "IRT", "IRR"}):
            found.add("USD")

        preferred = ["TOMAN", "IRT", "USD", "USDT", "EUR", "IRR"]
        return sorted(
            found,
            key=lambda c: (preferred.index(c) if c in preferred else 99, c),
        )

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
            raise PaymentStateError("این سفارش در انتظار پرداخت نیست.")
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
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
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
        code = normalize_currency_code(currency)
        if code in ("TOMAN", "IRT"):
            row = self.conn.execute(
                "SELECT SUM(balance) as total FROM wallet_accounts WHERE customer_id = ? AND currency IN ('TOMAN', 'IRT')",
                (int(customer["id"]),),
            ).fetchone()
            return int(row["total"] if row and row["total"] is not None else 0)
        row = self.conn.execute(
            "SELECT balance FROM wallet_accounts WHERE customer_id = ? AND currency = ?",
            (int(customer["id"]), code),
        ).fetchone()
        return int(row["balance"] if row and row["balance"] is not None else 0)

    def pay_order_from_wallet(self, actor_id: int, order_id: int) -> dict[str, Any]:
        order = self.get_order(actor_id, order_id)
        if order["status"] != "pending_payment" or order["kind"] == "wallet_topup":
            raise PaymentStateError("این سفارش از کیف پول قابل پرداخت نیست.")
        amount = int(order["amount"])
        currency = str(order["currency"])
        code = normalize_currency_code(currency)
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
            if changed.rowcount != 1 and code in ("TOMAN", "IRT"):
                alt = "IRT" if currency == "TOMAN" else "TOMAN"
                changed = self.conn.execute(
                    "UPDATE wallet_accounts SET balance = balance - ?, updated_at = ?"
                    " WHERE customer_id = ? AND currency = ? AND balance >= ?",
                    (amount, now, customer_id, alt, amount),
                )
                if changed.rowcount == 1:
                    currency = alt
            if changed.rowcount != 1:
                raise PaymentStateError("موجودی کیف پول کافی نیست.")
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
                    raise PaymentStateError("سرویس موردنظر برای تمدید در دسترس نیست.")
                renew_license(
                    self.conn,
                    license_id=int(license_row["id"]),
                    tenant_id=tenant_id,
                    extra_days=int(plan["duration_days"]),
                    actor_id=int(actor_id),
                )
                final_status = "fulfilled"
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = ?, paid_at = ?, updated_at = ?"
                " WHERE id = ? AND customer_id = ? AND status = 'pending_payment'",
                (final_status, now, now, int(order_id), customer_id),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
        return self.get_order(actor_id, order_id)

    def claim_trial(self, actor_id: int) -> dict[str, Any]:
        customer = self._customer(actor_id)
        if not self.settings.get_bool("trial_enabled"):
            raise CustomerPortalError("🎁 لایسنس تست در حال حاضر غیرفعال است.")
        selected_plan_id = self.settings.all().get("trial_plan_id")
        if selected_plan_id is not None:
            plan_row = self.conn.execute(
                "SELECT * FROM license_plans WHERE id = ? AND status = 'active'"
                " AND is_public = 1 AND trial_days > 0",
                (int(selected_plan_id),),
            ).fetchone()
        else:
            plan_row = self.conn.execute(
                "SELECT * FROM license_plans WHERE status = 'active' AND is_public = 1"
                " AND trial_days > 0 ORDER BY price ASC, id ASC LIMIT 1"
            ).fetchone()
        if plan_row is None:
            raise CustomerPortalError("🎁 در حال حاضر پلن تست فعالی وجود ندارد.")
        plan = dict(plan_row)
        now = iso_utc(utcnow())
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO customer_orders"
                    " (public_id, customer_id, plan_id, tenant_id, kind, amount, currency, status, paid_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, NULL, 'trial', 0, ?, 'paid', ?, ?, ?)",
                    (_public_id("try"), int(customer["id"]), int(plan["id"]), str(plan["currency"]), now, now, now),
                )
                order_id = int(cursor.lastrowid or 0)
                self.conn.execute(
                    "INSERT INTO trial_claims"
                    " (customer_id, plan_id, order_id, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'eligible', ?, ?)",
                    (int(customer["id"]), int(plan["id"]), order_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise CustomerPortalError("❌ شما قبلاً از لایسنس تست استفاده کرده‌اید.") from exc
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
            raise PaymentStateError("این سفارش هنوز آماده راه‌اندازی نیست.")
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
            raise PaymentStateError("این سفارش هنوز آماده راه‌اندازی نیست.")
        plan = PlanRepository(self.conn).get_by_id(int(order["plan_id"]))
        if plan is None:
            raise NotFoundError("plan not found")
        duration = int(plan["duration_days"])
        if order["kind"] == "trial":
            duration = int(plan.get("trial_days") or 0)
            if duration <= 0:
                raise CustomerPortalError("پلن تست دیگر فعال نیست.")
        now_dt = utcnow()
        with transaction(self.conn):
            fresh = self.get_order(actor_id, order_id)
            if fresh["status"] != "paid" or fresh["tenant_id"] is not None:
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
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
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
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

    # ---- owner customer administration ----

    def list_platform_customers(
        self,
        actor_id: int,
        *,
        page: int = 0,
        page_size: int = 8,
        query: str = "",
    ) -> Page:
        require_master_admin(actor_id, self.master_admin_id)
        safe_page = max(0, int(page))
        safe_size = max(1, min(int(page_size), 20))
        offset = safe_page * safe_size
        text = str(query or "").strip()
        where = ""
        params: list[Any] = []
        if text:
            like = f"%{text}%"
            clauses = ["c.display_name LIKE ?", "c.username LIKE ?"]
            params.extend([like, like])
            if text.isdigit():
                clauses.extend(["c.telegram_user_id = ?", "c.id = ?"])
                params.extend([int(text), int(text)])
            where = " WHERE " + " OR ".join(clauses)
        rows = self.conn.execute(
            "SELECT c.*,"
            " (SELECT COUNT(*) FROM customer_orders AS o WHERE o.customer_id = c.id) AS order_count,"
            " (SELECT COUNT(*) FROM tenants AS t WHERE t.owner_telegram_id = c.telegram_user_id) AS tenant_count,"
            " (SELECT COUNT(*) FROM payment_receipts AS r"
            "   JOIN customer_orders AS po ON po.id = r.order_id"
            "   WHERE po.customer_id = c.id AND r.status = 'pending') AS pending_receipts"
            " FROM platform_customers AS c"
            + where
            + " ORDER BY c.id DESC LIMIT ? OFFSET ?",
            tuple(params + [safe_size + 1, offset]),
        ).fetchall()
        items = [dict(row) for row in rows]
        return Page(
            items=items[:safe_size],
            page=safe_page,
            page_size=safe_size,
            has_previous=safe_page > 0,
            has_next=len(items) > safe_size,
        )

    def get_platform_customer_admin(
        self, actor_id: int, customer_id: int
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        row = self.conn.execute(
            "SELECT c.*,"
            " (SELECT COUNT(*) FROM customer_orders AS o WHERE o.customer_id = c.id) AS order_count,"
            " (SELECT COUNT(*) FROM tenants AS t WHERE t.owner_telegram_id = c.telegram_user_id) AS tenant_count,"
            " (SELECT COUNT(*) FROM payment_receipts AS r"
            "   JOIN customer_orders AS po ON po.id = r.order_id"
            "   WHERE po.customer_id = c.id AND r.status = 'pending') AS pending_receipts"
            " FROM platform_customers AS c WHERE c.id = ?",
            (int(customer_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("platform customer not found")
        result = dict(row)
        wallets = self.conn.execute(
            "SELECT currency, balance FROM wallet_accounts"
            " WHERE customer_id = ? ORDER BY currency",
            (int(customer_id),),
        ).fetchall()
        result["wallets"] = [dict(item) for item in wallets]
        recent_orders = self.conn.execute(
            "SELECT o.id, o.public_id, o.kind, o.amount, o.currency, o.status,"
            " p.name AS plan_name, o.created_at"
            " FROM customer_orders AS o"
            " LEFT JOIN license_plans AS p ON p.id = o.plan_id"
            " WHERE o.customer_id = ? ORDER BY o.id DESC LIMIT 5",
            (int(customer_id),),
        ).fetchall()
        result["recent_orders"] = [dict(item) for item in recent_orders]
        return result

    def adjust_customer_wallet(
        self,
        actor_id: int,
        customer_id: int,
        *,
        currency: str,
        amount: int,
        note: str = "",
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        customer = self.get_platform_customer_admin(actor_id, int(customer_id))
        code = _clean_text(currency, maximum=8).upper()
        if not 3 <= len(code) <= 8 or not code.isalnum():
            raise ValueError("currency must be 3-8 alphanumeric characters")
        delta = int(amount)
        if delta == 0:
            raise ValueError("wallet adjustment must be non-zero")
        clean_note = _clean_text(note, maximum=180, required=False)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO wallet_accounts (customer_id, currency, balance, updated_at)"
                " VALUES (?, ?, 0, ?) ON CONFLICT(customer_id, currency) DO NOTHING",
                (int(customer_id), code, now),
            )
            if delta > 0:
                changed = self.conn.execute(
                    "UPDATE wallet_accounts SET balance = balance + ?, updated_at = ?"
                    " WHERE customer_id = ? AND currency = ?",
                    (delta, now, int(customer_id), code),
                )
            else:
                changed = self.conn.execute(
                    "UPDATE wallet_accounts SET balance = balance + ?, updated_at = ?"
                    " WHERE customer_id = ? AND currency = ? AND balance >= ?",
                    (delta, now, int(customer_id), code, abs(delta)),
                )
            if changed.rowcount != 1:
                raise PaymentStateError("موجودی برای این کاهش کافی نیست.")
            row = self.conn.execute(
                "SELECT balance FROM wallet_accounts WHERE customer_id = ? AND currency = ?",
                (int(customer_id), code),
            ).fetchone()
            assert row is not None
            balance = int(row["balance"])
            self.conn.execute(
                "INSERT INTO wallet_transactions"
                " (customer_id, currency, amount, kind, order_id, idempotency_key,"
                " resulting_balance, created_at)"
                " VALUES (?, ?, ?, 'admin', NULL, ?, ?, ?)",
                (
                    int(customer_id), code, delta,
                    f"admin-adjust:{secrets.token_hex(12)}", balance, now,
                ),
            )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=None,
                action="wallet.admin_adjust",
                entity_type="platform_customer",
                entity_id=str(int(customer_id)),
                metadata={
                    "currency": code,
                    "amount": delta,
                    "resulting_balance": balance,
                    "note": clean_note,
                    "telegram_user_id": int(customer["telegram_user_id"]),
                },
            )
        return {
            "customer_id": int(customer_id),
            "currency": code,
            "amount": delta,
            "balance": balance,
        }

    def set_platform_customer_status(
        self, actor_id: int, customer_id: int, status: str
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        if status not in ("active", "blocked"):
            raise ValueError("invalid platform customer status")
        current = self.get_platform_customer_admin(actor_id, customer_id)
        if current["status"] == status:
            return current
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE platform_customers SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, int(customer_id)),
            )
            if changed.rowcount != 1:
                raise NotFoundError("platform customer not found")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=None,
                action="platform_customer.status",
                entity_type="platform_customer",
                entity_id=str(int(customer_id)),
                metadata={"from": str(current["status"]), "to": status},
            )
        return self.get_platform_customer_admin(actor_id, customer_id)

    # ---- owner sales / finance administration ----

    def financial_summary(self, actor_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        since_24h = iso_utc(utcnow() - timedelta(hours=24))

        def grouped(sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
            rows = self.conn.execute(sql, params).fetchall()
            return {str(row["currency"]): int(row["amount"] or 0) for row in rows}

        service_sales = grouped(
            "SELECT currency, COALESCE(SUM(amount), 0) AS amount"
            " FROM customer_orders"
            " WHERE kind IN ('purchase', 'renewal')"
            " AND status IN ('paid', 'fulfilled') GROUP BY currency"
        )
        service_sales_24h = grouped(
            "SELECT currency, COALESCE(SUM(amount), 0) AS amount"
            " FROM customer_orders"
            " WHERE kind IN ('purchase', 'renewal')"
            " AND status IN ('paid', 'fulfilled') AND paid_at >= ?"
            " GROUP BY currency",
            (since_24h,),
        )
        approved_receipts = grouped(
            "SELECT o.currency, COALESCE(SUM(o.amount), 0) AS amount"
            " FROM payment_receipts AS r"
            " JOIN customer_orders AS o ON o.id = r.order_id"
            " WHERE r.status = 'approved' GROUP BY o.currency"
        )
        wallet_topups = grouped(
            "SELECT currency, COALESCE(SUM(amount), 0) AS amount"
            " FROM customer_orders"
            " WHERE kind = 'wallet_topup' AND status = 'fulfilled'"
            " GROUP BY currency"
        )
        wallet_balances = grouped(
            "SELECT currency, COALESCE(SUM(balance), 0) AS amount"
            " FROM wallet_accounts GROUP BY currency"
        )

        status_rows = self.conn.execute(
            "SELECT status, COUNT(*) AS total FROM customer_orders GROUP BY status"
        ).fetchall()
        kind_rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS total FROM customer_orders GROUP BY kind"
        ).fetchall()
        return {
            "customers_total": int(
                self.conn.execute("SELECT COUNT(*) FROM platform_customers").fetchone()[0]
            ),
            "customers_active": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM platform_customers WHERE status = 'active'"
                ).fetchone()[0]
            ),
            "orders_total": int(
                self.conn.execute("SELECT COUNT(*) FROM customer_orders").fetchone()[0]
            ),
            "pending_receipts": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM payment_receipts WHERE status = 'pending'"
                ).fetchone()[0]
            ),
            "service_sales": service_sales,
            "service_sales_24h": service_sales_24h,
            "approved_receipts": approved_receipts,
            "wallet_topups": wallet_topups,
            "wallet_balances": wallet_balances,
            "orders_by_status": {
                str(row["status"]): int(row["total"]) for row in status_rows
            },
            "orders_by_kind": {
                str(row["kind"]): int(row["total"]) for row in kind_rows
            },
        }

    def list_all_orders(
        self,
        actor_id: int,
        *,
        page: int = 0,
        page_size: int = 8,
        status: str = "all",
        query: str = "",
    ) -> Page:
        require_master_admin(actor_id, self.master_admin_id)
        allowed = {
            "all", "pending_payment", "payment_review", "paid",
            "fulfilled", "rejected", "cancelled",
        }
        state = str(status or "all").strip()
        if state not in allowed:
            raise ValueError("invalid order status filter")
        safe_page = max(0, int(page))
        safe_size = max(1, min(int(page_size), 20))
        offset = safe_page * safe_size
        clauses: list[str] = []
        params: list[Any] = []
        if state != "all":
            clauses.append("o.status = ?")
            params.append(state)
        text = str(query or "").strip()
        if text:
            like = f"%{text}%"
            search_parts = [
                "o.public_id LIKE ?",
                "c.display_name LIKE ?",
                "c.username LIKE ?",
            ]
            params.extend([like, like, like])
            if text.isdigit():
                search_parts.extend([
                    "c.telegram_user_id = ?",
                    "o.id = ?",
                ])
                params.extend([int(text), int(text)])
            clauses.append("(" + " OR ".join(search_parts) + ")")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT o.*, c.telegram_user_id, c.display_name, c.username,"
            " p.name AS plan_name, t.name AS tenant_name"
            " FROM customer_orders AS o"
            " JOIN platform_customers AS c ON c.id = o.customer_id"
            " LEFT JOIN license_plans AS p ON p.id = o.plan_id"
            " LEFT JOIN tenants AS t ON t.id = o.tenant_id"
            + where
            + " ORDER BY o.id DESC LIMIT ? OFFSET ?",
            tuple(params + [safe_size + 1, offset]),
        ).fetchall()
        items = [dict(row) for row in rows]
        return Page(
            items=items[:safe_size],
            page=safe_page,
            page_size=safe_size,
            has_previous=safe_page > 0,
            has_next=len(items) > safe_size,
        )

    def get_order_admin(self, actor_id: int, order_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        row = self.conn.execute(
            "SELECT o.*, c.telegram_user_id, c.display_name, c.username,"
            " p.name AS plan_name, p.duration_days, t.name AS tenant_name"
            " FROM customer_orders AS o"
            " JOIN platform_customers AS c ON c.id = o.customer_id"
            " LEFT JOIN license_plans AS p ON p.id = o.plan_id"
            " LEFT JOIN tenants AS t ON t.id = o.tenant_id"
            " WHERE o.id = ?",
            (int(order_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("order not found")
        result = dict(row)
        receipts = self.conn.execute(
            "SELECT r.*, m.title AS method_title, m.kind AS method_kind"
            " FROM payment_receipts AS r"
            " JOIN payment_methods AS m ON m.id = r.payment_method_id"
            " WHERE r.order_id = ? ORDER BY r.id DESC",
            (int(order_id),),
        ).fetchall()
        result["receipts"] = [dict(item) for item in receipts]
        wallet_tx = self.conn.execute(
            "SELECT id, amount, kind, resulting_balance, created_at"
            " FROM wallet_transactions WHERE order_id = ? ORDER BY id DESC",
            (int(order_id),),
        ).fetchall()
        result["wallet_transactions"] = [dict(item) for item in wallet_tx]
        return result

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
        code = normalize_currency_code(_clean_text(currency, maximum=8))
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
        code = normalize_currency_code(_clean_text(currency, maximum=8))
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
        result = self.settings.all()
        plan_id = result.get("trial_plan_id")
        result["trial_plan_name"] = None
        if plan_id is not None:
            plan = PlanRepository(self.conn).get_by_id(int(plan_id))
            if plan is not None:
                result["trial_plan_name"] = str(plan["name"])
        return result

    def set_trial_plan(self, actor_id: int, plan_id: Optional[int]) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        selected: Optional[dict[str, Any]] = None
        if plan_id is not None:
            selected = PlanRepository(self.conn).get_by_id(int(plan_id))
            if selected is None:
                raise NotFoundError("plan not found")
            if selected["status"] != "active":
                raise ValueError("trial plan must be active")
            if not int(selected.get("is_public", 1)):
                raise ValueError("trial plan must be public")
            if int(selected.get("trial_days", 0)) <= 0:
                raise ValueError("trial plan must have trial days")
        value = "" if selected is None else str(int(selected["id"]))
        with transaction(self.conn):
            self.settings.set_text("trial_plan_id", value, maximum=20)
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=None,
                action="platform_setting.trial_plan",
                entity_type="license_plan",
                entity_id=value or "auto",
                metadata={"plan_name": selected["name"] if selected else None},
            )
        return self.get_platform_settings(actor_id)

    def list_trial_plan_candidates(self, actor_id: int) -> list[dict[str, Any]]:
        require_master_admin(actor_id, self.master_admin_id)
        rows = self.conn.execute(
            "SELECT * FROM license_plans"
            " WHERE status = 'active' AND is_public = 1 AND trial_days > 0"
            " ORDER BY price ASC, id ASC"
        ).fetchall()
        return [dict(row) for row in rows]

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
        with transaction(self.conn):
            result = self.settings.set_text(key, clean_value, maximum=limits[key])
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
        if key not in {"sales_enabled", "trial_enabled", "auto_backup_enabled"}:
            raise ValueError("invalid boolean setting")
        with transaction(self.conn):
            self.settings.set_bool(key, bool(enabled))
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_setting.update", entity_type="platform_setting",
                entity_id=key, metadata={"enabled": bool(enabled)},
            )
        return self.settings.all()

    def get_backup_status(self, actor_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        settings = self.settings.all()
        return {
            "auto_backup_enabled": bool(settings.get("auto_backup_enabled")),
            "backup_last_at": str(settings.get("backup_last_at") or ""),
            "auto_backup_interval_hours": int(settings.get("auto_backup_interval_hours") or 12),
        }

    def set_backup_auto(self, actor_id: int, enabled: bool) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        with transaction(self.conn):
            self.settings.set_bool("auto_backup_enabled", bool(enabled))
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_backup.auto_toggle", entity_type="platform_setting",
                entity_id="auto_backup_enabled", metadata={"enabled": bool(enabled)},
            )
        return self.get_backup_status(actor_id)

    def set_backup_interval(self, actor_id: int, hours: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        from MasterBot.platform_settings import BACKUP_INTERVAL_PRESETS

        try:
            value = int(hours)
        except (TypeError, ValueError):
            raise ValueError("invalid backup interval") from None
        if value not in BACKUP_INTERVAL_PRESETS:
            raise ValueError("invalid backup interval")
        with transaction(self.conn):
            self.settings.set_text("auto_backup_interval_hours", str(value), maximum=10)
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_backup.interval", entity_type="platform_setting",
                entity_id="auto_backup_interval_hours", metadata={"hours": value},
            )
        return self.get_backup_status(actor_id)

    def set_backup_passphrase(
        self, actor_id: int, passphrase: str, *, env_path: Optional[str] = None
    ) -> dict[str, Any]:
        """Store the full-backup passphrase in the server .env file.

        The secret itself is never written to the database, audit log or
        Telegram chat; only the fact that it changed is audited.
        """
        require_master_admin(actor_id, self.master_admin_id)
        clean = str(passphrase or "").strip()
        if len(clean.encode("utf-8")) < 12:
            raise ValueError("backup passphrase must contain at least 12 bytes")
        if len(clean) > 200 or any(ch in clean for ch in "\r\n\x00"):
            raise ValueError("backup passphrase is invalid")
        target = Path(env_path) if env_path else Path(__file__).resolve().parents[1] / ".env"
        if not target.is_file():
            raise ValueError("environment file is missing")
        original = target.read_text(encoding="utf-8").splitlines()
        rendered: list[str] = []
        replaced = False
        for line in original:
            if line.startswith("BACKUP_PASSPHRASE="):
                if not replaced:
                    rendered.append("BACKUP_PASSPHRASE=" + clean)
                    replaced = True
                continue
            rendered.append(line)
        if not replaced:
            rendered.append("BACKUP_PASSPHRASE=" + clean)
        mode = stat.S_IMODE(target.stat().st_mode) or 0o600
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            temp = Path(handle.name)
            handle.write("\n".join(rendered).rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, mode)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        os.environ["BACKUP_PASSPHRASE"] = clean
        with transaction(self.conn):
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_backup.passphrase_set", entity_type="platform_setting",
                entity_id="BACKUP_PASSPHRASE", metadata={"configured": True},
            )
        return self.get_backup_status(actor_id)

    def record_backup_completed(
        self, actor_id: int, *, filename: str, sha256: str
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        clean_name = str(filename or "")[:120]
        clean_sha = str(sha256 or "")[:64]
        with transaction(self.conn):
            self.settings.set_text("backup_last_at", iso_utc(utcnow()), maximum=40)
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="platform_backup.create", entity_type="platform_setting",
                entity_id="full-backup",
                metadata={"filename": clean_name, "sha256": clean_sha},
            )
        return self.get_backup_status(actor_id)

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
        code = normalize_currency_code(_clean_text(currency, maximum=8))
        if not 3 <= len(code) <= 8 or not code.isalnum():
            raise ValueError("currency must be 3-8 alphanumeric characters")
        clean_recipient = (
            _clean_text(recipient, maximum=100, required=False) or None
            if current["kind"] == "card" else None
        )
        clean_network = _clean_text(network, maximum=40, required=False) or None
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

    def _init_sms_tables(self) -> None:
        with transaction(self.conn):
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS master_sms_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                encrypted_secret TEXT,
                plain_secret TEXT,
                max_pending_age_minutes INTEGER NOT NULL DEFAULT 360,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS master_sms_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                sender TEXT NOT NULL DEFAULT '',
                amount_raw INTEGER NOT NULL DEFAULT 0,
                currency_raw TEXT NOT NULL DEFAULT '',
                amount_toman INTEGER NOT NULL DEFAULT 0,
                reference TEXT NOT NULL DEFAULT '',
                card_last4 TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'received'
                    CHECK (status IN (
                        'received', 'test_received', 'invalid_amount', 'no_pending_match',
                        'ambiguous', 'approved', 'approved_duplicate', 'attached_manual',
                        'approve_failed'
                    )),
                message TEXT NOT NULL DEFAULT '',
                matched_receipt_id INTEGER,
                received_at INTEGER NOT NULL DEFAULT 0,
                device_time INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """)
            self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_master_sms_events_match
                ON master_sms_events (status, amount_toman, created_at)
            """)

    SMS_SECRET_BYTES = 32
    SMS_DEFAULT_MAX_PENDING_AGE_MINUTES = 360
    SMS_MANUAL_ATTACH_WINDOW_MINUTES = 120
    SMS_RECEIPT_LOOKBACK_MINUTES = 30

    @staticmethod
    def mask_sms_secret(secret: object) -> str:
        s = str(secret or "").strip()
        if len(s) < 10:
            return "••••••••"
        return f"{s[:4]}••••{s[-4:]}"

    def _sms_settings_row(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM master_sms_settings WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def sms_webhook_path(self) -> str:
        return "/payment/sms/master"

    def sms_status_admin(self, actor_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        row = self._sms_settings_row()
        secret = str(row["plain_secret"] or "") if row else ""
        return {
            "enabled": bool(row["enabled"]) if row else False,
            "has_secret": bool(secret),
            "masked_secret": self.mask_sms_secret(secret) if secret else "",
            "max_pending_age_minutes": (
                int(row["max_pending_age_minutes"]) if row else self.SMS_DEFAULT_MAX_PENDING_AGE_MINUTES
            ),
            "webhook_path": self.sms_webhook_path(),
        }

    def _sms_ensure_secret_tx(self) -> str:
        row = self._sms_settings_row()
        if row and row.get("plain_secret"):
            return str(row["plain_secret"])
        secret = secrets.token_hex(self.SMS_SECRET_BYTES)
        now = iso_utc(utcnow())
        if row is None:
            self.conn.execute(
                "INSERT INTO master_sms_settings (id, enabled, plain_secret, max_pending_age_minutes, created_at, updated_at) "
                "VALUES (1, 0, ?, ?, ?, ?)",
                (secret, self.SMS_DEFAULT_MAX_PENDING_AGE_MINUTES, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE master_sms_settings SET plain_secret = ?, updated_at = ? WHERE id = 1",
                (secret, now),
            )
        return secret

    def set_sms_enabled_admin(self, actor_id: int, *, enabled: bool) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            if enabled:
                self._sms_ensure_secret_tx()
            row = self._sms_settings_row()
            if row is None:
                secret = secrets.token_hex(self.SMS_SECRET_BYTES)
                self.conn.execute(
                    "INSERT INTO master_sms_settings (id, enabled, plain_secret, max_pending_age_minutes, created_at, updated_at) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (1 if enabled else 0, secret, self.SMS_DEFAULT_MAX_PENDING_AGE_MINUTES, now, now),
                )
            else:
                self.conn.execute(
                    "UPDATE master_sms_settings SET enabled = ?, updated_at = ? WHERE id = 1",
                    (1 if enabled else 0, now),
                )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment.sms_enabled", entity_type="platform_sms",
                entity_id="master", metadata={"enabled": bool(enabled)},
            )
        return self.sms_status_admin(actor_id)

    def rotate_sms_secret_admin(self, actor_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        secret = secrets.token_hex(self.SMS_SECRET_BYTES)
        now = iso_utc(utcnow())
        with transaction(self.conn):
            row = self._sms_settings_row()
            if row is None:
                self.conn.execute(
                    "INSERT INTO master_sms_settings (id, enabled, plain_secret, max_pending_age_minutes, created_at, updated_at) "
                    "VALUES (1, 0, ?, ?, ?, ?)",
                    (secret, self.SMS_DEFAULT_MAX_PENDING_AGE_MINUTES, now, now),
                )
            else:
                self.conn.execute(
                    "UPDATE master_sms_settings SET plain_secret = ?, updated_at = ? WHERE id = 1",
                    (secret, now),
                )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment.sms_secret_rotate", entity_type="platform_sms",
                entity_id="master", metadata={"status": "rotated"},
            )
        result = self.sms_status_admin(actor_id)
        result["plain_secret"] = secret
        return result

    def sms_plain_secret_admin(self, actor_id: int) -> str:
        require_master_admin(actor_id, self.master_admin_id)
        row = self._sms_settings_row()
        if row is None or not row.get("plain_secret"):
            with transaction(self.conn):
                return self._sms_ensure_secret_tx()
        return str(row["plain_secret"])

    def sms_secret_matches(self, provided: str) -> bool:
        row = self._sms_settings_row()
        if row is None or not row.get("plain_secret"):
            return False
        return hmac.compare_digest(str(provided or "").strip(), str(row["plain_secret"]).strip())

    def record_sms_event(self, data: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        now = iso_utc(utcnow())
        event_id = str(data.get("event_id") or "").strip()
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO master_sms_events "
                    "(event_id, sender, amount_raw, currency_raw, amount_toman, "
                    "reference, card_last4, body, status, message, matched_receipt_id, "
                    "received_at, device_time, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        str(data.get("sender") or "")[:64],
                        int(data.get("amount_raw") or 0),
                        str(data.get("currency_raw") or "")[:16],
                        int(data.get("amount_toman") or 0),
                        str(data.get("reference") or "")[:128],
                        str(data.get("card_last4") or "")[:8],
                        str(data.get("body") or "")[:2000],
                        str(data.get("status") or "received"),
                        str(data.get("message") or "")[:255],
                        int(data["matched_receipt_id"]) if data.get("matched_receipt_id") else None,
                        int(data.get("received_at") or 0),
                        int(data.get("device_time") or 0),
                        now,
                    ),
                )
            row = self.conn.execute(
                "SELECT * FROM master_sms_events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return True, dict(row) if row else None
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT * FROM master_sms_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return False, dict(row) if row else None

    def update_sms_event(self, event_id: str, **kwargs: Any) -> None:
        fields = []
        values = []
        for k, v in kwargs.items():
            if k in ("status", "message", "matched_receipt_id", "amount_toman"):
                fields.append(f"{k} = ?")
                values.append(v)
        if not fields:
            return
        values.append(str(event_id))
        with transaction(self.conn):
            self.conn.execute(
                f"UPDATE master_sms_events SET {', '.join(fields)} WHERE event_id = ?",
                values,
            )

    def find_pending_card_receipts_for_sms(
        self,
        amount_toman: int,
        *,
        card_last4: str = "",
        max_age_minutes: int = 360,
        sms_time_ms: int = 0,
    ) -> list[dict[str, Any]]:
        amount = int(amount_toman or 0)
        if amount <= 0:
            return []
        from Shared.timeutils import parse_utc
        age = max(5, int(max_age_minutes or 360))
        cutoff = iso_utc(utcnow() - timedelta(minutes=age))
        rows = self.conn.execute(
            "SELECT r.*, o.amount AS order_amount, o.currency AS order_currency, "
            "o.customer_id, o.kind AS order_kind, c.display_name, c.username, c.telegram_user_id, "
            "m.kind AS method_kind "
            "FROM payment_receipts r "
            "JOIN customer_orders o ON o.id = r.order_id "
            "JOIN platform_customers c ON c.id = o.customer_id "
            "JOIN payment_methods m ON m.id = r.payment_method_id "
            "WHERE r.status = 'pending' AND o.status = 'payment_review' "
            "AND m.kind = 'card' AND r.created_at >= ? "
            "ORDER BY r.id DESC LIMIT 10",
            (cutoff,),
        ).fetchall()
        last4 = str(card_last4 or "").strip()
        sms_dt = None
        if int(sms_time_ms or 0) > 0:
            try:
                sms_dt = datetime.fromtimestamp(int(sms_time_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError, OverflowError, OSError):
                sms_dt = None
        matched = []
        for row in rows:
            item = dict(row)
            curr = str(item.get("order_currency") or "").upper()
            order_toman = int(item.get("order_amount") or 0)
            if curr in ("IRR", "RIAL"):
                order_toman //= 10
            if order_toman != amount:
                continue
            payer_last4 = ""
            for part in reversed(str(item.get("reference") or "").split("|")):
                cleaned = re.sub(r"\D", "", part.strip())
                if len(cleaned) == 4:
                    payer_last4 = cleaned
                    break
            if last4:
                if not payer_last4 or payer_last4 != last4:
                    continue
            elif payer_last4:
                continue
            if sms_dt is not None:
                try:
                    receipt_dt = parse_utc(str(item.get("created_at") or ""))
                    if sms_dt < receipt_dt - timedelta(minutes=self.SMS_RECEIPT_LOOKBACK_MINUTES):
                        continue
                    if sms_dt > receipt_dt + timedelta(minutes=5):
                        continue
                except Exception:
                    pass
            matched.append(item)
        return matched

    def recently_approved_card_receipts_for_sms(
        self,
        amount_toman: int,
        *,
        max_age_minutes: int = 120,
    ) -> list[dict[str, Any]]:
        amount = int(amount_toman or 0)
        if amount <= 0:
            return []
        window = max(5, min(1440, int(max_age_minutes or 120)))
        lower = iso_utc(utcnow() - timedelta(minutes=window))
        rows = self.conn.execute(
            "SELECT r.*, o.amount AS order_amount, o.currency AS order_currency "
            "FROM payment_receipts r "
            "JOIN customer_orders o ON o.id = r.order_id "
            "JOIN payment_methods m ON m.id = r.payment_method_id "
            "WHERE r.status = 'approved' AND m.kind = 'card' "
            "AND COALESCE(r.reviewed_at, '') >= ? "
            "ORDER BY r.id DESC LIMIT 5",
            (lower,),
        ).fetchall()
        matched = []
        for row in rows:
            item = dict(row)
            curr = str(item.get("order_currency") or "").upper()
            order_toman = int(item.get("order_amount") or 0)
            if curr in ("IRR", "RIAL"):
                order_toman //= 10
            if order_toman == amount:
                matched.append(item)
        return matched

    def approve_receipt_by_sms(
        self,
        receipt_id: int,
        *,
        event_id: str,
        sender: str = "",
    ) -> dict[str, Any]:
        note = "تأیید خودکار SMS بانک"
        if str(sender or "").strip():
            note += f" ({str(sender).strip()})"
        return self.review_receipt(
            self.master_admin_id,
            int(receipt_id),
            approve=True,
            review_note=note,
        )

    def remove_payment_method_admin(
        self, actor_id: int, *, method_id: int
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        current = self.get_payment_method_admin(actor_id, method_id)
        refs = self.conn.execute(
            "SELECT COUNT(*) AS total FROM payment_receipts WHERE payment_method_id = ?",
            (int(method_id),),
        ).fetchone()
        total = int(refs["total"] or 0) if refs is not None else 0
        with transaction(self.conn):
            if total > 0:
                self.conn.execute(
                    "UPDATE payment_methods SET status = 'disabled', updated_at = ? WHERE id = ?",
                    (iso_utc(utcnow()), int(method_id)),
                )
                AuditRepository(self.conn).append(
                    actor_id=int(actor_id), tenant_id=None,
                    action="payment_method.disable", entity_type="payment_method",
                    entity_id=str(int(method_id)), metadata={"has_receipts": True},
                )
                res = self.get_payment_method_admin(actor_id, method_id)
                res["removed"] = False
                return res
            self.conn.execute(
                "DELETE FROM payment_methods WHERE id = ?", (int(method_id),)
            )
            AuditRepository(self.conn).append(
                actor_id=int(actor_id), tenant_id=None,
                action="payment_method.delete", entity_type="payment_method",
                entity_id=str(int(method_id)), metadata={"deleted": True},
            )
        res = dict(current)
        res["removed"] = True
        return res


    def list_receipts_admin(
        self,
        actor_id: int,
        *,
        page: int = 0,
        page_size: int = 8,
        status: str = "all",
        query: str = "",
    ) -> Page:
        require_master_admin(actor_id, self.master_admin_id)
        allowed = {"all", "pending", "approved", "rejected"}
        state = str(status or "all").strip()
        if state not in allowed:
            raise ValueError("invalid receipt status filter")
        safe_page = max(0, int(page))
        safe_size = max(1, min(int(page_size), 20))
        offset = safe_page * safe_size
        clauses: list[str] = []
        params: list[Any] = []
        if state != "all":
            clauses.append("r.status = ?")
            params.append(state)
        text = str(query or "").strip()
        if text:
            like = f"%{text}%"
            parts = [
                "o.public_id LIKE ?",
                "c.display_name LIKE ?",
                "c.username LIKE ?",
                "r.reference LIKE ?",
            ]
            params.extend([like, like, like, like])
            if text.isdigit():
                parts.extend([
                    "c.telegram_user_id = ?",
                    "r.id = ?",
                    "o.id = ?",
                ])
                params.extend([int(text), int(text), int(text)])
            clauses.append("(" + " OR ".join(parts) + ")")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT r.*, o.public_id, o.amount, o.currency, o.kind,"
            " o.status AS order_status, o.customer_id,"
            " c.telegram_user_id, c.display_name, c.username,"
            " m.title AS method_title, m.kind AS method_kind"
            " FROM payment_receipts AS r"
            " JOIN customer_orders AS o ON o.id = r.order_id"
            " JOIN platform_customers AS c ON c.id = o.customer_id"
            " JOIN payment_methods AS m ON m.id = r.payment_method_id"
            + where
            + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
            tuple(params + [safe_size + 1, offset]),
        ).fetchall()
        items = [dict(row) for row in rows]
        return Page(
            items=items[:safe_size],
            page=safe_page,
            page_size=safe_size,
            has_previous=safe_page > 0,
            has_next=len(items) > safe_size,
        )

    def get_receipt_admin(self, actor_id: int, receipt_id: int) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        row = self.conn.execute(
            "SELECT r.*, o.public_id, o.amount, o.currency, o.kind,"
            " o.status AS order_status, o.customer_id,"
            " c.telegram_user_id, c.display_name, c.username,"
            " m.title AS method_title, m.kind AS method_kind,"
            " m.destination AS method_destination"
            " FROM payment_receipts AS r"
            " JOIN customer_orders AS o ON o.id = r.order_id"
            " JOIN platform_customers AS c ON c.id = o.customer_id"
            " JOIN payment_methods AS m ON m.id = r.payment_method_id"
            " WHERE r.id = ?",
            (int(receipt_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("receipt not found")
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
        self,
        actor_id: int,
        receipt_id: int,
        *,
        approve: bool,
        review_note: str = "",
    ) -> dict[str, Any]:
        require_master_admin(actor_id, self.master_admin_id)
        note = _clean_text(review_note, maximum=300, required=False)
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
                raise PaymentStateError("این رسید قبلاً بررسی شده است.")
            receipt_status = "approved" if approve else "rejected"
            order_status = "paid" if approve else "rejected"
            changed = self.conn.execute(
                "UPDATE payment_receipts SET status = ?, reviewed_by = ?, reviewed_at = ?,"
                " review_note = ? WHERE id = ? AND status = 'pending'",
                (
                    receipt_status,
                    int(actor_id),
                    now,
                    note or None,
                    int(receipt_id),
                ),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("وضعیت رسید تغییر کرده است؛ دوباره بررسی کنید.")
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
                    raise PaymentStateError("سرویس موردنظر برای تمدید در دسترس نیست.")
                renew_license(
                    self.conn, license_id=int(license_row["id"]),
                    tenant_id=int(receipt["tenant_id"]), extra_days=int(plan["duration_days"]),
                    actor_id=int(actor_id),
                )
                order_status = "fulfilled"
            changed = self.conn.execute(
                "UPDATE customer_orders SET status = ?, paid_at = ?, updated_at = ?"
                " WHERE id = ? AND status = 'payment_review'",
                (
                    order_status,
                    now if approve else None,
                    now,
                    int(receipt["order_id"]),
                ),
            )
            if changed.rowcount != 1:
                raise PaymentStateError("وضعیت سفارش تغییر کرده است؛ دوباره بررسی کنید.")
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=(int(receipt["tenant_id"]) if receipt["tenant_id"] is not None else None),
                action="payment.receipt_review", entity_type="payment_receipt",
                entity_id=str(int(receipt_id)),
                metadata={
                    "approved": bool(approve),
                    "order_kind": str(receipt["kind"]),
                    "review_note_set": bool(note),
                },
            )
        row = self.conn.execute(
            "SELECT r.*, o.status AS order_status, o.kind AS order_kind,"
            " o.public_id, o.amount, o.currency, c.telegram_user_id"
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
