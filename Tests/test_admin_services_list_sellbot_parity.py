"""SellBot parity for the AdminBot customer services list.

SellBot (AdminBot/userbot.send_user_services_list):
- header: #️⃣ لیست سرویس‌ها + description + 📦 total + 🟢 active + 🔴 expired
- buttons: "{emoji} |{name}" (🔵 active / 🔴 expired / 🟡 test), 3 per row
  reversed (RTL), callback userbot:svc:{id}
- back: "بازگشت🔙" -> userbot:user:{id}
- empty: same header with zeros + "❌ سرویسی برای نمایش این کاربر یافت نشد."
"""

from __future__ import annotations

import pathlib

from TenantRuntime.AdminBot import userbot_management as admin_userbot


def _item(sid, name="vpn-100001", status="active"):
    return {
        "id": sid, "service_name": name, "plan_name": "Pro", "status": status,
    }


def test_services_list_header_and_counts_match_sellbot() -> None:
    items = [
        _item(1, "vpn-100001", "active"),
        _item(2, "vpn-100002", "expired"),
        _item(3, "vpn-100003", "disabled"),
    ]
    text, _ = admin_userbot._services_list_content(items, 44)
    assert text == (
        "#️⃣ لیست سرویس‌ها\n"
        "شما می‌توانید لیست سرویس‌ها و اطلاعات آن‌ها را اینجا مشاهده کنید\n"
        "📦 تعداد کل سرویس‌ها: 3\n"
        "🟢 سرویس‌های فعال: 2\n"
        "🔴 سرویس‌های منقضی: 1"
    )


def test_services_list_buttons_match_sellbot() -> None:
    items = [
        _item(1, "vpn-100001", "active"),
        _item(2, "vpn-100002", "expired"),
    ]
    _, markup = admin_userbot._services_list_content(items, 44)
    rows = markup.inline_keyboard
    assert rows[-1][0].text == "بازگشت🔙"
    assert rows[-1][0].callback_data == "userbot:user:44"
    service_buttons = [
        button for row in rows[:-1] for button in row
    ]
    by_callback = {b.callback_data: b.text for b in service_buttons}
    assert by_callback["userbot:svc:1"] == "🔵 |vpn-100001"
    assert by_callback["userbot:svc:2"] == "🔴 |vpn-100002"


def test_services_list_packs_three_per_row_reversed() -> None:
    items = [_item(i, f"vpn-10000{i}", "active") for i in range(1, 5)]
    _, markup = admin_userbot._services_list_content(items, 44)
    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [3, 1, 1]
    assert [b.callback_data for b in rows[0]] == [
        "userbot:svc:3", "userbot:svc:2", "userbot:svc:1",
    ]


def test_services_list_empty_matches_sellbot() -> None:
    text, markup = admin_userbot._services_list_content([], 44)
    assert "📦 تعداد کل سرویس‌ها: 0" in text
    assert "🟢 سرویس‌های فعال: 0" in text
    assert "🔴 سرویس‌های منقضی: 0" in text
    assert "❌ سرویسی برای نمایش این کاربر یافت نشد." in text
    rows = markup.inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].text == "🔙بازگشت"
    assert rows[0][0].callback_data == "userbot:user:44"


def test_services_action_uses_sellbot_content() -> None:
    source = pathlib.Path(
        "TenantRuntime/AdminBot/userbot_management.py"
    ).read_text(encoding="utf-8")
    assert "_services_list_content(" in source
    # Old one-button-per-row layout with raw status must be gone.
    assert "📦 #{x['id']} · {x['plan_name']} · {x['status']}" not in source
    assert "📋 لیست سرویس‌ها\nتعداد:" not in source
