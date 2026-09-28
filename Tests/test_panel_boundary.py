"""Encrypted tenant panel credential and provisioning boundary tests."""

from __future__ import annotations

import pytest

from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import (
    PanelError,
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)


class FakePanel:
    def __init__(self) -> None:
        self.requests: list[ProvisionRequest] = []
        self.secrets: list[str] = []
        self.usage_calls: list[str] = []
        self.enabled_calls: list[tuple[str, bool]] = []
        self.deleted: list[str] = []
        self.renewals: list[RenewRequest] = []

    def provision(self, *, target: PanelTarget, secret: str, request: ProvisionRequest) -> ProvisionResult:
        assert target.endpoint == "https://panel.example"
        self.secrets.append(secret)
        self.requests.append(request)
        return ProvisionResult(external_ref=f"remote-{request.subscription_id}")

    def usage(self, *, target: PanelTarget, secret: str, external_ref: str) -> UsageResult:
        assert target.endpoint == "https://panel.example"
        self.secrets.append(secret)
        self.usage_calls.append(external_ref)
        return UsageResult(usage_bytes=12345, active=True)

    def get_user(self, *, target: PanelTarget, secret: str, external_ref: str) -> PanelUserResult:
        self.secrets.append(secret)
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=12345,
            active=True,
            traffic_bytes=20 * 1024**3,
            last_online="2026-09-29 00:15:00",
            subscription_url=self.subscription_link(target=target, external_ref=external_ref),
        )

    def renew(
        self, *, target: PanelTarget, secret: str, external_ref: str, request: RenewRequest
    ) -> PanelUserResult:
        self.secrets.append(secret)
        self.renewals.append(request)
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=0,
            active=True,
            traffic_bytes=request.traffic_bytes,
            subscription_url=self.subscription_link(target=target, external_ref=external_ref),
        )

    def set_enabled(
        self, *, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> PanelUserResult:
        self.secrets.append(secret)
        self.enabled_calls.append((external_ref, enabled))
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=12345,
            active=enabled,
            subscription_url=self.subscription_link(target=target, external_ref=external_ref),
        )

    def delete_user(self, *, target: PanelTarget, secret: str, external_ref: str) -> None:
        self.secrets.append(secret)
        self.deleted.append(external_ref)

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        return f"{target.endpoint}/user/{external_ref}/all.txt"


def _prepared_service(conn, factories, cipher):
    tenant = factories.tenant(owner_telegram_id=7001)
    panel = FakePanel()
    service = TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=7001,
        secret_cipher=cipher, panel_adapter=panel,
    )
    server = service.add_server(7001, label="DE", panel_kind="hiddify", endpoint="https://panel.example")
    plan = service.add_plan(7001, name="Monthly", traffic_gb=20, duration_days=30, price=1000)
    method = service.add_payment_method(7001, kind="card", title="Card", currency="IRR", destination="1111")
    service.register_customer(31, display_name="Buyer", username=None)
    order = service.create_order(31, int(plan["id"]))
    receipt = service.submit_receipt(31, order_id=int(order["id"]), method_id=int(method["id"]), reference="reference")
    service.review_receipt(7001, int(receipt["id"]), approve=True)
    subscription = service.list_subscriptions(31)[0]
    return service, panel, server, subscription


def test_panel_secret_is_encrypted_and_never_returned(conn, factories, cipher) -> None:
    tenant = factories.tenant(owner_telegram_id=7001)
    service = TenantBusinessService(conn, tenant_id=int(tenant["id"]), owner_telegram_id=7001, secret_cipher=cipher)
    server = service.add_server(7001, label="DE", panel_kind="xui", endpoint="https://panel.example")
    raw = "very-secret-panel-password"
    assert service.set_panel_credential(7001, server_id=int(server["id"]), secret=raw) == {"server_id": int(server["id"]), "configured": True}
    stored = conn.execute("SELECT encrypted_secret, secret_fingerprint FROM tenant_panel_credentials").fetchone()
    assert raw not in str(stored["encrypted_secret"])
    assert raw not in str(stored["secret_fingerprint"])
    assert service.panel_status(int(server["id"])) == {"server_id": int(server["id"]), "configured": True}


