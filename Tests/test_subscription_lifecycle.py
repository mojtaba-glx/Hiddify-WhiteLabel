"""Paid renewals, expiry enforcement and periodic usage synchronization."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.lifecycle import TenantLifecycleCoordinator
from TenantRuntime.panels import (
    PanelError,
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)


class LifecyclePanel:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.renewals: list[RenewRequest] = []
        self.enabled_calls: list[tuple[str, bool]] = []
        self.fail_renew = False

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        ref = f"remote-{request.subscription_id}"
        self.users[ref] = {
            "usage_bytes": 0,
            "active": True,
            "traffic_bytes": request.traffic_bytes,
            "last_online": None,
        }
        return ProvisionResult(
            external_ref=ref,
            subscription_url=self.subscription_link(target=target, external_ref=ref),
        )

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        assert secret
        user = self.users[external_ref]
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=int(user["usage_bytes"]),
            active=bool(user["active"]),
            traffic_bytes=int(user["traffic_bytes"]),
            last_online=user.get("last_online"),
            subscription_url=self.subscription_link(
                target=target, external_ref=external_ref
            ),
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
        self.renewals.append(request)
        if self.fail_renew:
            raise PanelError("renew failed")
        user = self.users[external_ref]
        user["usage_bytes"] = 0
        user["active"] = True
        user["traffic_bytes"] = request.traffic_bytes
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
        self.enabled_calls.append((external_ref, bool(enabled)))
        self.users[external_ref]["active"] = bool(enabled)
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        assert secret
        self.users.pop(external_ref, None)

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        assert secret
        user = self.users[external_ref]
        return UsageResult(
            usage_bytes=int(user["usage_bytes"]),
            active=bool(user["active"]),
            last_online=user.get("last_online"),
        )

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        return f"{target.endpoint}/user/{external_ref}/all.txt"


def _prepared(conn, factories, cipher):
    owner, customer = 7001, 31
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = LifecyclePanel()
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
    service.set_panel_credential(
        owner, server_id=int(server["id"]), secret="panel-key"
    )
    plan = service.add_plan(
        owner,
        name="Monthly 20",
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
        reference="purchase-ref",
    )
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)
    active = service.fulfill_paid_order(owner, order_id=int(reviewed["order_id"]))
    subscription = service.list_subscriptions(customer)[0]
    return {
        "owner": owner,
        "customer": customer,
        "tenant": tenant,
        "panel": panel,
        "service": service,
        "server": server,
        "plan": plan,
        "method": method,
        "subscription": subscription,
        "active": active,
    }


def _make_expired(state, conn) -> None:
    sub = state["subscription"]
    ref = str(sub["external_ref"])
    state["panel"].users[ref]["active"] = False
    conn.execute(
        "UPDATE tenant_subscriptions "
        "SET status='expired', expires_at=?, expired_at=?, usage_bytes=traffic_bytes "
        "WHERE id=? AND tenant_id=?",
        (
            iso_utc(utcnow() - timedelta(minutes=5)),
            iso_utc(utcnow()),
            int(sub["id"]),
            int(state["tenant"]["id"]),
        ),
    )


def test_paid_renewal_reactivates_expired_subscription_and_updates_plan(
    conn, factories, cipher
) -> None:
    state = _prepared(conn, factories, cipher)
    _make_expired(state, conn)
    service = state["service"]
    owner, customer = state["owner"], state["customer"]
    subscription_id = int(state["subscription"]["id"])

    renewal_plan = service.add_plan(
        owner,
        name="Renew 50",
        traffic_gb=50,
        duration_days=45,
        price=2200,
        currency="IRR",
    )
    order = service.create_renewal_order(
        customer,
        subscription_id=subscription_id,
        plan_id=int(renewal_plan["id"]),
    )
    assert order["operation"] == "renewal"
    assert order["renewal_subscription_id"] == subscription_id

    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(state["method"]["id"]),
        reference="renew-ref",
    )
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)
    assert reviewed["operation"] == "renewal"
    assert reviewed["subscription_id"] == subscription_id
    assert service.order(customer, int(order["id"]))["status"] == "paid"

    result = service.fulfill_paid_order(owner, order_id=int(order["id"]))
    assert result["operation"] == "renewal"
    assert result["status"] == "active"
    assert service.order(customer, int(order["id"]))["status"] == "fulfilled"

    renewed = service.list_subscriptions(customer)[0]
    assert renewed["id"] == subscription_id
    assert renewed["plan_id"] == renewal_plan["id"]
    assert renewed["status"] == "active"
    assert renewed["usage_bytes"] == 0
    assert renewed["traffic_bytes"] == 50 * 1024**3
    assert renewed["expired_at"] is None
    request = state["panel"].renewals[-1]
    assert request.idempotency_key == (
        f"tenant:{state['tenant']['id']}:renewal-order:{order['id']}"
    )


def test_failed_renewal_keeps_paid_order_retryable(conn, factories, cipher) -> None:
    state = _prepared(conn, factories, cipher)
    _make_expired(state, conn)
    service = state["service"]
    owner, customer = state["owner"], state["customer"]
    subscription_id = int(state["subscription"]["id"])
    plan = service.add_plan(
        owner,
        name="Retry",
        traffic_gb=30,
        duration_days=30,
        price=1500,
        currency="IRR",
    )
    order = service.create_renewal_order(
        customer, subscription_id=subscription_id, plan_id=int(plan["id"])
    )
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(state["method"]["id"]),
        reference="retry-ref",
    )
    service.review_receipt(owner, int(receipt["id"]), approve=True)

    state["panel"].fail_renew = True
    with pytest.raises(TenantBusinessError, match="panel renewal failed"):
        service.fulfill_paid_order(owner, order_id=int(order["id"]))
    assert service.order(customer, int(order["id"]))["status"] == "paid"
    assert service.list_subscriptions(customer)[0]["status"] == "expired"

    state["panel"].fail_renew = False
    assert service.fulfill_paid_order(owner, order_id=int(order["id"]))["status"] == "active"
    assert service.order(customer, int(order["id"]))["status"] == "fulfilled"


def test_expired_subscription_cannot_be_reenabled_without_renewal(
    conn, factories, cipher
) -> None:
    state = _prepared(conn, factories, cipher)
    _make_expired(state, conn)
    service = state["service"]
    before = list(state["panel"].enabled_calls)
    with pytest.raises(TenantBusinessError, match="requires renewal"):
        service.set_subscription_enabled(
            state["owner"],
            subscription_id=int(state["subscription"]["id"]),
            enabled=True,
        )
    assert state["panel"].enabled_calls == before
    with pytest.raises(TenantBusinessError, match="not active"):
        service.subscription_link(
            state["customer"], subscription_id=int(state["subscription"]["id"])
        )


def test_usage_sync_persists_last_online_and_expires_depleted_service(
    conn, factories, cipher
) -> None:
    state = _prepared(conn, factories, cipher)
    service = state["service"]
    subscription = service.list_subscriptions(state["customer"])[0]
    ref = str(subscription["external_ref"])
    state["panel"].users[ref]["usage_bytes"] = int(subscription["traffic_bytes"])
    state["panel"].users[ref]["last_online"] = "2026-09-29 02:10:00"

    result = service.sync_subscription_usage(
        state["owner"], subscription_id=int(subscription["id"])
    )
    assert result["status"] == "expired"
    assert result["last_online"] == "2026-09-29 02:10:00"
    assert state["panel"].enabled_calls[-1] == (ref, False)

    stored = conn.execute(
        "SELECT status, usage_bytes, last_online, last_synced_at, expired_at "
        "FROM tenant_subscriptions WHERE id=? AND tenant_id=?",
        (int(subscription["id"]), int(state["tenant"]["id"])),
    ).fetchone()
    assert stored["status"] == "expired"
    assert stored["usage_bytes"] == subscription["traffic_bytes"]
    assert stored["last_online"] == "2026-09-29 02:10:00"
    assert stored["last_synced_at"] and stored["expired_at"]


def test_time_expiry_is_disabled_remotely_before_marking_expired(
    conn, factories, cipher
) -> None:
    state = _prepared(conn, factories, cipher)
    sub = service_sub = state["service"].list_subscriptions(state["customer"])[0]
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=? AND tenant_id=?",
        (
            iso_utc(utcnow() - timedelta(seconds=1)),
            int(sub["id"]),
            int(state["tenant"]["id"]),
        ),
    )
    result = state["service"].expire_due_subscriptions(state["owner"])
    assert result == {"expired": 1, "errors": 0}
    assert state["panel"].enabled_calls[-1] == (str(service_sub["external_ref"]), False)
    stored = state["service"].list_subscriptions(state["customer"])[0]
    assert stored["status"] == "expired"
    assert stored["expired_at"]


def test_only_owner_customer_can_create_renewal_order(conn, factories, cipher) -> None:
    state = _prepared(conn, factories, cipher)
    foreign_tenant = factories.tenant(owner_telegram_id=8001)
    foreign = TenantBusinessService(
        conn,
        tenant_id=int(foreign_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
        panel_adapter=state["panel"],
    )
    foreign.register_customer(state["customer"], display_name="Other", username=None)
    foreign_plan = foreign.add_plan(
        8001,
        name="Foreign",
        traffic_gb=10,
        duration_days=10,
        price=10,
        currency="IRR",
    )
    with pytest.raises(TenantBusinessError, match="cannot be renewed"):
        foreign.create_renewal_order(
            state["customer"],
            subscription_id=int(state["subscription"]["id"]),
            plan_id=int(foreign_plan["id"]),
        )


def test_lifecycle_coordinator_syncs_only_its_shard(
    conn, db_path, factories, cipher
) -> None:
    state = _prepared(conn, factories, cipher)
    # The next auto-increment tenant id has opposite parity, so a two-shard
    # coordinator must ignore it when it owns the prepared tenant.
    other = factories.tenant(owner_telegram_id=8001)
    assert int(other["id"]) % 2 != int(state["tenant"]["id"]) % 2

    sub = state["service"].list_subscriptions(state["customer"])[0]
    ref = str(sub["external_ref"])
    state["panel"].users[ref]["usage_bytes"] = 2 * 1024**3
    state["panel"].users[ref]["last_online"] = "2026-09-29 02:20:00"

    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=2,
        shard_index=int(state["tenant"]["id"]) % 2,
        panel_adapter_factory=lambda: state["panel"],
    )
    report = asyncio.run(coordinator.run_once())
    assert report.tenants == 1
    assert report.synced == 1
    assert report.errors == 0

    stored = conn.execute(
        "SELECT usage_bytes, last_online, last_synced_at "
        "FROM tenant_subscriptions WHERE id=? AND tenant_id=?",
        (int(sub["id"]), int(state["tenant"]["id"])),
    ).fetchone()
    assert stored["usage_bytes"] == 2 * 1024**3
    assert stored["last_online"] == "2026-09-29 02:20:00"
    assert stored["last_synced_at"]
