"""Master storefront settings and payment administration regression tests."""

from __future__ import annotations

import pytest

from MasterBot.customer_service import CustomerPortalError, CustomerPortalService, PaymentStateError
from MasterBot.service import MasterService
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, generate_key
from Shared.timeutils import parse_utc


@pytest.fixture()
def portal(conn) -> CustomerPortalService:
    master = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
    )
    return CustomerPortalService(conn, master_service=master)


def test_platform_settings_defaults_and_persistence(portal) -> None:
    settings = portal.get_platform_settings(9001)
    assert settings["store_name"] == "فروش ربات اختصاصی"
    assert settings["sales_enabled"] is True
    assert settings["trial_enabled"] is True

    portal.set_platform_text_setting(9001, "store_name", "Speed Bot")
    portal.set_platform_text_setting(9001, "support_contact", "@speed_support")
    portal.set_platform_bool_setting(9001, "sales_enabled", False)

    updated = portal.get_platform_settings(9001)
    assert updated["store_name"] == "Speed Bot"
    assert updated["support_contact"] == "@speed_support"
    assert updated["sales_enabled"] is False

    stored = portal.conn.execute(
        "SELECT value FROM platform_settings WHERE key = 'store_name'"
    ).fetchone()
    assert stored is not None and stored["value"] == "Speed Bot"


def test_customer_cannot_change_platform_settings(portal) -> None:
    with pytest.raises(AccessDenied):
        portal.set_platform_bool_setting(101, "sales_enabled", False)
    with pytest.raises(AccessDenied):
        portal.set_platform_text_setting(101, "store_name", "Forged")


def test_sales_switch_blocks_new_purchase_and_renewal(portal, factories) -> None:
    plan = factories.plan(price=100)
    portal.register_customer(101, display_name="Customer")
    tenant = factories.tenant(owner_telegram_id=101)
    factories.license(int(tenant["id"]), int(plan["id"]))

    portal.set_platform_bool_setting(9001, "sales_enabled", False)

    with pytest.raises(CustomerPortalError):
        portal.create_purchase_order(101, int(plan["id"]))
    with pytest.raises(CustomerPortalError):
        portal.create_renewal_order(
            101, tenant_id=int(tenant["id"]), plan_id=int(plan["id"])
        )

    count = portal.conn.execute("SELECT COUNT(*) AS c FROM customer_orders").fetchone()
    assert count is not None and int(count["c"]) == 0


def test_trial_switch_blocks_claim(portal, factories) -> None:
    plan = factories.plan(price=1)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=7
    )
    portal.register_customer(101, display_name="Customer")
    portal.set_platform_bool_setting(9001, "trial_enabled", False)

    with pytest.raises(CustomerPortalError):
        portal.claim_trial(101)


def test_payment_method_can_be_edited_without_changing_kind_or_status(portal) -> None:
    method = portal.add_payment_method(
        9001,
        kind="card",
        title="Old card",
        currency="USD",
        destination="1111",
        recipient="Old owner",
    )
    updated = portal.update_payment_method(
        9001,
        int(method["id"]),
        title="Main card",
        currency="IRR",
        destination="2222",
        recipient="New owner",
        instructions="بعد از پرداخت رسید را ارسال کنید",
    )

    assert updated["kind"] == "card"
    assert updated["status"] == "active"
    assert updated["title"] == "Main card"
    assert updated["currency"] == "IRR"
    assert updated["destination"] == "2222"
    assert updated["recipient"] == "New owner"

    public = portal.list_payment_methods("IRR")
    assert [row["id"] for row in public] == [method["id"]]


def test_support_contact_can_be_cleared_with_dash(portal) -> None:
    portal.set_platform_text_setting(9001, "support_contact", "@support")
    portal.set_platform_text_setting(9001, "support_contact", "-")
    assert portal.get_platform_settings(9001)["support_contact"] == ""


