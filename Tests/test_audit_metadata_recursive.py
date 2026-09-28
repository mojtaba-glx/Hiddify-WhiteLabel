"""Audit metadata: recursive sensitive-key rejection with no value leakage."""

from __future__ import annotations

import pytest

from Database.repositories import AuditRepository, sanitize_metadata


def test_top_level_sensitive_key_rejected() -> None:
    for key in ("token", "bot_token", "webhook_secret", "password", "api_key", "encryption_key"):
        with pytest.raises(ValueError):
            sanitize_metadata({key: "some-secret-value"})


def test_nested_sensitive_key_rejected() -> None:
    nested = {"level1": {"level2": [{"deep_token": "x"}]}}
    with pytest.raises(ValueError):
        sanitize_metadata(nested)
    in_list = {"items": [{"ok": 1}, ("a", {"my_secret": "v"})]}
    with pytest.raises(ValueError):
        sanitize_metadata(in_list)


def test_value_not_in_exception_message() -> None:
    secret_value = "SUPER-SECRET-VALUE-123"
    try:
        sanitize_metadata({"nested": {"api_key": secret_value}})
        raise AssertionError("expected rejection")
    except ValueError as exc:
        assert secret_value not in str(exc)


def test_safe_nested_metadata_allowed() -> None:
    payload = {"from": "active", "to": "grace", "counts": [1, 2, 3], "meta": {"reason": "expiry"}}
    encoded = sanitize_metadata(payload)
    assert "active" in encoded and "expiry" in encoded


def test_audit_rejects_nested_and_persists_nothing(conn, factories) -> None:
    tenant = factories.tenant()
    audits = AuditRepository(conn)
    with pytest.raises(ValueError):
        audits.append(
            actor_id=1, tenant_id=int(tenant["id"]), action="x", entity_type="tenant",
            entity_id=str(tenant["id"]), metadata={"outer": {"webhook_secret": "abc"}},
        )
    assert audits.list_by_tenant(int(tenant["id"])) == []
