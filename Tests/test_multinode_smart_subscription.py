"""Multi-node provisioning and managed smart subscription tests."""

from __future__ import annotations

import base64
import os
from datetime import timedelta

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import (
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)
from TenantRuntime.smart_subscription import SmartSubscriptionService


class MultiPanel:
    def __init__(self) -> None:
        self.users: dict[tuple[str, str], dict] = {}
        self.provision_calls: list[tuple[str, ProvisionRequest]] = []
        self.renew_calls: list[tuple[str, RenewRequest]] = []
        self.enabled_calls: list[tuple[str, str, bool]] = []
        self.deleted: list[tuple[str, str]] = []
        self.contents: dict[str, str] = {}
        self.usage_by_endpoint: dict[str, int] = {}

    def _key(self, target: PanelTarget, external_ref: str) -> tuple[str, str]:
        return (target.endpoint, external_ref)

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        ref = f"ref-{request.server_id}-{request.subscription_id}"
        self.provision_calls.append((target.endpoint, request))
        self.users[self._key(target, ref)] = {
            "usage": 0,
            "active": True,
            "traffic": request.traffic_bytes,
            "expires": request.expires_at,
            "last_online": None,
        }
        return ProvisionResult(
            external_ref=ref,
            subscription_url=f"{target.endpoint}/native/{ref}",
        )

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        assert secret
        row = self.users[self._key(target, external_ref)]
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=int(row["usage"]),
            active=bool(row["active"]),
            traffic_bytes=int(row["traffic"]),
            expires_at=str(row["expires"]),
            last_online=row["last_online"],
            subscription_url=f"{target.endpoint}/native/{external_ref}",
        )

    def renew(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        request: RenewRequest,
    ) -> PanelUserResult:
        assert secret
        self.renew_calls.append((target.endpoint, request))
        row = self.users[self._key(target, external_ref)]
        row["traffic"] = request.traffic_bytes
        row["expires"] = request.expires_at
        row["usage"] = 0
        row["active"] = True
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def set_enabled(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        enabled: bool,
    ) -> PanelUserResult:
        assert secret
        self.enabled_calls.append((target.endpoint, external_ref, bool(enabled)))
        self.users[self._key(target, external_ref)]["active"] = bool(enabled)
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        assert secret
        self.deleted.append((target.endpoint, external_ref))
        self.users.pop(self._key(target, external_ref), None)

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        assert secret
        row = self.users[self._key(target, external_ref)]
        usage = int(self.usage_by_endpoint.get(target.endpoint, row["usage"]))
        return UsageResult(
            usage_bytes=usage,
            active=bool(row["active"]),
            last_online=row["last_online"],
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        return f"{target.endpoint}/native/{external_ref}"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        assert secret
        return self.contents[target.endpoint]


def _setup(conn, factories, cipher):
    owner = 7001
    customer = 31
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = MultiPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    primary = service.add_server(
        owner,
        label="TR",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        admin_path="admin",
        user_path="user",
    )
    xui = service.add_server(
        owner,
        label="DE",
        panel_kind="xui",
        endpoint="https://de.example",
        xui_flavor="sanaei",
    )
    xnet = service.add_server(
        owner,
        label="FR",
        panel_kind="xnet",
        endpoint="https://fr.example",
    )
    service.set_panel_credential(
        owner, server_id=int(primary["id"]), secret="primary-secret"
    )
    service.set_xui_credential(
        owner, server_id=int(xui["id"]), api_token="xui-secret"
    )
    service.set_xnet_credential(
        owner, server_id=int(xnet["id"]), api_token="xnet-secret"
    )
    service.set_default_server(owner, server_id=int(primary["id"]))
    service.add_node(
        owner,
        label="Germany",
        server_id=int(xui["id"]),
        location="DE",
    )
    service.add_node(
        owner,
        label="France",
        server_id=int(xnet["id"]),
        location="FR",
    )
    plan = service.add_plan(
        owner,
        name="Multi 20",
        traffic_gb=20,
        duration_days=30,
        price=1000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="1111",
    )
    service.register_customer(customer, display_name="Buyer", username=None)
    order = service.create_order(customer, int(plan["id"]))
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="multi-ref",
    )
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)
    subscription = service.list_subscriptions(customer)[0]
    return {
        "owner": owner,
        "customer": customer,
        "tenant": tenant,
        "panel": panel,
        "service": service,
        "primary": primary,
        "xui": xui,
        "xnet": xnet,
        "plan": plan,
        "order_id": int(reviewed["order_id"]),
        "subscription": subscription,
    }