def test_wallet_paid_renewal_extends_license_atomically(portal, factories) -> None:
    plan = factories.plan(price=100, duration_days=30)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Customer")
    tenant = factories.tenant(owner_telegram_id=101)
    license_row = factories.license(
        int(tenant["id"]), int(plan["id"]), duration_days=5, grace_days=0
    )
    before_expiry = parse_utc(str(license_row["expires_at"]))

    method = portal.add_payment_method(
        9001, kind="card", title="Card", currency="USD", destination="1111"
    )
    topup = portal.create_wallet_topup(101, currency="USD", amount=300)
    receipt = portal.submit_receipt(
        101,
        order_id=int(topup["id"]),
        payment_method_id=int(method["id"]),
        reference="TOP-UP",
    )
    portal.review_receipt(9001, int(receipt["id"]), approve=True)
    assert portal.wallet_balance(101, "USD") == 300

    renewal = portal.create_renewal_order(
        101, tenant_id=int(tenant["id"]), plan_id=int(plan["id"])
    )
    paid = portal.pay_order_from_wallet(101, int(renewal["id"]))

    assert paid["status"] == "fulfilled"
    assert portal.wallet_balance(101, "USD") == 200
    refreshed = portal.conn.execute(
        "SELECT * FROM licenses WHERE id = ?", (int(license_row["id"]),)
    ).fetchone()
    assert refreshed is not None
    assert parse_utc(str(refreshed["expires_at"])) > before_expiry


def test_owner_can_list_search_and_block_storefront_customers(portal) -> None:
    portal.register_customer(101, display_name="Ali Customer", username="ali_shop")
    portal.register_customer(202, display_name="Sara Customer", username="sara_shop")

    page = portal.list_platform_customers(9001, page_size=10)
    assert {int(row["telegram_user_id"]) for row in page.items} == {101, 202}

    searched = portal.list_platform_customers(9001, query="ali_shop")
    assert len(searched.items) == 1
    customer_id = int(searched.items[0]["id"])

    blocked = portal.set_platform_customer_status(9001, customer_id, "blocked")
    assert blocked["status"] == "blocked"
    with pytest.raises(CustomerPortalError):
        portal.create_wallet_topup(101, currency="USD", amount=10)

    active = portal.set_platform_customer_status(9001, customer_id, "active")
    assert active["status"] == "active"


def test_storefront_customer_detail_includes_wallet_orders_and_tenants(portal, factories) -> None:
    plan = factories.plan(price=100)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    customer = portal.register_customer(101, display_name="Customer", username="buyer")
    factories.tenant(owner_telegram_id=101)
    portal.create_purchase_order(101, int(plan["id"]))

    detail = portal.get_platform_customer_admin(9001, int(customer["id"]))
    assert int(detail["order_count"]) == 1
    assert int(detail["tenant_count"]) == 1
    assert detail["recent_orders"][0]["kind"] == "purchase"


def test_financial_summary_separates_sales_cash_inflow_and_wallet(portal, factories) -> None:
    plan = factories.plan(price=100, duration_days=30)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Buyer", username="buyer")
    method = portal.add_payment_method(
        9001, kind="card", title="Main Card", currency="USD",
        destination="1111", recipient="Owner"
    )

    purchase = portal.create_purchase_order(101, int(plan["id"]))
    purchase_receipt = portal.submit_receipt(
        101, order_id=int(purchase["id"]),
        payment_method_id=int(method["id"]), reference="BUY-1"
    )
    portal.review_receipt(9001, int(purchase_receipt["id"]), approve=True)

    topup = portal.create_wallet_topup(101, currency="USD", amount=300)
    topup_receipt = portal.submit_receipt(
        101, order_id=int(topup["id"]),
        payment_method_id=int(method["id"]), reference="TOP-1"
    )
    portal.review_receipt(9001, int(topup_receipt["id"]), approve=True)

    portal.create_purchase_order(101, int(plan["id"]))

    summary = portal.financial_summary(9001)
    assert summary["service_sales"]["USD"] == 100
    assert summary["approved_receipts"]["USD"] == 400
    assert summary["wallet_topups"]["USD"] == 300
    assert summary["wallet_balances"]["USD"] == 300
    assert summary["orders_total"] == 3
    assert summary["orders_by_status"]["pending_payment"] == 1
    assert summary["pending_receipts"] == 0


