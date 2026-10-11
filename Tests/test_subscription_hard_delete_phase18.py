"""Phase 18 full subscription deletion: panel + database, SellBot parity."""

from __future__ import annotations

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import PanelError


class FakePanel:
    def __init__(self, fail_mode: str = "") -> None:
        self.deleted: list[str] = []
        self.fail_mode = fail_mode

    def delete_user(self, *, target, secret: str, external_ref: str) -> None:
        if self.fail_mode == "missing":
            raise PanelError("HTTP 404 user not found")
        if self.fail_mode == "offline":
            raise PanelError("panel connection failed")
        self.deleted.append(str(external_ref))


def _provisioned(conn, factories, cipher, owner=7801, customer_tg=7811):
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = FakePanel()
    service = TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=owner,
        secret_cipher=cipher, panel_adapter=panel,
    )
    server = service.add_server(
        owner, label="TR", panel_kind="hiddify",
        endpoint="https://panel.example",
    )
    service.set_panel_credential(owner, server_id=int(server["id"]), secret="k")
    plan = service.add_plan(
        owner, name="Plan", traffic_gb=10, duration_days=30,
        price=100000, currency="IRR",
    )
    method = service.add_payment_method(
        owner, kind="card", title="Card", currency="IRR", destination="6037"
    )
    service.register_customer(
        customer_tg, display_name="Buyer", username=None
    )
    order = service.create_order(customer_tg, int(plan["id"]))
    receipt = service.submit_receipt(
        customer_tg, order_id=int(order["id"]),
        method_id=int(method["id"]), reference="ref-1",
    )
    service.review_receipt(owner, int(receipt["id"]), approve=True)
    sub = service.list_subscriptions(customer_tg)[0]
    now = iso_utc(utcnow())
    conn.execute(
        "UPDATE tenant_subscriptions SET server_id=?, external_ref=?,"
        " status='active', updated_at=? WHERE id=? AND tenant_id=?",
        (int(server["id"]), "panel-ref-1", now, int(sub["id"]), int(tenant["id"])),
    )
    conn.execute(
        "INSERT INTO tenant_subscription_nodes "
        "(tenant_id, subscription_id, server_id, external_ref, is_primary,"
        " status, created_at, updated_at) VALUES (?,?,?,?,1,'active',?,?)",
        (int(tenant["id"]), int(sub["id"]), int(server["id"]),
         "panel-ref-1", now, now),
    )
    conn.execute(
        "INSERT INTO tenant_subscription_rotations "
        "(tenant_id, subscription_id, new_ref, created_at) VALUES (?,?,?,?)",
        (int(tenant["id"]), int(sub["id"]), "new-ref", now),
    )
    conn.execute(
        "INSERT INTO tenant_smart_links "
        "(tenant_id, code, label, target, status, created_at, updated_at)"
        " VALUES (?,?,?,?, 'active',?,?)",
        (int(tenant["id"]), "linkcode1", "L",
         f"subscription:{int(sub['id'])}", now, now),
    )
    conn.commit()
    return tenant, service, panel, sub


def test_hard_delete_removes_panel_and_database_rows(
    conn, factories, cipher
) -> None:
    tenant, service, panel, sub = _provisioned(conn, factories, cipher)
    sid = int(sub["id"])
    order_id = int(sub["order_id"])

    result = service.delete_subscription_admin(7801, subscription_id=sid)

    assert result["status"] == "deleted"
    assert panel.deleted == ["panel-ref-1"]
    assert conn.execute(
        "SELECT id FROM tenant_subscriptions WHERE id=?", (sid,)
    ).fetchone() is None
    assert conn.execute(
        "SELECT id FROM tenant_subscription_nodes WHERE subscription_id=?",
        (sid,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT * FROM tenant_subscription_rotations WHERE subscription_id=?",
        (sid,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT id FROM tenant_smart_links WHERE target=?",
        (f"subscription:{sid}",),
    ).fetchone() is None
    # Financial history stays intact (SellBot parity).
    assert conn.execute(
        "SELECT status FROM tenant_orders WHERE id=?", (order_id,)
    ).fetchone() is not None
    assert conn.execute(
        "SELECT id FROM tenant_receipts WHERE order_id=?", (order_id,)
    ).fetchone() is not None


def test_hard_delete_keeps_row_when_panel_unreachable(
    conn, factories, cipher
) -> None:
    tenant, service, panel, sub = _provisioned(conn, factories, cipher)
    panel.fail_mode = "offline"
    with pytest.raises(TenantBusinessError):
        service.delete_subscription_admin(7801, subscription_id=int(sub["id"]))
    assert conn.execute(
        "SELECT id FROM tenant_subscriptions WHERE id=?", (int(sub["id"]),)
    ).fetchone() is not None


def test_hard_delete_tolerates_already_missing_panel_user(
    conn, factories, cipher
) -> None:
    tenant, service, panel, sub = _provisioned(conn, factories, cipher)
    panel.fail_mode = "missing"
    result = service.delete_subscription_admin(
        7801, subscription_id=int(sub["id"])
    )
    assert result["status"] == "deleted"
    assert conn.execute(
        "SELECT id FROM tenant_subscriptions WHERE id=?", (int(sub["id"]),)
    ).fetchone() is None


def test_hard_delete_cleans_renewal_link_and_coupon_reference(
    conn, factories, cipher
) -> None:
    tenant, service, panel, sub = _provisioned(conn, factories, cipher)
    sid = int(sub["id"])
    now = iso_utc(utcnow())
    customer = conn.execute(
        "SELECT id FROM tenant_customers WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()
    plan = conn.execute(
        "SELECT id FROM tenant_sale_plans WHERE tenant_id=?",
        (int(tenant["id"]),),
    ).fetchone()
    renewal_order = conn.execute(
        "INSERT INTO tenant_orders (tenant_id, customer_id, plan_id, amount,"
        " currency, status, order_kind, created_at, updated_at)"
        " VALUES (?,?,?,?,?,'paid','renewal',?,?)",
        (int(tenant["id"]), int(customer["id"]), int(plan["id"]),
         1000, "IRR", now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO tenant_renewal_orders (order_id, tenant_id,"
        " subscription_id, created_at) VALUES (?,?,?,?)",
        (int(renewal_order), int(tenant["id"]), sid, now),
    )
    conn.commit()

    service.delete_subscription_admin(7801, subscription_id=sid)
    assert conn.execute(
        "SELECT * FROM tenant_renewal_orders WHERE subscription_id=?",
        (sid,),
    ).fetchone() is None
    # The renewal order itself (money history) survives.
    assert conn.execute(
        "SELECT status FROM tenant_orders WHERE id=?", (int(renewal_order),)
    ).fetchone() is not None
