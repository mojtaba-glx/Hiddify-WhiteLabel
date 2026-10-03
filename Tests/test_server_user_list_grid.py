"""SellBot server-list layout, live status counts and real callback navigation."""

import asyncio
import json
from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.AdminBot import server_actions
from Tests.test_server_management_complete import setup, ui, click, Query


def labels(rows):
    return [[button.text for button in row] for row in rows]


def test_113_users_match_screenshot_grid_and_every_page_is_reachable(
    monkeypatch, conn, factories, cipher
):
    b, panel, service, server, _, _, _ = setup(conn, factories, cipher)
    endpoint, sid = server["endpoint"], server["id"]
    old = iso_utc(utcnow() - timedelta(minutes=5))
    panel.users[endpoint, "status-user-1"].update(name="mojtaba", last_online=old)
    for i in range(112):
        panel.seed(endpoint, f"native-{i}", name=f"User {i}")
        if i >= 21:
            panel.users[endpoint, f"native-{i}"]["last_online"] = old
        if i >= 108:
            panel.users[endpoint, f"native-{i}"]["usage_bytes"] = 10 * 1024**3
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f"srv:users:{sid}")
        text, kwargs = message.sent[-1]
        assert text == (
            "[📋 لیست کاربران]\n"
            "❕ شما می‌توانید لیست کاربران و اطلاعات آن‌ها را اینجا مشاهده کنید.\n"
            "📄 صفحه: 1/6\n👥 تعداد کاربران: 113\n🔵 آنلاین: 21\n"
            "🟡 آفلاین: 88\n⚫ غیرفعال: 0\n🔴 منقضی شده: 4\n❄️ یخ‌زده (نود قطع): 0"
        )
        rows = kwargs["reply_markup"].inline_keyboard
        assert [len(row) for row in rows] == [3, 3, 3, 3, 3, 3, 2, 2, 1]
        assert labels(rows)[0] == ["🔵User 1", "🔵User 0", "🟡mojtaba"]
        assert labels(rows)[-2:] == [["1/6", "⬅️"], ["بازگشت"]]
        # Each displayed cell opens its own panel user, including the last partial row.
        chosen = rows[6][0]
        uid = int(chosen.callback_data.rsplit(":", 1)[1])
        await click(update, context, chosen.callback_data)
        assert service.user(7001, sid, uid)["name"] in message.sent[-1][0]
        await click(update, context, f"srv:users:{sid}")
        seen = []
        for page in range(6):
            text, kwargs = message.sent[-1]
            rows = kwargs["reply_markup"].inline_keyboard
            assert f"📄 صفحه: {page+1}/6" in text
            users = [
                button
                for row in rows
                for button in row
                if button.callback_data.startswith("srv:puser:")
            ]
            seen.extend(button.callback_data for button in users)
            assert len(users) == (20 if page < 5 else 13)
            nav = rows[-2]
            assert [x.text for x in nav] == (
                ["1/6", "⬅️"]
                if page == 0
                else ["➡️", "6/6"] if page == 5 else ["➡️", f"{page+1}/6", "⬅️"]
            )
            if page < 5:
                await click(update, context, nav[-1].callback_data)
        assert len(seen) == len(set(seen)) == 113
        await click(update, context, rows[-2][0].callback_data)
        assert "📄 صفحه: 5/6" in message.sent[-1][0]
        await click(update, context, f"srv:users:{sid}:all:999")
        assert "📄 صفحه: 6/6" in message.sent[-1][0]
        await click(update, context, f"srv:users:{sid}:all:-1")
        assert "📄 صفحه: 1/6" in message.sent[-1][0]
        # The counter acknowledges clicks without an invalid-button alert or mutation.
        before = len(message.sent)
        query = Query("noop", message)
        update.callback_query = query
        from TenantRuntime.AdminBot import handlers

        await handlers.on_callback(update, context)
        assert len(message.sent) == before and query.answers == [((), {})]
        await click(update, context, rows[-1][0].callback_data)
        assert "🖥 سرور:" in message.sent[-1][0]
        assert not panel.mutations

    asyncio.run(run())


