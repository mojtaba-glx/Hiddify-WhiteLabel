"""SellBot parity for admin panel-user expiry (0-day stays active).

Repro: a Hiddify user whose panel expiry is today (date-only or an exact
midnight "2026-10-14T00:00:00Z", "0 روز دیگر") was shown as 🔴 منقضی in
WhiteLabel while SellBot and the panel itself show 🟢 فعال until midnight.

Root: TenantRuntime/server_admin.user_status compared the exact instant
(parse_utc(expires_at) <= utcnow()), so midnight was already past at any
later hour. SellBot (AdminBot/servers.classify_user_status) compares
calendar days (end_dt.date() < today) for admin display. Exact-instant
locking lives only in the delivery path (Shared/sub_aggregator parity).
"""

from __future__ import annotations

from datetime import timedelta

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.server_admin import _panel_expiry_passed, user_status


def _row(**overrides):
    base = {
        "state": "active",
        "usage_bytes": 0,
        "traffic_bytes": 20 * 1024**3,
        "active": 1,
    }
    base.update(overrides)
    return base


def test_date_only_today_is_not_expired() -> None:
    today = utcnow().date().isoformat()
    assert _panel_expiry_passed(today) is False
    # SellBot admin display is calendar-day based: even an exact midnight
    # "todayT00:00:00" stays active through the day (live repro 2026-10-14).
    assert _panel_expiry_passed(today + "T00:00:00") is False
    assert _panel_expiry_passed(today + "T00:00:00Z") is False
    row = _row(expires_at=today)
    assert user_status(row) == "active"


def test_date_only_yesterday_is_expired() -> None:
    yesterday = (utcnow().date() - timedelta(days=1)).isoformat()
    assert _panel_expiry_passed(yesterday) is True
    row = _row(expires_at=yesterday)
    assert user_status(row) == "expired"


def test_exact_timestamp_same_day_is_still_active_like_sellbot() -> None:
    # SellBot admin display (AdminBot/servers.classify_user_status) compares
    # calendar days: a live row "2026-10-14T00:00:00Z" stays active all day.
    past_hour = iso_utc(utcnow() - timedelta(hours=1))
    future_hour = iso_utc(utcnow() + timedelta(hours=1))
    assert _panel_expiry_passed(past_hour) is False
    assert _panel_expiry_passed(future_hour) is False
    assert user_status(_row(expires_at=past_hour)) == "active"
    assert user_status(_row(expires_at=future_hour)) == "active"
    # Yesterday at any hour is past.
    yesterday_noon = (utcnow() - timedelta(days=1)).date().isoformat() + "T12:00:00Z"
    assert _panel_expiry_passed(yesterday_noon) is True
    assert user_status(_row(expires_at=yesterday_noon)) == "expired"


def test_empty_and_zero_expiry_never_expires() -> None:
    for raw in ("", "0", "0.0", None):
        assert _panel_expiry_passed(raw) is False
    assert user_status(_row(expires_at="")) == "active"


def test_volume_and_disabled_branches_unchanged() -> None:
    today = utcnow().date().isoformat()
    # Volume over quota still expires even with a valid date.
    assert user_status(_row(
        expires_at=today, usage_bytes=25 * 1024**3,
    )) == "expired"
    # Panel-disabled Hiddify reads as expired (SellBot parity).
    assert user_status(_row(expires_at=today, active=0)) == "expired"
    # X-NET keeps an explicit disabled state.
    assert user_status(
        _row(expires_at=today, active=0), provider="xnet"
    ) == "disabled"


def test_user_detail_text_matches_sellbot_for_zero_day_user() -> None:
    from TenantRuntime.AdminBot import server_actions

    today = utcnow().date().isoformat()
    row = {
        "name": "حسابدار گردو",
        "external_ref": "ebd56dac-e229-41f1-b697-ac04907c2260",
        "usage_bytes": int(13.92 * 1024**3),
        "traffic_bytes": 20 * 1024**3,
        "expires_at": today,
        "active": 1,
        "state": "active",
        "comment": "HiddifyBot:6147282491",
        "last_online": iso_utc(utcnow()),
    }

    class FakeServer:
        def __init__(self):
            self._server = {"id": 1, "label": "لوکیشن ترکیه 🇹🇷"}

        def server(self, sid):
            return dict(self._server)

    class FakeBusiness:
        def server(self, sid):
            return {"id": 1, "label": "لوکیشن ترکیه 🇹🇷", "panel_kind": "hiddify"}

        def _panel_target(self, server):
            from TenantRuntime.panels import PanelTarget

            return PanelTarget(
                public_origin="", kind="hiddify", endpoint="https://panel.example",
                admin_path="", user_path="",
                xui_flavor="", xui_inbound_ids="", xui_public_origin="",
                xui_sub_path="", xnet_inbound_ids="", xnet_public_origin="",
                xnet_sub_port=0, xnet_sub_path="", xnet_api_url="",
            )

    class FakeService:
        b = FakeBusiness()

        def related_targets(self, actor, sid, uid):
            return None, [(1, "")]

    # Patch link builders to avoid panel credentials in this unit test.
    orig_native = server_actions._panel_native_link
    orig_page = server_actions._panel_user_page_link
    server_actions._panel_native_link = lambda *a, **k: "https://sub.example/all.txt"
    server_actions._panel_user_page_link = lambda *a, **k: "https://user.example/u/"
    try:
        text = server_actions._user_detail_text(
            FakeService(), 7001, 1, 9, dict(row)
        )
    finally:
        server_actions._panel_native_link = orig_native
        server_actions._panel_user_page_link = orig_page

    # SellBot status: 0-day is active, not expired.
    assert "🟢وضعیت حساب: فعال" in text
    assert "🔴وضعیت حساب: منقضی" not in text
    assert "📆انقضا: 0 روز دیگر" in text
    # SellBot usage: current trimmed to 2 decimals (13.92), limit 1 decimal.
    assert "📊مصرف: 13.92 از 20.0 گیگابایت" in text
    # SellBot server line (never the old cluster label).
    assert "⬖ سرور:  لوکیشن ترکیه 🇹🇷" in text
    assert "◇ سرویس لوکیشن" not in text
    # SellBot header uses a double space after the colon.
    assert "👤 کاربر:  <a href=" in text


def test_user_detail_source_has_no_cluster_label() -> None:
    import pathlib

    source = pathlib.Path(
        "TenantRuntime/AdminBot/server_actions.py"
    ).read_text(encoding="utf-8")
    section = source[source.index("def _user_detail_text("):]
    section = section[: section.index("def _user_detail_rows(")]
    assert "◇ سرویس لوکیشن" not in section
    assert "⬖ سرور:  " in section
    assert "_format_usage_current(usage)" in section
