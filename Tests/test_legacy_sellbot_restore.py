"""Regression coverage for real Hiddify-SellBot Backup_All import."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from types import SimpleNamespace

import pytest

from TenantRuntime.AdminBot import userbot_management
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.legacy_sellbot_restore import (
    LEGACY_FORMAT,
    LegacySellBotRestoreError,
    decrypt_legacy_asset,
    is_sellbot_backup,
    restore_sellbot_backup,
)


def _sqlite_bytes(script: str, inserts: list[tuple[str, tuple]]) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(script)
            for sql, values in inserts:
                conn.execute(sql, values)
            conn.commit()
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _legacy_main_db() -> bytes:
    script = """
    CREATE TABLE userbot_users (
      id INTEGER PRIMARY KEY, telegram_id INTEGER, username TEXT, full_name TEXT,
      created_at TEXT, wallet_balance INTEGER, is_banned INTEGER,
      got_free_trial INTEGER, invited_by_user_id INTEGER, referral_code TEXT
    );
    CREATE TABLE userbot_services (
      id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, server_id INTEGER,
      server_title TEXT, usage_current REAL, usage_limit REAL, days_left INTEGER,
      last_online TEXT, comment TEXT, expired_at TEXT
    );
    CREATE TABLE userbot_orders (
      id INTEGER PRIMARY KEY, order_id TEXT, user_id INTEGER, telegram_id INTEGER,
      username TEXT, full_name TEXT, created_at TEXT, volume_gb REAL, days INTEGER,
      price INTEGER, plan_title TEXT, server_location TEXT, status TEXT,
      renew_service_id INTEGER
    );
    CREATE TABLE userbot_settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE userbot_service_nodes (
      id INTEGER PRIMARY KEY, service_id INTEGER, server_id INTEGER,
      server_title TEXT, panel_user_uuid TEXT, panel_user_id TEXT,
      is_active INTEGER, created_at TEXT, updated_at TEXT, marzban_username TEXT,
      usage_current REAL, days_left INTEGER, frozen INTEGER, fail_count INTEGER,
      last_ok_at TEXT, deleted INTEGER, frozen_at TEXT, frozen_reason TEXT
    );
    CREATE TABLE userbot_tickets (
      id INTEGER PRIMARY KEY, ticket_code TEXT, user_id INTEGER,
      telegram_id INTEGER, username TEXT, full_name TEXT, service_name TEXT,
      title TEXT, question TEXT, receipt_photo_id TEXT, status TEXT,
      admin_name TEXT, admin_telegram_id INTEGER, created_at TEXT, updated_at TEXT
    );
    CREATE TABLE userbot_ticket_messages (
      id INTEGER PRIMARY KEY, ticket_code TEXT, sender_type TEXT,
      sender_name TEXT, message_text TEXT, photo_file_id TEXT, created_at TEXT
    );
    CREATE TABLE userbot_referrals (
      id INTEGER PRIMARY KEY, inviter_id INTEGER, invitee_id INTEGER,
      invited_by_code TEXT, status TEXT, rejection_reason TEXT, fraud_flag INTEGER,
      invitee_qualified INTEGER, first_seen_payload TEXT, created_at TEXT,
      updated_at TEXT
    );
    CREATE TABLE userbot_referral_rewards (
      id INTEGER PRIMARY KEY, referral_id INTEGER, inviter_id INTEGER,
      invitee_id INTEGER, reward_type TEXT, reward_source TEXT,
      amount_toman INTEGER, voucher_code TEXT, payment_id INTEGER, status TEXT,
      revoked_at TEXT, created_at TEXT
    );
    CREATE TABLE server_traffic_daily (
      server_id INTEGER, day TEXT, baseline_gb REAL, last_total_gb REAL,
      updated_at TEXT
    );
    """
    rows = [
      ("INSERT INTO userbot_users VALUES (?,?,?,?,?,?,?,?,?,?)",
       (1, 71001, "buyer", "Legacy Buyer", "2026-09-01 10:00:00",
        125000, 0, 1, 0, "REF71001")),
      ("INSERT INTO userbot_users VALUES (?,?,?,?,?,?,?,?,?,?)",
       (2, 71002, "friend", "Legacy Friend", "2026-09-02 10:00:00",
        0, 0, 0, 1, "REF71002")),
      ("INSERT INTO userbot_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (1, "LEG-1", 1, 71001, "buyer", "Legacy Buyer",
        "2026-09-10 12:00:00", 30.0, 30, 150000,
        "30GB", "Main Germany", "approved", 0)),
      ("INSERT INTO userbot_services VALUES (?,?,?,?,?,?,?,?,?,?,?)",
       (10, 1, "legacy-service", 1, "Main Germany",
        5.5, 30.0, 17, "2026-09-30 20:00:00", "legacy note", "")),
      ("INSERT INTO userbot_service_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (1, 10, 1, "Main Germany", "uuid-main", "", 1,
        "2026-09-10 12:05:00", "2026-10-01 00:00:00", "",
        5.5, 17, 0, 0, "2026-10-01 00:00:00", 0, "", "")),
      ("INSERT INTO userbot_service_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (2, 10, 2, "France Node", "uuid-fr", "", 1,
        "2026-09-10 12:05:00", "2026-10-01 00:00:00", "",
        5.5, 17, 0, 0, "2026-10-01 00:00:00", 0, "", "")),
      ("INSERT INTO userbot_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (1, "9001", 1, 71001, "buyer", "Legacy Buyer", "legacy-service",
        "Help", "Question", "", "closed", "Admin", 7001,
        "2026-09-20 10:00:00", "2026-09-20 11:00:00")),
      ("INSERT INTO userbot_ticket_messages VALUES (?,?,?,?,?,?,?)",
       (1, "9001", "user", "Legacy Buyer", "Question", "", "2026-09-20 10:00:00")),
      ("INSERT INTO userbot_ticket_messages VALUES (?,?,?,?,?,?,?)",
       (2, "9001", "admin", "Admin", "Answer", "", "2026-09-20 11:00:00")),
      ("INSERT INTO userbot_referrals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
       (1, 1, 2, "REF71001", "active", "", 0, 1, "", "2026-09-02 10:00:00",
        "2026-09-02 10:00:00")),
      ("INSERT INTO userbot_referral_rewards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       (1, 1, 1, 2, "trial", "trial", 10000, "", 0, "paid", "",
        "2026-09-02 10:05:00")),
      ("INSERT INTO server_traffic_daily VALUES (?,?,?,?,?)",
       (2, "2026-09-30", 10.0, 12.5, "2026-09-30 23:59:00")),
      ("INSERT INTO userbot_settings VALUES (?,?)",
       ("buy_renew_settings", json.dumps({
          "enable_buy": True, "enable_renew": True, "plan_columns": 2,
          "server_columns": 1, "renew_max_days": 3,
          "renew_max_remaining_gb": 3, "renew_policy": "advanced",
          "renew_volume_mode": "reset", "renew_time_mode": "reset",
          "show_renew_in_main_menu": True
       }))),
      ("INSERT INTO userbot_settings VALUES (?,?)",
       ("force_join_settings", json.dumps({
          "enabled": True, "channel_id": "-100123", "channel_username": "legacychannel",
          "channel_link": "https://t.me/legacychannel", "guide_text": "Join first"
       }))),
      ("INSERT INTO userbot_settings VALUES (?,?)",
       ("trial_spec_settings", json.dumps({
          "enabled": True, "usage_gb": 1, "days": 1, "announce_enabled": True
       }))),
      ("INSERT INTO userbot_settings VALUES (?,?)",
       ("referral_settings", json.dumps({
          "referral_enabled": True, "trial_reward_amount": 10000,
          "purchase_reward_amount": 20000, "min_purchase_amount": 100000,
          "max_successful_referrals": 10, "trial_reward_enabled": True,
          "purchase_reward_enabled": True, "invite_intro_text": "Invite"
       }))),
      ("INSERT INTO userbot_settings VALUES (?,?)",
       ("managed_sub_base_url", "https://sub.example")),
    ]
    return _sqlite_bytes(script, rows)


def _simple_db(table: str) -> bytes:
    return _sqlite_bytes(
        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT);",
        [(f"INSERT INTO {table}(id,value) VALUES (?,?)", (1, "preserved"))],
    )


def _sellbot_zip(*, tamper_manifest: bool = False) -> bytes:
    servers = {
      "servers": [
        {
          "id": 1, "title": "Main Germany", "panel_url": "https://main.example",
          "admin_proxy_path": "admin", "admin_uuid": "legacy-hiddify-key",
          "user_proxy_path": "user", "users_limit": 500,
          "domains": ["sub-main.example"],
          "nodes": [{
            "id": 1, "title": "France Node", "target_server_id": 2,
            "status": "up", "last_check": "2026-09-30 20:00:00", "fail_count": 0
          }]
        },
        {
          "id": 2, "title": "France Node", "panel_url": "https://fr.example",
          "panel_type": "xui", "xui_username": "admin",
          "xui_password": "legacy-password", "xui_api_token": "",
          "xui_sub_domain": "fr-sub.example", "xui_inbound_id": "0",
          "users_limit": 200, "domains": [], "nodes": [],
          "is_node": True, "parent_server_id": 1
        }
      ],
      "settings": {
        "cards": [{"bank": "Test Bank", "number": "6037990000000000", "owner": "Owner"}],
        "card_rr_index": 0
      }
    }
    plans = {
      "servers": {
        "1": {
          "display_mode": "dynamic",
          "categories": [{"id": 1, "title": "Main", "priority": 0}],
          "plans": [{
            "id": 1, "category_id": 1, "title": "30GB",
            "price": 150000, "days": 30, "gb": 30, "priority": 0
          }],
          "dynamic_settings": {"price_per_gb": 10000, "min_gb": 10, "max_gb": 100},
          "next_category_id": 2, "next_plan_id": 2
        }
      }
    }
    files = {
      "Shared/hiddify_sellbot.db": _legacy_main_db(),
      "Shared/servers.json": json.dumps(servers, ensure_ascii=False).encode(),
      "Shared/plans.json": json.dumps(plans, ensure_ascii=False).encode(),
      "Shared/agency.db": _simple_db("agent_users"),
      "customer_bot.db": _simple_db("customer_users"),
      "AgentBot/agent_bot.db": _simple_db("agent_orders"),
      "Receiptions/test.jpg": b"legacy-receipt-bytes",
    }
    manifest_files = []
    for path, raw in files.items():
        digest = hashlib.sha256(raw).hexdigest()
        if tamper_manifest and path == "Shared/hiddify_sellbot.db":
            digest = "0" * 64
        manifest_files.append({
          "path": path, "size": len(raw), "sha256": digest
        })
    bot_name = "Backup_Bot_30-09-2026_20-30-07.json"
    bot_manifest = {
      "created_at": "2026-09-30 20:30:07",
      "backup_type": "bot",
      "files_count": len(manifest_files),
      "files": manifest_files,
    }
    full_name = "Backup_All_01-10-2026_00-00-08.json"
    full_manifest = {
      "created_at": "2026-10-01 00:00:08",
      "backup_type": "full",
      "bot_backup_file": bot_name,
      "panel_backups_count": 2,
      "panel_errors_count": 0,
      "panel_backups": [],
      "panel_errors": [],
    }
    files[bot_name] = json.dumps(bot_manifest, ensure_ascii=False).encode()
    files["PanelBackups/Main Germany/main.json"] = b'{"panel":"main"}'
    files["PanelBackups/France Node/xui.db"] = b"legacy-xui-db"
    files[full_name] = json.dumps(full_manifest, ensure_ascii=False).encode()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, raw in files.items():
            zf.writestr(path, raw)
    return out.getvalue()


def _service(conn, factories, cipher, owner=7001):
    tenant = factories.tenant(owner_telegram_id=owner)
    admin_row, admin_token = factories.bot(int(tenant["id"]), "admin", cipher)
    factories.bot(int(tenant["id"]), "user", cipher)
    business = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    return tenant, business, admin_row, admin_token


def test_detects_real_sellbot_backup_shape() -> None:
    assert is_sellbot_backup(_sellbot_zip()) is True
    assert is_sellbot_backup(b"not-a-zip") is False


def test_sellbot_restore_is_tenant_scoped_operational_and_lossless(
    conn, factories, cipher
) -> None:
    tenant, business, admin_row, _ = _service(conn, factories, cipher)
    other, other_business, *_ = _service(conn, factories, cipher, owner=8001)
    other_customer = other_business.register_customer(
        81001, display_name="Other Tenant", username="other"
    )

    old_customer = business.register_customer(
        79999, display_name="Should disappear", username="old"
    )
    before_bot_fp = conn.execute(
        "SELECT token_fingerprint FROM tenant_bots "
        "WHERE tenant_id=? AND role='admin'",
        (int(tenant["id"]),),
    ).fetchone()["token_fingerprint"]

    report = restore_sellbot_backup(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=int(tenant["owner_telegram_id"]),
        cipher=cipher,
        data=_sellbot_zip(),
    )
    assert report.format == LEGACY_FORMAT
    assert report.users == 2
    assert report.services == 1
    assert report.tickets == 1
    assert report.panel_backups == 2
    assert report.assets_preserved >= 10

    # Old Tenant state was replaced, but the other Tenant survived untouched.
    assert conn.execute(
        "SELECT 1 FROM tenant_customers WHERE tenant_id=? AND telegram_user_id=79999",
        (int(tenant["id"]),),
    ).fetchone() is None
    other_row = conn.execute(
        "SELECT display_name FROM tenant_customers WHERE tenant_id=? AND id=?",
        (int(other["id"]), int(other_customer["id"])),
    ).fetchone()
    assert other_row["display_name"] == "Other Tenant"

    customers = conn.execute(
        "SELECT telegram_user_id,display_name,status FROM tenant_customers "
        "WHERE tenant_id=? ORDER BY telegram_user_id",
        (int(tenant["id"]),),
    ).fetchall()
    assert [row["telegram_user_id"] for row in customers] == [71001, 71002]
    wallet = conn.execute(
        "SELECT balance FROM tenant_wallet_accounts a "
        "JOIN tenant_customers c ON c.id=a.customer_id "
        "WHERE a.tenant_id=? AND c.telegram_user_id=71001",
        (int(tenant["id"]),),
    ).fetchone()
    assert int(wallet["balance"]) == 125000

    subscription = conn.execute(
        "SELECT s.*,c.telegram_user_id FROM tenant_subscriptions s "
        "JOIN tenant_customers c ON c.id=s.customer_id "
        "WHERE s.tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()
    assert int(subscription["telegram_user_id"]) == 71001
    assert subscription["service_name"] == "legacy-service"
    assert subscription["external_ref"] == "uuid-main"
    assert int(subscription["usage_bytes"]) == int(5.5 * 1024**3)
    assert int(subscription["traffic_bytes"]) == 30 * 1024**3
    nodes = conn.execute(
        "SELECT n.external_ref,s.label,n.is_primary FROM tenant_subscription_nodes n "
        "JOIN tenant_servers s ON s.id=n.server_id "
        "WHERE n.tenant_id=? ORDER BY n.is_primary DESC,s.label",
        (int(tenant["id"]),),
    ).fetchall()
    assert {(r["external_ref"], r["label"], r["is_primary"]) for r in nodes} == {
        ("uuid-main", "Main Germany", 1),
        ("uuid-fr", "France Node", 0),
    }

    # Server topology and credentials are native WhiteLabel state.
    servers = conn.execute(
        "SELECT id,label,provider_kind FROM tenant_servers "
        "WHERE tenant_id=? ORDER BY label",
        (int(tenant["id"]),),
    ).fetchall()
    assert {r["label"] for r in servers} == {"Main Germany", "France Node"}
    main_id = next(r["id"] for r in servers if r["label"] == "Main Germany")
    encrypted = conn.execute(
        "SELECT encrypted_secret FROM tenant_panel_credentials "
        "WHERE tenant_id=? AND server_id=?",
        (int(tenant["id"]), int(main_id)),
    ).fetchone()
    assert cipher.decrypt_secret(encrypted["encrypted_secret"]) == "legacy-hiddify-key"
    relation = conn.execute(
        "SELECT p.label parent,c.label child FROM tenant_nodes n "
        "JOIN tenant_servers p ON p.id=n.parent_server_id "
        "JOIN tenant_servers c ON c.id=n.server_id "
        "WHERE n.tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()
    assert (relation["parent"], relation["child"]) == (
        "Main Germany", "France Node"
    )

    # Current active WhiteLabel bot credentials are deliberately not replaced.
    after_bot_fp = conn.execute(
        "SELECT token_fingerprint FROM tenant_bots "
        "WHERE tenant_id=? AND role='admin'",
        (int(tenant["id"]),),
    ).fetchone()["token_fingerprint"]
    assert after_bot_fp == before_bot_fp == admin_row["token_fingerprint"]

    # Unsupported Agent/Customer history and panel backups are preserved encrypted.
    asset = conn.execute(
        "SELECT path,size,sha256,encoding,content "
        "FROM tenant_legacy_restore_assets "
        "WHERE tenant_id=? AND path='Shared/agency.db'",
        (int(tenant["id"]),),
    ).fetchone()
    assert asset["encoding"] == "fernet-chunked-b64"
    raw = decrypt_legacy_asset(cipher, bytes(asset["content"]))
    assert len(raw) == int(asset["size"])
    assert hashlib.sha256(raw).hexdigest() == asset["sha256"]
    panel_asset = conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_legacy_restore_assets "
        "WHERE tenant_id=? AND kind='panel_backup'",
        (int(tenant["id"]),),
    ).fetchone()
    assert int(panel_asset["n"]) == 2

    # Settings, tickets and referrals were converted to native Tenant rows.
    force = conn.execute(
        "SELECT value FROM tenant_userbot_settings "
        "WHERE tenant_id=? AND key='force_join_enabled'",
        (int(tenant["id"]),),
    ).fetchone()
    assert json.loads(force["value"]) is True
    ticket = conn.execute(
        "SELECT status,subject FROM tenant_tickets WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()
    assert ticket["status"] == "closed"
    assert ticket["subject"] == "Help"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_ticket_messages WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()["n"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_referrals WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()["n"] == 1


def test_tampered_sellbot_manifest_fails_before_current_tenant_mutation(
    conn, factories, cipher
) -> None:
    tenant, business, *_ = _service(conn, factories, cipher)
    keep = business.register_customer(
        72001, display_name="Keep Me", username="keep"
    )
    with pytest.raises(LegacySellBotRestoreError, match="checksum"):
        restore_sellbot_backup(
            conn,
            tenant_id=int(tenant["id"]),
            owner_telegram_id=int(tenant["owner_telegram_id"]),
            cipher=cipher,
            data=_sellbot_zip(tamper_manifest=True),
        )
    row = conn.execute(
        "SELECT display_name FROM tenant_customers "
        "WHERE tenant_id=? AND id=?",
        (int(tenant["id"]), int(keep["id"])),
    ).fetchone()
    assert row["display_name"] == "Keep Me"


def test_admin_restore_helper_auto_detects_sellbot_backup(
    conn, factories, cipher
) -> None:
    tenant, business, *_ = _service(conn, factories, cipher)
    report = asyncio.run(
        userbot_management._restore_backup(
            business,
            _sellbot_zip(),
        )
    )
    assert report.format == LEGACY_FORMAT
    assert report.users == 2
    assert report.services == 1


def test_sellbot_restore_assets_are_rebacked_up_as_encrypted_tenant_data(
    conn, factories, cipher
) -> None:
    from TenantRuntime.backup import create_tenant_backup, decode_tenant_backup

    tenant, business, *_ = _service(conn, factories, cipher)
    restore_sellbot_backup(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=int(tenant["owner_telegram_id"]),
        cipher=cipher,
        data=_sellbot_zip(),
    )
    artifact = create_tenant_backup(
        conn,
        tenant_id=int(tenant["id"]),
    )
    snapshot = decode_tenant_backup(artifact.data)
    assets = snapshot["tables"]["tenant_legacy_restore_assets"]
    assert assets
    assert all(row["encoding"] == "fernet-chunked-b64" for row in assets)
    # The future backup contains only ciphertext envelopes, never raw legacy DB bytes.
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "legacy-hiddify-key" not in serialized
    assert "legacy-password" not in serialized
