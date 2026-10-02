"""Owner-only application service for the Phase-2 MasterBot.

Telegram handlers are deliberately thin. Every public operation in this
service repeats the global owner check, re-reads referenced rows, scopes
writes to the selected tenant and records mutations in the audit table.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional, Protocol

from Database.connection import transaction
from Database.repositories import (
    AuditRepository,
    BotRepository,
    LicenseRepository,
    NotificationRepository,
    PlanRepository,
    TenantRepository,
    TenantRuntimeRepository,
)
from LicenseService.service import renew_license, transition_license
from Provisioning.service import (
    BotEnrollment,
    PreparedBot,
    ProvisioningError,
    ProvisioningResult,
    ProvisioningStatus,
    TenantProvisioner,
)
from Shared.access import require_master_admin
from Shared.crypto import TokenCipher
from Shared.timeutils import iso_utc, parse_utc, utcnow


class MasterServiceError(RuntimeError):
    """Safe, user-presentable service failure without secret material."""


class NotFoundError(MasterServiceError):
    """Requested entity does not exist."""


class TokenVerificationError(MasterServiceError):
    """Telegram rejected a tenant bot token or returned an invalid identity."""


@dataclass(frozen=True)
class BotIdentity:
    telegram_bot_id: int
    username: Optional[str]


class BotTokenVerifier(Protocol):
    async def verify(self, token: str) -> BotIdentity:
        """Verify a raw token and return its Telegram identity."""


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    page: int
    page_size: int
    has_previous: bool
    has_next: bool


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int]:
    safe_page = max(0, int(page))
    safe_size = max(1, min(int(page_size), 20))
    return safe_page, safe_size, safe_page * safe_size


class MasterService:
    """All MasterBot use cases behind a mandatory owner gate."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        master_admin_id: int,
        cipher: TokenCipher,
        bot_verifier: Optional[BotTokenVerifier] = None,
        cache_invalidator: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.conn = conn
        self.master_admin_id = int(master_admin_id)
        self.cipher = cipher
        self.bot_verifier = bot_verifier
        self.cache_invalidator = cache_invalidator

    def _invalidate_tenant(self, tenant_id: int) -> None:
        if self.cache_invalidator is None:
            return
        try:
            self.cache_invalidator(int(tenant_id))
        except Exception:
            # Cache invalidation is best-effort; a cache callback must never
            # turn an already committed database mutation into a false failure.
            return

    def _provisioner(self) -> TenantProvisioner:
        return TenantProvisioner(
            self.conn,
            master_admin_id=self.master_admin_id,
            cipher=self.cipher,
            bot_verifier=self.bot_verifier,
            cache_invalidator=self.cache_invalidator,
        )

    def authorize(self, actor_id: int | None) -> None:
        require_master_admin(actor_id, self.master_admin_id)

    def list_tenants(
        self, actor_id: int, *, page: int = 0, page_size: int = 8, query: str = ""
    ) -> Page:
        self.authorize(actor_id)
        page, page_size, offset = _page_bounds(page, page_size)
        repo = TenantRepository(self.conn)
        if str(query or "").strip():
            items = repo.search(str(query), limit=page_size + 1, offset=offset)
        else:
            items = repo.list(limit=page_size + 1, offset=offset)
        return Page(
            items=items[:page_size],
            page=page,
            page_size=page_size,
            has_previous=page > 0,
            has_next=len(items) > page_size,
        )

    def get_tenant(self, actor_id: int, tenant_id: int) -> dict[str, Any]:
        self.authorize(actor_id)
        row = TenantRepository(self.conn).get_by_id(int(tenant_id))
        if row is None:
            raise NotFoundError("tenant not found")
        return row

    def get_tenant_by_owner_telegram_id(
        self, actor_id: int, owner_telegram_id: int
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        matches = TenantRepository(self.conn).list_by_owner_telegram_id(
            int(owner_telegram_id), limit=2
        )
        if not matches:
            raise NotFoundError("tenant owner Telegram ID not found")
        if len(matches) > 1:
            raise MasterServiceError(
                "multiple tenants use this owner Telegram ID; choose a tenant explicitly"
            )
        return matches[0]

    def create_tenant(
        self, actor_id: int, *, name: str, slug: str, owner_telegram_id: int
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            row = TenantRepository(self.conn).create(
                name=name, slug=slug, owner_telegram_id=int(owner_telegram_id)
            )
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=int(row["id"]),
                action="tenant.create",
                entity_type="tenant",
                entity_id=str(row["id"]),
                metadata={"slug": row["slug"]},
            )
        self._invalidate_tenant(int(row["id"]))
        return row

    def update_tenant(
        self,
        actor_id: int,
        tenant_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            if TenantRepository(self.conn).get_by_id(int(tenant_id)) is None:
                raise NotFoundError("tenant not found")
            row = TenantRepository(self.conn).update_details(
                int(tenant_id),
                name=name,
                slug=slug,
                owner_telegram_id=int(owner_telegram_id),
            )
            assert row is not None
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=int(tenant_id),
                action="tenant.update",
                entity_type="tenant",
                entity_id=str(tenant_id),
                metadata={"slug": row["slug"]},
            )
        return row

    def set_tenant_status(
        self, actor_id: int, tenant_id: int, status: str
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            repo = TenantRepository(self.conn)
            before = repo.get_by_id(int(tenant_id))
            if before is None:
                raise NotFoundError("tenant not found")
            runtime_repo = TenantRuntimeRepository(self.conn)
            runtime = runtime_repo.get_by_tenant(int(tenant_id))
            if runtime is not None:
                if status == "active":
                    if not BotRepository(self.conn).readiness(int(tenant_id))["ready"]:
                        raise MasterServiceError(
                            "both tenant bots must be active before enabling"
                        )
                    runtime_repo.update_status(int(tenant_id), "ready")
                else:
                    runtime_repo.update_status(int(tenant_id), "disabled")
            row = repo.update_status(int(tenant_id), status)
            assert row is not None
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=int(tenant_id),
                action="tenant.status",
                entity_type="tenant",
                entity_id=str(tenant_id),
                metadata={"from": before["status"], "to": status},
            )
        self._invalidate_tenant(int(tenant_id))
        return row

    async def register_tenant_bot(
        self, actor_id: int, tenant_id: int, *, role: str, plain_token: str
    ) -> dict[str, Any]:
        try:
            enrollment = await self.register_tenant_bot_with_secret(
                actor_id,
                tenant_id,
                role=role,
                plain_token=plain_token,
            )
        except ProvisioningError as exc:
            raise TokenVerificationError(str(exc)) from None
        row = BotRepository(self.conn).get_by_id(int(enrollment.bot.id))
        assert row is not None
        return row

    async def register_tenant_bot_with_secret(
        self, actor_id: int, tenant_id: int, *, role: str, plain_token: str
    ) -> BotEnrollment:
        self.authorize(actor_id)
        return await self._provisioner().enroll_bot(
            actor_id,
            int(tenant_id),
            role=role,
            plain_token=plain_token,
        )

    async def prepare_tenant_bot(
        self, actor_id: int, *, role: str, plain_token: str
    ) -> PreparedBot:
        self.authorize(actor_id)
        return await self._provisioner().prepare_bot(
            actor_id, role=role, plain_token=plain_token
        )

    async def provision_tenant(
        self,
        actor_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
        admin_token: str,
        user_token: str,
    ) -> ProvisioningResult:
        self.authorize(actor_id)
        return await self._provisioner().provision(
            actor_id,
            name=name,
            slug=slug,
            owner_telegram_id=int(owner_telegram_id),
            admin_token=admin_token,
            user_token=user_token,
        )

    def provision_tenant_prepared(
        self,
        actor_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
        admin_bot: PreparedBot,
        user_bot: PreparedBot,
    ) -> ProvisioningResult:
        self.authorize(actor_id)
        return self._provisioner().provision_prepared(
            actor_id,
            name=name,
            slug=slug,
            owner_telegram_id=int(owner_telegram_id),
            admin_bot=admin_bot,
            user_bot=user_bot,
        )

    def provisioning_status(
        self, actor_id: int, tenant_id: int
    ) -> ProvisioningStatus:
        self.authorize(actor_id)
        return self._provisioner().status(actor_id, int(tenant_id))

    def set_provisioned_tenant_enabled(
        self, actor_id: int, tenant_id: int, *, enabled: bool
    ) -> ProvisioningStatus:
        self.authorize(actor_id)
        return self._provisioner().set_enabled(
            actor_id, int(tenant_id), enabled=bool(enabled)
        )

    def rotate_tenant_webhook_secret(
        self, actor_id: int, tenant_id: int, *, role: str
    ) -> BotEnrollment:
        self.authorize(actor_id)
        return self._provisioner().rotate_webhook_secret(
            actor_id, int(tenant_id), role=role
        )

    def list_plans(
        self, actor_id: int, *, page: int = 0, page_size: int = 8, query: str = ""
    ) -> Page:
        self.authorize(actor_id)
        page, page_size, offset = _page_bounds(page, page_size)
        repo = PlanRepository(self.conn)
        items = (
            repo.search(query, limit=page_size + 1, offset=offset)
            if str(query or "").strip()
            else repo.list(limit=page_size + 1, offset=offset)
        )
        return Page(items[:page_size], page, page_size, page > 0, len(items) > page_size)

    def list_active_plans(self, actor_id: int) -> list[dict[str, Any]]:
        self.authorize(actor_id)
        return PlanRepository(self.conn).list_active()

    def get_plan(self, actor_id: int, plan_id: int) -> dict[str, Any]:
        self.authorize(actor_id)
        row = PlanRepository(self.conn).get_by_id(int(plan_id))
        if row is None:
            raise NotFoundError("plan not found")
        return row

    def create_plan(
        self,
        actor_id: int,
        *,
        name: str,
        duration_days: int,
        price: int,
        max_servers: int,
        max_users: int,
        features: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            row = PlanRepository(self.conn).create(
                name=name,
                duration_days=duration_days,
                price=price,
                max_servers=max_servers,
                max_users=max_users,
                features=features,
            )
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=None,
                action="plan.create",
                entity_type="license_plan",
                entity_id=str(row["id"]),
                metadata={"name": row["name"]},
            )
        return row

    def update_plan(
        self,
        actor_id: int,
        plan_id: int,
        *,
        name: str,
        duration_days: int,
        price: int,
        max_servers: int,
        max_users: int,
        features: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            repo = PlanRepository(self.conn)
            if repo.get_by_id(int(plan_id)) is None:
                raise NotFoundError("plan not found")
            row = repo.update(
                int(plan_id),
                name=name,
                duration_days=duration_days,
                price=price,
                max_servers=max_servers,
                max_users=max_users,
                features=features,
            )
            assert row is not None
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=None,
                action="plan.update",
                entity_type="license_plan",
                entity_id=str(plan_id),
                metadata={"name": row["name"]},
            )
        return row

    def set_plan_status(self, actor_id: int, plan_id: int, status: str) -> dict[str, Any]:
        self.authorize(actor_id)
        with transaction(self.conn):
            repo = PlanRepository(self.conn)
            before = repo.get_by_id(int(plan_id))
            if before is None:
                raise NotFoundError("plan not found")
            row = repo.update_status(int(plan_id), status)
            assert row is not None
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=None,
                action="plan.status",
                entity_type="license_plan",
                entity_id=str(plan_id),
                metadata={"from": before["status"], "to": status},
            )
        return row

    def list_licenses(
        self, actor_id: int, *, page: int = 0, page_size: int = 8, query: str = ""
    ) -> Page:
        self.authorize(actor_id)
        page, page_size, offset = _page_bounds(page, page_size)
        repo = LicenseRepository(self.conn)
        items = (
            repo.search(query, limit=page_size + 1, offset=offset)
            if str(query or "").strip()
            else repo.list(limit=page_size + 1, offset=offset)
        )
        return Page(items[:page_size], page, page_size, page > 0, len(items) > page_size)

    def get_license(self, actor_id: int, license_id: int) -> dict[str, Any]:
        self.authorize(actor_id)
        row = self.conn.execute(
            "SELECT l.*, t.name AS tenant_name, p.name AS plan_name"
            " FROM licenses AS l JOIN tenants AS t ON t.id = l.tenant_id"
            " JOIN license_plans AS p ON p.id = l.plan_id WHERE l.id = ?",
            (int(license_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError("license not found")
        return dict(row)

    def create_license(
        self, actor_id: int, *, tenant_id: int, plan_id: int, grace_days: int = 0
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        if int(grace_days) < 0:
            raise ValueError("grace_days must be >= 0")
        with transaction(self.conn):
            tenant = TenantRepository(self.conn).get_by_id(int(tenant_id))
            plan = PlanRepository(self.conn).get_by_id(int(plan_id))
            if tenant is None:
                raise NotFoundError("tenant not found")
            if plan is None or plan["status"] != "active":
                raise NotFoundError("active plan not found")
            starts = utcnow()
            expires = starts + timedelta(days=int(plan["duration_days"]))
            grace = expires + timedelta(days=int(grace_days)) if int(grace_days) else None
            row = LicenseRepository(self.conn).create(
                tenant_id=int(tenant_id),
                plan_id=int(plan_id),
                status="active",
                starts_at=iso_utc(starts),
                expires_at=iso_utc(expires),
                grace_until=iso_utc(grace) if grace else None,
            )
            AuditRepository(self.conn).append(
                actor_id=actor_id,
                tenant_id=int(tenant_id),
                action="license.create",
                entity_type="license",
                entity_id=str(row["id"]),
                metadata={"plan_id": int(plan_id), "grace_days": int(grace_days)},
            )
        self._invalidate_tenant(int(tenant_id))
        return row

    def renew(
        self, actor_id: int, license_id: int, *, extra_days: int, grace_days: int = 0
    ) -> dict[str, Any]:
        self.authorize(actor_id)
        current = LicenseRepository(self.conn).get_by_id(int(license_id))
        if current is None:
            raise NotFoundError("license not found")
        row = renew_license(
            self.conn,
            license_id=int(license_id),
            tenant_id=int(current["tenant_id"]),
            extra_days=int(extra_days),
            grace_days=int(grace_days),
            actor_id=actor_id,
        )
        self._invalidate_tenant(int(current["tenant_id"]))
        return row

    def suspend(self, actor_id: int, license_id: int) -> dict[str, Any]:
        self.authorize(actor_id)
        current = LicenseRepository(self.conn).get_by_id(int(license_id))
        if current is None:
            raise NotFoundError("license not found")
        if current["status"] == "suspended":
            self._invalidate_tenant(int(current["tenant_id"]))
            return current
        row = transition_license(
            self.conn,
            license_id=int(license_id),
            tenant_id=int(current["tenant_id"]),
            new_status="suspended",
            actor_id=actor_id,
        )
        self._invalidate_tenant(int(current["tenant_id"]))
        return row

    def reactivate(self, actor_id: int, license_id: int) -> dict[str, Any]:
        self.authorize(actor_id)
        current = LicenseRepository(self.conn).get_by_id(int(license_id))
        if current is None:
            raise NotFoundError("license not found")
        if current["status"] == "active":
            self._invalidate_tenant(int(current["tenant_id"]))
            return current
        if parse_utc(str(current["expires_at"])) <= utcnow():
            raise MasterServiceError("expired license must be renewed")
        row = transition_license(
            self.conn,
            license_id=int(license_id),
            tenant_id=int(current["tenant_id"]),
            new_status="active",
            actor_id=actor_id,
        )
        self._invalidate_tenant(int(current["tenant_id"]))
        return row

    def statistics(self, actor_id: int) -> dict[str, int]:
        self.authorize(actor_id)
        queries = {
            "tenants_total": "SELECT COUNT(*) FROM tenants",
            "tenants_active": "SELECT COUNT(*) FROM tenants WHERE status = 'active'",
            "bots_active": "SELECT COUNT(*) FROM tenant_bots WHERE status = 'active'",
            "plans_active": "SELECT COUNT(*) FROM license_plans WHERE status = 'active'",
            "licenses_active": "SELECT COUNT(*) FROM licenses WHERE status IN ('active', 'grace')",
            "licenses_suspended": "SELECT COUNT(*) FROM licenses WHERE status = 'suspended'",
            "warnings_open": (
                "SELECT COUNT(*) FROM notification_events"
                " WHERE status IN ('pending', 'processing', 'failed')"
            ),
        }
        return {
            key: int(self.conn.execute(sql).fetchone()[0])
            for key, sql in queries.items()
        }

    def list_warnings(
        self, actor_id: int, *, page: int = 0, page_size: int = 10
    ) -> Page:
        self.authorize(actor_id)
        page, page_size, offset = _page_bounds(page, page_size)
        items = NotificationRepository(self.conn).list_open(
            limit=page_size + 1, offset=offset
        )
        return Page(items[:page_size], page, page_size, page > 0, len(items) > page_size)

    def list_audit(self, actor_id: int, *, page: int = 0, page_size: int = 10) -> Page:
        self.authorize(actor_id)
        page, page_size, offset = _page_bounds(page, page_size)
        items = AuditRepository(self.conn).list_recent(
            limit=page_size + 1, offset=offset
        )
        for item in items:
            try:
                item["metadata"] = json.loads(item.pop("safe_metadata"))
            except (TypeError, ValueError):
                item["metadata"] = {}
                item.pop("safe_metadata", None)
        return Page(items[:page_size], page, page_size, page > 0, len(items) > page_size)
