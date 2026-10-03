"""Migrations: all tables exist, reruns idempotent, versions+checksums, atomicity."""

from __future__ import annotations

import pytest

from Database.migrate import (
    MigrationChecksumError,
    MigrationError,
    applied_checksums,
    applied_versions,
    apply_migration,
    migrate,
    split_statements,
)
from Database.connection import connect

EXPECTED_TABLES = {
    "schema_migrations", "tenants", "tenant_bots", "license_plans",
    "licenses", "audit_events", "notification_events",
    "tenant_runtime_configs",
    "tenant_user_state",
    "platform_customers", "payment_methods", "customer_orders", "payment_receipts",
    "wallet_accounts", "wallet_transactions", "trial_claims",
    "tenant_servers", "tenant_nodes", "tenant_sale_plans", "tenant_customers",
    "tenant_payment_methods", "tenant_orders", "tenant_receipts", "tenant_subscriptions",
    "tenant_tickets", "tenant_smart_links",
    "tenant_panel_credentials", "tenant_plan_categories",
    "tenant_referral_manual_rewards", "tenant_payment_events",
}


def test_migration_creates_all_tables(db_path) -> None:
    conn = connect(db_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert EXPECTED_TABLES.issubset(tables)


def test_migration_is_idempotent(db_path) -> None:
    assert migrate(db_path) == []
    conn = connect(db_path)
    try:
        versions = applied_versions(conn)
    finally:
        conn.close()
    assert "0001_init" in versions
    assert "0002_tenant_runtime" in versions
    assert "0003_runtime_state" in versions
    assert "0004_customer_commerce" in versions
    assert "0005_tenant_business" in versions
    assert "0006_panel_credentials" in versions
    assert "0007_platform_settings" in versions
    assert "0008_order_paid_at" in versions
    assert "0021_purchase_catalog" in versions
    assert "0022_renewal_policy" in versions
    assert "0023_trial_reminder_lifecycle" in versions
    assert "0024_referral_phase10" in versions
    assert "0025_payment_phase11" in versions


def test_foreign_keys_enforced(conn, factories) -> None:
    plan = factories.plan()
    with_orphan = None
    try:
        conn.execute(
            "INSERT INTO licenses (tenant_id, plan_id, status, starts_at, expires_at,"
            " created_at, updated_at) VALUES (999999, ?, 'pending', 'x', 'y', 'z', 'w')",
            (int(plan["id"]),),
        )
    except Exception as exc:
        with_orphan = exc
    assert with_orphan is not None  # FK RESTRICT blocks orphan licenses
    conn.rollback()


def test_checksums_recorded(db_path) -> None:
    conn = connect(db_path)
    try:
        recorded = applied_checksums(conn)
    finally:
        conn.close()
    assert "0001_init" in recorded
    assert len(recorded["0001_init"]) == 64
    assert len(recorded["0002_tenant_runtime"]) == 64
    assert len(recorded["0003_runtime_state"]) == 64
    assert len(recorded["0004_customer_commerce"]) == 64
    assert len(recorded["0005_tenant_business"]) == 64
    assert len(recorded["0006_panel_credentials"]) == 64
    assert len(recorded["0007_platform_settings"]) == 64
    assert len(recorded["0008_order_paid_at"]) == 64
    assert len(recorded["0021_purchase_catalog"]) == 64
    assert len(recorded["0022_renewal_policy"]) == 64
    assert len(recorded["0023_trial_reminder_lifecycle"]) == 64
    assert len(recorded["0024_referral_phase10"]) == 64
    assert len(recorded["0025_payment_phase11"]) == 64


def test_checksum_mismatch_detected(db_path, tmp_path) -> None:
    # A mutation of an already-applied file is rejected before applying anything.
    tampered_dir = tmp_path / "migrations"
    tampered_dir.mkdir()
    original = "CREATE TABLE a (id INTEGER);\nCREATE TABLE b (id INTEGER);"
    (tampered_dir / "0001_x.sql").write_text(original, encoding="utf-8")
    assert migrate(db_path, tampered_dir) == ["0001_x"]
    (tampered_dir / "0001_x.sql").write_text(original + "\nCREATE TABLE c (id INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationChecksumError):
        migrate(db_path, tampered_dir)
    conn = connect(db_path)
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "c" not in tables  # tampered change was never applied


def test_broken_migration_is_atomic(db_path, tmp_path) -> None:
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    (broken_dir / "0001_partial.sql").write_text(
        "CREATE TABLE keep_me (id INTEGER); CREATE TABLE boom (id INTEGER) THIS IS BAD;",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError):
        migrate(db_path, broken_dir)
    conn = connect(db_path)
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = applied_versions(conn)
    finally:
        conn.close()
    assert "keep_me" not in tables  # no half-applied table
    assert "boom" not in tables
    assert "0001_partial" not in versions  # no version row either


def test_split_statements_ignores_comment_semicolons() -> None:
    sql = "-- header; with semicolon; notes\nCREATE TABLE a (id INTEGER);\n-- tail;\nCREATE TABLE b (id INTEGER);"
    statements = split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert statements[1].startswith("CREATE TABLE b")


def test_apply_migration_records_atomically(conn, tmp_path) -> None:
    d = tmp_path / "one"
    d.mkdir()
    path = d / "0001_ok.sql"
    path.write_text("CREATE TABLE atomic_t (id INTEGER);", encoding="utf-8")
    assert apply_migration(conn, path) == "0001_ok"
    assert "0001_ok" in applied_versions(conn)
    assert "atomic_t" in {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
