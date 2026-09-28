"""DB uniqueness: fingerprint global, (tenant, role) pair, slug/public_id."""

from __future__ import annotations

import sqlite3

import pytest

from Database.repositories import BotRepository
from Shared.crypto import fingerprint_token
from Tests.conftest import make_fake_token


def test_duplicate_fingerprint_rejected(conn, factories, cipher) -> None:
    tenant_a = factories.tenant()
    tenant_b = factories.tenant()
    shared = make_fake_token("dupe")
    factories.bot(int(tenant_a["id"]), "admin", cipher, token=shared)
    with pytest.raises(sqlite3.IntegrityError):
        BotRepository(conn).register(
            tenant_id=int(tenant_b["id"]),
            role="admin",
            cipher=cipher,
            plain_token=shared,
        )
    conn.rollback()


def test_one_admin_and_one_user_per_tenant(conn, factories, cipher) -> None:
    tenant = factories.tenant()
    factories.bot(int(tenant["id"]), "admin", cipher)
    with pytest.raises(sqlite3.IntegrityError):
        factories.bot(int(tenant["id"]), "admin", cipher)
    conn.rollback()
    # user role still free after the failed duplicate admin insert
    row, _ = factories.bot(int(tenant["id"]), "user", cipher)
    assert row["role"] == "user"


def test_duplicate_slug_rejected(conn, factories) -> None:
    tenant = factories.tenant()
    with pytest.raises(sqlite3.IntegrityError):
        factories.tenant(slug=tenant["slug"])
    conn.rollback()


def test_public_id_is_generated_and_unique(conn, factories) -> None:
    a = factories.tenant()
    b = factories.tenant()
    assert a["public_id"] != b["public_id"]
    assert len(a["public_id"]) >= 16
    assert " " not in a["public_id"]
    assert a["public_id"] != "1" and a["public_id"] != "abc"
