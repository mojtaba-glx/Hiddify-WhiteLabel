"""Phase 15: complete Tenant backup/restore and shared agency infrastructure."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.agency_infra import (
    AgencyInfrastructureError,
    TenantAgencyInfrastructure,
)
from TenantRuntime.backup import (
    BACKUP_FORMAT,
    LEGACY_FORMAT,
    TenantBackupError,
    auto_backup_slot_key,
    claim_auto_backup_slot,
    complete_auto_backup_delivery,
    create_tenant_backup,
    decode_tenant_backup,
    finish_auto_backup_slot,
    prepare_auto_backup_delivery,
    restore_tenant_backup,
    tenant_backup_tables,
)
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.lifecycle import TenantLifecycleCoordinator


class NoopPanel:
    pass


def _tenant_service(conn, factories, cipher, *, owner: int = 7001):
    tenant = factories.tenant(owner_telegram_id=owner)
    admin_row, admin_token = factories.bot(
        int(tenant["id"]), "admin", cipher
    )
    user_row, user_token = factories.bot(
        int(tenant["id"]), "user", cipher
    )
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    return tenant, service, admin_row, admin_token, user_row, user_token


def _seed_operational_tenant(conn, factories, cipher):
    tenant, service, admin_row, admin_token, user_row, user_token = (
        _tenant_service(conn, factories, cipher)
    )
    owner = int(tenant["owner_telegram_id"])
    customer_tid = 7101

    server = service.add_server(
        owner,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        admin_path="admin",
        user_path="user",
    )
    service.set_panel_credential(
        owner,
        server_id=int(server["id"]),
        secret="phase15-panel-secret",
    )
    plan = service.add_plan(
        owner,
        name="20GB",
        traffic_gb=20,
        duration_days=30,
        price=180000,
        currency="IRR",
    )
    customer = service.register_customer(
        customer_tid,
        display_name="Backup User",
        username="backup_user",
    )
    order = service.create_order(
        customer_tid,
        int(plan["id"]),
        server_id=int(server["id"]),
    )
    now = iso_utc(utcnow())
    sub = conn.execute(
        "INSERT INTO tenant_subscriptions "
        "(tenant_id,customer_id,plan_id,order_id,server_id,external_ref,status,"
        "usage_bytes,traffic_bytes,expires_at,last_online,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?)",
        (
            int(tenant["id"]),
            int(customer["id"]),
            int(plan["id"]),
            int(order["id"]),
            int(server["id"]),
            "phase15-sub",
            3 * 1024**3,
            20 * 1024**3,
            iso_utc(utcnow() + timedelta(days=20)),
            now,
            now,
            now,
        ),
    )
    subscription_id = int(sub.lastrowid or 0)

    conn.execute(
        "INSERT INTO tenant_payment_receipt_media "
        "(tenant_id,payment_source,receipt_id,mime_type,media,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            int(tenant["id"]),
            "order",
            999,
            "image/jpeg",
            b"phase15-binary-receipt",
            now,
        ),
    )

    agency = TenantAgencyInfrastructure(
        conn,
        tenant_id=int(tenant["id"]),
        cipher=cipher,
    )
    agent = agency.register_agent(
        telegram_user_id=7201,
        display_name="Representative",
        username="rep",
        tier_code="silver",
        settings={"future": True},
    )
    agency.adjust_wallet(
        int(agent.id),
        currency="IRR",
        amount=500000,
        kind="admin_credit",
        idempotency_key=f"tenant:{tenant['id']}:agent:{agent.id}:seed",
        note="seed",
    )
    agency.set_server_access(
        int(agent.id),
        server_id=int(server["id"]),
        enabled=True,
    )
    agency.set_plan_access(
        int(agent.id),
        plan_id=int(plan["id"]),
        wholesale_price=120000,
        currency="IRR",
    )
    customer_bot = agency.register_customer_bot(
        int(agent.id),
        plain_token=(
            "990001:FAKE_phase15_customer_bot_token_"
            "abcdefghijklmnopqrstuvwxyz_123456"
        ),
        settings={"brand": "future"},
    )
    agency.attribute_subscription(
        int(agent.id),
        subscription_id=subscription_id,
        customer_id=int(customer["id"]),
        source="customer_bot",
        customer_bot_id=int(customer_bot.id),
        wholesale_amount=120000,
        retail_amount=180000,
        currency="IRR",
    )
    conn.commit()
    return {
        "tenant": tenant,
        "service": service,
        "admin_row": admin_row,
        "admin_token": admin_token,
        "user_row": user_row,
        "user_token": user_token,
        "server": server,
        "plan": plan,
        "customer": customer,
        "order": order,
        "subscription_id": subscription_id,
        "agency": agency,
        "agent": agent,
        "customer_bot": customer_bot,
    }


def test_phase15_schema_exists_but_runtime_roles_remain_admin_user_only(
    conn,
) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for name in {
        "tenant_backup_runs",
        "tenant_agents",
        "tenant_agent_wallet_accounts",
        "tenant_agent_wallet_transactions",
        "tenant_customer_bots",
        "tenant_agent_server_access",
        "tenant_agent_plan_access",
        "tenant_agent_customers",
        "tenant_agent_subscriptions",
    }:
        assert name in tables

    migration = open(
        "Migrations/0031_backup_agency_infra.sql",
        encoding="utf-8",
    ).read()
    assert "AgentBot/CustomerBot runtime/menu is enabled" not in migration
    dispatcher = open("TenantRuntime/handlers.py", encoding="utf-8").read()
    assert 'spec.role == "admin"' in dispatcher
    assert 'spec.role == "user"' in dispatcher
    assert "unsupported tenant bot role" in dispatcher


def test_backup_discovers_all_tenant_tables_and_excludes_live_bot_credentials(
    conn, factories, cipher
) -> None:
    state = _seed_operational_tenant(conn, factories, cipher)
    tables = set(tenant_backup_tables(conn))
    assert "tenant_agents" in tables
    assert "tenant_customer_bots" in tables
    assert "tenant_agent_subscriptions" in tables
    assert "tenant_customers" in tables
    assert "tenant_orders" in tables
    assert "tenant_subscriptions" in tables
    assert "tenant_panel_credentials" in tables
    assert "tenant_bots" not in tables
    assert "tenant_backup_runs" not in tables

    artifact = create_tenant_backup(
        conn,
        tenant_id=int(state["tenant"]["id"]),
    )
    snapshot = decode_tenant_backup(artifact.data)
    assert snapshot["format"] == BACKUP_FORMAT
    assert set(snapshot["tables"]) == tables
    assert "tenant_bots" in set(snapshot["excluded_tables"])
    assert artifact.table_count == len(tables)
    assert artifact.row_count > 0


def test_full_backup_round_trip_restores_operational_blob_and_agency_data(
    conn, factories, cipher
) -> None:
    state = _seed_operational_tenant(conn, factories, cipher)
    tenant_id = int(state["tenant"]["id"])
    customer_id = int(state["customer"]["id"])
    agent_id = int(state["agent"].id)

    original_admin_fp = conn.execute(
        "SELECT token_fingerprint FROM tenant_bots "
        "WHERE tenant_id=? AND role='admin'",
        (tenant_id,),
    ).fetchone()["token_fingerprint"]

    artifact = create_tenant_backup(conn, tenant_id=tenant_id)

    conn.execute(
        "UPDATE tenant_customers SET display_name='MUTATED' "
        "WHERE id=? AND tenant_id=?",
        (customer_id, tenant_id),
    )
    conn.execute(
        "UPDATE tenant_payment_receipt_media SET media=? WHERE tenant_id=?",
        (b"mutated-media", tenant_id),
    )
    conn.execute(
        "UPDATE tenant_agent_wallet_accounts SET balance=1 "
        "WHERE tenant_id=? AND agent_id=?",
        (tenant_id, agent_id),
    )
    state["service"].register_customer(
        7999,
        display_name="Should disappear",
        username="extra",
    )
    conn.commit()

    report = restore_tenant_backup(
        conn,
        tenant_id=tenant_id,
        data=artifact.data,
    )
    assert report.format == BACKUP_FORMAT
    assert report.tables_restored == artifact.table_count
    assert report.rows_restored == artifact.row_count

    restored_customer = conn.execute(
        "SELECT display_name FROM tenant_customers WHERE id=? AND tenant_id=?",
        (customer_id, tenant_id),
    ).fetchone()
    assert restored_customer["display_name"] == "Backup User"
    assert conn.execute(
        "SELECT 1 FROM tenant_customers "
        "WHERE tenant_id=? AND telegram_user_id=7999",
        (tenant_id,),
    ).fetchone() is None

    media = conn.execute(
        "SELECT media FROM tenant_payment_receipt_media WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    assert bytes(media["media"]) == b"phase15-binary-receipt"
    assert state["agency"].wallet_balance(agent_id, currency="IRR") == 500000
    attributed = conn.execute(
        "SELECT source,wholesale_amount,retail_amount "
        "FROM tenant_agent_subscriptions "
        "WHERE tenant_id=? AND subscription_id=?",
        (tenant_id, int(state["subscription_id"])),
    ).fetchone()
    assert attributed["source"] == "customer_bot"
    assert int(attributed["wholesale_amount"]) == 120000
    assert int(attributed["retail_amount"]) == 180000

    # Live runtime bot credentials are outside Tenant backup/restore.
    after_admin_fp = conn.execute(
        "SELECT token_fingerprint FROM tenant_bots "
        "WHERE tenant_id=? AND role='admin'",
        (tenant_id,),
    ).fetchone()["token_fingerprint"]
    assert after_admin_fp == original_admin_fp


def test_backup_from_another_tenant_is_rejected_before_mutation(
    conn, factories, cipher
) -> None:
    first = _seed_operational_tenant(conn, factories, cipher)
    second, service2, *_ = _tenant_service(
        conn, factories, cipher, owner=8001
    )
    service2.register_customer(
        8101, display_name="Tenant Two", username="two"
    )
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_customers WHERE tenant_id=?",
        (int(second["id"]),),
    ).fetchone()["n"]

    artifact = create_tenant_backup(
        conn,
        tenant_id=int(first["tenant"]["id"]),
    )
    with pytest.raises(TenantBackupError, match="another tenant"):
        restore_tenant_backup(
            conn,
            tenant_id=int(second["id"]),
            data=artifact.data,
        )

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_customers WHERE tenant_id=?",
        (int(second["id"]),),
    ).fetchone()["n"]
    assert int(after) == int(before)


def test_zip_payload_tampering_is_rejected_by_checksum(
    conn, factories, cipher
) -> None:
    state = _seed_operational_tenant(conn, factories, cipher)
    artifact = create_tenant_backup(
        conn, tenant_id=int(state["tenant"]["id"])
    )
    with zipfile.ZipFile(io.BytesIO(artifact.data), "r") as src:
        manifest = src.read("manifest.json")
        payload = json.loads(src.read("tenant.json").decode("utf-8"))
    payload["created_at"] = "tampered"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.writestr("manifest.json", manifest)
        dst.writestr(
            "tenant.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    with pytest.raises(TenantBackupError, match="checksum"):
        decode_tenant_backup(out.getvalue())


def test_legacy_phase14_json_restore_stays_non_destructive(
    conn, factories, cipher
) -> None:
    state = _seed_operational_tenant(conn, factories, cipher)
    tenant_id = int(state["tenant"]["id"])
    plan_id = int(state["plan"]["id"])
    original = dict(
        conn.execute(
            "SELECT * FROM tenant_sale_plans WHERE id=? AND tenant_id=?",
            (plan_id, tenant_id),
        ).fetchone()
    )
    original["price"] = 111111
    payload = {
        "format": LEGACY_FORMAT,
        "tenant_id": tenant_id,
        "tables": {"tenant_sale_plans": [original]},
    }
    conn.execute(
        "UPDATE tenant_sale_plans SET price=999999 WHERE id=?",
        (plan_id,),
    )
    order_count = conn.execute(
        "SELECT COUNT(*) AS n FROM tenant_orders WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()["n"]

    report = restore_tenant_backup(
        conn,
        tenant_id=tenant_id,
        data=json.dumps(payload).encode("utf-8"),
    )
    assert report.format == LEGACY_FORMAT
    assert int(
        conn.execute(
            "SELECT price FROM tenant_sale_plans WHERE id=?",
            (plan_id,),
        ).fetchone()["price"]
    ) == 111111
    assert int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM tenant_orders WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()["n"]
    ) == int(order_count)


def test_auto_backup_slot_is_durable_deduplicated_and_retryable(
    conn, factories
) -> None:
    tenant = factories.tenant(owner_telegram_id=7001)
    tenant_id = int(tenant["id"])
    slot = auto_backup_slot_key()
    assert claim_auto_backup_slot(
        conn, tenant_id=tenant_id, slot_key=slot
    ) is True
    assert claim_auto_backup_slot(
        conn, tenant_id=tenant_id, slot_key=slot
    ) is False

    finish_auto_backup_slot(
        conn,
        tenant_id=tenant_id,
        slot_key=slot,
        success=False,
        error="temporary",
    )
    assert claim_auto_backup_slot(
        conn, tenant_id=tenant_id, slot_key=slot
    ) is True
    finish_auto_backup_slot(
        conn,
        tenant_id=tenant_id,
        slot_key=slot,
        success=True,
        file_size=123,
        sha256="a" * 64,
    )
    assert claim_auto_backup_slot(
        conn, tenant_id=tenant_id, slot_key=slot
    ) is False
    row = conn.execute(
        "SELECT status,attempts,file_size,sha256 FROM tenant_backup_runs "
        "WHERE tenant_id=? AND slot_key=?",
        (tenant_id, slot),
    ).fetchone()
    assert row["status"] == "success"
    assert int(row["attempts"]) == 2
    assert int(row["file_size"]) == 123
    assert row["sha256"] == "a" * 64


def test_auto_backup_delivery_uses_tenant_admin_bot_and_excludes_run_history(
    conn, factories, cipher
) -> None:
    state = _seed_operational_tenant(conn, factories, cipher)
    tenant_id = int(state["tenant"]["id"])
    state["service"].set_userbot_setting_admin(
        int(state["tenant"]["owner_telegram_id"]),
        key="system_event_channel_enabled",
        value=True,
    )
    state["service"].set_userbot_setting_admin(
        int(state["tenant"]["owner_telegram_id"]),
        key="system_event_channel_id",
        value="@tenant_backups",
    )
    delivery = prepare_auto_backup_delivery(
        conn,
        tenant_id=tenant_id,
        owner_telegram_id=int(state["tenant"]["owner_telegram_id"]),
        cipher=cipher,
        settings=state["service"].runtime_userbot_settings(),
    )
    assert delivery is not None
    assert delivery.bot_token == state["admin_token"]
    assert delivery.event_target == "@tenant_backups"
    snapshot = decode_tenant_backup(delivery.artifact.data)
    assert "tenant_backup_runs" not in snapshot["tables"]
    complete_auto_backup_delivery(
        conn,
        delivery=delivery,
        success=True,
    )
    row = conn.execute(
        "SELECT status FROM tenant_backup_runs "
        "WHERE tenant_id=? AND slot_key=?",
        (tenant_id, delivery.slot_key),
    ).fetchone()
    assert row["status"] == "success"


class FakeBackupSender:
    def __init__(self) -> None:
        self.deliveries = []

    async def send(self, delivery) -> None:
        self.deliveries.append(delivery)


def test_lifecycle_sends_one_auto_backup_per_six_hour_slot(
    conn, db_path, factories, cipher
) -> None:
    tenant, service, *_ = _tenant_service(conn, factories, cipher)
    sender = FakeBackupSender()
    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        backup_sender=sender,
    )

    first = asyncio.run(coordinator.run_once())
    assert first.backups_claimed == 1
    assert first.backups_sent == 1
    assert first.backups_failed == 0
    assert len(sender.deliveries) == 1
    assert int(sender.deliveries[0].tenant_id) == int(tenant["id"])

    second = asyncio.run(coordinator.run_once())
    assert second.backups_claimed == 0
    assert second.backups_sent == 0
    assert len(sender.deliveries) == 1


def test_agency_infrastructure_is_strictly_tenant_scoped(
    conn, factories, cipher
) -> None:
    t1 = factories.tenant(owner_telegram_id=7001)
    t2 = factories.tenant(owner_telegram_id=8001)
    a1 = TenantAgencyInfrastructure(
        conn, tenant_id=int(t1["id"]), cipher=cipher
    )
    a2 = TenantAgencyInfrastructure(
        conn, tenant_id=int(t2["id"]), cipher=cipher
    )
    agent = a1.register_agent(
        telegram_user_id=7101,
        display_name="Tenant One Rep",
    )
    assert a1.agent(int(agent.id)).tenant_id == int(t1["id"])
    with pytest.raises(AgencyInfrastructureError, match="agent not found"):
        a2.agent(int(agent.id))


def test_phase15_backup_ui_and_auto_toggle_are_wired() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for token in (
        "ارسال خودکار بکاپ",
        "userbot:settings:backup_restore:auto_toggle",
        "userbot:settings:backup:download",
        "userbot:settings:backup:restore",
        "ZIP v2",
        "JSON v1",
    ):
        assert token in source
