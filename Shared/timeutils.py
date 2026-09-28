"""UTC-first time helpers. Storage is always UTC; display may be Asia/Tehran."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
DEFAULT_DISPLAY_TZ = "Asia/Tehran"


def utcnow() -> datetime:
    """Current time as timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; naive values are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    """ISO-8601 string in UTC, e.g. 2026-09-13T12:00:00+00:00."""
    return ensure_utc(value).isoformat()


def parse_utc(raw: str) -> datetime:
    """Parse an ISO-8601 string produced by iso_utc(); naive -> UTC."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def format_tehran(value: datetime, tz_name: str = DEFAULT_DISPLAY_TZ) -> str:
    """Human display in Asia/Tehran (or given tz); input may be naive UTC."""
    aware = ensure_utc(value)
    return aware.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
