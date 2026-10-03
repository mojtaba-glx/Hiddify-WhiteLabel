"""Tenant reports, customer management, account summary and ticket lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from Shared.timeutils import iso_utc
from TenantRuntime.AdminBot import handlers as admin_handlers
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import (
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)


class ReportPanel:
    def __init__(self) -> None:
        self.users: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(target: PanelTarget, ref: str) -> tuple[str, str]:
        return target.endpoint, ref

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        ref = f"r-{request.server_id}-{request.subscription_id}"
        self.users[self._key(target, ref)] = {
            "usage": 0,
            "active": True,
            "traffic": request.traffic_bytes,
            "expires_at": request.expires_at,
        }
        return ProvisionResult(
            external_ref=ref,
            subscription_url=f"{target.endpoint}/sub/{ref}",
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
            expires_at=str(row["expires_at"]),
            last_online=None,
            subscription_url=f"{target.endpoint}/sub/{external_ref}",
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
        row = self.users[self._key(target, external_ref)]
        row.update(
            usage=0,
            active=True,
            traffic=request.traffic_bytes,
            expires_at=request.expires_at,
        )
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
        self.users[self._key(target, external_ref)]["active"] = bool(enabled)
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        assert secret
        self.users.pop(self._key(target, external_ref), None)

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        user = self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )
        return UsageResult(
            usage_bytes=user.usage_bytes,
            active=user.active,
            last_online=user.last_online,
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        return f"{target.endpoint}/sub/{external_ref}"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        assert secret
        return f"vless://{external_ref}@edge.example:443?type=tcp#report"


def _service(conn, factories, cipher, *, owner=7001, customer=7101):
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = ReportPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server = service.add_server(
        owner,
        label="Primary",
        panel_kind="hiddify",
        endpoint="https://primary.example",
        admin_path="admin",
        user_path="user",
    )
    service.set_panel_credential(
        owner, server_id=int(server["id"]), secret="api-secret"
    )
    service.set_default_server(owner, server_id=int(server["id"]))
    plan = service.add_plan(
        owner,
        name="20GB",
        traffic_gb=20,
        duration_days=30,
        price=120000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="1111",
    )
    service.register_customer(
        customer,
        display_name="Ali Report",
        username="ali_report",
    )
    return tenant, service, panel, plan, method


def _approve_purchase(service, *, customer, plan_id, method_id, reference):
    order = service.create_order(customer, plan_id)
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=method_id,
        reference=reference,
    )
    reviewed = service.review_receipt(
        service.owner_telegram_id,
        int(receipt["id"]),
        approve=True,
    )
    return order, reviewed


def test_receipt_approval_sets_paid_at_and_reports_ignore_later_updated_at(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="purchase-1",
    )
    stored = conn.execute(
        "SELECT paid_at, updated_at, status FROM tenant_orders WHERE id=?",
        (int(order["id"]),),
    ).fetchone()
    assert stored["status"] == "paid"
    assert stored["paid_at"]

    paid_at = str(stored["paid_at"])
    conn.execute(
        "UPDATE tenant_orders SET updated_at=? WHERE id=?",
        ("2099-01-01T00:00:00Z", int(order["id"])),
    )
    all_time = service.sales_report(7001, days=0, tz_name="UTC")
    assert all_time["paid_orders"] == 1
    assert all_time["totals"] == [
        {"currency": "IRR", "count": 1, "amount": 120000}
    ]
    assert conn.execute(
        "SELECT paid_at FROM tenant_orders WHERE id=?",
        (int(reviewed["order_id"]),),
    ).fetchone()["paid_at"] == paid_at


def test_sales_report_separates_purchase_and_renewal_by_paid_at_period(
    conn, factories, cipher
) -> None:
    _tenant, service, panel, plan, method = _service(
        conn, factories, cipher
    )
    order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="purchase-period",
    )
    service.fulfill_paid_order(7001, order_id=int(reviewed["order_id"]))
    subscription = service.list_subscriptions(7101)[0]

    # This test exercises reporting; make the service eligible under the same
    # renewal window used by every policy profile.
    conn.execute(
        "UPDATE tenant_subscriptions SET usage_bytes=traffic_bytes-? WHERE id=?",
        (1024 ** 3, int(subscription["id"])),
    )
    ref = str(subscription["external_ref"])
    primary_endpoint = "https://primary.example"
    panel.users[(primary_endpoint, ref)]["usage"] = (
        int(subscription["traffic_bytes"]) - 1024 ** 3
    )
    conn.commit()
    renewal = service.create_renewal_order(
        7101,
        subscription_id=int(subscription["id"]),
        plan_id=int(plan["id"]),
    )
    renewal_receipt = service.submit_receipt(
        7101,
        order_id=int(renewal["id"]),
        method_id=int(method["id"]),
        reference="renew-period",
    )
    service.review_receipt(7001, int(renewal_receipt["id"]), approve=True)

    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    today = iso_utc(now - timedelta(hours=2))
    old = iso_utc(now - timedelta(days=10))
    conn.execute(
        "UPDATE tenant_orders SET paid_at=? WHERE id=?",
        (today, int(renewal["id"])),
    )
    conn.execute(
        "UPDATE tenant_orders SET paid_at=? WHERE id=?",
        (old, int(order["id"])),
    )

    report = service.sales_report(
        7001,
        days=7,
        now=now,
        tz_name="UTC",
    )
    assert report["paid_orders"] == 1
    assert report["unique_customers"] == 1
    assert report["operations"] == [
        {
            "currency": "IRR",
            "purchase_count": 0,
            "purchase_amount": 0,
            "renewal_count": 1,
            "renewal_amount": 120000,
            "traffic_gb": 20,
        }
    ]


def test_dashboard_surfaces_current_attention_counts(
    conn, factories, cipher
) -> None:
    tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    _order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="dashboard",
    )
    # Payment is approved but not fulfilled: both pending provisioning and
    # fulfillment-pending counters must surface it.
    report = service.dashboard_summary(7001, tz_name="UTC")
    current = report["current"]
    assert int(current["customers_total"]) == 1
    assert int(current["subs_pending"]) == 1
    assert int(current["fulfillment_pending"]) == 1
    assert int(current["receipts_pending"]) == 0

    attention = service.service_attention_admin(7001)
    assert len(attention) == 1
    assert int(attention[0]["order_id"]) == int(reviewed["order_id"])
    assert attention[0]["status"] == "pending_provisioning"
    assert int(attention[0]["tenant_id"]) == int(tenant["id"])


def test_customer_search_profile_blocking_and_account_are_tenant_scoped(
    conn, factories, cipher
) -> None:
    tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    foreign_tenant, foreign, _fp, foreign_plan, _fm = _service(
        conn,
        factories,
        cipher,
        owner=8001,
        customer=8101,
    )
    foreign.register_customer(
        8102,
        display_name="Ali Report Foreign",
        username="ali_report",
    )

    _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="scope",
    )
    results = service.search_customers_admin(7001, "Ali Report")
    assert len(results) == 1
    assert int(results[0]["tenant_id"]) == int(tenant["id"])
    assert all(
        int(row["tenant_id"]) != int(foreign_tenant["id"])
        for row in results
    )

    profile = service.customer_profile_admin(
        7001, customer_id=int(results[0]["id"])
    )
    assert profile["display_name"] == "Ali Report"
    assert profile["paid_totals"] == [
        {"currency": "IRR", "count": 1, "amount": 120000}
    ]

    blocked = service.set_customer_status_admin(
        7001,
        customer_id=int(profile["id"]),
        status="blocked",
    )
    assert blocked["status"] == "blocked"
    with pytest.raises(TenantBusinessError):
        service.create_order(7101, int(plan["id"]))

    # A blocked customer can still view their account/history.
    account = service.customer_account_summary(7101)
    assert account["customer"]["status"] == "blocked"
    assert account["paid_totals"][0]["amount"] == 120000

    with pytest.raises(TenantBusinessError):
        foreign.customer_profile_admin(
            8001, customer_id=int(profile["id"])
        )


def test_ticket_reply_close_and_user_history(
    conn, factories, cipher
) -> None:
    tenant, service, _panel, _plan, _method = _service(
        conn, factories, cipher
    )
    ticket = service.create_ticket(
        7101,
        subject="اتصال",
        body="لینک من وصل نمی‌شود",
    )
    listed = service.list_tickets_admin(7001)
    assert len(listed) == 1
    assert int(listed[0]["tenant_id"]) == int(tenant["id"])

    replied = service.reply_ticket_admin(
        7001,
        ticket_id=int(ticket["id"]),
        reply="لینک را یک‌بار به‌روزرسانی کنید.",
    )
    assert replied["status"] == "answered"
    assert replied["admin_reply"]

    user_tickets = service.list_tickets(7101)
    assert len(user_tickets) == 1
    assert user_tickets[0]["status"] == "answered"
    assert "به‌روزرسانی" in str(user_tickets[0]["admin_reply"])

    closed = service.close_ticket_admin(
        7001, ticket_id=int(ticket["id"])
    )
    assert closed["status"] == "closed"
    with pytest.raises(TenantBusinessError):
        service.reply_ticket_admin(
            7001,
            ticket_id=int(ticket["id"]),
            reply="نباید ثبت شود",
        )



def test_admin_subscription_search_expired_review_and_daily_report(
    conn, factories, cipher
) -> None:
    _tenant, service, panel, plan, method = _service(
        conn, factories, cipher
    )
    order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="search-report",
    )
    activated = service.fulfill_paid_order(
        7001, order_id=int(reviewed["order_id"])
    )
    sid = int(activated["id"])

    by_name = service.search_subscriptions_admin(7001, "Ali Report")
    by_tid = service.search_subscriptions_admin(7001, "7101")
    by_sid = service.search_subscriptions_admin(7001, str(sid))
    assert [int(x["id"]) for x in by_name] == [sid]
    assert [int(x["id"]) for x in by_tid] == [sid]
    assert [int(x["id"]) for x in by_sid] == [sid]

    link = service.admin_subscription_link(7001, subscription_id=sid)
    assert link

    edited = service.edit_subscription_terms_admin(
        7001, subscription_id=sid, traffic_gb=25
    )
    assert int(edited["traffic_bytes"]) == 25 * 1024**3

    conn.execute(
        "UPDATE tenant_subscriptions SET status='expired' WHERE id=?",
        (sid,),
    )
    expired = service.list_subscriptions_tracking_admin(
        7001, status="expired"
    )
    assert any(int(x["id"]) == sid for x in expired)

    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    yesterday = iso_utc(now - timedelta(days=1, hours=2))
    conn.execute(
        "UPDATE tenant_orders SET paid_at=? WHERE id=?",
        (yesterday, int(order["id"])),
    )
    conn.execute(
        "UPDATE tenant_receipts SET reviewed_at=? WHERE order_id=?",
        (yesterday, int(order["id"])),
    )
    report = service.daily_admin_report(
        7001, now=now, tz_name="UTC"
    )
    assert report["report_day"] == "2026-10-01"
    assert report["approved_receipts"] == 1
    assert report["cash"] == [
        {"currency": "IRR", "count": 1, "amount": 120000}
    ]
    assert report["services"][0]["buy_count"] == 1



def test_unstarted_cleanup_preserves_receipt_history(conn, factories, cipher) -> None:
    _tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="unstarted-cleanup",
    )
    pending = service.list_subscriptions(7101)[0]
    assert pending["status"] == "pending_provisioning"

    result = service.cleanup_unstarted_subscription_admin(
        7001, subscription_id=int(pending["id"])
    )
    assert result["status"] == "removed"
    assert conn.execute(
        "SELECT 1 FROM tenant_subscriptions WHERE id=?",
        (int(pending["id"]),),
    ).fetchone() is None
    assert conn.execute(
        "SELECT status FROM tenant_orders WHERE id=?",
        (int(order["id"]),),
    ).fetchone()["status"] == "cancelled"
    assert conn.execute(
        "SELECT status FROM tenant_receipts WHERE order_id=?",
        (int(reviewed["order_id"]),),
    ).fetchone()["status"] == "approved"



def test_admin_smart_search_accepts_uuid_inside_config_link(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="link-search",
    )
    activated = service.fulfill_paid_order(
        7001, order_id=int(reviewed["order_id"])
    )
    sid = int(activated["id"])
    uuid = "12345678-1234-4234-8234-123456789abc"
    conn.execute(
        "UPDATE tenant_subscriptions SET external_ref=? WHERE id=?",
        (uuid, sid),
    )
    results = service.search_subscriptions_admin(
        7001,
        f"vless://{uuid}@example.com:443?security=tls#test",
    )
    assert [int(item["id"]) for item in results] == [sid]


def test_admin_search_uses_real_panel_inventory_and_sellbot_service_code(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, _plan, _method = _service(
        conn, factories, cipher
    )
    server_id = int(service.list_servers()[0]["id"])
    future = iso_utc(datetime.now(timezone.utc) + timedelta(days=10))
    panel_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    cursor = conn.execute(
        "INSERT INTO tenant_panel_users "
        "(tenant_id,server_id,external_ref,name,comment,usage_bytes,traffic_bytes,"
        "expires_at,active,state,extra_json,last_synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,'active',?,?)",
        (
            service.tenant_id,
            server_id,
            panel_uuid,
            "vpn-0506795",
            f"uuid:{panel_uuid}|admin:1|code:0506795",
            1024,
            10 * 1024**3,
            future,
            '{"legacy_service_id":71,"legacy_admin_inventory":true}',
            iso_utc(datetime.now(timezone.utc)),
        ),
    )
    panel_user_id = int(cursor.lastrowid)

    by_name = service.search_panel_users_admin(7001, "vpn-050")
    by_code = service.search_panel_users_admin(7001, "0506795")
    by_legacy_id = service.search_panel_users_admin(7001, "71")
    by_link = service.search_panel_users_admin(
        7001, f"vless://{panel_uuid}@example.com:443#test"
    )
    assert [int(x["id"]) for x in by_name] == [panel_user_id]
    assert [int(x["id"]) for x in by_code] == [panel_user_id]
    assert [int(x["id"]) for x in by_legacy_id] == [panel_user_id]
    assert [int(x["id"]) for x in by_link] == [panel_user_id]
    assert by_code[0]["service_code"] == "0506795"
    assert int(by_code[0]["legacy_service_id"]) == 71

    merged = admin_handlers._smart_search_items(service, 7001, "vpn-0506795")
    assert len(merged) == 1
    assert merged[0]["_result_type"] == "panel"
    text, keyboard = admin_handlers._search_results_view(merged)
    assert "1" in text
    callback = keyboard.inline_keyboard[0][0].callback_data
    assert callback == f"search:panel:{server_id}:{panel_user_id}"

    tracking = admin_handlers._tracking_items(service, 7001, "0506795")
    assert len(tracking) == 1
    assert tracking[0]["_result_type"] == "panel"
    assert int(tracking[0]["id"]) == panel_user_id


def test_admin_search_finds_customer_name_through_linked_panel_user(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    _order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="panel-customer-search",
    )
    activated = service.fulfill_paid_order(
        7001, order_id=int(reviewed["order_id"])
    )
    stored = service.subscription_admin(
        7001, subscription_id=int(activated["id"])
    )
    server_id = int(stored["server_id"])
    external_ref = str(stored["external_ref"])
    cursor = conn.execute(
        "INSERT INTO tenant_panel_users "
        "(tenant_id,server_id,external_ref,name,comment,usage_bytes,traffic_bytes,"
        "expires_at,active,state,extra_json,last_synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,'active','{}',?)",
        (
            service.tenant_id,
            server_id,
            external_ref,
            "panel-random-name",
            "code:7654321",
            0,
            int(stored["traffic_bytes"]),
            str(stored["expires_at"]),
            iso_utc(datetime.now(timezone.utc)),
        ),
    )
    panel_user_id = int(cursor.lastrowid)

    rows = service.search_panel_users_admin(7001, "Ali Report")
    assert [int(x["id"]) for x in rows] == [panel_user_id]
    assert int(rows[0]["subscription_id"]) == int(activated["id"])
    assert rows[0]["customer_display_name"] == "Ali Report"


def test_expired_lists_use_effective_time_and_volume_not_only_db_status(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method = _service(
        conn, factories, cipher
    )
    _order, reviewed = _approve_purchase(
        service,
        customer=7101,
        plan_id=int(plan["id"]),
        method_id=int(method["id"]),
        reference="effective-expired",
    )
    activated = service.fulfill_paid_order(
        7001, order_id=int(reviewed["order_id"])
    )
    subscription_id = int(activated["id"])
    yesterday = iso_utc(datetime.now(timezone.utc) - timedelta(days=1))
    conn.execute(
        "UPDATE tenant_subscriptions SET status='active', expires_at=? WHERE id=?",
        (yesterday, subscription_id),
    )

    expired_subscriptions = service.list_subscriptions_tracking_admin(
        7001, status="expired"
    )
    assert [int(x["id"]) for x in expired_subscriptions] == [subscription_id]
    assert expired_subscriptions[0]["status"] == "expired"

    server_id = int(service.subscription_admin(7001, subscription_id=int(activated["id"]))["server_id"])
    conn.execute(
        "INSERT INTO tenant_panel_users "
        "(tenant_id,server_id,external_ref,name,comment,usage_bytes,traffic_bytes,"
        "expires_at,active,state,extra_json,last_synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,'active','{}',?)",
        (
            service.tenant_id,
            server_id,
            "expired-panel-user",
            "expired-panel-user",
            "",
            5 * 1024**3,
            5 * 1024**3,
            iso_utc(datetime.now(timezone.utc) + timedelta(days=30)),
            iso_utc(datetime.now(timezone.utc)),
        ),
    )
    expired_panel = service.search_panel_users_admin(
        7001, "", status="expired"
    )
    assert any(
        str(x["external_ref"]) == "expired-panel-user"
        for x in expired_panel
    )
