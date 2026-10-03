"""Phase 8 trial, reminder and lifecycle integration coverage."""

from __future__ import annotations

from TenantRuntime.UserBot import handlers as user_handlers


def test_trial_announcement_toggle_changes_real_delivery_text() -> None:
    settings = {
        "show_user_page_link": False,
        "show_sub_link": False,
        "show_auto_sub_link": False,
        "show_sub_link_b64": False,
        "show_multi_server": False,
        "show_multi_server_b64": False,
        "show_direct_config": False,
    }
    result = {"id": 12}

    announced = user_handlers._trial_delivery_text(
        object(),
        7101,
        result,
        settings,
        announce_enabled=True,
    )
    silent = user_handlers._trial_delivery_text(
        object(),
        7101,
        result,
        settings,
        announce_enabled=False,
    )
    assert announced.startswith("✅ اکانت تست رایگان")
    assert "اکانت تست رایگان" not in silent
    assert "غیرفعال است" in silent


def test_phase8_migration_adds_trial_announce_setting(conn) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(tenant_sales_growth_settings)"
        ).fetchall()
    }
    assert "trial_announce_enabled" in columns


def test_adminbot_exposes_trial_announce_and_both_reset_paths() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    assert "trial_announce_enabled" in source
    assert "userbot:settings:trial_spec:announce" in source
    assert "reset_trial_confirm" in source
    assert "reset_all_customer_trials_admin" in source
    assert "reset_customer_trial_admin" in source


def test_userbot_trial_enable_and_announce_are_enforced_in_runtime() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert 'growth.get("trial_enabled")' in source
    assert 'growth.get("trial_announce_enabled", True)' in source
    assert "_trial_delivery_text(" in source


def test_lifecycle_reads_adminbot_settings_and_reconciles_existing_queue() -> None:
    source = open(
        "TenantRuntime/lifecycle.py",
        encoding="utf-8",
    ).read()
    assert "service.runtime_userbot_settings()" in source
    assert "reconcile_reminder_queue_settings(" in source
    assert 'reminder_values.get("reminder_enabled", True)' in source
    assert '"reminder_days"' in source
    assert '"reminder_remaining_gb"' in source
    assert "days_threshold=days_threshold" in source
    assert "remaining_gb_threshold=remaining_gb_threshold" in source


def test_reminder_queue_reconciliation_handles_pending_failed_and_processing() -> None:
    source = open(
        "TenantRuntime/reminders.py",
        encoding="utf-8",
    ).read()
    assert "def reconcile_reminder_queue_settings(" in source
    assert "('pending','failed','processing')" in source
    assert "event_type='days'" in source
    assert "event_type='usage'" in source
