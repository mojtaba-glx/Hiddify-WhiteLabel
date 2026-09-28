"""Verify bot identities and provision one complete tenant atomically."""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from Database.connection import transaction
from Database.repositories import (
    AuditRepository,
    BotRepository,
    TenantRepository,
    TenantRuntimeRepository,
)
from Shared.access import require_master_admin
from Shared.crypto import (
    TokenCipher,
    fingerprint_token,
    generate_webhook_secret,
    hash_webhook_secret,
    token_tail,
)


class ProvisioningError(RuntimeError):
    """Safe provisioning failure; its message never includes credentials."""


class BotVerifier(Protocol):
    async def verify(self, token: str) -> Any:
        """Return an object with telegram_bot_id and optional username."""


@dataclass(frozen=True)
class PreparedBot:
    """Verified bot draft safe to keep briefly in an in-memory UI flow."""

    role: str
    telegram_bot_id: int
    telegram_username: Optional[str]
    token_tail: str
    encrypted_token: str = field(repr=False)
    token_fingerprint: str = field(repr=False)


@dataclass(frozen=True)
class ProvisionedBotInfo:
    id: int
    role: str
    telegram_bot_id: int
    telegram_username: Optional[str]
    token_tail: str


@dataclass(frozen=True)
class WebhookSecrets:
    """Raw secrets returned once; only their hashes are persisted."""

    admin: str = field(repr=False)
    user: str = field(repr=False)


@dataclass(frozen=True)
class BotEnrollment:
    bot: ProvisionedBotInfo
    webhook_secret: str = field(repr=False)


@dataclass(frozen=True)
class ProvisioningResult:
    tenant_id: int
    tenant_public_id: str
    data_namespace: str
    admin_bot: ProvisionedBotInfo
    user_bot: ProvisionedBotInfo
    webhook_secrets: WebhookSecrets = field(repr=False)


@dataclass(frozen=True)
class ProvisioningStatus:
    tenant_id: int
    runtime_status: str
    admin_bot: bool
    user_bot: bool
    enabled: bool
    ready: bool


def _bot_info(row: dict[str, Any]) -> ProvisionedBotInfo:
    return ProvisionedBotInfo(
        id=int(row["id"]),
        role=str(row["role"]),
        telegram_bot_id=int(row["telegram_bot_id"]),
        telegram_username=(str(row["telegram_username"]) if row["telegram_username"] else None),
        token_tail=str(row["token_tail"]),
    )