def test_owner_order_admin_supports_filter_search_and_detail(portal, factories) -> None:
    plan = factories.plan(price=120)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Ali Buyer", username="ali_buyer")
    method = portal.add_payment_method(
        9001, kind="card", title="Card", currency="USD",
        destination="1111", recipient="Owner"
    )
    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]),
        payment_method_id=int(method["id"]), reference="REF-ORDER"
    )

    review_page = portal.list_all_orders(9001, status="payment_review")
    assert [int(item["id"]) for item in review_page.items] == [int(order["id"])]

    searched = portal.list_all_orders(9001, query=str(order["public_id"]))
    assert len(searched.items) == 1
    assert searched.items[0]["display_name"] == "Ali Buyer"

    detail = portal.get_order_admin(9001, int(order["id"]))
    assert detail["public_id"] == order["public_id"]
    assert detail["username"] == "ali_buyer"
    assert detail["plan_name"] == plan["name"]
    assert detail["receipts"][0]["id"] == receipt["id"]
    assert detail["receipts"][0]["status"] == "pending"


def test_payment_history_lists_filters_searches_and_details(portal, factories) -> None:
    plan = factories.plan(price=90)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Receipt User", username="receipt_user")
    method = portal.add_payment_method(
        9001, kind="card", title="Primary", currency="USD",
        destination="1111", recipient="Owner"
    )
    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]),
        payment_method_id=int(method["id"]), reference="TRACK-123"
    )

    pending = portal.list_receipts_admin(9001, status="pending")
    assert [int(item["id"]) for item in pending.items] == [int(receipt["id"])]

    searched = portal.list_receipts_admin(9001, query="TRACK-123")
    assert len(searched.items) == 1
    assert searched.items[0]["display_name"] == "Receipt User"

    detail = portal.get_receipt_admin(9001, int(receipt["id"]))
    assert detail["order_id"] == order["id"]
    assert detail["method_title"] == "Primary"
    assert detail["status"] == "pending"

    portal.review_receipt(9001, int(receipt["id"]), approve=True)
    approved = portal.list_receipts_admin(9001, status="approved")
    assert [int(item["id"]) for item in approved.items] == [int(receipt["id"])]


def test_approved_order_records_paid_at_and_finance_uses_it(portal, factories) -> None:
    plan = factories.plan(price=75)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Paid At User")
    method = portal.add_payment_method(
        9001, kind="card", title="Card", currency="USD",
        destination="1111", recipient="Owner"
    )
    order = portal.create_purchase_order(101, int(plan["id"]))
    assert portal.get_order(101, int(order["id"]))["paid_at"] is None

    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]),
        payment_method_id=int(method["id"]), reference="PAID-AT"
    )
    portal.review_receipt(9001, int(receipt["id"]), approve=True)

    paid = portal.get_order(101, int(order["id"]))
    assert paid["paid_at"] is not None
    summary = portal.financial_summary(9001)
    assert summary["service_sales_24h"]["USD"] == 75


def test_rejected_order_does_not_receive_paid_at(portal, factories) -> None:
    plan = factories.plan(price=50)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Rejected User")
    method = portal.add_payment_method(
        9001, kind="card", title="Card", currency="USD",
        destination="1111", recipient="Owner"
    )
    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101, order_id=int(order["id"]),
        payment_method_id=int(method["id"]), reference="REJECT-ME"
    )
    portal.review_receipt(9001, int(receipt["id"]), approve=False)

    rejected = portal.get_order(101, int(order["id"]))
    assert rejected["status"] == "rejected"
    assert rejected["paid_at"] is None


