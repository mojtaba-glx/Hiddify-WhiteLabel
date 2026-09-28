"""Tenant isolation: scoped reads, state keys, routing, cross-tenant guard."""

from __future__ import annotations

import pytest

from Database.repositories import BotRepository
from Gateway.routing import (
    CrossTenantError,
    assert_bot_usable,
    assert_same_tenant,
    resolve_tenant_by_fingerprint,
    tenant_state_key,
)
from Shared.crypto import fingerprint_token


def test_bots_are_scoped_per_tenant(conn, factories, cipher) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    bot_a, _ = factories.bot(int(tenant_a["id"]), "admin", cipher)
    bots = BotRepository(conn)
    assert bots.get_by_tenant_role(int(tenant_a["id"]), "admin")["id"] == bot_a["id"]
    assert bots.get_by_tenant_role(int(tenant_b["id"]), "admin") is None
    assert bots.list_by_tenant(int(tenant_b["id"])) == []


def test_state_keys_differ_per_tenant_role_user(conn, factories) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    key_a = tenant_state_key(tenant_id=int(tenant_a["id"]), bot_role="admin", telegram_user_id=5)
    key_b = tenant_state_key(tenant_id=int(tenant_b["id"]), bot_role="admin", telegram_user_id=5)
    key_c = tenant_state_key(tenant_id=int(tenant_a["id"]), bot_role="user", telegram_user_id=5)
    assert len({key_a, key_b, key_c}) == 3
    with pytest.raises(ValueError):
        tenant_state_key(tenant_id=int(tenant_a["id"]), bot_role="owner", telegram_user_id=5)


def test_resolve_by_fingerprint_routes_correctly(conn, factories, cipher) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    _, plain_a = factories.bot(int(tenant_a["id"]), "user", cipher)
    _, plain_b = factories.bot(int(tenant_b["id"]), "user", cipher)
    row = resolve_tenant_by_fingerprint(conn, token_fingerprint=fingerprint_token(plain_a))
    assert row is not None and int(row["tenant_id"]) == int(tenant_a["id"])
    assert resolve_tenant_by_fingerprint(conn, token_fingerprint="0" * 64) is None
    assert resolve_tenant_by_fingerprint(conn, token_fingerprint="") is None


def test_cross_tenant_guard(factories) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    assert_same_tenant(int(tenant_a["id"]), int(tenant_a["id"]))
    with pytest.raises(CrossTenantError):
        assert_same_tenant(int(tenant_a["id"]), int(tenant_b["id"]))
    with pytest.raises(CrossTenantError):
        assert_same_tenant(0, int(tenant_b["id"]))


def test_disabled_bot_is_unusable(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    bot, _ = factories.bot(int(tenant["id"]), "admin", cipher)
    assert_bot_usable(bot)
    BotRepository(conn).update_status(int(bot["id"]), "revoked")
    conn.commit()
    with pytest.raises(CrossTenantError):
        assert_bot_usable(BotRepository(conn).get_by_id(int(bot["id"])))
    with pytest.raises(CrossTenantError):
        assert_bot_usable(None)
