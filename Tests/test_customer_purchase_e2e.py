"""Offline end-to-end customer purchase and provisioning flows."""

from __future__ import annotations

import asyncio

from Database.repositories import BotRepository, LicenseRepository, TenantRuntimeRepository
from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import BotIdentity, MasterService
from Shared.crypto import FernetTokenCipher, generate_key
from Shared.timeutils import parse_utc
from Tests.conftest import make_fake_token


class MappingVerifier:
    def __init__(self) -> None:
        self.identities: dict[str, BotIdentity] = {}

    async def verify(self, token: str) -> BotIdentity:
        identity = self.identities.get(token)
        if identity is None:
            raise RuntimeError("token rejected")
        return identity


def _portal(conn):
    verifier = MappingVerifier()
    master = MasterService(
        conn,
        master_admin_id=9001,
        cipher=FernetTokenCipher(generate_key()),
        bot_verifier=verifier,
    )
    return CustomerPortalService(conn, master_service=master), verifier


def _bot_tokens(verifier: MappingVerifier) -> tuple[str, str]:
    admin = make_fake_token("e2e-admin")
    user = make_fake_token("e2e-user")
    verifier.identities[admin] = BotIdentity(880001, "shop_admin_bot")
    verifier.identities[user] = BotIdentity(880002, "shop_user_bot")
    return admin, user


def test_paid_purchase_runs_from_storefront_to_ready_tenant(conn, factories) -> None:
    portal, verifier = _portal(conn)
    plan = factories.plan(name="Commercial", price=500, duration_days=30)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=3
    )
    portal.register_customer(101, display_name="Real Customer", username="real_customer")
    method = portal.add_payment_method(
        9001,
        kind="card",
        title="Main Card",
        currency="USD",
        destination="1111222233334444",
        recipient="Store Owner",
    )

    order = portal.create_purchase_order(101, int(plan["id"]))
    receipt = portal.submit_receipt(
        101,
        order_id=int(order["id"]),
        payment_method_id=int(method["id"]),
        reference="E2E-PAID",
    )
    reviewed = portal.review_receipt(9001, int(receipt["id"]), approve=True)
    assert reviewed["order_status"] == "paid"

    admin_token, user_token = _bot_tokens(verifier)
    admin = asyncio.run(
        portal.prepare_customer_bot(
            101, order_id=int(order["id"]), role="admin", plain_token=admin_token
        )
    )
    user = asyncio.run(
        portal.prepare_customer_bot(
            101, order_id=int(order["id"]), role="user", plain_token=user_token
        )
    )
    result = portal.provision_paid_order(
        101,
        order_id=int(order["id"]),
        name="Customer Brand",
        slug="store-101-e2e",
        admin_bot=admin,
        user_bot=user,
    )

    fulfilled = portal.get_order(101, int(order["id"]))
    assert fulfilled["status"] == "fulfilled"
    assert int(fulfilled["tenant_id"]) == int(result.provisioning.tenant_id)
    assert fulfilled["paid_at"] is not None

    tenant_id = int(result.provisioning.tenant_id)
    runtime = TenantRuntimeRepository(conn).get_by_tenant(tenant_id)
    assert runtime is not None and runtime["status"] == "ready"

    bots = BotRepository(conn).list_by_tenant(tenant_id)
    assert {row["role"] for row in bots} == {"admin", "user"}
    assert {row["telegram_username"] for row in bots} == {
        "shop_admin_bot", "shop_user_bot"
    }

    license_row = LicenseRepository(conn).latest_by_tenant(tenant_id)
    assert license_row is not None
    assert license_row["status"] == "active"
    assert int(license_row["plan_id"]) == int(plan["id"])
    assert parse_utc(str(license_row["expires_at"])) > parse_utc(str(license_row["starts_at"]))

    dump = "\n".join(conn.iterdump())
    assert admin_token not in dump
    assert user_token not in dump


def test_selected_trial_runs_to_ready_tenant_with_trial_duration(conn, factories) -> None:
    portal, verifier = _portal(conn)
    plan = factories.plan(name="Trial Product", price=900, duration_days=30)
    portal.configure_plan_commerce(
        9001, int(plan["id"]), currency="USD", is_public=True, trial_days=2
    )
    portal.set_trial_plan(9001, int(plan["id"]))
    portal.register_customer(202, display_name="Trial Customer")

    order = portal.claim_trial(202)
    assert order["kind"] == "trial"
    assert order["status"] == "paid"
    assert int(order["amount"]) == 0

    admin_token, user_token = _bot_tokens(verifier)
    # Give the second flow unique Telegram identities.
    verifier.identities[admin_token] = BotIdentity(881001, "trial_admin_bot")
    verifier.identities[user_token] = BotIdentity(881002, "trial_user_bot")

    admin = asyncio.run(
        portal.prepare_customer_bot(
            202, order_id=int(order["id"]), role="admin", plain_token=admin_token
        )
    )
    user = asyncio.run(
        portal.prepare_customer_bot(
            202, order_id=int(order["id"]), role="user", plain_token=user_token
        )
    )
    result = portal.provision_paid_order(
        202,
        order_id=int(order["id"]),
        name="Trial Brand",
        slug="store-202-trial",
        admin_bot=admin,
        user_bot=user,
    )

    license_row = result.license
    seconds = (
        parse_utc(str(license_row["expires_at"]))
        - parse_utc(str(license_row["starts_at"]))
    ).total_seconds()
    assert seconds == 2 * 24 * 60 * 60

    fulfilled = portal.get_order(202, int(order["id"]))
    assert fulfilled["status"] == "fulfilled"
    assert int(fulfilled["tenant_id"]) == int(result.provisioning.tenant_id)
