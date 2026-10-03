"""Regression coverage for the SellBot-compatible Tenant AdminBot /debug report."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace


from Gateway.catalog import RuntimeBotSpec
from TenantRuntime.AdminBot import handlers
from TenantRuntime.business import TenantBusinessService


def _service(conn, factories, cipher, *, owner: int, name: str):
    tenant = factories.tenant(owner_telegram_id=owner, name=name)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    return tenant, service


def _spec(tenant: dict, *, owner: int) -> RuntimeBotSpec:
    return RuntimeBotSpec(
        bot_id=901,
        tenant_id=int(tenant["id"]),
        role="admin",
        telegram_bot_id=990001,
        telegram_username="tenant_admin_bot",
        tenant_name=str(tenant["name"]),
        owner_telegram_id=owner,
        data_namespace=f"tenant-{int(tenant['id'])}",
        token_tail="WXYZ",
        encrypted_token="encrypted-not-printed",
        token_fingerprint="fingerprint-not-printed",
    )


def test_debug_stats_are_strictly_tenant_scoped(conn, factories, cipher) -> None:
    tenant_a, service_a = _service(
        conn, factories, cipher, owner=7001, name="Tenant A"
    )
    tenant_b, service_b = _service(
        conn, factories, cipher, owner=8001, name="Tenant B"
    )

    service_a.register_customer(
        7101, display_name="A User", username="a_user"
    )
    service_b.register_customer(
        8101, display_name="B User", username="b_user"
    )
    service_b.register_customer(
        8102, display_name="B User 2", username="b_user_2"
    )
    service_a.add_server(
        7001,
        label="A Server",
        panel_kind="hiddify",
        endpoint="https://a.example",
    )
    service_b.add_server(
        8001,
        label="B Server",
        panel_kind="hiddify",
        endpoint="https://b.example",
    )

    stats_a = handlers._tenant_debug_stats(service_a)
    stats_b = handlers._tenant_debug_stats(service_b)

    assert stats_a["customers"] == 1
    assert stats_b["customers"] == 2
    assert stats_a["servers"] == 1
    assert stats_b["servers"] == 1
    assert int(tenant_a["id"]) != int(tenant_b["id"])


def test_debug_report_matches_sellbot_sections_without_secret_leak(
    conn, factories, cipher, monkeypatch
) -> None:
    tenant, service = _service(
        conn, factories, cipher, owner=7001, name="Debug Tenant"
    )
    service.register_customer(
        7101, display_name="Debug User", username="debug_user"
    )
    spec = _spec(tenant, owner=7001)

    async def probe(_host: str, _port: int, timeout: float = 1.8):
        del timeout
        return True, "ok"

    monkeypatch.setattr(handlers, "_debug_tcp_probe", probe)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"runtime_started_at": time.time() - 65},
            job_queue=None,
        )
    )

    report = asyncio.run(
        handlers._build_admin_debug_report(
            context,
            business=service,
            spec=spec,
            actor=7001,
        )
    )

    for section in (
        "🧪 Debug Report",
        "🔐 Runtime / Tenant",
        "📁 Runtime Storage",
        "📊 Data",
        "🏢 Reseller / CustomerBot",
        "🗄 Database Health",
        "⚙️ Jobs",
        "🌐 Network",
        "📜 Tenant Error Snapshot",
    ):
        assert section in report

    assert "TENANT: Debug Tenant" in report
    assert "customers=1" in report
    assert "✅ integrity OK" in report
    assert "***WXYZ" in report
    assert "encrypted-not-printed" not in report
    assert "fingerprint-not-printed" not in report
    assert "Raw shard journal" in report


def test_debug_helpers_match_sellbot_delivery_guards(conn) -> None:
    assert handlers._debug_fmt_duration(3661) == "01:01:01"
    assert handlers._debug_sqlite_quick_check(conn) == "✅ integrity OK"

    long_line = "x" * 1300
    parts = handlers._debug_split_text(long_line, chunk_size=500)
    assert len(parts) == 3
    assert all(len(part) <= 500 for part in parts)
    assert "".join(parts) == long_line


def test_adminbot_registers_debug_command() -> None:
    source = open(
        "TenantRuntime/AdminBot/handlers.py", encoding="utf-8"
    ).read()
    assert 'CommandHandler("debug", debug)' in source

    worker = open("TenantRuntime/worker.py", encoding="utf-8").read()
    assert 'application.bot_data["runtime_started_at"] = time.time()' in worker
