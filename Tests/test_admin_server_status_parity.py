"""Regression tests for SellBot-compatible Tenant AdminBot server status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from Gateway.catalog import RuntimeBotSpec
from TenantRuntime.AdminBot import handlers
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.panels import PanelTarget
from TenantRuntime.xnet import XnetPanelAdapter
from TenantRuntime.xui import XuiPanelAdapter


class SequenceStatsPanel:
    def __init__(self, values: list[dict] | None = None) -> None:
        self.values = list(values or [])
        self.calls = 0

    def server_stats(self, *, target, secret):
        assert secret
        assert target.kind in {"hiddify", "xui", "xnet"}
        index = min(self.calls, max(0, len(self.values) - 1))
        self.calls += 1
        return dict(self.values[index]) if self.values else {}


class StateStore:
    def __init__(self) -> None:
        self.values: dict[int, dict] = {}

    def load(self, user_id: int):
        return dict(self.values.get(int(user_id), {}))

    def save(self, user_id: int, value: dict):
        self.values[int(user_id)] = dict(value)


class AllowPolicy:
    def check(self, spec, *, telegram_user_id):
        return SimpleNamespace(allowed=True, license_status="active")


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return self


class Query:
    def __init__(self, data: str = "") -> None:
        self.data = data
        self.edits: list[tuple[str, dict]] = []
        self.answers = 0
        self.deleted = False
        self.message = Message()

    async def answer(self, *args, **kwargs):
        self.answers += 1

    async def edit_message_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return self

    async def delete(self):
        self.deleted = True


def _spec(tenant_id: int, owner: int) -> RuntimeBotSpec:
    return RuntimeBotSpec(
        tenant_id=tenant_id,
        tenant_name="Status Tenant",
        role="admin",
        token="123456:FAKE_status_token_abcdefghijklmnopqrstuvwxyz",
        owner_telegram_id=owner,
        runtime_status="ready",
        license_status="active",
    )


def _service(conn, factories, cipher, *, owner: int = 7701, panel=None):
    tenant = factories.tenant(owner_telegram_id=owner)
    business = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel or SequenceStatsPanel(),
    )
    return tenant, business


def _add_server(business, owner: int, *, label: str, kind: str = "hiddify"):
    server = business.add_server(
        owner,
        label=label,
        panel_kind=kind,
        xui_flavor="sanaei" if kind == "xui" else "",
        endpoint=f"https://{label.lower().replace(' ', '-')}.example",
        admin_path="admin" if kind == "hiddify" else "",
        user_path="user" if kind == "hiddify" else "",
    )
    if kind == "xui":
        business.set_xui_credential(
            owner,
            server_id=int(server["id"]),
            api_token="status-token",
        )
    elif kind == "xnet":
        business.set_xnet_credential(
            owner,
            server_id=int(server["id"]),
            api_token="status-token",
        )
    else:
        business.set_panel_credential(
            owner,
            server_id=int(server["id"]),
            secret="status-key",
        )
    return server


def test_status_server_list_matches_sellbot_text_order_and_callbacks(
    conn, factories, cipher
):
    tenant, business = _service(conn, factories, cipher)
    owner = int(tenant["owner_telegram_id"])
    first = _add_server(business, owner, label="Turkey")
    second = _add_server(business, owner, label="France")

    text, markup = handlers._status_servers_view(business)
    assert text == "📈 **وضعیت سرور**\n\nیکی از سرورهای زیر را انتخاب کنید:"
    rows = markup.inline_keyboard
    assert [row[0].text for row in rows] == [
        second["label"],
        first["label"],
        "بازگشت🔙",
    ]
    assert [row[0].callback_data for row in rows] == [
        f"status_srv:{second['id']}",
        f"status_srv:{first['id']}",
        "status:back",
    ]


def test_status_detail_matches_sellbot_and_normalizes_xui_bytes() -> None:
    gib = 1024 ** 3
    text = handlers._server_status_detail_text(
        {
            "server_label": "France",
            "panel_kind": "xui",
            "cpu_percent": 12.345,
            "cpu_cores": 4,
            "ram_used": 2 * gib,
            "ram_total": 4 * gib,
            "disk_used": 10 * gib,
            "disk_total": 20 * gib,
            "users_total": 106,
            "usage_today_gb": 3.5,
            "users_online": 30,
            "now_net_recv_mb": 4.25,
            "now_net_sent_mb": 2.75,
            "users_today": 41,
            "users_month": 88,
            "usage_30days_gb": 99.9,
            "traffic_dl": 70.2,
            "traffic_ul": 29.7,
        }
    )
    assert text == (
        "Server: France\n"
        "--------------------------------\n"
        "SYSTEM INFO\n"
        "CPU: 12.35% - 4 CORE\n"
        "RAM: 2.00 GB / 4.00 GB (50.00%)\n"
        "DISK: 10.00 GB / 20.00 GB  (50.00%)\n\n"
        "NETWORK INFO\n"
        "Total Users: 106 User\n"
        "Usage (Today): 3.50 GB\n"
        "Online (Now): 30 User\n"
        "Now Network Received: 4.25 MB\n"
        "Now Network Sent: 2.75 MB\n"
        "Online (Today): 41 User\n"
        "Online(30 Days): 88 User\n"
        "Usage(30 Days): 99.90 GB\n"
        "Total Download (Server): 70.20 GB\n"
        "Total Upload (Server): 29.70 GB"
    )


def test_xnet_status_uses_realtime_rate_units() -> None:
    text = handlers._server_status_detail_text(
        {
            "server_label": "X-Net",
            "panel_kind": "xnet",
            "ram_total": 1,
            "disk_total": 1,
            "now_net_recv_mb": 1.25,
            "now_net_sent_mb": 0.75,
        }
    )
    assert "Now Network Received: 1.25 MB/s" in text
    assert "Now Network Sent: 0.75 MB/s" in text


def test_status_business_is_tenant_scoped_and_xui_daily_usage_is_delta(
    conn, factories, cipher
):
    panel = SequenceStatsPanel(
        [
            {"usage_30days_gb": 10.0, "users_total": 5},
            {"usage_30days_gb": 12.5, "users_total": 5},
        ]
    )
    tenant, business = _service(conn, factories, cipher, panel=panel)
    owner = int(tenant["owner_telegram_id"])
    server = _add_server(business, owner, label="Sanaei", kind="xui")

    first = business.server_status_admin(owner, server_id=int(server["id"]))
    second = business.server_status_admin(owner, server_id=int(server["id"]))
    assert first["usage_today_gb"] == 0
    assert second["usage_today_gb"] == pytest.approx(2.5)

    row = conn.execute(
        "SELECT * FROM tenant_server_traffic_daily "
        "WHERE tenant_id=? AND server_id=?",
        (int(tenant["id"]), int(server["id"])),
    ).fetchone()
    assert row is not None
    assert float(row["baseline_gb"]) == pytest.approx(10.0)
    assert float(row["last_total_gb"]) == pytest.approx(12.5)

    other_tenant, other = _service(
        conn,
        factories,
        cipher,
        owner=8801,
        panel=panel,
    )
    with pytest.raises(TenantBusinessError):
        other.server_status_admin(
            int(other_tenant["owner_telegram_id"]),
            server_id=int(server["id"]),
        )


def test_status_main_button_routes_to_server_list_not_runtime_status(
    monkeypatch, conn, factories, cipher
):
    tenant, business = _service(conn, factories, cipher)
    owner = int(tenant["owner_telegram_id"])
    message = Message(handlers.BTN_STATUS)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={})
    called = {"server": 0, "runtime": 0}

    monkeypatch.setattr(
        handlers,
        "_services",
        lambda ctx: (
            SimpleNamespace(role="admin"),
            None,
            None,
            business,
        ),
    )

    async def no_server_action(*args, **kwargs):
        return False

    async def server_status(*args, **kwargs):
        called["server"] += 1

    async def runtime_status(*args, **kwargs):
        called["runtime"] += 1

    from TenantRuntime.AdminBot import server_actions

    monkeypatch.setattr(server_actions, "handle_text", no_server_action)
    monkeypatch.setattr(handlers, "show_server_status_list", server_status)
    monkeypatch.setattr(handlers, "show_status", runtime_status)

    asyncio.run(handlers.unknown_text(update, context))
    assert called == {"server": 1, "runtime": 0}


def test_status_list_and_detail_handlers_use_sellbot_flow(
    monkeypatch, conn, factories, cipher
):
    stats = {
        "cpu_percent": 5,
        "cpu_cores": 2,
        "ram_used": 1,
        "ram_total": 2,
        "disk_used": 5,
        "disk_total": 10,
        "users_total": 7,
        "users_online": 2,
        "users_today": 3,
        "users_month": 6,
        "usage_today_gb": 1.5,
        "usage_30days_gb": 12,
        "traffic_dl": 8,
        "traffic_ul": 4,
        "now_net_recv_mb": 0.3,
        "now_net_sent_mb": 0.2,
    }
    panel = SequenceStatsPanel([stats])
    tenant, business = _service(conn, factories, cipher, panel=panel)
    owner = int(tenant["owner_telegram_id"])
    server = _add_server(business, owner, label="Turkey")
    state = StateStore()
    services = (
        _spec(int(tenant["id"]), owner),
        AllowPolicy(),
        state,
        business,
    )
    monkeypatch.setattr(handlers, "_services", lambda ctx: services)

    message = Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={})
    asyncio.run(handlers.show_server_status_list(update, context))
    assert message.sent[-1][0] == (
        "📈 **وضعیت سرور**\n\nیکی از سرورهای زیر را انتخاب کنید:"
    )
    assert message.sent[-1][1]["parse_mode"] == "Markdown"
    assert message.sent[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"status_srv:{server['id']}"
    )

    query = Query(f"status_srv:{server['id']}")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner),
        effective_message=query.message,
        effective_chat=None,
        callback_query=query,
    )
    asyncio.run(
        handlers.show_server_status_detail(
            update,
            context,
            server_id=int(server["id"]),
        )
    )
    assert query.edits[0][0] == "⏳ در حال دریافت اطلاعات از سرور..."
    assert query.edits[-1][0].startswith("Server: Turkey\n")
    assert query.edits[-1][1]["reply_markup"].inline_keyboard[0][0].text == "بازگشت🔙"
    assert query.edits[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == (
        "status:back_to_list"
    )


def test_hiddify_adapter_reads_real_server_status_contract() -> None:
    gib = 1024 ** 3
    user = {
        "uuid": "u-1",
        "name": "User",
        "current_usage_GB": 1,
        "usage_limit_GB": 10,
        "package_days": 30,
        "start_date": "2026-10-01",
        "last_online": "2026-10-03T09:00:00+00:00",
        "is_active": True,
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/api/v2/admin/user/"):
            return httpx.Response(200, json=[user], request=request)
        if path.endswith("/api/v2/panel/info/"):
            return httpx.Response(200, json={"version": "12"}, request=request)
        if path.endswith("/api/v2/admin/server_status/"):
            return httpx.Response(
                200,
                json={
                    "stats": {
                        "system": {
                            "cpu_percent": 25,
                            "num_cpus": 4,
                            "ram_used": 2,
                            "ram_total": 4,
                            "disk_used": 10,
                            "disk_total": 20,
                            "bytes_recv": 2 * 1024**2,
                            "bytes_sent": 1024**2,
                            "bytes_recv_cumulative": 30 * gib,
                            "net_sent_cumulative_GB": 10,
                        }
                    },
                    "usage_history": {
                        "total": {"users": 9},
                        "m5": {"online": 2},
                        "today": {"online": 4, "usage": 3 * gib},
                        "last_30_days": {"online": 8, "usage": 40 * gib},
                    },
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        "hiddify",
        "https://panel.example",
        admin_path="admin",
        user_path="user",
    )
    stats = adapter.server_stats(target=target, secret="key")
    assert stats["cpu_percent"] == 25
    assert stats["cpu_cores"] == 4
    assert stats["users_total"] == 9
    assert stats["users_online"] == 2
    assert stats["usage_today_gb"] == pytest.approx(3)
    assert stats["usage_30days_gb"] == pytest.approx(40)
    assert stats["traffic_dl"] == pytest.approx(30)
    assert stats["traffic_ul"] == pytest.approx(10)
    assert stats["now_net_recv_mb"] == pytest.approx(2)


def test_xui_adapter_reads_status_presence_and_client_traffic() -> None:
    gib = 1024 ** 3
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    client = {
        "subId": "sub-1",
        "email": "buyer",
        "enable": True,
        "totalGB": 20 * gib,
        "expiryTime": now_ms + 30 * 86400 * 1000,
        "traffic": {"up": gib, "down": 2 * gib},
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/inbounds/list"):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/clients/list"):
            return httpx.Response(200, json=[client], request=request)
        if path.endswith("/clients/onlines"):
            return httpx.Response(200, json=["buyer"], request=request)
        if path.endswith("/clients/lastOnline"):
            return httpx.Response(
                200,
                json={"buyer": now_ms},
                request=request,
            )
        if path.endswith("/server/status"):
            return httpx.Response(
                200,
                json={
                    "cpu": 15,
                    "cpuCount": 2,
                    "mem": {"current": 2 * gib, "total": 4 * gib},
                    "disk": {"current": 5 * gib, "total": 10 * gib},
                    "netIO": {"down": 3 * 1024**2, "up": 2 * 1024**2},
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        "xui",
        "https://xui.example",
        xui_flavor="sanaei",
    )
    stats = adapter.server_stats(target=target, secret="api-token")
    assert stats["cpu_percent"] == pytest.approx(15)
    assert stats["cpu_cores"] == 2
    assert stats["ram_used"] == 2 * gib
    assert stats["users_total"] == 1
    assert stats["users_online"] == 1
    assert stats["usage_30days_gb"] == pytest.approx(3)
    assert stats["traffic_ul"] == pytest.approx(1)
    assert stats["traffic_dl"] == pytest.approx(2)
    assert stats["now_net_recv_mb"] == pytest.approx(3)


def test_xnet_adapter_reads_metrics_traffic_analytics_and_realtime() -> None:
    gib = 1024 ** 3
    expiry = datetime.now(timezone.utc).timestamp() + 30 * 86400
    inbound = {
        "id": "in-1",
        "enabled": True,
        "protocol": "vless",
        "clients": [
            {
                "id": "client-1",
                "uuid": "sub-1",
                "username": "buyer",
                "status": "active",
                "trafficUsedBytes": gib,
                "trafficLimitBytes": 20 * gib,
                "expireDate": datetime.fromtimestamp(
                    expiry, tz=timezone.utc
                ).isoformat(),
            }
        ],
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/api/v1/ping"):
            return httpx.Response(200, json={"ok": True}, request=request)
        if path.endswith("/api/inbounds"):
            return httpx.Response(200, json=[inbound], request=request)
        if path.endswith("/api/online-users"):
            return httpx.Response(
                200,
                json={"singbox": [{"clientId": "client-1"}]},
                request=request,
            )
        if path.endswith("/api/metrics"):
            return httpx.Response(
                200,
                json={
                    "cpuUsage": 20,
                    "cpuCores": 4,
                    "ramUsage": {"used": 1.5, "total": 4},
                    "storageUsage": {"used": 8, "total": 20},
                    "onlineUsersCount": 1,
                },
                request=request,
            )
        if path.endswith("/api/traffic/singbox/summary"):
            return httpx.Response(
                200,
                json={
                    "totalUpload": 10 * gib,
                    "totalDownload": 20 * gib,
                    "todayUpload": gib,
                    "todayDownload": 2 * gib,
                    "activeClients": 1,
                },
                request=request,
            )
        if path.endswith("/api/traffic/singbox/analytics"):
            return httpx.Response(
                200,
                json={
                    "consumers": [
                        {
                            "kind": "vpn",
                            "clientId": "client-1",
                            "periodTotal": 12 * gib,
                        }
                    ]
                },
                request=request,
            )
        if path.endswith("/api/metrics/tick"):
            return httpx.Response(
                200,
                json={"networkTraffic": {"down": 3.5, "up": 1.25}},
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = XnetPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget("xnet", "https://xnet.example")
    stats = adapter.server_stats(target=target, secret="api-token")
    assert stats["cpu_percent"] == pytest.approx(20)
    assert stats["cpu_cores"] == 4
    assert stats["users_total"] == 1
    assert stats["users_online"] == 1
    assert stats["usage_today_gb"] == pytest.approx(3)
    assert stats["usage_30days_gb"] == pytest.approx(12)
    assert stats["traffic_ul"] == pytest.approx(10)
    assert stats["traffic_dl"] == pytest.approx(20)
    assert stats["now_net_recv_mb"] == pytest.approx(3.5)
    assert stats["now_net_sent_mb"] == pytest.approx(1.25)
