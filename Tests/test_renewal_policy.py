"""End-to-end renewal policy tests for tenant UserBot/AdminBot."""

from __future__ import annotations

from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, parse_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import PanelUserResult, RenewRequest


GIB = 1024 ** 3


class RenewalPanel:
    def __init__(self) -> None:
        self.renew_requests: list[RenewRequest] = []

    def renew(self, *, target, secret: str, external_ref: str, request: RenewRequest):
        assert secret
        self.renew_requests.append(request)
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=(0 if request.reset_usage else 10 * GIB),
            active=True,
            traffic_bytes=request.traffic_bytes,
            expires_at=request.expires_at,
            last_online=iso_utc(utcnow()),
            subscription_url=f"{target.endpoint}/sub/{external_ref}",
        )


def _service(conn, factories, cipher, *, panel=None):
    tenant = factories.tenant(owner_telegram_id=7001)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    return tenant, service


def _active_subscription(
    conn,
    service: TenantBusinessService,
    *,
    owner: int = 7001,
    customer: int = 7101,
    expires_days: int = 10,
    traffic_gb: int = 50,
    usage_gb: int = 10,
):
    server = service.add_server(
        owner,
        label="TR",
        panel_kind="hiddify",
        endpoint="https://tr.example",
    )
    service.set_panel_credential(
        owner,
        server_id=int(server["id"]),
        secret="renew-api-key",
    )
    base_plan = service.add_plan(
        owner,
        name="Base 50G",
        traffic_gb=traffic_gb,
        duration_days=30,
        price=100000,
        currency="IRR",
    )
    customer_row = service.register_customer(
        customer,
        display_name="Renew Buyer",
        username="renew_buyer",
    )
    purchase = service.create_order(
        customer,
        int(base_plan["id"]),
        server_id=int(server["id"]),
    )
    now = iso_utc(utcnow())
    expires = iso_utc(utcnow() + timedelta(days=expires_days))
    cursor = conn.execute(
        "INSERT INTO tenant_subscriptions "
        "(tenant_id,customer_id,plan_id,order_id,server_id,external_ref,status,"
        "usage_bytes,traffic_bytes,expires_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
        (
            service.tenant_id,
            int(customer_row["id"]),
            int(base_plan["id"]),
            int(purchase["id"]),
            int(server["id"]),
            "remote-renew-1",
            int(usage_gb) * GIB,
            int(traffic_gb) * GIB,
            expires,
            now,
            now,
        ),
    )
    conn.commit()
    subscription_id = int(cursor.lastrowid or 0)
    return {
        "subscription_id": subscription_id,
        "server_id": int(server["id"]),
        "plan_id": int(base_plan["id"]),
        "expires_at": expires,
    }


def test_policy_profiles_map_to_expected_rollover_modes(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)

    fair = service.set_renewal_policy_admin(7001, policy="fair")
    assert fair["renew_policy"] == "fair"
    assert fair["renew_volume_mode"] == "add"
    assert fair["renew_time_mode"] == "add"

    default = service.set_renewal_policy_admin(7001, policy="default")
    assert default["renew_policy"] == "default"
    assert default["renew_volume_mode"] == "add"
    assert default["renew_time_mode"] == "reset"

    advanced = service.set_renewal_policy_admin(7001, policy="advanced")
    assert advanced["renew_policy"] == "advanced"
    assert advanced["renew_volume_mode"] == "reset"
    assert advanced["renew_time_mode"] == "reset"

    overridden = service.set_renewal_rollover_admin(
        7001,
        kind="volume",
        mode="add",
    )
    assert overridden["renew_volume_mode"] == "add"


def test_advanced_policy_requires_near_expiry_or_low_remaining_volume(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    seeded = _active_subscription(conn, service)
    subscription_id = int(seeded["subscription_id"])

    service.set_renewal_policy_admin(7001, policy="advanced")
    blocked = service.renewal_eligibility(
        7101,
        subscription_id=subscription_id,
    )
    assert blocked["allowed"] is False
    assert blocked["reason"] == "advanced_limits"

    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=?",
        (
            iso_utc(utcnow() + timedelta(days=1)),
            subscription_id,
        ),
    )
    conn.commit()
    near_time = service.renewal_eligibility(
        7101,
        subscription_id=subscription_id,
    )
    assert near_time["allowed"] is True

    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=?, usage_bytes=? WHERE id=?",
        (
            iso_utc(utcnow() + timedelta(days=10)),
            49 * GIB,
            subscription_id,
        ),
    )
    conn.commit()
    low_volume = service.renewal_eligibility(
        7101,
        subscription_id=subscription_id,
    )
    assert low_volume["allowed"] is True


