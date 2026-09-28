"""Typed, validated settings. Secrets never appear in repr or logs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


class SettingsError(ValueError):
    """Raised when environment configuration is missing or invalid."""


def _parse_admin_id(raw: str) -> int:
    try:
        value = int(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise SettingsError("MASTER_ADMIN_ID must be a positive integer") from exc
    if value <= 0:
        raise SettingsError("MASTER_ADMIN_ID must be a positive integer")
    return value


def _parse_positive_int(
    raw: object, *, name: str, default: int, minimum: int = 1
) -> int:
    text = str(raw or "").strip()
    if not text:
        return int(default)
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < int(minimum):
        raise SettingsError(f"{name} must be at least {int(minimum)}")
    return value


def _parse_nonnegative_int(raw: object, *, name: str, default: int = 0) -> int:
    text = str(raw or "").strip()
    if not text:
        return int(default)
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < 0:
        raise SettingsError(f"{name} must be non-negative")
    return value


@dataclass
class Settings:
    """Validated platform settings (no plain secrets in repr)."""

    master_bot_token: str = field(repr=False)
    master_admin_id: int
    token_encryption_key: str = field(repr=False)
    database_path: str = "data/whitelabel.db"
    display_timezone: str = "Asia/Tehran"
    license_job_interval_seconds: int = 60
    notification_lease_seconds: int = 120
    max_notification_retries: int = 5
    notification_retry_base_seconds: int = 30
    runtime_cache_ttl_seconds: int = 15

    def __post_init__(self) -> None:
        if not str(self.master_bot_token or "").strip() or ":" not in str(self.master_bot_token):
            raise SettingsError("MASTER_BOT_TOKEN is missing or malformed")
        if not isinstance(self.master_admin_id, int) or self.master_admin_id <= 0:
            raise SettingsError("MASTER_ADMIN_ID must be a positive integer")
        if not str(self.token_encryption_key or "").strip():
            raise SettingsError("TOKEN_ENCRYPTION_KEY is missing")
        if not str(self.database_path or "").strip():
            raise SettingsError("DATABASE_PATH is missing")
        if not str(self.display_timezone or "").strip():
            raise SettingsError("DISPLAY_TIMEZONE is missing")
        if int(self.license_job_interval_seconds) < 5:
            raise SettingsError("LICENSE_JOB_INTERVAL_SECONDS must be at least 5")
        if int(self.notification_lease_seconds) < 10:
            raise SettingsError("NOTIFICATION_LEASE_SECONDS must be at least 10")
        if int(self.max_notification_retries) < 1:
            raise SettingsError("MAX_NOTIFICATION_RETRIES must be at least 1")
        if int(self.notification_retry_base_seconds) < 1:
            raise SettingsError("NOTIFICATION_RETRY_BASE_SECONDS must be at least 1")
        if int(self.runtime_cache_ttl_seconds) < 1:
            raise SettingsError("RUNTIME_CACHE_TTL_SECONDS must be at least 1")

    def __repr__(self) -> str:
        return (
            "Settings(master_bot_token=[REDACTED], "
            f"master_admin_id={self.master_admin_id}, "
            "token_encryption_key=[REDACTED], "
            f"database_path={self.database_path!r}, "
            f"display_timezone={self.display_timezone!r}, "
            f"license_job_interval_seconds={self.license_job_interval_seconds}, "
            f"notification_lease_seconds={self.notification_lease_seconds}, "
            f"max_notification_retries={self.max_notification_retries}, "
            f"notification_retry_base_seconds={self.notification_retry_base_seconds}, "
            f"runtime_cache_ttl_seconds={self.runtime_cache_ttl_seconds})"
        )


@dataclass
class RuntimeSettings:
    """Tenant runner settings; intentionally excludes the MasterBot token."""

    token_encryption_key: str = field(repr=False)
    database_path: str = "data/whitelabel.db"
    shard_count: int = 4
    shard_index: int = 0
    reconcile_seconds: int = 15
    start_concurrency: int = 8
    poll_timeout_seconds: int = 20
    license_cache_ttl_seconds: int = 15

    def __post_init__(self) -> None:
        if not str(self.token_encryption_key or "").strip():
            raise SettingsError("TOKEN_ENCRYPTION_KEY is missing")
        if not str(self.database_path or "").strip():
            raise SettingsError("DATABASE_PATH is missing")
        if not 1 <= int(self.shard_count) <= 64:
            raise SettingsError("RUNTIME_SHARD_COUNT must be between 1 and 64")
        if not 0 <= int(self.shard_index) < int(self.shard_count):
            raise SettingsError("RUNTIME_SHARD_INDEX must be inside the shard range")
        if int(self.reconcile_seconds) < 5:
            raise SettingsError("RUNTIME_RECONCILE_SECONDS must be at least 5")
        if not 1 <= int(self.start_concurrency) <= 32:
            raise SettingsError("RUNTIME_START_CONCURRENCY must be between 1 and 32")
        if not 5 <= int(self.poll_timeout_seconds) <= 50:
            raise SettingsError("RUNTIME_POLL_TIMEOUT_SECONDS must be between 5 and 50")
        if int(self.license_cache_ttl_seconds) < 1:
            raise SettingsError("RUNTIME_CACHE_TTL_SECONDS must be at least 1")

    def __repr__(self) -> str:
        return (
            "RuntimeSettings(token_encryption_key=[REDACTED], "
            f"database_path={self.database_path!r}, "
            f"shard_count={self.shard_count}, shard_index={self.shard_index}, "
            f"reconcile_seconds={self.reconcile_seconds}, "
            f"start_concurrency={self.start_concurrency}, "
            f"poll_timeout_seconds={self.poll_timeout_seconds}, "
            f"license_cache_ttl_seconds={self.license_cache_ttl_seconds})"
        )


def load_from_env(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build validated Settings from a mapping (defaults to os.environ)."""
    source = env if env is not None else os.environ
    database_path = str(source.get("DATABASE_PATH", "") or "").strip() or "data/whitelabel.db"
    return Settings(
        master_bot_token=str(source.get("MASTER_BOT_TOKEN", "") or "").strip(),
        master_admin_id=_parse_admin_id(str(source.get("MASTER_ADMIN_ID", "") or "")),
        token_encryption_key=str(source.get("TOKEN_ENCRYPTION_KEY", "") or "").strip(),
        database_path=database_path,
        display_timezone=str(source.get("DISPLAY_TIMEZONE", "") or "Asia/Tehran").strip() or "Asia/Tehran",
        license_job_interval_seconds=_parse_positive_int(
            source.get("LICENSE_JOB_INTERVAL_SECONDS"),
            name="LICENSE_JOB_INTERVAL_SECONDS", default=60, minimum=5,
        ),
        notification_lease_seconds=_parse_positive_int(
            source.get("NOTIFICATION_LEASE_SECONDS"),
            name="NOTIFICATION_LEASE_SECONDS", default=120, minimum=10,
        ),
        max_notification_retries=_parse_positive_int(
            source.get("MAX_NOTIFICATION_RETRIES"),
            name="MAX_NOTIFICATION_RETRIES", default=5,
        ),
        notification_retry_base_seconds=_parse_positive_int(
            source.get("NOTIFICATION_RETRY_BASE_SECONDS"),
            name="NOTIFICATION_RETRY_BASE_SECONDS", default=30,
        ),
        runtime_cache_ttl_seconds=_parse_positive_int(
            source.get("RUNTIME_CACHE_TTL_SECONDS"),
            name="RUNTIME_CACHE_TTL_SECONDS", default=15,
        ),
    )


