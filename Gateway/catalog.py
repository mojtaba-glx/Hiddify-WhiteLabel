"""Load and validate the bot subset assigned to one runtime shard."""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass, field

from Database.repositories import BotRepository
from Shared.crypto import TokenCipher, fingerprint_token


class RuntimeCatalogError(RuntimeError):
    """The runtime catalog could not be read safely."""


@dataclass(frozen=True)
class RuntimeBotSpec:
    bot_id: int
    tenant_id: int
    role: str
    telegram_bot_id: int
    telegram_username: str | None
    tenant_name: str
    owner_telegram_id: int
    data_namespace: str
    token_tail: str
    encrypted_token: str = field(repr=False)
    token_fingerprint: str = field(repr=False)

    @property
    def revision(self) -> tuple[object, ...]:
        """Any routing/presentation change causes a clean worker restart."""
        return (
            self.bot_id,
            self.tenant_id,
            self.role,
            self.telegram_bot_id,
            self.telegram_username,
            self.tenant_name,
            self.owner_telegram_id,
            self.data_namespace,
            self.token_fingerprint,
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    specs: tuple[RuntimeBotSpec, ...]
    rejected_bot_ids: tuple[int, ...] = ()


class RuntimeCatalog:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cipher: TokenCipher,
        shard_count: int,
        shard_index: int,
    ) -> None:
        self.conn = conn
        self.cipher = cipher
        self.shard_count = int(shard_count)
        self.shard_index = int(shard_index)
        if self.shard_count < 1 or self.shard_count > 64:
            raise ValueError("shard_count must be between 1 and 64")
        if self.shard_index < 0 or self.shard_index >= self.shard_count:
            raise ValueError("shard_index is outside shard_count")

    def _validated_spec(self, row: dict) -> RuntimeBotSpec:
        role = str(row.get("role") or "")
        if role not in ("admin", "user"):
            raise RuntimeCatalogError("invalid bot role")
        try:
            plain = self.cipher.decrypt(str(row.get("encrypted_token") or ""))
            actual = fingerprint_token(plain)
            expected = str(row.get("token_fingerprint") or "")
            if not hmac.compare_digest(actual, expected):
                raise ValueError("fingerprint mismatch")
            bot_id = int(row["bot_id"])
            tenant_id = int(row["tenant_id"])
            telegram_bot_id = int(row["telegram_bot_id"])
            owner_id = int(row["owner_telegram_id"])
            if min(bot_id, tenant_id, telegram_bot_id, owner_id) <= 0:
                raise ValueError("invalid catalog identity")
            namespace = str(row.get("data_namespace") or "").strip()
            if not namespace:
                raise ValueError("missing runtime namespace")
        except Exception:
            raise RuntimeCatalogError("invalid encrypted bot catalog entry") from None
        finally:
            try:
                del plain
            except UnboundLocalError:
                pass
        return RuntimeBotSpec(
            bot_id=bot_id,
            tenant_id=tenant_id,
            role=role,
            telegram_bot_id=telegram_bot_id,
            telegram_username=(
                str(row["telegram_username"]) if row.get("telegram_username") else None
            ),
            tenant_name=str(row.get("tenant_name") or "Tenant"),
            owner_telegram_id=owner_id,
            data_namespace=namespace,
            token_tail=str(row.get("token_tail") or "****"),
            encrypted_token=str(row["encrypted_token"]),
            token_fingerprint=expected,
        )

    def load(self) -> CatalogSnapshot:
        try:
            rows = BotRepository(self.conn).list_runtime_candidates(
                shard_count=self.shard_count, shard_index=self.shard_index
            )
        except Exception:
            raise RuntimeCatalogError("runtime catalog query failed") from None
        specs: list[RuntimeBotSpec] = []
        rejected: list[int] = []
        for row in rows:
            try:
                specs.append(self._validated_spec(row))
            except RuntimeCatalogError:
                try:
                    rejected.append(int(row["bot_id"]))
                except Exception:
                    continue
        return CatalogSnapshot(tuple(specs), tuple(rejected))

    def decrypt_for_worker(self, spec: RuntimeBotSpec) -> str:
        """Decrypt just before Application construction and recheck integrity."""
        try:
            plain = self.cipher.decrypt(spec.encrypted_token)
            if not hmac.compare_digest(
                fingerprint_token(plain), spec.token_fingerprint
            ):
                raise ValueError("fingerprint mismatch")
            return plain
        except Exception:
            raise RuntimeCatalogError("bot credential integrity check failed") from None
