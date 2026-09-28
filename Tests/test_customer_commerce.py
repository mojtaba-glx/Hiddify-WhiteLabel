"""Customer storefront: ownership, payment state and provisioning invariants."""

from __future__ import annotations

import asyncio

import pytest

from MasterBot.customer_service import CustomerPortalError, CustomerPortalService, PaymentStateError
from MasterBot.service import BotIdentity, MasterService
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, generate_key


class Verifier:
    async def verify(self, token: str) -> BotIdentity:
        if token.endswith("admin"):
            return BotIdentity(telegram_bot_id=700001, username="customer_admin")
        return BotIdentity(telegram_bot_id=700002, username="customer_user")


@pytest.fixture()
def portal(conn) -> CustomerPortalService:
    master = MasterService(
        conn, master_admin_id=9001, cipher=FernetTokenCipher(generate_key()), bot_verifier=Verifier()
    )
    return CustomerPortalService(conn, master_service=master)


def _customer(portal: CustomerPortalService, actor: int, name: str = "Customer") -> None:
    portal.register_customer(actor, display_name=name, username=None)


def _method(portal: CustomerPortalService, *, currency: str = "USD") -> dict:
    return portal.add_payment_method(
        9001, kind="card", title="Card", currency=currency,
        destination="1111-2222-3333-4444", recipient="Owner",
    )


def test_customer_price_is_authoritative_and_orders_are_private(portal, factories) -> None:
    plan = factories.plan(price=1500, duration_days=30)
    _customer(portal, 101)
    _customer(portal, 202)
    order = portal.create_purchase_order(101, int(plan["id"]))
    assert int(order["amount"]) == 1500
    assert order["currency"] == "USD"
    with pytest.raises(Exception):
        portal.get_order(202, int(order["id"]))
    assert portal.list_public_plans()[0]["id"] == plan["id"]


def test_payment_receipt_is_single_review_and_paid_purchase_becomes_setup_candidate(portal, factories) -> None:
    plan = factories.plan(price=250)
    _customer(portal, 101)
    method = _method(portal)
    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]), payment_method_id=int(method["id"]), reference="REF-1"
    )
    reviewed = portal.review_receipt(9001, int(receipt["id"]), approve=True)
    assert reviewed["order_status"] == "paid"
    assert [row["id"] for row in portal.setup_candidates(101)] == [order["id"]]
    with pytest.raises(PaymentStateError):
        portal.review_receipt(9001, int(receipt["id"]), approve=True)


def test_wallet_topup_credit_then_atomic_purchase(portal, factories) -> None:
    plan = factories.plan(price=200)
    _customer(portal, 101)
    method = _method(portal)
    topup = portal.create_wallet_topup(101, currency="USD", amount=500)
    receipt = portal.submit_receipt(
        101, order_id=int(topup["id"]), payment_method_id=int(method["id"]), reference="TOP-1"
    )
    approved = portal.review_receipt(9001, int(receipt["id"]), approve=True)
    assert approved["order_status"] == "fulfilled"
    assert portal.wallet_balance(101, "USD") == 500
    order = portal.create_purchase_order(101, int(plan["id"]))
    paid = portal.pay_order_from_wallet(101, int(order["id"]))
    assert paid["status"] == "paid"
    assert portal.wallet_balance(101, "USD") == 300
    with pytest.raises(PaymentStateError):
        portal.pay_order_from_wallet(101, int(order["id"]))


def test_trial_is_once_per_customer_and_uses_configured_duration(portal, factories) -> None:
    plan = factories.plan(price=1)
    portal.configure_plan_commerce(9001, int(plan["id"]), currency="USD", is_public=True, trial_days=7)
    _customer(portal, 101)
    trial = portal.claim_trial(101)
    assert trial["kind"] == "trial" and int(trial["trial_days"]) == 7
    with pytest.raises(CustomerPortalError):
        portal.claim_trial(101)


def test_paid_order_provisions_two_verified_bots_and_license(portal, factories) -> None:
    plan = factories.plan(price=20, duration_days=31)
    _customer(portal, 101)
    method = _method(portal)
    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]), payment_method_id=int(method["id"]), reference="PAY-1"
    )
    portal.review_receipt(9001, int(receipt["id"]), approve=True)
    admin_token = "990001:FAKE_customer_token_abcdefghijklmnopqrstuvwxyz_admin"
    user_token = "990002:FAKE_customer_token_abcdefghijklmnopqrstuvwxyz_user"
    admin = asyncio.run(portal.prepare_customer_bot(101, order_id=int(order["id"]), role="admin", plain_token=admin_token))
    user = asyncio.run(portal.prepare_customer_bot(101, order_id=int(order["id"]), role="user", plain_token=user_token))
    result = portal.provision_paid_order(
        101, order_id=int(order["id"]), name="Store", slug="store-101", admin_bot=admin, user_bot=user
    )
    assert result.license["status"] == "active"
    assert portal.get_order(101, int(order["id"]))["status"] == "fulfilled"
    assert len(portal.list_services(101)) == 1
    bots = portal.conn.execute("SELECT encrypted_token FROM tenant_bots WHERE tenant_id = ?", (result.provisioning.tenant_id,)).fetchall()
    assert len(bots) == 2
    assert all(admin_token not in str(row["encrypted_token"]) and user_token not in str(row["encrypted_token"]) for row in bots)


def test_customer_cannot_manage_platform_payment_configuration(portal, factories) -> None:
    factories.plan()
    _customer(portal, 101)
    with pytest.raises(AccessDenied):
        portal.add_payment_method(101, kind="card", title="x", currency="USD", destination="x")
    with pytest.raises(AccessDenied):
        portal.list_pending_receipts(101)