@pytest.mark.parametrize("policy", ["default", "fair"])
def test_default_and_fair_allow_renewal_without_advanced_limits(
    conn, factories, cipher, policy
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    seeded = _active_subscription(conn, service)
    service.set_renewal_policy_admin(7001, policy=policy)

    result = service.renewal_eligibility(
        7101,
        subscription_id=int(seeded["subscription_id"]),
    )
    assert result["allowed"] is True
    assert result["policy"] == policy


def test_renewal_order_snapshots_modes_against_later_admin_changes(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    seeded = _active_subscription(conn, service)
    renew_plan = service.add_plan(
        7001,
        name="Renew 20G",
        traffic_gb=20,
        duration_days=30,
        price=80000,
        currency="IRR",
    )

    service.set_renewal_policy_admin(7001, policy="fair")
    order = service.create_renewal_order(
        7101,
        subscription_id=int(seeded["subscription_id"]),
        plan_id=int(renew_plan["id"]),
    )
    snapshot = conn.execute(
        "SELECT renew_volume_mode,renew_time_mode "
        "FROM tenant_renewal_orders WHERE order_id=?",
        (int(order["id"]),),
    ).fetchone()
    assert snapshot["renew_volume_mode"] == "add"
    assert snapshot["renew_time_mode"] == "add"

    service.set_renewal_policy_admin(7001, policy="advanced")
    unchanged = conn.execute(
        "SELECT renew_volume_mode,renew_time_mode "
        "FROM tenant_renewal_orders WHERE order_id=?",
        (int(order["id"]),),
    ).fetchone()
    assert unchanged["renew_volume_mode"] == "add"
    assert unchanged["renew_time_mode"] == "add"


def test_fulfillment_uses_snapshotted_add_add_modes(
    conn, factories, cipher
) -> None:
    panel = RenewalPanel()
    _tenant, service = _service(conn, factories, cipher, panel=panel)
    seeded = _active_subscription(conn, service)
    old_expiry = parse_utc(str(seeded["expires_at"]))
    renew_plan = service.add_plan(
        7001,
        name="Renew Add 20G",
        traffic_gb=20,
        duration_days=30,
        price=80000,
        currency="IRR",
    )

    service.set_renewal_policy_admin(7001, policy="fair")
    order = service.create_renewal_order(
        7101,
        subscription_id=int(seeded["subscription_id"]),
        plan_id=int(renew_plan["id"]),
    )
    # A later admin change must not alter the already-created order.
    service.set_renewal_policy_admin(7001, policy="advanced")
    conn.execute(
        "UPDATE tenant_orders SET status='paid',paid_at=?,updated_at=? WHERE id=?",
        (iso_utc(utcnow()), iso_utc(utcnow()), int(order["id"])),
    )
    conn.commit()

    result = service.fulfill_paid_order(7001, order_id=int(order["id"]))
    assert result["operation"] == "renewal"
    assert result["renew_volume_mode"] == "add"
    assert result["renew_time_mode"] == "add"
    assert len(panel.renew_requests) == 1

    request = panel.renew_requests[0]
    assert request.traffic_bytes == 70 * GIB
    assert request.reset_usage is False
    assert request.reset_time is False
    target_expiry = parse_utc(request.expires_at)
    assert abs((target_expiry - (old_expiry + timedelta(days=30))).total_seconds()) < 2

    subscription = service.list_subscriptions(7101)[0]
    assert int(subscription["traffic_bytes"]) == 70 * GIB
    assert service.order(7101, int(order["id"]))["status"] == "fulfilled"


def test_unlimited_flags_change_subscription_presentation_only(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        7001, key="renew_unlimited_volume", value=True
    )
    service.set_userbot_setting_admin(
        7001, key="renew_unlimited_volume_from_gb", value=1000
    )
    service.set_userbot_setting_admin(
        7001, key="renew_unlimited_time", value=True
    )
    service.set_userbot_setting_admin(
        7001, key="renew_unlimited_time_from_days", value=365
    )
    settings = service.runtime_userbot_settings()

    from TenantRuntime.UserBot import handlers as user_handlers

    usage_text, expiry_text = user_handlers._subscription_limit_lines(
        {
            "usage_bytes": 12 * GIB,
            "traffic_bytes": 1000 * GIB,
            "expires_at": iso_utc(utcnow() + timedelta(days=400)),
        },
        settings,
    )
    assert usage_text == "12.00/نامحدود"
    assert expiry_text == "نامحدود"


def test_advanced_policy_blocks_order_creation_not_just_ui(
    conn, factories, cipher
) -> None:
    _tenant, service = _service(conn, factories, cipher)
    seeded = _active_subscription(conn, service)
    renew_plan = service.add_plan(
        7001,
        name="Blocked Renew",
        traffic_gb=20,
        duration_days=30,
        price=80000,
        currency="IRR",
    )
    service.set_renewal_policy_admin(7001, policy="advanced")

    with pytest.raises(
        TenantBusinessError,
        match="renewal policy does not allow renewal",
    ):
        service.create_renewal_order(
            7101,
            subscription_id=int(seeded["subscription_id"]),
            plan_id=int(renew_plan["id"]),
        )