class TenantProvisioner:
    """Owner-gated, all-or-nothing provisioning and reversible enable/disable."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        master_admin_id: int,
        cipher: TokenCipher,
        bot_verifier: Optional[BotVerifier],
        cache_invalidator: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.conn = conn
        self.master_admin_id = int(master_admin_id)
        self.cipher = cipher
        self.bot_verifier = bot_verifier
        self.cache_invalidator = cache_invalidator

    def authorize(self, actor_id: int | None) -> None:
        require_master_admin(actor_id, self.master_admin_id)

    def _invalidate(self, tenant_id: int) -> None:
        if self.cache_invalidator is None:
            return
        try:
            self.cache_invalidator(int(tenant_id))
        except Exception:
            return

    async def prepare_bot(
        self, actor_id: int, *, role: str, plain_token: str
    ) -> PreparedBot:
        """Verify with Telegram, then retain only encrypted token material."""
        self.authorize(actor_id)
        if role not in ("admin", "user"):
            raise ValueError("role must be admin or user")
        if self.bot_verifier is None:
            raise ProvisioningError("bot verifier is unavailable")
        try:
            identity = await self.bot_verifier.verify(plain_token)
            telegram_bot_id = int(identity.telegram_bot_id)
            if telegram_bot_id <= 0:
                raise ValueError("invalid bot identity")
            username_raw = getattr(identity, "username", None)
            username = str(username_raw).strip() if username_raw else None
            encrypted = self.cipher.encrypt(plain_token)
            fingerprint = fingerprint_token(plain_token)
            tail = token_tail(plain_token)
        except Exception:
            raise ProvisioningError("Telegram bot token verification failed") from None
        return PreparedBot(
            role=role,
            telegram_bot_id=telegram_bot_id,
            telegram_username=username,
            token_tail=tail,
            encrypted_token=encrypted,
            token_fingerprint=fingerprint,
        )

    def _open_prepared(self, prepared: PreparedBot, expected_role: str) -> str:
        if prepared.role != expected_role or int(prepared.telegram_bot_id) <= 0:
            raise ProvisioningError("prepared bot does not match the required role")
        try:
            plain = self.cipher.decrypt(prepared.encrypted_token)
            fingerprint_ok = hmac.compare_digest(
                fingerprint_token(plain), prepared.token_fingerprint
            )
            tail_ok = hmac.compare_digest(token_tail(plain), prepared.token_tail)
            if not fingerprint_ok or not tail_ok:
                raise ValueError("credential mismatch")
            return plain
        except Exception:
            raise ProvisioningError("prepared bot credential is invalid") from None

    async def provision(
        self,
        actor_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
        admin_token: str,
        user_token: str,
    ) -> ProvisioningResult:
        admin = await self.prepare_bot(actor_id, role="admin", plain_token=admin_token)
        user = await self.prepare_bot(actor_id, role="user", plain_token=user_token)
        return self.provision_prepared(
            actor_id,
            name=name,
            slug=slug,
            owner_telegram_id=owner_telegram_id,
            admin_bot=admin,
            user_bot=user,
        )

    def provision_prepared(
        self,
        actor_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
        admin_bot: PreparedBot,
        user_bot: PreparedBot,
    ) -> ProvisioningResult:
        """Create tenant, namespace and both bots in one SQLite transaction."""
        self.authorize(actor_id)
        if int(admin_bot.telegram_bot_id) == int(user_bot.telegram_bot_id):
            raise ProvisioningError("admin and user bots must be different")
        if hmac.compare_digest(admin_bot.token_fingerprint, user_bot.token_fingerprint):
            raise ProvisioningError("admin and user bots must be different")

        admin_plain = self._open_prepared(admin_bot, "admin")
        user_plain = self._open_prepared(user_bot, "user")
        admin_secret = generate_webhook_secret()
        user_secret = generate_webhook_secret()
        try:
            with transaction(self.conn):
                tenant = TenantRepository(self.conn).create(
                    name=name,
                    slug=slug,
                    owner_telegram_id=int(owner_telegram_id),
                    status="active",
                )
                tenant_id = int(tenant["id"])
                runtime_repo = TenantRuntimeRepository(self.conn)
                runtime = runtime_repo.create(tenant_id=tenant_id, status="provisioning")
                bots = BotRepository(self.conn)
                admin_row = bots.register(
                    tenant_id=tenant_id,
                    role="admin",
                    cipher=self.cipher,
                    plain_token=admin_plain,
                    telegram_bot_id=int(admin_bot.telegram_bot_id),
                    telegram_username=admin_bot.telegram_username,
                    webhook_secret_hash=hash_webhook_secret(admin_secret),
                )
                user_row = bots.register(
                    tenant_id=tenant_id,
                    role="user",
                    cipher=self.cipher,
                    plain_token=user_plain,
                    telegram_bot_id=int(user_bot.telegram_bot_id),
                    telegram_username=user_bot.telegram_username,
                    webhook_secret_hash=hash_webhook_secret(user_secret),
                )
                runtime = runtime_repo.update_status(tenant_id, "ready")
                assert runtime is not None
                AuditRepository(self.conn).append(
                    actor_id=int(actor_id),
                    tenant_id=tenant_id,
                    action="tenant.provision",
                    entity_type="tenant",
                    entity_id=str(tenant_id),
                    metadata={
                        "admin_bot_id": int(admin_bot.telegram_bot_id),
                        "user_bot_id": int(user_bot.telegram_bot_id),
                        "data_namespace": str(runtime["data_namespace"]),
                    },
                )
        finally:
            # Minimize plaintext lifetime; Python cannot guarantee memory zeroing.
            del admin_plain
            del user_plain

        self._invalidate(tenant_id)
        return ProvisioningResult(
            tenant_id=tenant_id,
            tenant_public_id=str(tenant["public_id"]),
            data_namespace=str(runtime["data_namespace"]),
            admin_bot=_bot_info(admin_row),
            user_bot=_bot_info(user_row),
            webhook_secrets=WebhookSecrets(admin=admin_secret, user=user_secret),
        )

    def enroll_prepared_bot(
        self, actor_id: int, tenant_id: int, *, prepared: PreparedBot
    ) -> BotEnrollment:
        """Register/rotate one bot and issue a fresh one-time webhook secret."""
        self.authorize(actor_id)
        plain = self._open_prepared(prepared, prepared.role)
        webhook_secret = generate_webhook_secret()
        try:
            with transaction(self.conn):
                tenants = TenantRepository(self.conn)
                tenant = tenants.get_by_id(int(tenant_id))
                if tenant is None:
                    raise ProvisioningError("tenant not found")
                bots = BotRepository(self.conn)
                current = bots.get_by_tenant_role(int(tenant_id), prepared.role)
                if current is None:
                    row = bots.register(
                        tenant_id=int(tenant_id),
                        role=prepared.role,
                        cipher=self.cipher,
                        plain_token=plain,
                        telegram_bot_id=int(prepared.telegram_bot_id),
                        telegram_username=prepared.telegram_username,
                        webhook_secret_hash=hash_webhook_secret(webhook_secret),
                    )
                    operation = "registered"
                else:
                    row = bots.rotate_token(
                        int(current["id"]), cipher=self.cipher, plain_token=plain
                    )
                    assert row is not None
                    row = bots.set_telegram_identity(
                        int(row["id"]),
                        telegram_bot_id=int(prepared.telegram_bot_id),
                        telegram_username=prepared.telegram_username,
                    )
                    assert row is not None
                    row = bots.update_webhook_secret_hash(
                        int(row["id"]), hash_webhook_secret(webhook_secret)
                    )
                    assert row is not None
                    if row["status"] != "active":
                        row = bots.update_status(int(row["id"]), "active")
                        assert row is not None
                    operation = "rotated"

                runtime_repo = TenantRuntimeRepository(self.conn)
                runtime = runtime_repo.get_by_tenant(int(tenant_id))
                if runtime is None:
                    runtime = runtime_repo.create(
                        tenant_id=int(tenant_id), status="provisioning"
                    )
                readiness = bots.readiness(int(tenant_id))
                target = "ready" if readiness["ready"] and tenant["status"] == "active" else str(runtime["status"])
                if target == "ready" and runtime["status"] != "ready":
                    runtime = runtime_repo.update_status(int(tenant_id), "ready")
                    assert runtime is not None
                AuditRepository(self.conn).append(
                    actor_id=int(actor_id),
                    tenant_id=int(tenant_id),
                    action="tenant_bot.enroll",
                    entity_type="tenant_bot",
                    entity_id=str(row["id"]),
                    metadata={
                        "role": prepared.role,
                        "operation": operation,
                        "telegram_bot_id": int(prepared.telegram_bot_id),
                    },
                )
        finally:
            del plain
        self._invalidate(int(tenant_id))
        return BotEnrollment(bot=_bot_info(row), webhook_secret=webhook_secret)

    async def enroll_bot(
        self,
        actor_id: int,
        tenant_id: int,
        *,
        role: str,
        plain_token: str,
    ) -> BotEnrollment:
        prepared = await self.prepare_bot(actor_id, role=role, plain_token=plain_token)
        return self.enroll_prepared_bot(actor_id, tenant_id, prepared=prepared)

    def rotate_webhook_secret(
        self, actor_id: int, tenant_id: int, *, role: str
    ) -> BotEnrollment:
        self.authorize(actor_id)
        if role not in ("admin", "user"):
            raise ValueError("role must be admin or user")
        secret = generate_webhook_secret()
        with transaction(self.conn):
            bots = BotRepository(self.conn)
            row = bots.get_by_tenant_role(int(tenant_id), role)
            if row is None:
                raise ProvisioningError("tenant bot not found")
            row = bots.update_webhook_secret_hash(
                int(row["id"]), hash_webhook_secret(secret)
            )
            assert row is not None
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=int(tenant_id),
                action="tenant_bot.webhook_rotate",
                entity_type="tenant_bot",
                entity_id=str(row["id"]),
                metadata={"role": role},
            )
        return BotEnrollment(bot=_bot_info(row), webhook_secret=secret)

    def status(self, actor_id: int, tenant_id: int) -> ProvisioningStatus:
        self.authorize(actor_id)
        tenant = TenantRepository(self.conn).get_by_id(int(tenant_id))
        if tenant is None:
            raise ProvisioningError("tenant not found")
        runtime = TenantRuntimeRepository(self.conn).get_by_tenant(int(tenant_id))
        bot_state = BotRepository(self.conn).readiness(int(tenant_id))
        runtime_status = str(runtime["status"]) if runtime else "missing"
        enabled = tenant["status"] == "active" and runtime_status == "ready"
        ready = bool(enabled and bot_state["ready"])
        return ProvisioningStatus(
            tenant_id=int(tenant_id),
            runtime_status=runtime_status,
            admin_bot=bool(bot_state["admin"]),
            user_bot=bool(bot_state["user"]),
            enabled=bool(enabled),
            ready=ready,
        )

    def set_enabled(
        self, actor_id: int, tenant_id: int, *, enabled: bool
    ) -> ProvisioningStatus:
        self.authorize(actor_id)
        with transaction(self.conn):
            tenants = TenantRepository(self.conn)
            runtime_repo = TenantRuntimeRepository(self.conn)
            tenant = tenants.get_by_id(int(tenant_id))
            runtime = runtime_repo.get_by_tenant(int(tenant_id))
            if tenant is None or runtime is None:
                raise ProvisioningError("tenant is not provisioned")
            if enabled and not BotRepository(self.conn).readiness(int(tenant_id))["ready"]:
                raise ProvisioningError("both tenant bots must be active before enabling")
            tenant_target = "active" if enabled else "disabled"
            runtime_target = "ready" if enabled else "disabled"
            tenants.update_status(int(tenant_id), tenant_target)
            runtime_repo.update_status(int(tenant_id), runtime_target)
            AuditRepository(self.conn).append(
                actor_id=int(actor_id),
                tenant_id=int(tenant_id),
                action="tenant.runtime_enable" if enabled else "tenant.runtime_disable",
                entity_type="tenant_runtime",
                entity_id=str(tenant_id),
                metadata={"enabled": bool(enabled)},
            )
        self._invalidate(int(tenant_id))
        return self.status(actor_id, int(tenant_id))