def test_owner_can_adjust_customer_wallet_with_audit(portal) -> None:
    customer = portal.register_customer(101, display_name="Wallet User")
    customer_id = int(customer["id"])

    added = portal.adjust_customer_wallet(
        9001, customer_id, currency="IRR", amount=500000, note="manual credit"
    )
    assert added["balance"] == 500000
    assert portal.wallet_balance(101, "IRR") == 500000

    removed = portal.adjust_customer_wallet(
        9001, customer_id, currency="IRR", amount=-200000, note="manual correction"
    )
    assert removed["balance"] == 300000
    assert portal.wallet_balance(101, "IRR") == 300000

    tx = portal.conn.execute(
        "SELECT * FROM wallet_transactions WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    assert tx is not None
    assert tx["kind"] == "admin"
    assert int(tx["amount"]) == -200000
    assert int(tx["resulting_balance"]) == 300000

    audit = portal.conn.execute(
        "SELECT * FROM audit_events WHERE action = 'wallet.admin_adjust'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit is not None


def test_wallet_adjustment_cannot_overdraw_customer(portal) -> None:
    customer = portal.register_customer(101, display_name="Wallet User")
    customer_id = int(customer["id"])
    portal.adjust_customer_wallet(
        9001, customer_id, currency="USD", amount=10
    )
    with pytest.raises(PaymentStateError):
        portal.adjust_customer_wallet(
            9001, customer_id, currency="USD", amount=-11
        )
    assert portal.wallet_balance(101, "USD") == 10


def test_customer_cannot_adjust_any_wallet(portal) -> None:
    customer = portal.register_customer(101, display_name="Wallet User")
    with pytest.raises(AccessDenied):
        portal.adjust_customer_wallet(
            101, int(customer["id"]), currency="USD", amount=10
        )


def test_owner_can_select_specific_trial_plan(portal, factories) -> None:
    first = factories.plan(name="Trial A", price=100)
    second = factories.plan(name="Trial B", price=200)
    portal.configure_plan_commerce(
        9001, int(first["id"]), currency="USD", is_public=True, trial_days=3
    )
    portal.configure_plan_commerce(
        9001, int(second["id"]), currency="USD", is_public=True, trial_days=7
    )

    settings = portal.set_trial_plan(9001, int(second["id"]))
    assert settings["trial_plan_id"] == int(second["id"])
    assert settings["trial_plan_name"] == "Trial B"

    portal.register_customer(101, display_name="Trial User")
    order = portal.claim_trial(101)
    assert int(order["plan_id"]) == int(second["id"])
    assert int(order["trial_days"]) == 7


def test_trial_plan_must_be_public_active_and_have_trial_days(portal, factories) -> None:
    plan = factories.plan(name="Not Eligible", price=10)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=False, trial_days=3
    )
    with pytest.raises(ValueError):
        portal.set_trial_plan(9001, int(plan["id"]))

    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    with pytest.raises(ValueError):
        portal.set_trial_plan(9001, int(plan["id"]))


def test_selected_trial_plan_fails_closed_when_it_becomes_ineligible(portal, factories) -> None:
    plan = factories.plan(name="Selected Trial", price=10)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=2
    )
    portal.set_trial_plan(9001, int(plan["id"]))
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=False, trial_days=2
    )
    portal.register_customer(101, display_name="Trial User")

    with pytest.raises(CustomerPortalError):
        portal.claim_trial(101)


def test_trial_plan_can_return_to_automatic_selection(portal, factories) -> None:
    plan = factories.plan(name="Auto Trial", price=25)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=4
    )
    portal.set_trial_plan(9001, int(plan["id"]))
    settings = portal.set_trial_plan(9001, None)

    assert settings["trial_plan_id"] is None
    portal.register_customer(101, display_name="Auto User")
    order = portal.claim_trial(101)
    assert int(order["plan_id"]) == int(plan["id"])


def test_customer_can_cancel_only_unpaid_order(portal, factories) -> None:
    plan = factories.plan(price=100)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Cancel User")
    order = portal.create_purchase_order(101, int(plan["id"]))

    cancelled = portal.cancel_order(101, int(order["id"]))
    assert cancelled["status"] == "cancelled"

    with pytest.raises(PaymentStateError):
        portal.cancel_order(101, int(order["id"]))


def test_customer_cannot_cancel_order_after_receipt_submission(portal, factories) -> None:
    plan = factories.plan(price=100)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=0
    )
    portal.register_customer(101, display_name="Review User")
    method = portal.add_payment_method(
        9001, kind="card", title="Card", currency="USD",
        destination="1111", recipient="Owner"
    )
    order = portal.create_purchase_order(101, int(plan["id"]))
    portal.submit_receipt(
        101, order_id=int(order["id"]),
        payment_method_id=int(method["id"]), reference="NO-CANCEL"
    )

    with pytest.raises(PaymentStateError):
        portal.cancel_order(101, int(order["id"]))
