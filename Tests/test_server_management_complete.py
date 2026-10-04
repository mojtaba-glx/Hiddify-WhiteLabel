"""Live inventory and SellBot-style server action integration, without real panels."""

import asyncio
import json
import threading
import uuid
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from Database.connection import connect
from Database.migrate import migrate
from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import (
    PanelError,
    PanelUserResult,
    ProvisionResult,
    UsageResult,
    PanelTarget,
)
from TenantRuntime.server_admin import ServerAdminService, user_status
from TenantRuntime.AdminBot import handlers, server_actions
from TenantRuntime.UserBot import dynamic_plans
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.xui import XuiPanelAdapter
from TenantRuntime.xnet import XnetPanelAdapter
from Tests.test_server_connection_parity import Messages, Query
from Tests.test_user_account_status_phase6 import _service


class InventoryPanel:
    def __init__(self):
        self.users = {}
        self.mutations = []
        self.offline = set()
        self.network_threads = []

    def seed(
        self,
        endpoint,
        ref,
        *,
        name="Panel user",
        usage=3 * 1024**3,
        traffic=10 * 1024**3,
        active=True,
    ):
        self.users[endpoint, ref] = dict(
            external_ref=ref,
            name=name,
            comment="note",
            usage_bytes=usage,
            traffic_bytes=traffic,
            expires_at=iso_utc(utcnow() + timedelta(days=10)),
            active=active,
            last_online=iso_utc(utcnow()),
        )

    def list_users(self, *, target, secret):
        self.network_threads.append(threading.get_ident())
        if target.endpoint in self.offline:
            raise PanelError("offline")
        return [dict(v) for (e, r), v in self.users.items() if e == target.endpoint]

    def get_user(self, *, target, secret, external_ref):
        if (
            target.endpoint in self.offline
            or (target.endpoint, external_ref) not in self.users
        ):
            raise PanelError("not found")
        return PanelUserResult(**self.users[target.endpoint, external_ref])

    def provision(self, *, target, secret, request):
        ref = request.external_ref or str(
            uuid.uuid5(uuid.NAMESPACE_URL, request.idempotency_key)
        )
        if (target.endpoint, ref) not in self.users:
            self.seed(
                target.endpoint,
                ref,
                name=request.name or "Created",
                usage=0,
                traffic=request.traffic_bytes,
            )
            self.users[target.endpoint, ref]["expires_at"] = request.expires_at
            self.mutations.append(("create", target.endpoint, ref))
        return ProvisionResult(ref)

    def update_user(self, *, target, secret, external_ref, changes):
        if target.endpoint in self.offline:
            raise PanelError("offline")
        row = self.users[target.endpoint, external_ref]
        self.mutations.append(("edit", target.endpoint, external_ref, dict(changes)))
        row.update({k: v for k, v in changes.items() if k in row})
        if changes.get("reset_usage"):
            row["usage_bytes"] = 0
        return self.get_user(target=target, secret=secret, external_ref=external_ref)

    def set_enabled(self, *, target, secret, external_ref, enabled):
        self.users[target.endpoint, external_ref]["active"] = enabled
        self.mutations.append(("state", target.endpoint, external_ref, enabled))
        return self.get_user(target=target, secret=secret, external_ref=external_ref)

    def delete_user(self, *, target, secret, external_ref):
        self.users.pop((target.endpoint, external_ref), None)
        self.mutations.append(("delete", target.endpoint, external_ref))

    def renew(self, *, target, secret, external_ref, request):
        row = self.users[target.endpoint, external_ref]
        row.update(
            traffic_bytes=request.traffic_bytes,
            expires_at=request.expires_at,
            active=True,
        )
        if request.reset_usage:
            row["usage_bytes"] = 0
        return self.get_user(target=target, secret=secret, external_ref=external_ref)

    def usage(self, *, target, secret, external_ref):
        user = self.get_user(target=target, secret=secret, external_ref=external_ref)
        return UsageResult(user.usage_bytes, user.active, user.last_online)

    def subscription_link(self, *, target, external_ref):
        return (target.public_origin or target.endpoint) + "/sub/" + external_ref

    def subscription_content(self, **kwargs):
        return "vless://" + kwargs["external_ref"] + "@vpn.example:443"


def setup(conn, factories, cipher):
    b, _, actor, subid, _ = _service(conn, factories, cipher)
    panel = InventoryPanel()
    b.panel_adapter = panel
    source = b.list_servers()[0]
    panel.seed(source["endpoint"], "status-user-1")
    child = b.add_server(
        7001,
        label="Child",
        panel_kind="hiddify",
        endpoint="https://child.example",
        user_path="user",
    )
    b.set_panel_credential(7001, server_id=child["id"], secret="child-secret")
    return b, panel, ServerAdminService(b), source, child, actor, subid