def test_purchase_fans_out_and_keeps_native_and_smart_links_separate(
    conn, db_path, factories, cipher, monkeypatch
) -> None:
    state = _setup(conn, factories, cipher)
    monkeypatch.setenv("SMART_SUB_PUBLIC_BASE_URL", "https://smart.example")
    result = state["service"].fulfill_paid_order(
        state["owner"], order_id=state["order_id"]
    )
    assert result["status"] == "active"
    assert result["subscription_url"].startswith("https://tr.example/native/")
    assert result["smart_subscription_url"].startswith(
        "https://smart.example/sub/"
    )
    assert result["smart_subscription_url"].endswith("/all.txt")
    assert result["subscription_url"] != result["smart_subscription_url"]
    assert len(state["panel"].provision_calls) == 3

    nodes = state["service"].subscription_nodes_admin(
        state["owner"], subscription_id=int(state["subscription"]["id"])
    )
    assert len(nodes) == 3
    assert sum(int(row["is_primary"]) for row in nodes) == 1
    assert {row["provider_kind"] for row in nodes} == {
        "hiddify",
        "xui",
        "xnet",
    }
    assert all(row["status"] == "active" for row in nodes)

    links = state["service"].list_smart_links(state["owner"])
    managed = [
        row
        for row in links
        if row["target"] == f"subscription:{state['subscription']['id']}"
    ]
    assert len(managed) == 1
    assert managed[0]["public_url"] == result["smart_subscription_url"]
    assert managed[0]["public_url_b64"].endswith("/all.b64")


