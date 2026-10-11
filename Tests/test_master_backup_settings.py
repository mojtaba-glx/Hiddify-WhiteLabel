"""MasterBot full-backup settings section regression tests."""

from __future__ import annotations

import pytest

from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import MasterService
from MasterBot.views import AUDIT_ACTION_FA, backup_settings_view, platform_settings_view
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, generate_key


@pytest.fixture()
def portal(conn) -> CustomerPortalService:
    master = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
    )
    return CustomerPortalService(conn, master_service=master)


def test_settings_menu_has_full_backup_entry(portal) -> None:
    settings = portal.get_platform_settings(9001)
    _, keyboard = platform_settings_view(settings)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "menu:backup" in callbacks


def test_backup_page_has_three_row_layout(portal) -> None:
    settings = portal.get_platform_settings(9001)
    text, keyboard = backup_settings_view(settings)
    assert "🗄 بکاپ کامل" in text

    rows = keyboard.inline_keyboard
    assert len(rows) == 3

    # Row 1: big full-width auto-send toggle.
    assert [b.callback_data for b in rows[0]] == ["backup:auto:toggle"]
    assert "ارسال خودکار" in rows[0][0].text

    # Row 2: instant backup and restore side by side.
    assert [b.callback_data for b in rows[1]] == ["backup:now", "backup:restore"]

    # Row 3: big full-width back button.
    assert [b.callback_data for b in rows[2]] == ["menu:settings"]
    assert "بازگشت" in rows[2][0].text


def test_backup_auto_toggle_reflects_in_view(portal) -> None:
    portal.set_backup_auto(9001, True)
    settings = portal.get_platform_settings(9001)
    assert settings["auto_backup_enabled"] is True
    _, keyboard = backup_settings_view(settings)
    assert "🟢" in keyboard.inline_keyboard[0][0].text

    portal.set_backup_auto(9001, False)
    settings = portal.get_platform_settings(9001)
    assert settings["auto_backup_enabled"] is False


def test_backup_auto_toggle_is_owner_only(portal) -> None:
    with pytest.raises(AccessDenied):
        portal.set_backup_auto(101, True)
    with pytest.raises(AccessDenied):
        portal.get_backup_status(101)
    with pytest.raises(AccessDenied):
        portal.record_backup_completed(101, filename="x.wlbak", sha256="abc")


def test_record_backup_completed_sets_timestamp_and_audit(portal) -> None:
    status = portal.record_backup_completed(
        9001, filename="whitelabel-test.wlbak", sha256="deadbeef"
    )
    assert status["backup_last_at"] != ""

    audit = portal.conn.execute(
        "SELECT * FROM audit_events WHERE action = 'platform_backup.create'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit is not None

    toggled = portal.set_backup_auto(9001, True)
    assert toggled["auto_backup_enabled"] is True
    auto_audit = portal.conn.execute(
        "SELECT * FROM audit_events WHERE action = 'platform_backup.auto_toggle'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert auto_audit is not None


def test_backup_audit_labels_exist() -> None:
    assert AUDIT_ACTION_FA["platform_backup.create"] == "ساخت بکاپ کامل"
    assert AUDIT_ACTION_FA["platform_backup.auto_toggle"] == "تغییر ارسال خودکار بکاپ"