def test_provision_and_usage_are_tenant_scoped(conn, factories, cipher) -> None:
    service, panel, server, subscription = _prepared_service(conn, factories, cipher)
    service.set_panel_credential(7001, server_id=int(server["id"]), secret="panel-secret")
    active = service.activate_subscription(7001, subscription_id=int(subscription["id"]), server_id=int(server["id"]))
    assert active["status"] == "active"
    assert panel.requests[0].idempotency_key.endswith(f"subscription:{subscription['id']}")
    assert panel.secrets == ["panel-secret"]
    synced = service.sync_subscription_usage(7001, subscription_id=int(subscription["id"]))
    assert synced == {"id": int(subscription["id"]), "usage_bytes": 12345, "status": "active"}
    assert panel.usage_calls == [active["external_ref"]]


def test_missing_adapter_or_foreign_owner_cannot_provision(conn, factories, cipher) -> None:
    service, _, server, subscription = _prepared_service(conn, factories, cipher)
    service.set_panel_credential(7001, server_id=int(server["id"]), secret="panel-secret")
    with pytest.raises(PermissionError):
        service.activate_subscription(99, subscription_id=int(subscription["id"]), server_id=int(server["id"]))
    unconfigured = TenantBusinessService(
        conn, tenant_id=service.tenant_id, owner_telegram_id=7001, secret_cipher=cipher
    )
    with pytest.raises(TenantBusinessError, match="panel provisioning failed"):
        unconfigured.activate_subscription(7001, subscription_id=int(subscription["id"]), server_id=int(server["id"]))
    assert service.list_subscriptions(31)[0]["status"] == "pending_provisioning"


def test_manual_server_and_cross_tenant_secret_are_rejected(conn, factories, cipher) -> None:
    first = factories.tenant(owner_telegram_id=7001)
    second = factories.tenant(owner_telegram_id=7002)
    left = TenantBusinessService(conn, tenant_id=int(first["id"]), owner_telegram_id=7001, secret_cipher=cipher)
    right = TenantBusinessService(conn, tenant_id=int(second["id"]), owner_telegram_id=7002, secret_cipher=cipher)
    manual = left.add_server(7001, label="Manual", panel_kind="manual")
    with pytest.raises(TenantBusinessError):
        left.set_panel_credential(7001, server_id=int(manual["id"]), secret="secret")
    with pytest.raises(TenantBusinessError):
        right.panel_status(int(manual["id"]))


def test_live_hiddify_operations_stay_tenant_scoped(conn, factories, cipher) -> None:
    service, panel, server, subscription = _prepared_service(conn, factories, cipher)
    service.set_panel_credential(7001, server_id=int(server["id"]), secret="panel-secret")
    active = service.activate_subscription(
        7001, subscription_id=int(subscription["id"]), server_id=int(server["id"])
    )
    external_ref = active["external_ref"]

    snapshot = service.get_subscription_panel_user(
        7001, subscription_id=int(subscription["id"])
    )
    assert snapshot["external_ref"] == external_ref
    assert snapshot["last_online"] == "2026-09-29 00:15:00"

    renewed = service.renew_subscription(
        7001, subscription_id=int(subscription["id"]), traffic_gb=50, duration_days=30
    )
    assert renewed["status"] == "active"
    assert renewed["traffic_bytes"] == 50 * 1024**3
    assert panel.renewals[-1].reset_usage is True

    disabled = service.set_subscription_enabled(
        7001, subscription_id=int(subscription["id"]), enabled=False
    )
    assert disabled["status"] == "disabled"
    enabled = service.set_subscription_enabled(
        7001, subscription_id=int(subscription["id"]), enabled=True
    )
    assert enabled["status"] == "active"

    assert service.subscription_link(31, subscription_id=int(subscription["id"])).endswith(
        f"/{external_ref}/all.txt"
    )

    foreign = factories.tenant(owner_telegram_id=7002)
    foreign_service = TenantBusinessService(
        conn,
        tenant_id=int(foreign["id"]),
        owner_telegram_id=7002,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    with pytest.raises(TenantBusinessError, match="subscription not found"):
        foreign_service.get_subscription_panel_user(
            7002, subscription_id=int(subscription["id"])
        )

    removed = service.delete_subscription_from_panel(
        7001, subscription_id=int(subscription["id"])
    )
    assert removed["status"] == "disabled"
    assert panel.deleted == [external_ref]
