"""Tenant-runtime business records stay isolated and order totals stay authoritative."""

import pytest

from TenantRuntime.business import TenantBusinessError, TenantBusinessService


def _service(conn, factories, owner=7001):
    tenant = factories.tenant(owner_telegram_id=owner)
    return tenant, TenantBusinessService(conn, tenant_id=int(tenant["id"]), owner_telegram_id=owner)


def test_tenant_business_scopes_servers_plans_and_customers(conn, factories) -> None:
    _, first = _service(conn, factories, 7001)
    _, second = _service(conn, factories, 7002)
    first.add_server(7001, label="DE", panel_kind="manual")
    plan = first.add_plan(7001, name="Monthly", traffic_gb=50, duration_days=30, price=1000)
    first.register_customer(31, display_name="One", username=None)
    second.register_customer(31, display_name="Two", username=None)
    assert len(first.list_servers()) == 1 and second.list_servers() == []
    assert first.list_plans()[0]["id"] == plan["id"] and second.list_plans() == []
    with pytest.raises(TenantBusinessError):
        second.plan(int(plan["id"]))


def test_order_receipt_review_creates_only_tenant_subscription(conn, factories) -> None:
    _, service = _service(conn, factories)
    plan = service.add_plan(7001, name="Starter", traffic_gb=20, duration_days=30, price=500, currency="USD")
    method = service.add_payment_method(7001, kind="crypto", title="USDT", currency="USD", destination="wallet", network="TRC20")
    service.register_customer(31, display_name="Buyer", username=None)
    order = service.create_order(31, int(plan["id"]))
    assert order["amount"] == 500 and order["currency"] == "USD"
    receipt = service.submit_receipt(31, order_id=int(order["id"]), method_id=int(method["id"]), reference="tx-1")
    result = service.review_receipt(7001, int(receipt["id"]), approve=True)
    assert result["status"] == "paid"
    assert result["subscription_id"] is not None
    subscriptions = service.list_subscriptions(31)
    assert len(subscriptions) == 1
    assert subscriptions[0]["status"] == "pending_provisioning"
    with pytest.raises(TenantBusinessError):
        service.review_receipt(7001, int(receipt["id"]), approve=True)


def test_non_owner_cannot_write_tenant_configuration(conn, factories) -> None:
    _, service = _service(conn, factories)
    with pytest.raises(PermissionError):
        service.add_plan(99, name="No", traffic_gb=1, duration_days=1, price=1)
    with pytest.raises(PermissionError):
        service.add_payment_method(99, kind="card", title="No", currency="IRR", destination="x")


def test_ticket_and_smart_link_are_tenant_scoped(conn, factories) -> None:
    _, service = _service(conn, factories)
    service.register_customer(31, display_name="Buyer", username=None)
    ticket = service.create_ticket(31, subject="help", body="need support")
    link = service.create_smart_link(7001, label="Campaign", target="buy")
    assert ticket["status"] == "open"
    assert link["code"] and len(service.list_tickets_admin(7001)) == 1
    assert len(service.list_smart_links(7001)) == 1



def test_server_admin_crud_priority_and_parent_nodes(conn, factories) -> None:
    _, service = _service(conn, factories)
    main = service.add_server(
        7001,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example.com",
        users_limit=500,
        priority=10,
    )
    node_server = service.add_server(
        7001,
        label="France",
        panel_kind="xnet",
        endpoint="https://fr.example.com",
        users_limit=300,
        priority=5,
    )
    updated = service.update_server(
        7001,
        server_id=int(main["id"]),
        label="Turkey Main",
        priority=20,
    )
    assert updated["label"] == "Turkey Main"
    assert int(updated["users_limit"]) == 500
    assert int(updated["priority"]) == 20

    node = service.add_node(
        7001,
        label="France",
        server_id=int(node_server["id"]),
        parent_server_id=int(main["id"]),
    )
    scoped = service.list_nodes(parent_server_id=int(main["id"]))
    assert [int(item["id"]) for item in scoped] == [int(node["id"])]

    summary = service.server_admin_summary(7001, server_id=int(main["id"]))
    assert summary["users_count"] == 0
    assert summary["plans_count"] == 0
    assert summary["nodes_count"] == 1

    service.delete_node(
        7001, node_id=int(node["id"]), parent_server_id=int(main["id"])
    )
    deleted = service.delete_server(7001, server_id=int(node_server["id"]))
    assert deleted["label"] == "France"
    assert [item["label"] for item in service.list_servers()] == ["Turkey Main"]
