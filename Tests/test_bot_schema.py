"""Bot schema hardening: readiness, telegram id uniqueness, token_tail, webhook hash."""

from __future__ import annotations

import sqlite3

import pytest

from Database.repositories import BotRepository
from Shared.crypto import (
    hash_webhook_secret,
    token_tail,
    verify_webhook_secret,
)


def test_readiness_requires_both_roles(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bots = BotRepository(conn)
    assert bots.readiness(int(tenant["id"])) == {"admin": False, "user": False, "ready": False}
    factories.bot(int(tenant["id"]), "admin", cipher)
    assert bots.readiness(int(tenant["id"]))["ready"] is False
    factories.bot(int(tenant["id"]), "user", cipher)
    assert bots.readiness(int(tenant["id"])) == {"admin": True, "user": True, "ready": True}


def test_readiness_ignores_inactive_bot(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, _ = factories.bot(int(tenant["id"]), "admin", cipher)
    factories.bot(int(tenant["id"]), "user", cipher)
    BotRepository(conn).update_status(int(bot["id"]), "disabled")
    conn.commit()
    assert BotRepository(conn).readiness(int(tenant["id"]))["ready"] is False


def test_telegram_bot_id_unique(conn, factories, cipher) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    bot_a, _ = factories.bot(int(tenant_a["id"]), "admin", cipher)
    bot_b, _ = factories.bot(int(tenant_b["id"]), "admin", cipher)
    bots = BotRepository(conn)
    bots.set_telegram_identity(int(bot_a["id"]), telegram_bot_id=5550001, telegram_username="a_bot")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        bots.set_telegram_identity(int(bot_b["id"]), telegram_bot_id=5550001)
    conn.rollback()


def test_token_tail_stored_safely(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, plain = factories.bot(int(tenant["id"]), "admin", cipher)
    assert bot["token_tail"] == token_tail(plain)
    assert bot["token_tail"] != plain
    assert plain not in str(bot["token_tail"])


def test_webhook_secret_stored_only_as_hash(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, _ = factories.bot(int(tenant["id"]), "admin", cipher)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenant_bots)")}
    assert "webhook_secret" not in columns
    assert "webhook_secret_hash" in columns
    stored = str(bot["webhook_secret_hash"] or "")
    assert len(stored) == 64


def test_constant_time_webhook_verification() -> None:
    secret = "raw-webhook-secret-value"
    digest = hash_webhook_secret(secret)
    assert verify_webhook_secret(secret, digest) is True
    assert verify_webhook_secret("wrong", digest) is False
    assert verify_webhook_secret("", digest) is False
    with pytest.raises(Exception):
        hash_webhook_secret("")
