"""Shared Phase-15 contracts for future Tenant AgentBot and CustomerBot.

This module deliberately contains no Telegram handlers or menus.  It freezes the
tenant-scoped storage/API seam that later AgentBot and CustomerBot runtimes can
consume without reaching into UserBot internals.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from Database.connection import transaction
from Shared.crypto import TokenCipher, build_bot_credential
from Shared.timeutils import iso_utc, utcnow


AgentStatus = Literal["active", "disabled"]
CustomerBotStatus = Literal["pending", "active", "disabled"]
SaleSource = Literal["agent", "customer_bot"]


class AgencyInfrastructureError(RuntimeError):
    """A tenant-scoped agency/customer-bot infrastructure operation failed."""


@dataclass(frozen=True)
class AgentPrincipal:
    id: int
    tenant_id: int
    telegram_user_id: int
    display_name: str
    username: str
    status: str
    tier_code: str


@dataclass(frozen=True)
class CustomerBotPrincipal:
    id: int
    tenant_id: int
    agent_id: int
    telegram_bot_id: int | None
    telegram_username: str
    status: str
    token_tail: str


class TenantAgencyInfrastructure:
    """Tenant-bound persistence seam shared by future AgentBot/CustomerBot code."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: int,
        cipher: TokenCipher,
    ) -> None:
        self.conn = conn
        self.tenant_id = int(tenant_id)
        self.cipher = cipher
        if self.tenant_id <= 0:
            raise ValueError("invalid tenant id")

    def _agent_row(self, agent_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM tenant_agents WHERE id=? AND tenant_id=?",
            (int(agent_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise AgencyInfrastructureError("agent not found")
        return row

    @staticmethod
    def _agent_principal(row: sqlite3.Row | dict[str, Any]) -> AgentPrincipal:
        item = dict(row)
        return AgentPrincipal(
            id=int(item["id"]),
            tenant_id=int(item["tenant_id"]),
            telegram_user_id=int(item["telegram_user_id"]),
            display_name=str(item.get("display_name") or ""),
            username=str(item.get("username") or ""),
            status=str(item.get("status") or "disabled"),
            tier_code=str(item.get("tier_code") or ""),
        )

    def register_agent(
        self,
        *,
        telegram_user_id: int,
        display_name: str = "",
        username: str = "",
        tier_code: str = "",
        settings: dict[str, Any] | None = None,
    ) -> AgentPrincipal:
        telegram_id = int(telegram_user_id)
        if telegram_id <= 0:
            raise ValueError("invalid agent telegram id")
        now = iso_utc(utcnow())
        payload = json.dumps(settings or {}, ensure_ascii=False, separators=(",", ":"))
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_agents "
                "(tenant_id,telegram_user_id,display_name,username,status,tier_code,"
                "settings_json,created_at,updated_at) "
                "VALUES (?,?,?,?, 'active', ?,?,?,?) "
                "ON CONFLICT(tenant_id,telegram_user_id) DO UPDATE SET "
                "display_name=excluded.display_name, username=excluded.username, "
                "tier_code=excluded.tier_code, settings_json=excluded.settings_json, "
                "updated_at=excluded.updated_at",
                (
                    self.tenant_id,
                    telegram_id,
                    str(display_name or "").strip(),
                    str(username or "").strip().lstrip("@"),
                    str(tier_code or "").strip(),
                    payload,
                    now,
                    now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM tenant_agents WHERE tenant_id=? AND telegram_user_id=?",
            (self.tenant_id, telegram_id),
        ).fetchone()
        if row is None:
            raise AgencyInfrastructureError("agent write failed")
        return self._agent_principal(row)

    def agent(self, agent_id: int) -> AgentPrincipal:
        return self._agent_principal(self._agent_row(int(agent_id)))

    def list_agents(self, *, status: str | None = None) -> list[AgentPrincipal]:
        query = "SELECT * FROM tenant_agents WHERE tenant_id=?"
        args: list[Any] = [self.tenant_id]
        if status is not None:
            normalized = str(status).strip().lower()
            if normalized not in {"active", "disabled"}:
                raise ValueError("invalid agent status")
            query += " AND status=?"
            args.append(normalized)
        query += " ORDER BY id DESC"
        return [
            self._agent_principal(row)
            for row in self.conn.execute(query, tuple(args)).fetchall()
        ]

    def set_agent_status(self, agent_id: int, *, status: AgentStatus) -> AgentPrincipal:
        normalized = str(status).strip().lower()
        if normalized not in {"active", "disabled"}:
            raise ValueError("invalid agent status")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            changed = self.conn.execute(
                "UPDATE tenant_agents SET status=?,updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (normalized, now, int(agent_id), self.tenant_id),
            )
            if changed.rowcount != 1:
                raise AgencyInfrastructureError("agent not found")
        return self.agent(int(agent_id))

    def wallet_balance(self, agent_id: int, *, currency: str) -> int:
        self._agent_row(int(agent_id))
        code = str(currency or "").strip().upper()
        row = self.conn.execute(
            "SELECT balance FROM tenant_agent_wallet_accounts "
            "WHERE tenant_id=? AND agent_id=? AND currency=?",
            (self.tenant_id, int(agent_id), code),
        ).fetchone()
        return int(row["balance"] or 0) if row is not None else 0

    def adjust_wallet(
        self,
        agent_id: int,
        *,
        currency: str,
        amount: int,
        kind: str,
        idempotency_key: str,
        note: str = "",
    ) -> int:
        self._agent_row(int(agent_id))
        code = str(currency or "").strip().upper()
        if not 3 <= len(code) <= 8:
            raise ValueError("invalid wallet currency")
        delta = int(amount)
        if delta == 0:
            raise ValueError("wallet amount must be non-zero")
        normalized_kind = str(kind or "").strip().lower()
        allowed = {
            "topup", "admin_credit", "admin_debit", "purchase",
            "refund", "adjustment", "customer_sale",
        }
        if normalized_kind not in allowed:
            raise ValueError("invalid wallet transaction kind")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency key is required")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT resulting_balance FROM tenant_agent_wallet_transactions "
                "WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                return int(existing["resulting_balance"])
            current = self.wallet_balance(int(agent_id), currency=code)
            resulting = current + delta
            if resulting < 0:
                raise AgencyInfrastructureError("insufficient agent wallet balance")
            self.conn.execute(
                "INSERT INTO tenant_agent_wallet_accounts "
                "(tenant_id,agent_id,currency,balance,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant_id,agent_id,currency) DO UPDATE SET "
                "balance=excluded.balance,updated_at=excluded.updated_at",
                (self.tenant_id, int(agent_id), code, resulting, now),
            )
            self.conn.execute(
                "INSERT INTO tenant_agent_wallet_transactions "
                "(tenant_id,agent_id,currency,amount,kind,idempotency_key,note,"
                "resulting_balance,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.tenant_id,
                    int(agent_id),
                    code,
                    delta,
                    normalized_kind,
                    key,
                    str(note or "").strip(),
                    resulting,
                    now,
                ),
            )
        return resulting

    def register_customer_bot(
        self,
        agent_id: int,
        *,
        plain_token: str,
        settings: dict[str, Any] | None = None,
    ) -> CustomerBotPrincipal:
        self._agent_row(int(agent_id))
        credential = build_bot_credential(self.cipher, str(plain_token or ""))
        now = iso_utc(utcnow())
        payload = json.dumps(settings or {}, ensure_ascii=False, separators=(",", ":"))
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO tenant_customer_bots "
                    "(tenant_id,agent_id,encrypted_token,token_fingerprint,token_tail,"
                    "status,settings_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'pending',?,?,?)",
                    (
                        self.tenant_id,
                        int(agent_id),
                        credential["encrypted_token"],
                        credential["token_fingerprint"],
                        credential["token_tail"],
                        payload,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AgencyInfrastructureError("customer bot token is already registered") from exc
        finally:
            plain_token = ""
        return self.customer_bot(int(cursor.lastrowid or 0))

    def customer_bot(self, customer_bot_id: int) -> CustomerBotPrincipal:
        row = self.conn.execute(
            "SELECT * FROM tenant_customer_bots WHERE id=? AND tenant_id=?",
            (int(customer_bot_id), self.tenant_id),
        ).fetchone()
        if row is None:
            raise AgencyInfrastructureError("customer bot not found")
        item = dict(row)
        return CustomerBotPrincipal(
            id=int(item["id"]),
            tenant_id=int(item["tenant_id"]),
            agent_id=int(item["agent_id"]),
            telegram_bot_id=(
                int(item["telegram_bot_id"])
                if item.get("telegram_bot_id") is not None
                else None
            ),
            telegram_username=str(item.get("telegram_username") or ""),
            status=str(item.get("status") or "pending"),
            token_tail=str(item.get("token_tail") or "****"),
        )

    def activate_customer_bot(
        self,
        customer_bot_id: int,
        *,
        telegram_bot_id: int,
        telegram_username: str = "",
    ) -> CustomerBotPrincipal:
        bot_id = int(telegram_bot_id)
        if bot_id <= 0:
            raise ValueError("invalid telegram bot id")
        now = iso_utc(utcnow())
        try:
            with transaction(self.conn):
                changed = self.conn.execute(
                    "UPDATE tenant_customer_bots SET telegram_bot_id=?,"
                    "telegram_username=?,status='active',updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (
                        bot_id,
                        str(telegram_username or "").strip().lstrip("@"),
                        now,
                        int(customer_bot_id),
                        self.tenant_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgencyInfrastructureError("customer bot not found")
        except sqlite3.IntegrityError as exc:
            raise AgencyInfrastructureError("telegram bot is already registered") from exc
        return self.customer_bot(int(customer_bot_id))

    def set_server_access(
        self,
        agent_id: int,
        *,
        server_id: int,
        enabled: bool,
    ) -> None:
        self._agent_row(int(agent_id))
        server = self.conn.execute(
            "SELECT id FROM tenant_servers WHERE id=? AND tenant_id=?",
            (int(server_id), self.tenant_id),
        ).fetchone()
        if server is None:
            raise AgencyInfrastructureError("server not found")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_agent_server_access "
                "(tenant_id,agent_id,server_id,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,agent_id,server_id) DO UPDATE SET "
                "status=excluded.status,updated_at=excluded.updated_at",
                (
                    self.tenant_id,
                    int(agent_id),
                    int(server_id),
                    "active" if bool(enabled) else "disabled",
                    now,
                    now,
                ),
            )

    def set_plan_access(
        self,
        agent_id: int,
        *,
        plan_id: int,
        wholesale_price: int,
        currency: str,
        enabled: bool = True,
    ) -> None:
        self._agent_row(int(agent_id))
        plan = self.conn.execute(
            "SELECT id FROM tenant_sale_plans WHERE id=? AND tenant_id=?",
            (int(plan_id), self.tenant_id),
        ).fetchone()
        if plan is None:
            raise AgencyInfrastructureError("plan not found")
        amount = int(wholesale_price)
        if amount < 0:
            raise ValueError("invalid wholesale price")
        code = str(currency or "").strip().upper()
        if not 3 <= len(code) <= 8:
            raise ValueError("invalid currency")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_agent_plan_access "
                "(tenant_id,agent_id,plan_id,wholesale_price,currency,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,agent_id,plan_id) DO UPDATE SET "
                "wholesale_price=excluded.wholesale_price,currency=excluded.currency,"
                "status=excluded.status,updated_at=excluded.updated_at",
                (
                    self.tenant_id,
                    int(agent_id),
                    int(plan_id),
                    amount,
                    code,
                    "active" if bool(enabled) else "disabled",
                    now,
                    now,
                ),
            )

    def bind_customer(
        self,
        agent_id: int,
        *,
        customer_id: int,
        source: SaleSource,
        customer_bot_id: int | None = None,
    ) -> None:
        self._agent_row(int(agent_id))
        normalized_source = str(source).strip().lower()
        if normalized_source not in {"agent", "customer_bot"}:
            raise ValueError("invalid customer source")
        customer = self.conn.execute(
            "SELECT id FROM tenant_customers WHERE id=? AND tenant_id=?",
            (int(customer_id), self.tenant_id),
        ).fetchone()
        if customer is None:
            raise AgencyInfrastructureError("customer not found")
        if normalized_source == "customer_bot":
            if customer_bot_id is None:
                raise ValueError("customer bot id is required")
            bot = self.customer_bot(int(customer_bot_id))
            if int(bot.agent_id) != int(agent_id):
                raise AgencyInfrastructureError("customer bot belongs to another agent")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_agent_customers "
                "(tenant_id,agent_id,customer_id,source,customer_bot_id,created_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,agent_id,customer_id) DO UPDATE SET "
                "source=excluded.source,customer_bot_id=excluded.customer_bot_id",
                (
                    self.tenant_id,
                    int(agent_id),
                    int(customer_id),
                    normalized_source,
                    int(customer_bot_id) if customer_bot_id is not None else None,
                    now,
                ),
            )

    def attribute_subscription(
        self,
        agent_id: int,
        *,
        subscription_id: int,
        customer_id: int,
        source: SaleSource,
        wholesale_amount: int,
        retail_amount: int,
        currency: str,
        customer_bot_id: int | None = None,
    ) -> None:
        self.bind_customer(
            int(agent_id),
            customer_id=int(customer_id),
            source=source,
            customer_bot_id=customer_bot_id,
        )
        subscription = self.conn.execute(
            "SELECT customer_id FROM tenant_subscriptions "
            "WHERE id=? AND tenant_id=?",
            (int(subscription_id), self.tenant_id),
        ).fetchone()
        if subscription is None:
            raise AgencyInfrastructureError("subscription not found")
        if int(subscription["customer_id"]) != int(customer_id):
            raise AgencyInfrastructureError("subscription customer mismatch")
        wholesale = int(wholesale_amount)
        retail = int(retail_amount)
        if wholesale < 0 or retail < 0:
            raise ValueError("invalid sale amount")
        code = str(currency or "").strip().upper()
        if not 3 <= len(code) <= 8:
            raise ValueError("invalid currency")
        now = iso_utc(utcnow())
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_agent_subscriptions "
                "(tenant_id,agent_id,subscription_id,customer_id,source,"
                "customer_bot_id,wholesale_amount,retail_amount,currency,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,subscription_id) DO UPDATE SET "
                "agent_id=excluded.agent_id,customer_id=excluded.customer_id,"
                "source=excluded.source,customer_bot_id=excluded.customer_bot_id,"
                "wholesale_amount=excluded.wholesale_amount,"
                "retail_amount=excluded.retail_amount,currency=excluded.currency",
                (
                    self.tenant_id,
                    int(agent_id),
                    int(subscription_id),
                    int(customer_id),
                    str(source),
                    int(customer_bot_id) if customer_bot_id is not None else None,
                    wholesale,
                    retail,
                    code,
                    now,
                ),
            )
