"""Phase 11 payment: provider boundary, unified receipts and wallet review."""

from __future__ import annotations

import pytest

from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.payments import (
    PaymentProviderSpec,
    method_view,
    provider_for_key,
    register_provider,
)


def _service(conn, factories, owner=7301):
    tenant = factories.tenant(owner_telegram_id=owner)
    return tenant, TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=owner
    )


def test_provider_registry_supports_extension_without_userbot_branching() -> None:
    register_provider(
        PaymentProviderSpec(
            key="test_gateway",
            title="Test Gateway",
            icon="🌐",
            legacy_kind="card",
            destination_label="Gateway",
            requires_receipt=False,
            manual_review=False,
            user_selectable=False,
        ),
        replace=True,
    )
    spec = provider_for_key("test_gateway", legacy_kind="card")
    assert spec.key == "test_gateway"
    assert spec.requires_receipt is False
    assert method_view({"kind": "card", "provider_key": "test_gateway"})["provider_title"] == "Test Gateway"


def test_payment_method_admin_update_and_safe_remove(conn, factories) -> None:
    _, service = _service(conn, factories)
    method = service.add_payment_method(
        7301,
        kind="card",
        title="Main Card",
        currency="IRR",
        destination="6037",
        priority=5,
    )
    updated = service.update_payment_method_admin(
        7301,
        method_id=int(method["id"]),
        title="Backup Card",
        destination="5892",
        priority=20,
        instructions="receipt required",
    )
    assert updated["provider_key"] == "card_manual"
    assert updated["title"] == "Backup Card"
    assert int(updated["priority"]) == 20
    removed = service.remove_payment_method_admin(7301, method_id=int(method["id"]))
    assert removed["removed"] is True


def test_unified_admin_payments_reviews_order_and_wallet_topup(conn, factories) -> None:
    _, service = _service(conn, factories)
    plan = service.add_plan(
        7301, name="Starter", traffic_gb=10, duration_days=30,
        price=100000, currency="IRR"
    )
    method = service.add_payment_method(
        7301, kind="card", title="Card", currency="IRR", destination="6037"
    )
    service.register_customer(41, display_name="Buyer", username=None)

    order = service.create_order(41, int(plan["id"]))
    order_receipt = service.submit_receipt(
        41, order_id=int(order["id"]), method_id=int(method["id"]), reference="order-ref"
    )
    topup = service.create_wallet_topup(41, amount=50000, currency="IRR")
    topup_receipt = service.submit_wallet_topup_receipt(
        41, topup_id=int(topup["id"]), method_id=int(method["id"]), reference="wallet-ref"
    )

    service.attach_payment_receipt_media(
        41,
        payment_key=f"order:{int(order_receipt['id'])}",
        media=b"fake-jpeg-bytes",
        mime_type="image/jpeg",
    )
    archived = service.payment_receipt_media_admin(
        7301, payment_key=f"order:{int(order_receipt['id'])}"
    )
    assert archived is not None
    assert archived["media"] == b"fake-jpeg-bytes"

    pending = service.list_payments_admin(7301, status="pending")
    keys = {item["payment_key"] for item in pending}
    assert f"order:{int(order_receipt['id'])}" in keys
    assert f"wallet_topup:{int(topup_receipt['id'])}" in keys

    wallet_result = service.review_payment_admin(
        7301,
        payment_key=f"wallet_topup:{int(topup_receipt['id'])}",
        approve=True,
        note="ok",
    )
    assert wallet_result["status"] == "paid"
    summary = service.wallet_summary(41)
    assert summary["accounts"][0]["balance"] == 50000

    order_result = service.review_payment_admin(
        7301,
        payment_key=f"order:{int(order_receipt['id'])}",
        approve=False,
        note="bad receipt",
    )
    assert order_result["status"] == "rejected"
    with pytest.raises(TenantBusinessError):
        service.review_payment_admin(
            7301,
            payment_key=f"order:{int(order_receipt['id'])}",
            approve=True,
        )


def test_wallet_paid_order_appears_in_customer_payment_status(conn, factories) -> None:
    _, service = _service(conn, factories)
    plan = service.add_plan(
        7301, name="Wallet Plan", traffic_gb=10, duration_days=30,
        price=25000, currency="IRR"
    )
    customer = service.register_customer(42, display_name="Wallet Buyer", username=None)
    service.adjust_wallet_admin(
        7301,
        customer_id=int(customer["id"]),
        currency="IRR",
        amount=30000,
        note="seed",
    )
    order = service.create_order(42, int(plan["id"]))
    service.pay_order_with_wallet(42, order_id=int(order["id"]))

    history = service.customer_payment_history(42)
    wallet = next(item for item in history if item["source"] == "wallet_order")
    assert wallet["status"] == "approved"
    assert wallet["provider_key"] == "wallet"
    assert wallet["amount"] == 25000


def test_referenced_payment_method_is_disabled_not_deleted(conn, factories) -> None:
    _, service = _service(conn, factories)
    plan = service.add_plan(
        7301, name="Ref Plan", traffic_gb=5, duration_days=30,
        price=1000, currency="IRR"
    )
    method = service.add_payment_method(
        7301, kind="crypto", title="USDT", currency="IRR",
        destination="wallet", network="TRC20"
    )
    service.register_customer(43, display_name="Buyer", username=None)
    order = service.create_order(43, int(plan["id"]))
    service.submit_receipt(
        43, order_id=int(order["id"]), method_id=int(method["id"]), reference="hash"
    )
    result = service.remove_payment_method_admin(7301, method_id=int(method["id"]))
    assert result["removed"] is False
    assert result["status"] == "disabled"
