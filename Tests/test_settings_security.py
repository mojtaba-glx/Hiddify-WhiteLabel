"""Settings validation, secret hygiene and owner gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from Shared.access import AccessDenied, is_master_admin, require_master_admin
from Shared.crypto import generate_key
from Shared.settings import Settings, SettingsError, load_from_env

ROOT = Path(__file__).resolve().parents[1]


def _env(**overrides):
    base = {
        "MASTER_BOT_TOKEN": "990001:FAKE_master_token_abcdefghijklmnopqrstuvwxyz_001",
        "MASTER_ADMIN_ID": "123456",
        "TOKEN_ENCRYPTION_KEY": generate_key(),
        "DATABASE_PATH": "data/whitelabel.db",
        "DISPLAY_TIMEZONE": "Asia/Tehran",
    }
    base.update(overrides)
    return base


def test_valid_settings_load() -> None:
    settings = load_from_env(_env())
    assert settings.master_admin_id == 123456
    assert "990001" not in repr(settings)
    assert settings.token_encryption_key not in repr(settings)
    assert settings.license_job_interval_seconds == 60
    assert settings.notification_lease_seconds == 120
    assert settings.max_notification_retries == 5


def test_missing_or_bad_settings_rejected() -> None:
    with pytest.raises(SettingsError):
        load_from_env(_env(MASTER_BOT_TOKEN=""))
    with pytest.raises(SettingsError):
        load_from_env(_env(MASTER_BOT_TOKEN="no-colon"))
    with pytest.raises(SettingsError):
        load_from_env(_env(MASTER_ADMIN_ID="0"))
    with pytest.raises(SettingsError):
        load_from_env(_env(MASTER_ADMIN_ID="abc"))
    with pytest.raises(SettingsError):
        load_from_env(_env(TOKEN_ENCRYPTION_KEY=""))
    with pytest.raises(SettingsError):
        load_from_env(_env(LICENSE_JOB_INTERVAL_SECONDS="4"))
    with pytest.raises(SettingsError):
        load_from_env(_env(NOTIFICATION_LEASE_SECONDS="bad"))
    with pytest.raises(SettingsError):
        load_from_env(_env(MAX_NOTIFICATION_RETRIES="0"))


def test_phase3_settings_can_be_overridden() -> None:
    settings = load_from_env(_env(
        LICENSE_JOB_INTERVAL_SECONDS="15",
        NOTIFICATION_LEASE_SECONDS="45",
        MAX_NOTIFICATION_RETRIES="3",
        NOTIFICATION_RETRY_BASE_SECONDS="5",
        RUNTIME_CACHE_TTL_SECONDS="2",
    ))
    assert settings.license_job_interval_seconds == 15
    assert settings.notification_lease_seconds == 45
    assert settings.max_notification_retries == 3
    assert settings.notification_retry_base_seconds == 5
    assert settings.runtime_cache_ttl_seconds == 2


def test_direct_construction_hides_secrets() -> None:
    settings = Settings(
        master_bot_token="1:fake",
        master_admin_id=9,
        token_encryption_key="k",
    )
    dumped = repr(settings)
    assert "1:fake" not in dumped
    assert "'k'" not in dumped and "=k" not in dumped


def test_env_example_has_no_secrets() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MASTER_BOT_TOKEN=" in text
    assert "MASTER_ADMIN_ID=" in text
    assert "TOKEN_ENCRYPTION_KEY=" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("MASTER_BOT_TOKEN=", "TOKEN_ENCRYPTION_KEY=")):
            value = stripped.split("=", 1)[1].strip()
            assert value == "", f"{stripped[:20]}... must stay empty, got {value!r}"


def test_owner_gate() -> None:
    assert is_master_admin(123, 123) is True
    assert is_master_admin(124, 123) is False
    assert is_master_admin(None, 123) is False
    assert is_master_admin(0, 0) is False
    require_master_admin(123, 123)
    with pytest.raises(AccessDenied):
        require_master_admin(124, 123)
