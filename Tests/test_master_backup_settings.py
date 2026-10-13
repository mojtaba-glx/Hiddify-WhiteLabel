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
    assert len(rows) == 5

    # Row 1: big full-width auto-send toggle.
    assert [b.callback_data for b in rows[0]] == ["backup:auto:toggle"]
    assert "ارسال خودکار" in rows[0][0].text

    # Row 2: cadence presets side by side.
    assert [b.callback_data for b in rows[1]] == [
        "backup:interval:6",
        "backup:interval:12",
        "backup:interval:24",
    ]
    assert "✅" in rows[1][1].text  # default 12h is marked
    assert "هر 12 ساعت" in text

    # Row 3: instant backup and restore side by side.
    assert [b.callback_data for b in rows[2]] == ["backup:now", "backup:restore"]

    # Row 4: big full-width passphrase setup.
    assert [b.callback_data for b in rows[3]] == ["backup:passphrase"]
    assert "رمز" in rows[3][0].text

    # Row 5: big full-width back button.
    assert [b.callback_data for b in rows[4]] == ["menu:settings"]
    assert "بازگشت" in rows[4][0].text


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
    assert AUDIT_ACTION_FA["platform_backup.interval"] == "تغییر دوره ارسال بکاپ"


def test_backup_page_shows_passphrase_status(portal) -> None:
    settings = portal.get_platform_settings(9001)
    text_without, _ = backup_settings_view(settings, has_passphrase=False)
    assert "❌ تنظیم نشده" in text_without

    text_with, _ = backup_settings_view(settings, has_passphrase=True)
    assert "✅ تنظیم شده" in text_with

    portal.set_backup_auto(9001, True)
    enabled = portal.get_platform_settings(9001)
    warned, _ = backup_settings_view(enabled, has_passphrase=False)
    assert "کار نمی‌کند" in warned


def test_set_backup_passphrase_writes_env_and_audits(portal, tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MASTER_BOT_TOKEN=x\n", encoding="utf-8")
    env_file.chmod(0o600)

    monkeypatch.setenv("BACKUP_PASSPHRASE", "pre-test-dummy")
    status = portal.set_backup_passphrase(
        9001, "correct-horse-123", env_path=str(env_file)
    )
    assert status["auto_backup_enabled"] is False

    content = env_file.read_text(encoding="utf-8")
    assert "BACKUP_PASSPHRASE=correct-horse-123" in content
    assert "MASTER_BOT_TOKEN=x" in content

    import os

    assert os.environ["BACKUP_PASSPHRASE"] == "correct-horse-123"

    audit = portal.conn.execute(
        "SELECT * FROM audit_events WHERE action = 'platform_backup.passphrase_set'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit is not None
    assert "correct-horse-123" not in str(audit["safe_metadata"])

    # Re-setting replaces the line instead of duplicating it.
    portal.set_backup_passphrase(9001, "another-secret-456", env_path=str(env_file))
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("BACKUP_PASSPHRASE=") for line in lines) == 1


def test_set_backup_passphrase_rejects_short_and_forbidden(portal, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    with pytest.raises(ValueError):
        portal.set_backup_passphrase(9001, "short", env_path=str(env_file))
    with pytest.raises(ValueError):
        portal.set_backup_passphrase(9001, "has\nnewline-12345", env_path=str(env_file))
    with pytest.raises(AccessDenied):
        portal.set_backup_passphrase(101, "correct-horse-123", env_path=str(env_file))


def test_set_backup_interval_accepts_presets_and_audits(portal) -> None:
    status = portal.set_backup_interval(9001, 6)
    assert status["auto_backup_interval_hours"] == 6
    settings = portal.get_platform_settings(9001)
    assert settings["auto_backup_interval_hours"] == 6
    _, keyboard = backup_settings_view(settings)
    assert "✅" in keyboard.inline_keyboard[1][0].text

    portal.set_backup_interval(9001, 24)
    assert portal.get_backup_status(9001)["auto_backup_interval_hours"] == 24

    audit = portal.conn.execute(
        "SELECT * FROM audit_events WHERE action = 'platform_backup.interval'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit is not None


def test_set_backup_interval_rejects_other_values(portal) -> None:
    with pytest.raises(ValueError):
        portal.set_backup_interval(9001, 5)
    with pytest.raises(ValueError):
        portal.set_backup_interval(9001, 0)
    with pytest.raises(ValueError):
        portal.set_backup_interval(9001, "every-hour")
    with pytest.raises(AccessDenied):
        portal.set_backup_interval(101, 6)
    # Failed attempts keep the previous value.
    assert portal.get_backup_status(9001)["auto_backup_interval_hours"] == 12
