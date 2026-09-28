"""Master storefront settings and payment administration regression tests."""

from __future__ import annotations

import pytest

from MasterBot.customer_service import CustomerPortalError, CustomerPortalService
from MasterBot.service import MasterService
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, generate_key


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
