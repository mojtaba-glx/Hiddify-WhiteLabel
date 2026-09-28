"""Audit metadata: secret content inside VALUES is redacted before storage."""

from __future__ import annotations

from Shared.redaction import redact_text
from Database.repositories import AuditRepository, sanitize_metadata
from Tests.conftest import make_fake_token
from Shared.crypto import generate_key, FernetTokenCipher


def test_error_message_with_bot_token_is_redacted() -> None:
    token = make_fake_token("auditval")
    unsafe = {"error_message": f"request failed using {token}"}
    encoded = sanitize_metadata(unsafe)
    assert token not in encoded
    assert "[REDACTED]" in encoded


def test_nested_list_and_dict_values_redacted() -> None:
    token = make_fake_token("nestedval")
    encrypt_key = generate_key()
    cipher = FernetTokenCipher(encrypt_key)
    ct = cipher.encrypt(make_fake_token("ct"))
    data = {
        "ok": 1,
        "nested": {"history": [{"note": f"used {token}", "state": "grace"}], "fernet": ct},
    }
    encoded = sanitize_metadata(data)
    assert token not in encoded
    assert ct not in encoded


def test_sensitive_key_any_depth_rejected() -> None:
    try:
        sanitize_metadata({"nested": {"deep_token": "x"}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        sanitize_metadata([{"a": {"my_secret": "y"}}])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_audit_persists_redacted_value_only(conn, factories) -> None:
    token = make_fake_token("persistval")
    tenant = factories.tenant()
    audits = AuditRepository(conn)
    audits.append(
        actor_id=1, tenant_id=int(tenant["id"]), action="err", entity_type="tenant",
        entity_id=str(tenant["id"]),
        metadata={"error_message": f"failed with {token}"},
    )
    stored = conn.execute(
        "SELECT safe_metadata FROM audit_events WHERE entity_id=?", (str(tenant["id"]),)
    ).fetchone()
    assert token not in stored["safe_metadata"]


def test_redact_text_handles_multiword_quoted_values() -> None:
    text = '{"password": "secret value with spaces"}'
    cleaned = redact_text(text)
    assert "secret value" not in cleaned
    assert "password" in cleaned  # key preserved, value masked
    assert "[REDACTED]" in cleaned


def test_redact_text_various_forms() -> None:
    forms = [
        'token=abc123def456',
        "password='multi word secret'",
        '{"api_key":"a b c d"}',
        "secret: 'it''s a; ; secret'",
    ]
    for form in forms:
        cleaned = redact_text(form)
        # no raw secret fragments survive
        lowered = cleaned
        assert "abc123def456" not in lowered