def test_usage_is_aggregated_across_nodes_and_global_quota_disables_every_node(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    state["service"].fulfill_paid_order(
        state["owner"], order_id=state["order_id"]
    )
    state["panel"].usage_by_endpoint = {
        "https://tr.example": 8 * 1024**3,
        "https://de.example": 7 * 1024**3,
        "https://fr.example": 6 * 1024**3,
    }
    result = state["service"].sync_subscription_usage(
        state["owner"],
        subscription_id=int(state["subscription"]["id"]),
    )
    assert result["usage_bytes"] == 21 * 1024**3
    assert result["status"] == "expired"
    assert len(state["panel"].enabled_calls) == 3
    assert all(enabled is False for _, _, enabled in state["panel"].enabled_calls)

    rows = state["service"].subscription_nodes_admin(
        state["owner"], subscription_id=int(state["subscription"]["id"])
    )
    assert all(row["status"] == "expired" for row in rows)


def test_paid_renewal_renews_primary_and_all_child_nodes(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    state["service"].fulfill_paid_order(
        state["owner"], order_id=state["order_id"]
    )
    result = state["service"].renew_subscription(
        state["owner"],
        subscription_id=int(state["subscription"]["id"]),
        traffic_gb=50,
        duration_days=45,
        idempotency_key="tenant-renewal-77",
    )
    assert result["status"] == "active"
    assert result["node_errors"] == 0
    assert len(state["panel"].renew_calls) == 3
    child_keys = [
        req.idempotency_key
        for endpoint, req in state["panel"].renew_calls
        if endpoint != "https://tr.example"
    ]
    assert all(":server:" in key for key in child_keys)


def test_smart_subscription_aggregates_plain_and_base64_and_deduplicates(
    conn, db_path, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    activated = state["service"].fulfill_paid_order(
        state["owner"], order_id=state["order_id"]
    )
    duplicate = (
        "vless://11111111-2222-4333-8444-555555555555@edge.example:443"
        "?security=tls&type=ws#same"
    )
    state["panel"].contents["https://tr.example"] = "\n".join(
        [
            "vless://aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee@tr.example:443?type=tcp#TR",
            duplicate,
        ]
    )
    child_text = "\n".join(
        [
            duplicate,
            "trojan://pass@de.example:443?type=grpc#DE",
        ]
    )
    state["panel"].contents["https://de.example"] = base64.b64encode(
        child_text.encode()
    ).decode()
    state["panel"].contents["https://fr.example"] = (
        "anytls://secret@fr.example:443?sni=fr.example#FR"
    )

    link = conn.execute(
        "SELECT code FROM tenant_smart_links "
        "WHERE tenant_id=? AND target=?",
        (
            int(state["tenant"]["id"]),
            f"subscription:{state['subscription']['id']}",
        ),
    ).fetchone()
    assert link is not None
    aggregator = SmartSubscriptionService(
        db_path=db_path,
        cipher=cipher,
        panel_adapter_factory=lambda: state["panel"],
    )
    response = aggregator.build(str(link["code"]), base64_output=False)
    lines = response.body.splitlines()
    assert len(lines) == 4
    assert sum("edge.example" in line for line in lines) == 1
    assert any(line.startswith("trojan://") for line in lines)
    assert any(line.startswith("anytls://") for line in lines)
    assert response.headers["subscription-userinfo"].startswith(
        "upload=0; download="
    )

    encoded = aggregator.build(str(link["code"]), base64_output=True)
    decoded = base64.b64decode(encoded.body).decode()
    assert decoded == response.body
    assert activated["smart_code"] == str(link["code"])


def test_foreign_tenant_cannot_attach_its_subscription_to_another_tenants_node(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    other = factories.tenant(owner_telegram_id=8001)
    foreign = TenantBusinessService(
        conn,
        tenant_id=int(other["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
        panel_adapter=state["panel"],
    )
    try:
        foreign.add_node(
            8001,
            label="bad",
            server_id=int(state["xui"]["id"]),
            location="DE",
        )
    except Exception:
        pass
    else:
        raise AssertionError("cross-tenant server mapping must be rejected")


def test_admin_panel_user_smart_link_aggregates_primary_and_node_configs(
    conn, db_path, factories, cipher, monkeypatch
) -> None:
    state = _setup(conn, factories, cipher)
    monkeypatch.setenv("SMART_SUB_PUBLIC_BASE_URL", "https://smart.example")
    expires = iso_utc(utcnow() + timedelta(days=30))
    cursor = conn.execute(
        "INSERT INTO tenant_panel_users "
        "(tenant_id,server_id,external_ref,name,comment,usage_bytes,traffic_bytes,"
        "expires_at,active,state,extra_json,last_synced_at) "
        "VALUES (?,?,?,?,?,0,?,?,1,'active','{}',?)",
        (
            int(state["tenant"]["id"]),
            int(state["primary"]["id"]),
            "admin-primary-ref",
            "Admin native",
            "",
            20 * 1024**3,
            expires,
            iso_utc(utcnow()),
        ),
    )
    panel_user_id = int(cursor.lastrowid)
    for server, ref in (
        (state["xui"], "admin-xui-ref"),
        (state["xnet"], "admin-xnet-ref"),
    ):
        conn.execute(
            "INSERT INTO tenant_panel_user_nodes "
            "(tenant_id,source_user_id,server_id,external_ref,updated_at) "
            "VALUES (?,?,?,?,?)",
            (
                int(state["tenant"]["id"]),
                panel_user_id,
                int(server["id"]),
                ref,
                iso_utc(utcnow()),
            ),
        )
    conn.commit()

    state["panel"].contents["https://tr.example"] = (
        "vless://aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee@tr.example:443?type=tcp#TR"
    )
    state["panel"].contents["https://de.example"] = (
        "trojan://pass@de.example:443?type=grpc#DE"
    )
    state["panel"].contents["https://fr.example"] = (
        "anytls://secret@fr.example:443?sni=fr.example#FR"
    )

    plain_url = state["service"].admin_panel_smart_subscription_link(
        state["owner"],
        panel_user_id=panel_user_id,
        base64_output=False,
    )
    b64_url = state["service"].admin_panel_smart_subscription_link(
        state["owner"],
        panel_user_id=panel_user_id,
        base64_output=True,
    )
    assert plain_url.startswith("https://smart.example/sub/")
    assert plain_url.endswith("/all.txt")
    assert b64_url.endswith("/all.b64")

    link = conn.execute(
        "SELECT code,target FROM tenant_smart_links "
        "WHERE tenant_id=? AND target=?",
        (
            int(state["tenant"]["id"]),
            f"paneluser:{panel_user_id}",
        ),
    ).fetchone()
    assert link is not None
    aggregator = SmartSubscriptionService(
        db_path=db_path,
        cipher=cipher,
        panel_adapter_factory=lambda: state["panel"],
    )
    result = aggregator.build(str(link["code"]), base64_output=False)
    assert "tr.example" in result.body
    assert "de.example" in result.body
    assert "fr.example" in result.body
    encoded = aggregator.build(str(link["code"]), base64_output=True)
    decoded = base64.b64decode(encoded.body).decode()
    assert decoded == result.body


def test_free_trial_uses_selected_server_name_and_does_not_require_default(
    conn, factories, cipher
) -> None:
    owner = 7001
    customer = 7101
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = MultiPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    primary = service.add_server(
        owner,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://trial-tr.example",
        admin_path="admin",
        user_path="user",
    )
    child = service.add_server(
        owner,
        label="Germany",
        panel_kind="hiddify",
        endpoint="https://trial-de.example",
        admin_path="admin",
        user_path="user",
    )
    service.set_panel_credential(
        owner, server_id=int(primary["id"]), secret="trial-primary-secret"
    )
    service.set_panel_credential(
        owner, server_id=int(child["id"]), secret="trial-child-secret"
    )
    service.add_node(
        owner,
        label="Germany",
        server_id=int(child["id"]),
        parent_server_id=int(primary["id"]),
        location="DE",
    )
    service.register_customer(
        customer, display_name="Trial Buyer", username="trial_buyer"
    )

    # A real prior purchase must not make the free-trial button fail: SellBot
    # gates the trial by its one-time trial flag, not by purchase history.
    paid_plan = service.add_plan(
        owner,
        name="Paid 5",
        traffic_gb=5,
        duration_days=30,
        price=100000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="1111",
    )
    order = service.create_order(
        customer,
        int(paid_plan["id"]),
        server_id=int(primary["id"]),
    )
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="paid-before-trial",
    )
    approved = service.review_receipt(owner, int(receipt["id"]), approve=True)
    service.fulfill_paid_order(owner, order_id=int(approved["order_id"]))

    # Deliberately keep both servers non-default. The selected trial location
    # must be enough to provision successfully.
    service.update_growth_settings(
        owner,
        trial_enabled=True,
        trial_traffic_gb=1,
        trial_duration_days=1,
    )
    state = service.free_trial_state(customer)
    assert state["enabled"] is True
    assert state["used"] is False

    result = service.claim_free_trial(
        customer,
        server_id=int(primary["id"]),
        service_name="تستی",
    )
    assert result["status"] == "active"
    assert result["selected_server_id"] == int(primary["id"])
    assert result["service_name"] == "تستی"

    trial_order = conn.execute(
        "SELECT selected_server_id,status FROM tenant_orders "
        "WHERE tenant_id=? AND customer_id=? AND order_kind='trial'",
        (int(tenant["id"]), int(service._customer(customer)["id"])),
    ).fetchone()
    assert int(trial_order["selected_server_id"]) == int(primary["id"])
    assert trial_order["status"] == "fulfilled"

    trial_sub = conn.execute(
        "SELECT service_name,status,server_id FROM tenant_subscriptions "
        "WHERE tenant_id=? AND order_id=?",
        (int(tenant["id"]), int(result["order_id"])),
    ).fetchone()
    assert trial_sub["service_name"] == "تستی"
    assert trial_sub["status"] == "active"
    assert int(trial_sub["server_id"]) == int(primary["id"])

    # Primary + attached node get the same requested service name.
    trial_calls = [
        request for _endpoint, request in panel.provision_calls
        if int(request.subscription_id) == int(result["id"])
    ]
    assert len(trial_calls) == 2
    assert {request.name for request in trial_calls} == {"تستی"}

    after = service.free_trial_state(customer)
    assert after["used"] is True