def ui(monkeypatch, b):
    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7001),
        effective_message=message,
        effective_chat=message,
        callback_query=None,
    )
    monkeypatch.setattr(
        handlers, "_services", lambda _: (SimpleNamespace(role="admin"), None, None, b)
    )
    return update, context, message


async def click(update, context, data):
    update.callback_query = Query(data, update.effective_message)
    await handlers.on_callback(update, context)
    update.callback_query = None


async def send(update, context, text):
    update.effective_message.text = text
    await handlers.unknown_text(update, context)


def cb(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_server_menu_matches_screenshot_and_uses_live_count(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    p.seed(source["endpoint"], "outside-master", name="Outside master")
    update, context, message = ui(monkeypatch, b)
    asyncio.run(click(update, context, f'srv:view:{source["id"]}'))
    text, kwargs = message.sent[-1]
    assert "تعداد کاربران: 2 از" in text
    labels = [row[0].text for row in kwargs["reply_markup"].inline_keyboard]
    assert labels == [
        "👤لیست کاربران",
        "🛡️عملیات کاربری",
        "📋پلن ها",
        "🔗لیست دامنه‌ها",
        "✏️ویرایش سرور",
        "🗑️حذف سرور",
        "⚙️لیست نودها",
        "🔄همگام سازی نودها",
        "❄️ کاربران یخ‌زده این سرور",
        "↩️بازگشت",
    ]
    assert "تست اتصال" not in str(labels) and "پیش‌فرض فروش" not in str(labels)
    assert "📦 نسخه: V11,12" in text
    assert all(t != threading.get_ident() for t in p.network_threads)


def test_inventory_lists_every_panel_user_and_pages_past_40(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    for i in range(48):
        p.seed(source["endpoint"], f"external-{i}", name=f"User {i}")
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f'srv:users:{source["id"]}')
        assert len(s.users(7001, source["id"])) == 49
        reachable = []
        for page in range(5):
            await click(update, context, f'srv:users:{source["id"]}:all:{page}')
            reachable.extend(
                int(x.rsplit(":", 1)[1])
                for x in cb(message.sent[-1][1]["reply_markup"])
                if x.startswith("srv:puser:")
            )
        assert len(set(reachable)) == 49
        assert conn.execute("SELECT COUNT(*) FROM tenant_customers").fetchone()[0] == 1

    asyncio.run(run())


def test_read_failure_preserves_cache_and_marks_stale(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    asyncio.run(s.refresh_users(7001, source["id"]))
    p.offline.add(source["endpoint"])
    update, context, message = ui(monkeypatch, b)
    asyncio.run(click(update, context, f'srv:users:{source["id"]}'))
    assert "ذخیره‌شده" in message.sent[-1][0]
    assert len(s.users(7001, source["id"])) == 1
    assert not p.mutations


def test_inventory_malformed_response_is_atomic(conn, factories, cipher):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    asyncio.run(s.refresh_users(7001, source["id"]))
    p.list_users = lambda **kw: [{"external_ref": ""}]
    with pytest.raises(TenantBusinessError):
        asyncio.run(s.refresh_users(7001, source["id"]))
    assert len(s.users(7001, source["id"])) == 1


def test_native_user_actions_and_scope_checks(conn, factories, cipher):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    p.seed(source["endpoint"], "outside-master", name="Native")
    users = asyncio.run(s.refresh_users(7001, source["id"]))
    native = next(u for u in users if u["external_ref"] == "outside-master")
    asyncio.run(
        s.edit_user(
            7001,
            source["id"],
            native["id"],
            {"name": "New name", "comment": "New note"},
        )
    )
    assert p.users[source["endpoint"], "outside-master"]["usage_bytes"] == 3 * 1024**3
    asyncio.run(
        s.edit_user(7001, source["id"], native["id"], {"traffic_bytes": 20 * 1024**3})
    )
    assert p.users[source["endpoint"], "outside-master"]["usage_bytes"] == 3 * 1024**3
    asyncio.run(s.toggle_user(7001, source["id"], native["id"]))
    assert not p.users[source["endpoint"], "outside-master"]["active"]
    with pytest.raises(TenantBusinessError):
        s.user(7001, child["id"], native["id"])
    with pytest.raises(PermissionError):
        s.user(7101, source["id"], native["id"])
    other = factories.tenant(owner_telegram_id=8001)
    foreign = ServerAdminService(
        TenantBusinessService(
            conn,
            tenant_id=other["id"],
            owner_telegram_id=8001,
            secret_cipher=cipher,
            panel_adapter=p,
        )
    )
    with pytest.raises(TenantBusinessError):
        foreign.user(8001, source["id"], native["id"])


def test_create_multi_retry_does_not_duplicate_users_or_payments(
    conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)

    async def run():
        a = await s.create_users(
            7001,
            source["id"],
            name="Batch",
            gb=10,
            days=30,
            count=3,
            operation_key="one",
        )
        again = await s.create_users(
            7001,
            source["id"],
            name="Batch",
            gb=10,
            days=30,
            count=3,
            operation_key="one",
        )
        assert len(a["users"]) == len(again["users"]) == 3
        assert len([x for x in p.mutations if x[0] == "create"]) == 3
        assert conn.execute("SELECT COUNT(*) FROM tenant_orders").fetchone()[0] == 1

    asyncio.run(run())


def test_button_wizards_create_edit_search_and_cancel(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f'srv:useradd:{source["id"]}:single')
        for text in ("New customer", "15", "20"):
            await send(update, context, text)
        assert server_actions.FLOW not in context.user_data
        assert not any(
            "یادداشت را وارد کنید" in str(sent[0])
            for sent in message.sent
        )
        users = s.users(7001, source["id"])
        row = next(x for x in users if x["name"] == "New customer")
        await click(update, context, f'srv:pfield:{source["id"]}:{row["id"]}:name')
        await send(update, context, "Changed name")
        assert (
            p.users[source["endpoint"], row["external_ref"]]["name"] == "Changed name"
        )
        await click(update, context, f'srv:usersearch:{source["id"]}')
        await send(update, context, "https://sub.example/sub/" + row["external_ref"])
        assert "1 نتیجه" in message.sent[-1][0]
        await click(update, context, f'srv:planadd:{source["id"]}')
        await send(update, context, "❌ لغو")
        assert server_actions.FLOW not in context.user_data

    asyncio.run(run())


def test_plan_user_creation_finishes_after_name_without_note(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    plan = b.add_plan(
        7001,
        name="Quick plan",
        traffic_gb=25,
        duration_days=45,
        price=100000,
        server_id=source["id"],
    )
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(
            update,
            context,
            f'srv:useraddplan:{source["id"]}:{plan["id"]}',
        )
        assert context.user_data[server_actions.FLOW]["kind"] == "create_name"
        await send(update, context, "Plan user")
        assert server_actions.FLOW not in context.user_data
        assert not any(
            "یادداشت را وارد کنید" in str(sent[0])
            for sent in message.sent
        )
        row = next(
            x for x in s.users(7001, source["id"])
            if x["name"] == "Plan user"
        )
        panel_row = p.users[source["endpoint"], row["external_ref"]]
        assert panel_row["traffic_bytes"] == 25 * 1024**3
        assert s.reset_duration(7001, source["id"], row["id"]) == 45

    asyncio.run(run())


def test_create_user_on_primary_auto_syncs_all_attached_panel_node_types(
    conn, factories, cipher
):
    b, p, s, source, hiddify_node, _, _ = setup(conn, factories, cipher)
    xui_node = b.add_server(
        7001,
        label="XUI node",
        panel_kind="xui",
        endpoint="https://xui-node.example",
        xui_flavor="sanaei",
    )
    b.set_panel_credential(
        7001, server_id=xui_node["id"], secret="xui-secret"
    )
    xnet_node = b.add_server(
        7001,
        label="XNET node",
        panel_kind="xnet",
        endpoint="https://xnet-node.example",
    )
    b.set_panel_credential(
        7001, server_id=xnet_node["id"], secret="xnet-secret"
    )
    for child in (hiddify_node, xui_node, xnet_node):
        s.attach_node(7001, source["id"], child["id"])

    async def run():
        report = await s.create_users(
            7001,
            source["id"],
            name="All nodes",
            gb=12,
            days=30,
            operation_key="all-node-types",
        )
        assert report["errors"] == 0
        assert report["node_errors"] == 0
        assert len(report["users"]) == 1
        ref = report["users"][0]["external_ref"]
        for server in (source, hiddify_node, xui_node, xnet_node):
            assert (server["endpoint"], ref) in p.users
            assert p.users[server["endpoint"], ref]["name"] == "All nodes"

    asyncio.run(run())


def test_create_user_directly_on_any_node_server_does_not_require_parent_flow(
    conn, factories, cipher
):
    b, p, s, source, hiddify_node, _, _ = setup(conn, factories, cipher)
    xui_node = b.add_server(
        7001,
        label="Direct XUI",
        panel_kind="xui",
        endpoint="https://direct-xui.example",
        xui_flavor="alireza",
    )
    b.set_panel_credential(
        7001, server_id=xui_node["id"], secret="xui-direct-secret"
    )
    xnet_node = b.add_server(
        7001,
        label="Direct XNET",
        panel_kind="xnet",
        endpoint="https://direct-xnet.example",
    )
    b.set_panel_credential(
        7001, server_id=xnet_node["id"], secret="xnet-direct-secret"
    )

    async def run():
        for index, server in enumerate((hiddify_node, xui_node, xnet_node), 1):
            report = await s.create_users(
                7001,
                server["id"],
                name=f"Direct {index}",
                gb=5,
                days=10,
                operation_key=f"direct-node-{index}",
            )
            assert report["errors"] == 0
            assert report["node_errors"] == 0
            ref = report["users"][0]["external_ref"]
            assert (server["endpoint"], ref) in p.users
            assert (source["endpoint"], ref) not in p.users

    asyncio.run(run())


def test_sync_report_is_read_only_and_missing_creates_same_uuid(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    p.seed(source["endpoint"], "native-user", name="Outside master", active=False)
    s.attach_node(7001, source["id"], child["id"])

    async def run():
        report = await s.sync_nodes(7001, source["id"], "report")
        assert report["missing"] == 2 and not p.mutations
        full = await s.sync_nodes(7001, source["id"], "missing")
        assert full["created"] == 2 and full["errors"] == 0
        assert (child["endpoint"], "native-user") in p.users
        assert not p.users[child["endpoint"], "native-user"]["active"]
        assert p.users[child["endpoint"], "native-user"]["name"] == "Outside master"
        again = await s.sync_nodes(7001, source["id"], "missing")
        assert again["created"] == 0

    asyncio.run(run())


def test_details_preserve_name_counters_and_disabled_state(conn, factories, cipher):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.seed(
        child["endpoint"],
        "status-user-1",
        name="Do not rename",
        usage=7 * 1024**3,
        traffic=99 * 1024**3,
        active=False,
    )
    result = asyncio.run(s.sync_nodes(7001, source["id"], "details"))
    row = p.users[child["endpoint"], "status-user-1"]
    assert result["updated"] == 1 and row["traffic_bytes"] == 10 * 1024**3
    assert (
        row["name"] == "Do not rename"
        and row["usage_bytes"] == 7 * 1024**3
        and row["active"] is False
    )
    assert not any(m[0] == "state" for m in p.mutations)


def test_extras_never_deleted_and_get_actionable_details(conn, factories, cipher):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.seed(child["endpoint"], "unrelated-native", name="Unrelated")
    report = asyncio.run(s.sync_nodes(7001, source["id"], "full"))
    assert len(report["extras"]) == 1
    extra = report["extras"][0]
    assert s.user(7001, child["id"], extra["id"])["external_ref"] == "unrelated-native"
    assert (child["endpoint"], "unrelated-native") in p.users
    assert not any(m[0] == "delete" for m in p.mutations)


def test_node_outage_creates_frozen_record_and_retry_clears_it(conn, factories, cipher):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.offline.add(child["endpoint"])
    report = asyncio.run(s.sync_nodes(7001, source["id"], "full"))
    assert report["errors"] and s.frozen(7001, source["id"])
    p.offline.clear()
    again = asyncio.run(s.sync_nodes(7001, source["id"], "full"))
    assert again["errors"] == 0 and not s.frozen(7001, source["id"])


def test_node_cycles_and_wrong_parent_rejected(conn, factories, cipher):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    n = s.attach_node(7001, source["id"], child["id"])
    with pytest.raises(TenantBusinessError):
        s.attach_node(7001, child["id"], source["id"])
    with pytest.raises(TenantBusinessError):
        s.attach_node(7001, source["id"], child["id"])
    with pytest.raises(TenantBusinessError):
        s.node(7001, child["id"], n["id"])


def test_node_disable_and_removal_only_touch_parent_replicas(conn, factories, cipher):
    b, p, s, source, child, _, subid = setup(conn, factories, cipher)
    n = s.attach_node(7001, source["id"], child["id"])
    p.seed(child["endpoint"], "unrelated", name="Unrelated")
    asyncio.run(s.sync_nodes(7001, source["id"], "full"))
    p.users[child["endpoint"], "status-user-1"]["usage_bytes"] = 1024**3
    b.sync_subscription_usage(7001, subscription_id=subid)
    before = b.subscription_admin(7001, subscription_id=subid)["usage_bytes"]
    asyncio.run(s.set_node_enabled(7001, source["id"], n["id"], False))
    assert p.users[child["endpoint"], "status-user-1"]["active"] is False
    assert p.users[child["endpoint"], "unrelated"]["active"] is True
    b.sync_subscription_usage(7001, subscription_id=subid)
    assert b.subscription_admin(7001, subscription_id=subid)["usage_bytes"] == before
    asyncio.run(s.remove_node(7001, source["id"], n["id"]))
    assert (child["endpoint"], "status-user-1") not in p.users and (
        child["endpoint"],
        "unrelated",
    ) in p.users
    b.sync_subscription_usage(7001, subscription_id=subid)
    assert b.subscription_admin(7001, subscription_id=subid)["usage_bytes"] == before


def test_server_plans_crud_scope_and_old_order_price(conn, factories, cipher):
    b, p, s, source, child, actor, _ = setup(conn, factories, cipher)
    plan = b.add_plan(
        7001,
        name="Source only",
        traffic_gb=20,
        duration_days=30,
        price=200,
        currency="IRT",
        server_id=source["id"],
    )
    assert plan["id"] in [r["id"] for r in s.plans(7001, source["id"])]
    assert plan["id"] not in [r["id"] for r in s.plans(7001, child["id"])]
    with pytest.raises(TenantBusinessError):
        b.create_order(actor, plan["id"], server_id=child["id"])
    order = b.create_order(actor, plan["id"], server_id=source["id"])
    s.edit_plan(7001, source["id"], plan["id"], {"price": 300})
    assert b.order(actor, order["id"])["amount"] == 200
    with pytest.raises(TenantBusinessError):
        b.change_purchase_order_server(
            actor, order_id=order["id"], server_id=child["id"]
        )
    s.edit_plan(7001, source["id"], plan["id"], {"status": "archived"})
    assert plan["id"] not in [r["id"] for r in s.plans(7001, source["id"])]
    assert b.order(actor, order["id"])["amount"] == 200


def test_domain_selection_updates_public_links_without_api_endpoint(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    d1 = s.save_domain(7001, source["id"], title="CDN", origin="https://cdn.example")
    d2 = s.save_domain(
        7001, source["id"], title="Backup", origin="https://backup.example"
    )
    s.select_domain(7001, source["id"], d2["id"])
    _, target, _ = b._panel_material(source["id"])
    assert (
        target.endpoint == source["endpoint"]
        and target.public_origin == "https://backup.example"
    )
    assert b.panel_adapter.subscription_link(
        target=target, external_ref="test"
    ).startswith("https://backup.example")
    with pytest.raises(TenantBusinessError):
        s.select_domain(7001, child["id"], d1["id"])
    s.delete_domain(7001, source["id"], d2["id"])
    assert b._panel_material(source["id"])[1].public_origin == "https://cdn.example"
    s.delete_domain(7001, source["id"], d1["id"])
    assert b._panel_material(source["id"])[1].public_origin == ""


def test_dynamic_price_boundaries_and_immutable_order(conn, factories, cipher):
    b, p, s, source, child, actor, _ = setup(conn, factories, cipher)
    s.set_sales(
        7001,
        source["id"],
        {"mode": "mixed", "price_gb": 100, "price_day": 10, "discount_percent": 20},
    )
    assert s.quote(source["id"], 10, 30) == (1040, "IRR")
    with pytest.raises(ValueError):
        s.quote(source["id"], 1, 30)
    plan = s.dynamic_plan(actor, source["id"], 10, 30)
    assert plan["is_dynamic"] == 1 and plan["id"] not in [
        p["id"] for p in b.list_plans()
    ]
    order = b.create_order(actor, plan["id"], server_id=source["id"])
    s.set_sales(7001, source["id"], {"price_gb": 999})
    assert b.order(actor, order["id"])["amount"] == 1040
    assert "پلن پویا" in b.order(actor, order["id"])["plan_name"]
    assert dynamic_plans.rows(b)


def test_double_reset_confirmation_does_not_reset_twice(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    p.seed(source["endpoint"], "native")
    native = next(
        x
        for x in asyncio.run(s.refresh_users(7001, source["id"]))
        if x["external_ref"] == "native"
    )
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f'srv:preset:{source["id"]}:{native["id"]}:usage')
        await click(
            update, context, f'srv:presetok:{source["id"]}:{native["id"]}:usage'
        )
        p.users[source["endpoint"], "native"]["usage_bytes"] = 123
        await click(
            update, context, f'srv:presetok:{source["id"]}:{native["id"]}:usage'
        )
        assert p.users[source["endpoint"], "native"]["usage_bytes"] == 123

    asyncio.run(run())


def test_hiddify_inventory_and_edits_preserve_uuid_usage_state():
    old = "e1f64a54-8b66-488b-9de0-d69b68b0c7fa"
    raw = dict(
        uuid=old,
        name="Panel name",
        current_usage_GB=3,
        usage_limit_GB=10,
        package_days=30,
        start_date="2026-10-01",
        is_active=False,
        comment="note",
    )
    bodies = []

    def handler(request):
        path = request.url.path
        if "/panel/info/" in path:
            return httpx.Response(200, json={"version": "13"}, request=request)
        if request.method == "PATCH":
            body = json.loads(request.content)
            bodies.append(body)
            raw.update(body)
            return httpx.Response(200, json=raw, request=request)
        return httpx.Response(
            200, json=[raw] if path.endswith("/user/") else raw, request=request
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        "hiddify", "https://panel.example", admin_path="admin", user_path="user"
    )
    users = adapter.list_users(target=target, secret="key")
    assert users[0]["name"] == "Panel name" and users[0]["usage_bytes"] == 3 * 1024**3
    adapter.update_user(
        target=target,
        secret="key",
        external_ref=old,
        changes={"name": "Renamed", "traffic_bytes": 20 * 1024**3},
    )
    assert bodies == [{"name": "Renamed", "usage_limit_GB": 20.0}]
    assert (
        raw["uuid"] == old
        and raw["current_usage_GB"] == 3
        and raw["is_active"] is False
    )


def test_fresh_and_upgrade_migrations_preserve_subscription_data(tmp_path):
    import shutil
    from pathlib import Path

    old = tmp_path / "old"
    old.mkdir()
    for f in Path("Migrations").glob("*.sql"):
        if f.name < "0029":
            shutil.copy(f, old / f.name)
    db = tmp_path / "upgrade.db"
    migrate(db, migrations_dir=old)
    conn = connect(db)
    versions = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    conn.close()
    applied = migrate(db)
    assert applied == [
        "0029_server_management",
        "0030_broadcast_channel_phase13",
        "0031_backup_agency_infra",
        "0032_server_status_daily",
        "0033_legacy_sellbot_restore",
    ]
    conn = connect(db)
    assert (
        conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        == versions + len(applied)
    )
    assert "server_id" in [
        r["name"] for r in conn.execute("PRAGMA table_info(tenant_sale_plans)")
    ]
    conn.close()


def test_full_sync_does_not_change_existing_name_usage_or_state(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.seed(
        child["endpoint"], "status-user-1", name="Child alias", usage=123, active=False
    )
    asyncio.run(s.sync_nodes(7001, source["id"], "full"))
    row = p.users[child["endpoint"], "status-user-1"]
    assert (
        row["name"] == "Child alias"
        and row["usage_bytes"] == 123
        and row["active"] is False
    )
    asyncio.run(s.sync_nodes(7001, source["id"], "status"))
    assert row["active"] is True


def test_native_child_operations_resolve_whole_service_and_clean_mappings(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.seed(source["endpoint"], "native-ref", name="Native")
    asyncio.run(s.sync_nodes(7001, source["id"], "missing"))
    uid = next(
        x["id"] for x in s.users(7001, child["id"]) if x["external_ref"] == "native-ref"
    )
    asyncio.run(s.edit_user(7001, child["id"], uid, {"comment": "All targets"}))
    assert (
        p.users[source["endpoint"], "native-ref"]["comment"]
        == p.users[child["endpoint"], "native-ref"]["comment"]
        == "All targets"
    )
    asyncio.run(s.delete_user(7001, child["id"], uid, all_targets=True))
    assert (source["endpoint"], "native-ref") not in p.users and (
        child["endpoint"],
        "native-ref",
    ) not in p.users
    assert (
        conn.execute("SELECT COUNT(*) FROM tenant_panel_user_nodes").fetchone()[0] == 0
    )


def test_import_registers_actual_existing_native_node_relationships(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    for server in (source, child):
        p.seed(server["endpoint"], "legacy-native", name="Old admin")
    report = asyncio.run(s.sync_nodes(7001, source["id"], "migrate"))
    assert report["registered"] == 2 and report["existing"] == 1 and not p.mutations
    assert len(s.users(7001, child["id"])) == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM tenant_panel_user_nodes").fetchone()[0] == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM tenant_customers").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tenant_orders").fetchone()[0] == 1


def test_extra_users_are_all_reachable_with_pagination(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    for i in range(55):
        p.seed(child["endpoint"], f"extra-{i}", name=f"Extra {i}")
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f'srv:syncrun:{source["id"]}:extra')
        seen = set()
        while True:
            buttons = cb(message.sent[-1][1]["reply_markup"])
            seen.update(x for x in buttons if x.startswith("srv:puser:"))
            next_button = next(
                (
                    b.callback_data
                    for row in message.sent[-1][1]["reply_markup"].inline_keyboard
                    for b in row
                    if b.text == "بعدی ▶️"
                ),
                None,
            )
            if not next_button:
                break
            await click(update, context, next_button)
        assert len(seen) == 55 and not p.mutations

    asyncio.run(run())


def test_dynamic_checkout_and_owned_renewal_use_normal_orders(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, child, actor, subid = setup(conn, factories, cipher)
    s.set_sales(7001, source["id"], {"mode": "mixed", "price_gb": 100})
    b.set_renewal_policy_admin(7001, policy="fair")
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=?",
        (iso_utc(utcnow() - timedelta(days=1)), subid),
    )
    update, context, message = ui(monkeypatch, b)

    async def run():
        for action in ("open", "gb_plus", "days_plus", "confirm"):
            update.callback_query = Query(
                f'shop:dynamic:{source["id"]}:{action}', message
            )
            await dynamic_plans.handle_callback(
                update,
                context,
                business=b,
                actor=actor,
                settings=b.runtime_userbot_settings(),
            )
        order = dict(
            conn.execute(
                "SELECT * FROM tenant_orders ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
        assert (
            order["amount"] == 2000
            and order["order_kind"] == "purchase"
            and order["selected_server_id"] == source["id"]
        )
        for action in ("open", "confirm"):
            update.callback_query = Query(
                f'shop:dynamic:{source["id"]}:{action}:{subid}', message
            )
            await dynamic_plans.handle_callback(
                update,
                context,
                business=b,
                actor=actor,
                settings=b.runtime_userbot_settings(),
            )
        order = dict(
            conn.execute(
                "SELECT * FROM tenant_orders ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
        assert order["order_kind"] == "renewal" and order["amount"] == 1000
        with pytest.raises(TenantBusinessError):
            update.callback_query = Query(
                f'shop:dynamic:{child["id"]}:open:{subid}', message
            )
            await dynamic_plans.handle_callback(
                update,
                context,
                business=b,
                actor=actor,
                settings=b.runtime_userbot_settings(),
            )

    asyncio.run(run())


def test_volume_tier_discount_and_expired_timers_use_best_rate(conn, factories, cipher):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    s.set_sales(
        7001,
        source["id"],
        {
            "mode": "dynamic",
            "price_gb": 100,
            "discount_simple_enabled": True,
            "discount_tiered_enabled": True,
            "discount_tiers": [{"gb": 100, "percent": 40}, {"gb": 50, "percent": 15}],
        },
    )
    assert s.quote(source["id"], 100, 30) == (6000, "IRR")
    assert s.quote(source["id"], 50, 30) == (4250, "IRR")
    s.set_sales(
        7001,
        source["id"],
        {"discount_tiered_until": iso_utc(utcnow() - timedelta(minutes=1))},
    )
    assert s.quote(source["id"], 100, 30) == (9000, "IRR")
    s.set_sales(
        7001,
        source["id"],
        {"discount_simple_until": iso_utc(utcnow() - timedelta(minutes=1))},
    )
    assert s.quote(source["id"], 100, 30) == (10000, "IRR")
    with pytest.raises(ValueError):
        s.set_sales(
            7001, source["id"], {"discount_tiers": [{"gb": 100, "percent": 101}]}
        )
    with pytest.raises(ValueError):
        s.set_sales(
            7001,
            source["id"],
            {
                "discount_tiers": [
                    {"gb": 100, "percent": 10},
                    {"gb": 100, "percent": 20},
                ]
            },
        )


def test_plan_domain_discount_wizards_and_root_buttons(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)
    sid = source["id"]

    async def run():
        for action in (
            "users",
            "userops",
            "plans",
            "domains",
            "nodes",
            "sync",
            "frozen",
            "settings",
            "discounts",
            "categories",
        ):
            await click(update, context, f"srv:{action}:{sid}")
            assert message.sent and "خطا" not in message.sent[-1][0]
        await click(update, context, f"srv:planadd:{sid}")
        for text in ("Server plan", "50", "30", "10000", "IRT"):
            await send(update, context, text)
        plan = next(x for x in s.plans(7001, sid) if x["name"] == "Server plan")
        await click(update, context, f'srv:planfield:{sid}:{plan["id"]}:price')
        await send(update, context, "15000")
        assert b.plan(plan["id"])["price"] == 15000
        await click(update, context, f"srv:domainadd:{sid}")
        for text in ("Public", "https://public.example"):
            await send(update, context, text)
        domain = s.domains(7001, sid)[0]
        await click(update, context, f'srv:domainedit:{sid}:{domain["id"]}')
        for text in ("Changed", "https://newpublic.example"):
            await send(update, context, text)
        assert s.domains(7001, sid)[0]["origin"] == "https://newpublic.example"
        await click(update, context, f"srv:discountedit:{sid}:simple")
        await send(update, context, "100 10 30")
        await click(update, context, f"srv:discountedit:{sid}:tiered")
        await send(update, context, "100:15 200:25")
        await click(update, context, f"srv:discounttoggle:{sid}:tiered:on")
        await click(update, context, f"srv:discountedit:{sid}:tiered_timer")
        await send(update, context, "60")
        sales = s.sales(sid)
        assert (
            sales["discount_step_gb"] == 100
            and sales["discount_tiers"][1]["percent"] == 25
            and sales["discount_tiered_until"]
        )

    asyncio.run(run())


def test_server_deletion_rejects_native_accounts_and_cleans_dependents(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    p.seed(child["endpoint"], "native-child")
    asyncio.run(s.refresh_users(7001, child["id"]))
    with pytest.raises(TenantBusinessError):
        b.delete_server(7001, server_id=child["id"])
    user = s.users(7001, child["id"])[0]
    asyncio.run(s.delete_user(7001, child["id"], user["id"]))
    s.save_domain(7001, child["id"], title="Public", origin="https://public.example")
    s.set_sales(7001, child["id"], {"mode": "fixed"})
    b.add_plan(
        7001,
        name="Deleted server plan",
        traffic_gb=10,
        duration_days=30,
        price=100,
        server_id=child["id"],
    )
    b.delete_server(7001, server_id=child["id"])
    assert not conn.execute(
        "SELECT 1 FROM tenant_server_domains WHERE server_id=?", (child["id"],)
    ).fetchone()
    assert not conn.execute(
        "SELECT 1 FROM tenant_server_sales_settings WHERE server_id=?", (child["id"],)
    ).fetchone()


def test_dynamic_only_hides_shared_fixed_plans_and_quotes_are_immutable(
    conn, factories, cipher
):
    b, p, s, source, child, actor, _ = setup(conn, factories, cipher)
    shared = b.add_plan(
        7001, name="Shared fixed", traffic_gb=10, duration_days=30, price=100
    )
    order = b.create_order(actor, shared["id"], server_id=child["id"])
    s.set_sales(7001, source["id"], {"mode": "dynamic", "price_gb": 100})
    assert not b.list_plans(server_id=source["id"])
    with pytest.raises(TenantBusinessError):
        b.change_purchase_order_server(
            actor, order_id=order["id"], server_id=source["id"]
        )
    quote = s.dynamic_plan(actor, source["id"], 10, 30)
    with pytest.raises(TenantBusinessError):
        s.edit_plan(7001, source["id"], quote["id"], {"price": 1})
    assert quote["id"] not in [p["id"] for p in b.list_plans(public=False)]


def test_reset_duration_preserves_admin_package_after_inventory_refresh(
    conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    user = asyncio.run(
        s.create_users(
            7001,
            source["id"],
            name="Forty days",
            gb=10,
            days=40,
            operation_key="package",
        )
    )["users"][0]
    asyncio.run(s.refresh_users(7001, source["id"]))
    assert s.reset_duration(7001, source["id"], user["id"]) == 40
    asyncio.run(
        s.edit_user(
            7001,
            source["id"],
            user["id"],
            {"expires_at": iso_utc(utcnow() + timedelta(days=20)), "reset_days": True},
        )
    )
    assert s.reset_duration(7001, source["id"], user["id"]) == 20


def test_delete_server_button_requires_confirmation(
    monkeypatch, conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)

    async def run():
        await click(update, context, f'srv:deleteok:{child["id"]}')
        assert b.server(child["id"])
        await click(update, context, f'srv:delete:{child["id"]}')
        await click(update, context, f'srv:deleteok:{child["id"]}')
        with pytest.raises(TenantBusinessError):
            b.server(child["id"])

    asyncio.run(run())


def test_removing_node_cleans_failed_creation_without_deleting_unrelated(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    node = s.attach_node(7001, source["id"], child["id"])
    p.offline.add(child["endpoint"])
    asyncio.run(s.sync_nodes(7001, source["id"], "missing"))
    assert s.frozen(7001, source["id"])
    p.offline.clear()
    p.seed(child["endpoint"], "unrelated")
    asyncio.run(s.remove_node(7001, source["id"], node["id"]))
    assert not s.frozen(7001, source["id"])
    assert (child["endpoint"], "unrelated") in p.users
    assert not any(x[0] == "delete" for x in p.mutations)


def test_frozen_list_does_not_offer_actions_for_deleted_panel_accounts(
    conn, factories, cipher
):
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    s.attach_node(7001, source["id"], child["id"])
    p.offline.add(child["endpoint"])
    asyncio.run(s.sync_nodes(7001, source["id"], "missing"))
    assert s.frozen(7001, source["id"])
    p.users.pop((source["endpoint"], "status-user-1"))
    asyncio.run(s.refresh_users(7001, source["id"]))
    assert not s.frozen(7001, source["id"])


def test_admin_create_recovers_when_remote_user_exists_after_provider_error(
    conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)

    def provision_then_error(*, target, secret, request):
        del secret
        ref = request.external_ref
        p.seed(
            target.endpoint,
            ref,
            name=request.name,
            usage=0,
            traffic=request.traffic_bytes,
        )
        p.users[target.endpoint, ref]["expires_at"] = request.expires_at
        raise PanelError("post-create stabilization failed")

    p.provision = provision_then_error

    async def run():
        report = await s.create_users(
            7001,
            source["id"],
            name="Recovered create",
            gb=12,
            days=15,
            count=1,
            operation_key="recover-after-create",
        )
        assert report["errors"] == 0
        assert report["error_details"] == []
        assert len(report["users"]) == 1
        row = report["users"][0]
        assert row["name"] == "Recovered create"
        assert row["state"] == "active"
        assert row["external_ref"]
        assert p.users[source["endpoint"], row["external_ref"]]["name"] == "Recovered create"

    asyncio.run(run())


def test_admin_create_does_not_require_redundant_name_patch(
    conn, factories, cipher
):
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)

    async def run():
        report = await s.create_users(
            7001,
            source["id"],
            name="No extra patch",
            gb=10,
            days=30,
            count=1,
            operation_key="no-redundant-patch",
        )
        assert report["errors"] == 0
        assert len(report["users"]) == 1
        ref = report["users"][0]["external_ref"]
        assert ("create", source["endpoint"], ref) in p.mutations
        assert not any(
            mutation[0] == "edit" and mutation[2] == ref
            for mutation in p.mutations
        )

    asyncio.run(run())