def load_database_path(env: Optional[Mapping[str, str]] = None) -> str:
    """Lenient helper: DATABASE_PATH or legacy DATABASE_PATH key, else default."""
    source = env if env is not None else os.environ
    for key in ("DATABASE_PATH", "DATABASE_URL", "DB_PATH"):
        value = str(source.get(key, "") or "").strip()
        if value and not value.startswith("postgres"):
            return value
    return "data/whitelabel.db"


def load_runtime_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> RuntimeSettings:
    """Load only the secrets and controls required by TenantRuntime."""
    source = env if env is not None else os.environ
    count = _parse_positive_int(
        source.get("RUNTIME_SHARD_COUNT"),
        name="RUNTIME_SHARD_COUNT",
        default=4,
    )
    index = _parse_nonnegative_int(
        source.get("RUNTIME_SHARD_INDEX"),
        name="RUNTIME_SHARD_INDEX",
        default=0,
    )
    return RuntimeSettings(
        token_encryption_key=str(source.get("TOKEN_ENCRYPTION_KEY", "") or "").strip(),
        database_path=(
            str(source.get("DATABASE_PATH", "") or "").strip()
            or "data/whitelabel.db"
        ),
        shard_count=count,
        shard_index=index,
        reconcile_seconds=_parse_positive_int(
            source.get("RUNTIME_RECONCILE_SECONDS"),
            name="RUNTIME_RECONCILE_SECONDS",
            default=15,
            minimum=5,
        ),
        start_concurrency=_parse_positive_int(
            source.get("RUNTIME_START_CONCURRENCY"),
            name="RUNTIME_START_CONCURRENCY",
            default=8,
        ),
        poll_timeout_seconds=_parse_positive_int(
            source.get("RUNTIME_POLL_TIMEOUT_SECONDS"),
            name="RUNTIME_POLL_TIMEOUT_SECONDS",
            default=20,
            minimum=5,
        ),
        license_cache_ttl_seconds=_parse_positive_int(
            source.get("RUNTIME_CACHE_TTL_SECONDS"),
            name="RUNTIME_CACHE_TTL_SECONDS",
            default=15,
        ),
    )