def test_empty_list_has_zero_counts_and_only_back(monkeypatch, conn, factories, cipher):
    b, panel, _, server, _, _, _ = setup(conn, factories, cipher)
    panel.users.clear()
    update, context, message = ui(monkeypatch, b)
    asyncio.run(click(update, context, f'srv:users:{server["id"]}'))
    text, kwargs = message.sent[-1]
    assert "هنوز هیچ کاربری" in text and "👥 تعداد کاربران: 0" in text
    assert labels(kwargs["reply_markup"].inline_keyboard) == [["بازگشت"]]
    assert not panel.mutations


def test_page_refresh_updates_status_and_offline_warning_keeps_grid(
    monkeypatch, conn, factories, cipher
):
    b, panel, _, server, _, _, _ = setup(conn, factories, cipher)
    endpoint, sid = server["endpoint"], server["id"]
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f"srv:users:{sid}")
        assert "🔵 آنلاین: 1" in message.sent[-1][0]
        panel.users[endpoint, "status-user-1"]["online"] = False
        await click(update, context, f"srv:users:{sid}:all:0")
        assert "🟡 آفلاین: 1" in message.sent[-1][0]
        panel.offline.add(endpoint)
        await click(update, context, f"srv:users:{sid}:all:0")
        text, kwargs = message.sent[-1]
        assert "ذخیره‌شده" in text and "🟡 آفلاین: 1" in text
        assert labels(kwargs["reply_markup"].inline_keyboard) == [
            ["🟡Panel user"],
            ["1/1"],
            ["بازگشت"],
        ]
        assert not panel.mutations

    asyncio.run(run())


def test_frozen_count_is_unique_and_names_keep_both_status_markers(
    monkeypatch, conn, factories, cipher
):
    b, panel, service, server, child, _, _ = setup(conn, factories, cipher)
    service.attach_node(7001, server["id"], child["id"])
    second = b.add_server(
        7001,
        label="Second",
        panel_kind="hiddify",
        endpoint="https://second.example",
        user_path="user",
    )
    b.set_panel_credential(7001, server_id=second["id"], secret="fake-key")
    service.attach_node(7001, server["id"], second["id"])
    panel.offline.update([child["endpoint"], second["endpoint"]])
    asyncio.run(service.sync_nodes(7001, server["id"], "missing"))
    assert len(service.frozen(7001, server["id"])) == 2
    update, context, message = ui(monkeypatch, b)
    asyncio.run(click(update, context, f'srv:users:{server["id"]}'))
    text, kwargs = message.sent[-1]
    assert "❄️ یخ‌زده (نود قطع): 1" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "❄️🔵Panel user"
    assert not panel.mutations


@pytest.mark.parametrize(
    "active,used,last_age,online,expected",
    [
        (True, 1, 30, None, "online"),
        (True, 1, 91, None, "offline"),
        (True, 1, -60, None, "online"),
        (True, 1, -300, None, "offline"),
        (True, 1, 0, False, "offline"),
        (True, 1, 3600, True, "online"),
        (False, 1, 0, True, "disabled"),
        (True, 10, 0, True, "expired"),
    ],
)
def test_status_colors_use_quota_state_live_presence_and_last_seen(
    active, used, last_age, online, expected
):
    now = utcnow()
    row = dict(
        state="active",
        active=active,
        usage_bytes=used,
        traffic_bytes=10,
        expires_at=iso_utc(now + timedelta(days=10)),
        last_online=iso_utc(now - timedelta(seconds=last_age)),
        last_synced_at=iso_utc(now),
        extra_json=json.dumps({"online": online}),
    )
    assert server_actions.list_user_status(row, now=now) == expected
    assert row["usage_bytes"] == used and row["active"] is active


def test_expired_time_and_invalid_last_seen_are_not_online():
    now = utcnow()
    row = dict(
        state="active",
        active=True,
        usage_bytes=0,
        traffic_bytes=0,
        last_online="invalid",
        extra_json="{}",
    )
    assert server_actions.list_user_status(row, now=now) == "offline"
    row.update(expires_at=iso_utc(now - timedelta(seconds=1)), last_online=iso_utc(now))
    assert server_actions.list_user_status(row, now=now) == "expired"
