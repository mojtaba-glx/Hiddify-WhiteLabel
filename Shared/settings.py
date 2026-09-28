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

def _parse_float_range(
    raw: object,
    *,
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    text = str(raw or "").strip()
    if not text:
        return float(default)
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if not float(minimum) <= value <= float(maximum):
        raise SettingsError(
            f"{name} must be between {float(minimum)} and {float(maximum)}"
        )
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
    lifecycle_seconds: int = 180
    smart_sub_host: str = "127.0.0.1"
    smart_sub_port: int = 8091
    smart_sub_public_base_url: str = ""
    enforcer_seconds: int = 20
    enforcer_batch_size: int = 30
    enforcer_hot_usage_ratio: float = 0.85
    node_freeze_failures: int = 3
    reminder_days: int = 3
    reminder_remaining_gb: int = 3
    reminder_lease_seconds: int = 120
    reminder_max_retries: int = 5
    reminder_retry_base_seconds: int = 30

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
        if int(self.lifecycle_seconds) < 60:
            raise SettingsError("RUNTIME_LIFECYCLE_SECONDS must be at least 60")
        if not str(self.smart_sub_host or "").strip():
            raise SettingsError("SMART_SUB_HOST is required")
        if not 1 <= int(self.smart_sub_port) <= 65535:
            raise SettingsError("SMART_SUB_PORT must be between 1 and 65535")
        public_base = str(self.smart_sub_public_base_url or "").strip()
        if public_base and not (
            public_base.startswith("http://") or public_base.startswith("https://")
        ):
            raise SettingsError("SMART_SUB_PUBLIC_BASE_URL must use http or https")
        if not 10 <= int(self.enforcer_seconds) <= 3600:
            raise SettingsError("RUNTIME_ENFORCER_SECONDS must be between 10 and 3600")
        if not 1 <= int(self.enforcer_batch_size) <= 500:
            raise SettingsError("RUNTIME_ENFORCER_BATCH_SIZE must be between 1 and 500")
        if not 0.5 <= float(self.enforcer_hot_usage_ratio) <= 1.0:
            raise SettingsError("RUNTIME_ENFORCER_HOT_USAGE_RATIO must be between 0.5 and 1.0")
        if not 1 <= int(self.node_freeze_failures) <= 20:
            raise SettingsError("RUNTIME_NODE_FREEZE_FAILURES must be between 1 and 20")
        if not 1 <= int(self.reminder_days) <= 30:
            raise SettingsError("RUNTIME_REMINDER_DAYS must be between 1 and 30")
        if not 1 <= int(self.reminder_remaining_gb) <= 1000:
            raise SettingsError("RUNTIME_REMINDER_REMAINING_GB must be between 1 and 1000")
        if int(self.reminder_lease_seconds) < 10:
            raise SettingsError("RUNTIME_REMINDER_LEASE_SECONDS must be at least 10")
        if not 1 <= int(self.reminder_max_retries) <= 20:
            raise SettingsError("RUNTIME_REMINDER_MAX_RETRIES must be between 1 and 20")
        if not 1 <= int(self.reminder_retry_base_seconds) <= 3600:
            raise SettingsError("RUNTIME_REMINDER_RETRY_BASE_SECONDS must be between 1 and 3600")

    def __repr__(self) -> str:
        return (
            "RuntimeSettings(token_encryption_key=[REDACTED], "
            f"database_path={self.database_path!r}, "
            f"shard_count={self.shard_count}, shard_index={self.shard_index}, "
            f"reconcile_seconds={self.reconcile_seconds}, "
            f"start_concurrency={self.start_concurrency}, "
            f"poll_timeout_seconds={self.poll_timeout_seconds}, "
            f"license_cache_ttl_seconds={self.license_cache_ttl_seconds}, "
            f"lifecycle_seconds={self.lifecycle_seconds}, "
            f"smart_sub_host={self.smart_sub_host!r}, "
            f"smart_sub_port={self.smart_sub_port}, "
            f"smart_sub_public_base_url={self.smart_sub_public_base_url!r}, "
            f"enforcer_seconds={self.enforcer_seconds}, "
            f"enforcer_batch_size={self.enforcer_batch_size}, "
            f"enforcer_hot_usage_ratio={self.enforcer_hot_usage_ratio}, "
            f"node_freeze_failures={self.node_freeze_failures}, "
            f"reminder_days={self.reminder_days}, "
            f"reminder_remaining_gb={self.reminder_remaining_gb}, "
            f"reminder_lease_seconds={self.reminder_lease_seconds}, "
            f"reminder_max_retries={self.reminder_max_retries}, "
            f"reminder_retry_base_seconds={self.reminder_retry_base_seconds})"
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
        lifecycle_seconds=_parse_positive_int(
            source.get("RUNTIME_LIFECYCLE_SECONDS"),
            name="RUNTIME_LIFECYCLE_SECONDS",
            default=180,
            minimum=60,
        ),
        smart_sub_host=str(
            source.get("SMART_SUB_HOST", "") or "127.0.0.1"
        ).strip(),
        smart_sub_port=_parse_positive_int(
            source.get("SMART_SUB_PORT"),
            name="SMART_SUB_PORT",
            default=8091,
        ),
        smart_sub_public_base_url=str(
            source.get("SMART_SUB_PUBLIC_BASE_URL", "") or ""
        ).strip().rstrip("/"),
        enforcer_seconds=_parse_positive_int(
            source.get("RUNTIME_ENFORCER_SECONDS"),
            name="RUNTIME_ENFORCER_SECONDS",
            default=20,
            minimum=10,
        ),
        enforcer_batch_size=_parse_positive_int(
            source.get("RUNTIME_ENFORCER_BATCH_SIZE"),
            name="RUNTIME_ENFORCER_BATCH_SIZE",
            default=30,
        ),
        enforcer_hot_usage_ratio=_parse_float_range(
            source.get("RUNTIME_ENFORCER_HOT_USAGE_RATIO"),
            name="RUNTIME_ENFORCER_HOT_USAGE_RATIO",
            default=0.85,
            minimum=0.5,
            maximum=1.0,
        ),
        node_freeze_failures=_parse_positive_int(
            source.get("RUNTIME_NODE_FREEZE_FAILURES"),
            name="RUNTIME_NODE_FREEZE_FAILURES",
            default=3,
        ),
        reminder_days=_parse_positive_int(
            source.get("RUNTIME_REMINDER_DAYS"),
            name="RUNTIME_REMINDER_DAYS",
            default=3,
        ),
        reminder_remaining_gb=_parse_positive_int(
            source.get("RUNTIME_REMINDER_REMAINING_GB"),
            name="RUNTIME_REMINDER_REMAINING_GB",
            default=3,
        ),
        reminder_lease_seconds=_parse_positive_int(
            source.get("RUNTIME_REMINDER_LEASE_SECONDS"),
            name="RUNTIME_REMINDER_LEASE_SECONDS",
            default=120,
            minimum=10,
        ),
        reminder_max_retries=_parse_positive_int(
            source.get("RUNTIME_REMINDER_MAX_RETRIES"),
            name="RUNTIME_REMINDER_MAX_RETRIES",
            default=5,
        ),
        reminder_retry_base_seconds=_parse_positive_int(
            source.get("RUNTIME_REMINDER_RETRY_BASE_SECONDS"),
            name="RUNTIME_REMINDER_RETRY_BASE_SECONDS",
            default=30,
        ),
    )
