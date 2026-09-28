"""Phase-1 final review: failing tests for known defects, written before fixes."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from Database.connection import connect, transaction
from Database.migrate import (
    MigrationError,
    apply_migration,
    migrate,
    split_statements,
)
from Database.repositories import (
    AuditRepository,
    LicenseConflictError,
    LicenseRepository,
    NotificationRepository,
    TenantMismatchError,
)
from LicenseService.service import (
    LicenseTransitionError,
    renew_license,
    transition_license,
)
from Shared.crypto import (
    build_bot_credential,
    fingerprint_token,
    generate_key,
    rotate_bot_credential,
    token_tail,
)
from Shared.redaction import redact_text
from Shared.timeutils import iso_utc, parse_utc, utcnow
from Tests.conftest import make_fake_token

# ---- 1. Migration splitter: CASE..END in trigger ----

_TRIGGER_SQL = """\
CREATE TABLE a(x);
CREATE TABLE l(x);
CREATE TRIGGER tr AFTER INSERT ON a
BEGIN
    INSERT INTO l VALUES(
        CASE WHEN NEW.x = 1 THEN 1 ELSE 2 END
    );
    INSERT INTO l VALUES(3);
END;
"""


def test_splitter_trigger_with_case_end() -> None:
    stmts = split_statements(_TRIGGER_SQL)
    assert len(stmts) == 3, f"expected 3 statements, got {len(stmts)}"
    assert "CREATE TABLE a" in stmts[0]
    assert "CREATE TABLE l" in stmts[1]
    assert "CREATE TRIGGER" in stmts[2] and "END" in stmts[2]


def test_splitter_trigger_executes_and_fires(conn, tmp_path) -> None:
    d = tmp_path / "mig_tr"
    d.mkdir()
    path = d / "0001_tr.sql"
    path.write_text(_TRIGGER_SQL, encoding="utf-8")
    apply_migration(conn, path)
    conn.execute("INSERT INTO a(x) VALUES (1)")
    conn.execute("INSERT INTO a(x) VALUES (2)")
    rows = conn.execute("SELECT x FROM l ORDER BY x").fetchall()
    assert [int(r["x"]) for r in rows] == [1, 2, 3, 3]


def test_splitter_line_comment_with_begin_end() -> None:
    sql = "-- BEGIN semicolon; not real\nCREATE TABLE t (id INTEGER);"
    stmts = split_statements(sql)
    assert len(stmts) == 1


def test_splitter_preserves_line_comment_token_boundary() -> None:
    statement = split_statements("SELECT 1-- boundary\nAS value;")[0]
    row = sqlite3.connect(":memory:").execute(statement).fetchone()
    assert row == (1,)


def test_splitter_preserves_block_comment_token_boundary() -> None:
    statement = split_statements("SELECT 1/* boundary */AS value;")[0]
    row = sqlite3.connect(":memory:").execute(statement).fetchone()
    assert row == (1,)


def test_splitter_weekend_not_counted() -> None:
    sql = "SELECT 1 WHERE 'weekend' != 'workday';CREATE TABLE x(id);"
    stmts = split_statements(sql)
    assert len(stmts) == 2


def test_splitter_unclosed_string_raises(conn, tmp_path) -> None:
    d = tmp_path / "unclosed"
    d.mkdir()
    path = d / "0001_bad.sql"
    path.write_text("CREATE TABLE t (v TEXT);\nSELECT 'unclosed;", encoding="utf-8")
    with pytest.raises(MigrationError):
        apply_migration(conn, path)


def test_splitter_unclosed_block_comment_raises(conn, tmp_path) -> None:
    d = tmp_path / "ucbc"
    d.mkdir()
    path = d / "0001_bc.sql"
    path.write_text("CREATE TABLE t (v TEXT);\nSELECT 1; /* never closed", encoding="utf-8")
    with pytest.raises(MigrationError):
        apply_migration(conn, path)


# ---- 2. Audit: non-JSON object rejected, action/entity strings redacted ----

class _UnsafeStr:
    def __str__(self) -> str:
        return "failed with 990001:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456"


def test_audit_rejects_unknown_object_type() -> None:
    """fail-closed: unknown types raise ValueError (no default=str leak)."""
    with pytest.raises(ValueError) as exc:
        from Database.repositories import sanitize_metadata
        sanitize_metadata({"error": _UnsafeStr()})
    msg = str(exc.value)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456" not in msg


def test_audit_action_entity_redacted_in_db(conn, factories) -> None:
    tenant = factories.tenant()
    token = make_fake_token("action_redact")
    audits = AuditRepository(conn)
    audits.append(
        actor_id=1, tenant_id=int(tenant["id"]),
        action=f"error with {token}",
        entity_type=f"tenant {token}",
        entity_id=f"id {token}",
        metadata={"msg": "ok"},
    )
    stored = conn.execute(
        "SELECT action, entity_type, entity_id, safe_metadata FROM audit_events WHERE id = ?"
        " ORDER BY id DESC LIMIT 1",
        (audits.append(
            actor_id=1, tenant_id=int(tenant["id"]),
            action=f"error with {token}",
            entity_type=f"tenant {token}",
            entity_id=f"id {token}",
            metadata={"msg": "ok"},
        )["id"],),
    ).fetchone()
    from Shared.redaction import redact_text
    assert token not in stored["action"]
    assert token not in stored["entity_type"]
    assert token not in stored["entity_id"]
    assert token not in stored["safe_metadata"]


# ---- 3. Redaction: escaped SQL quotes ----

def test_redact_sql_escaped_single_quote() -> None:
    text = "secret: 'it''s a; ; secret'"
    cleaned = redact_text(text)
    assert "it''s a" not in cleaned
    assert "a; ;" not in cleaned
    assert cleaned.count("[REDACTED]") >= 1


def test_redact_sql_escaped_double_quote() -> None:
    text = 'password: "some""thing; weird"'
    cleaned = redact_text(text)
    assert "some" not in cleaned
    assert "weird" not in cleaned
    assert cleaned.count("[REDACTED]") >= 1


def test_redact_backslash_escaped_single_quote_completely() -> None:
    cleaned = redact_text("password='it\\'s secret value' after")
    assert cleaned == "password=[REDACTED] after"


def test_redact_backslash_escaped_double_quote_completely() -> None:
    cleaned = redact_text('password="he said \\"secret\\" value" after')
    assert cleaned == "password=[REDACTED] after"


# ---- 4. Notification enqueue: FK, real race ----

def test_notification_enqueue_bad_fk_raises(conn, factories) -> None:
    repo = NotificationRepository(conn)
    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue(
            tenant_id=99999999, event_type="x", event_key="license:fk:7d",
            scheduled_at=iso_utc(utcnow()),
        )


def test_notification_enqueue_two_connections_real_race(db_path, factories) -> None:
    tenant = factories.tenant()
    key = "license:race:1d"
    now = iso_utc(utcnow())
    repo_a = NotificationRepository(connect(db_path))
    repo_b = NotificationRepository(connect(db_path))
    try:
        row_a = repo_a.enqueue(
            tenant_id=int(tenant["id"]), event_type="x", event_key=key, scheduled_at=now,
        )
        row_b = repo_b.enqueue(
            tenant_id=int(tenant["id"]), event_type="x", event_key=key, scheduled_at=now,
        )
        assert row_a is not None and row_b is not None
        assert row_a["id"] == row_b["id"]
    finally:
        repo_a.conn.close()
        repo_b.conn.close()


# ---- 5. LicenseRepository.create: IntegrityError scoping ----

def test_license_create_bad_tenant_raises_fk(conn, factories) -> None:
    plan = factories.plan()
    repo = LicenseRepository(conn)
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(
            tenant_id=99999999, plan_id=int(plan["id"]), status="pending",
            starts_at="2030-01-01T00:00:00Z", expires_at="2030-02-01T00:00:00Z",
        )


def test_license_create_bad_plan_raises_fk(conn, factories) -> None:
    tenant = factories.tenant()
    repo = LicenseRepository(conn)
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(
            tenant_id=int(tenant["id"]), plan_id=99999999, status="pending",
            starts_at="2030-01-01T00:00:00Z", expires_at="2030-02-01T00:00:00Z",
        )


def test_license_create_valid_conflict_raises_licenseconflict(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    with pytest.raises(LicenseConflictError):
        factories.license(tenant["id"], plan["id"], status="grace")


def test_pending_bad_plan_is_fk_even_with_existing_active_license(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    factories.license(tenant["id"], plan["id"], status="active")
    repo = LicenseRepository(conn)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        repo.create(
            tenant_id=int(tenant["id"]), plan_id=99999999, status="pending",
            starts_at="2030-01-01T00:00:00Z", expires_at="2030-02-01T00:00:00Z",
        )
    assert getattr(exc.value, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_FOREIGNKEY"


# ---- 6. suspended_at set/clear + atomic audit ----

def test_transition_to_suspended_sets_suspended_at(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    updated = transition_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        new_status="suspended", actor_id=1,
    )
    assert updated["status"] == "suspended"
    assert updated["suspended_at"] is not None
    assert updated["suspended_at"].endswith("+00:00")


def test_transition_out_of_suspended_clears_suspended_at(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="suspended")
    updated = transition_license(
        conn, license_id=int(lic["id"]), tenant_id=int(tenant["id"]),
        new_status="active", actor_id=1,
    )
    assert updated["status"] == "active"
    assert updated["suspended_at"] is None


def test_suspend_with_audit_rolls_back_on_audit_failure(conn, factories) -> None:
    tenant = factories.tenant()
    plan = factories.plan()
    lic = factories.license(tenant["id"], plan["id"], status="active")
    repo = LicenseRepository(conn)
    audits = AuditRepository(conn)
    with pytest.raises(ValueError):
        with transaction(conn):
            old = repo.get_by_id(int(lic["id"]))
            assert old["status"] == "active"
            assert repo.transition(int(lic["id"]), expected="active", new="suspended") is True
            # Inject a token into the audit metadata — this should cause
            # the transaction to roll back.
            audits.append(
                actor_id=1, tenant_id=int(tenant["id"]), action="license.transition",
                entity_type="license", entity_id=str(lic["id"]),
                metadata={"bot_token": make_fake_token("rollbacktest")},
            )
    assert repo.get_by_id(int(lic["id"]))["status"] == "active"


# ---- 7. Bot credential integrity ----

def test_build_credential_produces_consistent_fields() -> None:
    cipher = __import__("Shared.crypto", fromlist=["FernetTokenCipher"]).FernetTokenCipher(generate_key())
    token = make_fake_token("build")
    cred = build_bot_credential(cipher, token)
    assert cred["encrypted_token"].startswith("gAAAA")
    assert len(cred["token_fingerprint"]) == 64
    assert len(cred["token_tail"]) == 4
    assert cipher.decrypt(cred["encrypted_token"]) == token
    assert cred["token_fingerprint"] == fingerprint_token(token)
    assert cred["token_tail"] == token_tail(token)


def test_rotate_credential_produces_consistent_fields() -> None:
    cipher = __import__("Shared.crypto", fromlist=["FernetTokenCipher"]).FernetTokenCipher(generate_key())
    old_cred = build_bot_credential(cipher, make_fake_token("old"))
    new_token = make_fake_token("new")
    new_cred = rotate_bot_credential(cipher, new_token)
    assert new_cred["encrypted_token"] != old_cred["encrypted_token"]
    assert new_cred["token_fingerprint"] != old_cred["token_fingerprint"]
    assert cipher.decrypt(new_cred["encrypted_token"]) == new_token


def test_credential_api_guarantees_consistency(conn, factories, cipher) -> None:
    from Database.repositories import BotRepository
    from Shared.crypto import build_bot_credential, generate_webhook_secret, hash_webhook_secret
    tenant = factories.tenant()
    token = make_fake_token("consist")
    cred = build_bot_credential(cipher, token)
    row = BotRepository(conn).register(
        tenant_id=int(tenant["id"]), role="admin",
        cipher=cipher,
        plain_token=token,
        webhook_secret_hash=hash_webhook_secret(generate_webhook_secret()),
    )
    # Fernet uses a fresh nonce for each encryption, so ciphertext equality is
    # neither expected nor required. Both payloads must decrypt to the token.
    assert cipher.decrypt(cred["encrypted_token"]) == token
    assert cipher.decrypt(row["encrypted_token"]) == token
    assert row["token_fingerprint"] == cred["token_fingerprint"]
    assert row["token_tail"] == cred["token_tail"]
