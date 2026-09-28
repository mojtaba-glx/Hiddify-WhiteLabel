"""Bot registration derives an internally consistent encrypted credential."""

from __future__ import annotations

import inspect

import pytest

from Database.repositories import BotRepository, InvalidBotTokenFieldError
from Shared.crypto import (
    FernetTokenCipher,
    fingerprint_token,
    generate_key,
    token_tail,
)
from Tests.conftest import make_fake_token


def test_invalid_plain_token_rejected_by_register(conn, factories) -> None:
    tenant = factories.tenant()
    cipher = FernetTokenCipher(generate_key())
    plain = "not-a-telegram-token"
    with pytest.raises(InvalidBotTokenFieldError):
        BotRepository(conn).register(
            tenant_id=int(tenant["id"]),
            role="admin",
            cipher=cipher,
            plain_token=plain,
        )


def test_register_derives_all_credential_fields(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    token = make_fake_token("derived")
    row = BotRepository(conn).register(
        tenant_id=int(tenant["id"]), role="admin", cipher=cipher, plain_token=token
    )
    assert cipher.decrypt(row["encrypted_token"]) == token
    assert row["token_fingerprint"] == fingerprint_token(token)
    assert row["token_tail"] == token_tail(token)


def test_public_api_cannot_accept_independent_credential_fields() -> None:
    params = inspect.signature(BotRepository.register).parameters
    assert "encrypted_token" not in params
    assert "token_fingerprint" not in params
    assert "token_tail" not in params
    rotate_params = inspect.signature(BotRepository.rotate_token).parameters
    assert "encrypted_token" not in rotate_params
    assert "token_fingerprint" not in rotate_params
    assert "token_tail" not in rotate_params


def test_plain_hash_rejected(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    token = make_fake_token("hash")
    with pytest.raises(InvalidBotTokenFieldError):
        BotRepository(conn).register(
            tenant_id=int(tenant["id"]),
            role="admin",
            cipher=cipher,
            plain_token=token,
            webhook_secret_hash="plain-secret",  # not SHA-256 hex
        )


def test_rotate_rejects_invalid_plain_token(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, _ = factories.bot(int(tenant["id"]), "admin", cipher)
    with pytest.raises(InvalidBotTokenFieldError):
        BotRepository(conn).rotate_token(
            int(bot["id"]),
            cipher=cipher,
            plain_token="invalid-token",
        )


def test_rotate_derives_consistent_fields(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, _ = factories.bot(int(tenant["id"]), "admin", cipher)
    token = make_fake_token("rotated")
    row = BotRepository(conn).rotate_token(int(bot["id"]), cipher=cipher, plain_token=token)
    assert row is not None
    assert cipher.decrypt(row["encrypted_token"]) == token
    assert row["token_fingerprint"] == fingerprint_token(token)
    assert row["token_tail"] == token_tail(token)
