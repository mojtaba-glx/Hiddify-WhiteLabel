"""End-to-end tenant purchase -> approval -> Hiddify provisioning contract."""

from __future__ import annotations

import pytest

from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import PanelError, PanelTarget, ProvisionRequest, ProvisionResult


class E2EPanel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[ProvisionRequest] = []
        self.targets: list[PanelTarget] = []

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        self.targets.append(target)
        self.requests.append(request)
        if self.fail:
            raise PanelError("simulated provider failure")
        external_ref = f"remote-{request.subscription_id}"
        return ProvisionResult(
            external_ref=external_ref,
            subscription_url=self.subscription_link(target=target, external_ref=external_ref),
        )

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        return f"{target.endpoint}/user/{external_ref}/all.txt"


def _purchase(service: TenantBusinessService, *, owner: int, customer: int) -> tuple[dict, dict]:
    plan = service.add_plan(
        owner,
        name="Monthly",
        traffic_gb=30,
        duration_days=30,
        price=150000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="6037-0000-0000-0000",
    )
    service.register_customer(customer, display_name="Buyer", username="buyer")
    order = service.create_order(customer, int(plan["id"]))
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="bank-ref-1",
    )
    return order, receipt


def test_purchase_approval_provisions_and_delivers_subscription_link(conn, factories, cipher) -> None:
    owner, customer = 7001, 31
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = E2EPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server = service.add_server(
        owner,
        label="TR",
        panel_kind="hiddify",
        endpoint="https://panel.example",
        admin_path="admin-secret",
        user_path="user-secret",
    )
    service.set_panel_credential(owner, server_id=int(server["id"]), secret="api-key")
    order, receipt = _purchase(service, owner=owner, customer=customer)

    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)
    assert reviewed["status"] == "paid"
    assert service.order(customer, int(order["id"]))["status"] == "paid"

    active = service.provision_pending_subscription(
        owner, subscription_id=int(reviewed["subscription_id"])
    )
    assert active["status"] == "active"
    assert service.order(customer, int(order["id"]))["status"] == "fulfilled"
    assert len(panel.requests) == 1
    assert panel.requests[0].tenant_id == int(tenant["id"])
    assert panel.requests[0].idempotency_key == (
        f"tenant:{tenant['id']}:subscription:{reviewed['subscription_id']}"
    )

    subscriptions = service.list_subscriptions(customer)
    assert len(subscriptions) == 1
    assert subscriptions[0]["status"] == "active"
    assert subscriptions[0]["server_id"] == server["id"]
    link = service.subscription_link(customer, subscription_id=int(subscriptions[0]["id"]))
    assert link.endswith(f"/{active['external_ref']}/all.txt")


def test_panel_failure_keeps_payment_and_pending_subscription_retryable(conn, factories, cipher) -> None:
    owner, customer = 7001, 31
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = E2EPanel(fail=True)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server = service.add_server(
        owner, label="TR", panel_kind="hiddify", endpoint="https://panel.example"
    )
    service.set_panel_credential(owner, server_id=int(server["id"]), secret="api-key")
    order, receipt = _purchase(service, owner=owner, customer=customer)
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)

    with pytest.raises(TenantBusinessError, match="panel provisioning failed"):
        service.provision_pending_subscription(
            owner, subscription_id=int(reviewed["subscription_id"])
        )

    assert service.order(customer, int(order["id"]))["status"] == "paid"
    subscription = service.list_subscriptions(customer)[0]
    assert subscription["status"] == "pending_provisioning"
    assert subscription["server_id"] is None
    assert subscription["external_ref"] is None

    panel.fail = False
    active = service.provision_pending_subscription(
        owner, subscription_id=int(reviewed["subscription_id"])
    )
    assert active["status"] == "active"
    assert service.order(customer, int(order["id"]))["status"] == "fulfilled"
    assert len(panel.requests) == 2
    assert panel.requests[0].idempotency_key == panel.requests[1].idempotency_key


def test_multiple_hiddify_servers_require_tenant_default(conn, factories, cipher) -> None:
    owner, customer = 7001, 31
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = E2EPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    first = service.add_server(
        owner, label="TR", panel_kind="hiddify", endpoint="https://tr.example"
    )
    second = service.add_server(
        owner, label="DE", panel_kind="hiddify", endpoint="https://de.example"
    )
    service.set_panel_credential(owner, server_id=int(first["id"]), secret="tr-key")
    service.set_panel_credential(owner, server_id=int(second["id"]), secret="de-key")
    _, receipt = _purchase(service, owner=owner, customer=customer)
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)

    with pytest.raises(TenantBusinessError, match="default provisioning server is required"):
        service.provision_pending_subscription(
            owner, subscription_id=int(reviewed["subscription_id"])
        )

    chosen = service.set_default_server(owner, server_id=int(second["id"]))
    assert chosen["is_default"] == 1
    active = service.provision_pending_subscription(
        owner, subscription_id=int(reviewed["subscription_id"])
    )
    assert active["status"] == "active"
    assert panel.targets[-1].endpoint == "https://de.example"


def test_default_server_and_pending_subscription_never_cross_tenants(conn, factories, cipher) -> None:
    tenant_a = factories.tenant(owner_telegram_id=7001)
    tenant_b = factories.tenant(owner_telegram_id=7002)
    panel = E2EPanel()
    a = TenantBusinessService(
        conn,
        tenant_id=int(tenant_a["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    b = TenantBusinessService(
        conn,
        tenant_id=int(tenant_b["id"]),
        owner_telegram_id=7002,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server_a = a.add_server(
        7001, label="A", panel_kind="hiddify", endpoint="https://a.example"
    )
    a.set_panel_credential(7001, server_id=int(server_a["id"]), secret="a-key")

    with pytest.raises(TenantBusinessError, match="server not found"):
        b.set_default_server(7002, server_id=int(server_a["id"]))

    server_b = b.add_server(
        7002, label="B", panel_kind="hiddify", endpoint="https://b.example"
    )
    b.set_panel_credential(7002, server_id=int(server_b["id"]), secret="b-key")

    _, receipt = _purchase(a, owner=7001, customer=31)
    reviewed = a.review_receipt(7001, int(receipt["id"]), approve=True)
    with pytest.raises(TenantBusinessError, match="subscription is not awaiting provisioning"):
        b.provision_pending_subscription(
            7002, subscription_id=int(reviewed["subscription_id"])
        )
    assert panel.requests == []
