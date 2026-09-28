"""UTC storage + Tehran display of the same instant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from Shared.timeutils import ensure_utc, format_tehran, iso_utc, parse_utc, utcnow


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_iso_roundtrip() -> None:
    now = utcnow()
    assert parse_utc(iso_utc(now)) == ensure_utc(now)
    assert iso_utc(now).endswith("+00:00")


def test_naive_assumed_utc() -> None:
    naive = datetime(2026, 9, 13, 12, 0, 0)
    assert ensure_utc(naive) == naive.replace(tzinfo=timezone.utc)
    assert parse_utc("2026-09-13T12:00:00") == naive.replace(tzinfo=timezone.utc)


def test_z_suffix_accepted() -> None:
    assert parse_utc("2026-09-13T12:00:00Z") == datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def test_empty_rejected() -> None:
    with pytest.raises(ValueError):
        parse_utc("")


def test_tehran_display_same_instant() -> None:
    utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    text = format_tehran(utc)
    assert "2026-01-15 15:30" in text  # Asia/Tehran is UTC+3:30 (no DST since 2022)
    tehran = utc.astimezone(ZoneInfo("Asia/Tehran"))
    assert tehran.utcoffset() == timedelta(hours=3, minutes=30)
    assert tehran.timestamp() == utc.timestamp()


def test_stored_timestamps_are_utc(conn, factories) -> None:
    tenant = factories.tenant()
    for key in ("created_at", "updated_at"):
        parsed = parse_utc(tenant[key])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)
