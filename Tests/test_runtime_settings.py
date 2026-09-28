"""Tenant runner configuration has no MasterBot token dependency."""

from __future__ import annotations

import pytest

from Shared.crypto import generate_key
from Shared.settings import SettingsError, load_runtime_from_env


def _env(**changes):
    env = {"TOKEN_ENCRYPTION_KEY": generate_key(), "DATABASE_PATH": "data/test.db"}
    env.update(changes)
    return env


def test_runtime_defaults_and_secret_repr() -> None:
    settings = load_runtime_from_env(_env())
    assert (settings.shard_count, settings.shard_index) == (4, 0)
    assert (settings.reconcile_seconds, settings.poll_timeout_seconds) == (15, 20)
    assert settings.start_concurrency == 8
    assert settings.lifecycle_seconds == 180
    assert settings.smart_sub_host == "127.0.0.1"
    assert settings.smart_sub_port == 8091
    assert settings.smart_sub_public_base_url == ""
    assert settings.token_encryption_key not in repr(settings)
    assert not hasattr(settings, "master_bot_token")


def test_runtime_overrides() -> None:
    settings = load_runtime_from_env(
        _env(
            RUNTIME_SHARD_COUNT="8",
            RUNTIME_SHARD_INDEX="7",
            RUNTIME_RECONCILE_SECONDS="30",
            RUNTIME_START_CONCURRENCY="12",
            RUNTIME_POLL_TIMEOUT_SECONDS="25",
            RUNTIME_CACHE_TTL_SECONDS="3",
            RUNTIME_LIFECYCLE_SECONDS="240",
            SMART_SUB_HOST="0.0.0.0",
            SMART_SUB_PORT="18091",
            SMART_SUB_PUBLIC_BASE_URL="https://sub.example.com",
        )
    )
    assert (settings.shard_count, settings.shard_index) == (8, 7)
    assert settings.reconcile_seconds == 30
    assert settings.start_concurrency == 12
    assert settings.poll_timeout_seconds == 25
    assert settings.license_cache_ttl_seconds == 3
    assert settings.lifecycle_seconds == 240
    assert settings.smart_sub_host == "0.0.0.0"
    assert settings.smart_sub_port == 18091
    assert settings.smart_sub_public_base_url == "https://sub.example.com"


@pytest.mark.parametrize(
    "change",
    [
        {"TOKEN_ENCRYPTION_KEY": ""},
        {"RUNTIME_SHARD_COUNT": "0"},
        {"RUNTIME_SHARD_COUNT": "65"},
        {"RUNTIME_SHARD_COUNT": "4", "RUNTIME_SHARD_INDEX": "4"},
        {"RUNTIME_SHARD_INDEX": "-1"},
        {"RUNTIME_RECONCILE_SECONDS": "4"},
        {"RUNTIME_START_CONCURRENCY": "0"},
        {"RUNTIME_START_CONCURRENCY": "33"},
        {"RUNTIME_POLL_TIMEOUT_SECONDS": "4"},
        {"RUNTIME_POLL_TIMEOUT_SECONDS": "51"},
        {"RUNTIME_LIFECYCLE_SECONDS": "59"},
        {"SMART_SUB_PORT": "0"},
        {"SMART_SUB_PORT": "65536"},
        {"SMART_SUB_PUBLIC_BASE_URL": "ftp://invalid.example"},
    ],
)
def test_invalid_runtime_settings_fail_closed(change) -> None:
    with pytest.raises(SettingsError):
        load_runtime_from_env(_env(**change))
