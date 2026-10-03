"""Tenant wallet, coupons, referral rewards and one-time trial coverage."""

from __future__ import annotations

from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, parse_utc, utcnow
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


class GrowthPanel:
    def __init__(self) -> None:
        self.users: dict[tuple[str, str], dict] = {}
        self.fail_next_provision = False
        self.provision_count = 0

    @staticmethod
    def _key(target: PanelTarget, ref: str) -> tuple[str, str]:
        return target.endpoint, ref

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        if self.fail_next_provision:
            self.fail_next_provision = False
            raise PanelError("temporary provisioning failure")
        self.provision_count += 1
        ref = f"growth-{request.server_id}-{request.subscription_id}"
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
        row = self.users[self._key(target, external_ref)]
        row["usage"] = 0
        row["active"] = True
        row["traffic"] = request.traffic_bytes
        row["expires_at"] = request.expires_at
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
        self.users[self._key(target, external_ref)]["active"] = bool(enabled)
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
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
            last_online=None,
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        return f"{target.endpoint}/sub/{external_ref}"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        assert secret
        return f"vless://{external_ref}@edge.example:443?type=tcp#growth"


def _setup(conn, factories, cipher, *, owner=7001, customer=7101):
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = GrowthPanel()
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
        endpoint=f"https://{owner}.example",
        admin_path="admin",
        user_path="user",
    )
    service.set_panel_credential(
        owner, server_id=int(server["id"]), secret="panel-secret"
    )
    service.set_default_server(owner, server_id=int(server["id"]))
    plan = service.add_plan(
        owner,
        name="20GB",
        traffic_gb=20,
        duration_days=30,
        price=100000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="1111",
    )
    user = service.register_customer(
        customer,
        display_name=f"User {customer}",
        username=f"user{customer}",
    )
    return tenant, service, panel, plan, method, user


def test_wallet_full_payment_is_atomic_and_never_double_debits(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, _method, user = _setup(
        conn, factories, cipher
    )
    service.adjust_wallet_admin(
        7001,
        customer_id=int(user["id"]),
        currency="IRR",
        amount=150000,
        note="initial credit",
    )
    order = service.create_order(7101, int(plan["id"]))
    result = service.pay_order_with_wallet(7101, order_id=int(order["id"]))
    assert result["status"] == "active"

    wallet = service.wallet_summary(7101)
    account = next(x for x in wallet["accounts"] if x["currency"] == "IRR")
    assert int(account["balance"]) == 50000
    purchase_txs = [x for x in wallet["history"] if x["kind"] == "purchase"]
    assert len(purchase_txs) == 1
    assert int(purchase_txs[0]["amount"]) == -100000

    with pytest.raises(TenantBusinessError):
        service.pay_order_with_wallet(7101, order_id=int(order["id"]))
    wallet2 = service.wallet_summary(7101)
    assert next(x for x in wallet2["accounts"] if x["currency"] == "IRR")["balance"] == 50000
    assert len([x for x in wallet2["history"] if x["kind"] == "purchase"]) == 1


def test_percent_coupon_applies_once_and_rejection_releases_usage(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method, _user = _setup(
        conn, factories, cipher
    )
    coupon = service.add_coupon(
        7001,
        code="OFF20",
        discount_kind="percent",
        value=20,
        max_uses=1,
        per_customer_limit=1,
    )
    order = service.create_order(7101, int(plan["id"]))
    discounted = service.apply_coupon(
        7101, order_id=int(order["id"]), code="off20"
    )
    assert int(discounted["original_amount"]) == 100000
    assert int(discounted["discount_amount"]) == 20000
    assert int(discounted["amount"]) == 80000
    with pytest.raises(TenantBusinessError):
        service.apply_coupon(7101, order_id=int(order["id"]), code="OFF20")

    receipt = service.submit_receipt(
        7101,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="reject-me",
    )
    service.review_receipt(7001, int(receipt["id"]), approve=False)
    refreshed = service.coupon(int(coupon["id"]))
    assert int(refreshed["used_count"]) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_coupon_redemptions WHERE order_id=?",
        (int(order["id"]),),
    ).fetchone()[0] == 0

    order2 = service.create_order(7101, int(plan["id"]))
    reused = service.apply_coupon(
        7101, order_id=int(order2["id"]), code="OFF20"
    )
    assert int(reused["amount"]) == 80000


def test_admin_coupon_edits_drive_checkout_limits_and_reports(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, _method, _user = _setup(
        conn, factories, cipher
    )
    service.register_customer(
        7202, display_name="Coupon B", username="coupon_b"
    )
    service.register_customer(
        7303, display_name="Coupon C", username="coupon_c"
    )
    coupon = service.add_coupon(
        7001,
        code="OLD10",
        discount_kind="percent",
        value=10,
        max_uses=0,
        per_customer_limit=0,
    )
    updated = service.update_coupon_admin(
        7001,
        coupon_id=int(coupon["id"]),
        code="SAVE25",
        value=25,
        max_uses=2,
        per_customer_limit=1,
        expires_at=iso_utc(utcnow() + timedelta(days=2)),
    )
    assert updated["code"] == "SAVE25"
    assert int(updated["value"]) == 25
    assert int(updated["max_uses"]) == 2
    assert int(updated["per_customer_limit"]) == 1
    assert parse_utc(str(updated["expires_at"])) > utcnow()

    order_a = service.create_order(7101, int(plan["id"]))
    applied_a = service.apply_coupon(
        7101, order_id=int(order_a["id"]), code="save25"
    )
    assert int(applied_a["discount_amount"]) == 25000
    assert int(applied_a["amount"]) == 75000

    second_a = service.create_order(7101, int(plan["id"]))
    with pytest.raises(TenantBusinessError):
        service.apply_coupon(
            7101, order_id=int(second_a["id"]), code="SAVE25"
        )

    order_b = service.create_order(7202, int(plan["id"]))
    service.apply_coupon(7202, order_id=int(order_b["id"]), code="SAVE25")
    assert len(
        service.list_coupon_redemptions_admin(
            7001, coupon_id=int(coupon["id"])
        )
    ) == 2

    order_c = service.create_order(7303, int(plan["id"]))
    with pytest.raises(TenantBusinessError):
        service.apply_coupon(
            7303, order_id=int(order_c["id"]), code="SAVE25"
        )

    cleared = service.update_coupon_admin(
        7001,
        coupon_id=int(coupon["id"]),
        expires_at="0",
    )
    assert cleared["expires_at"] is None
    with pytest.raises(TenantBusinessError):
        service.delete_coupon_admin(7001, coupon_id=int(coupon["id"]))


def test_fixed_coupon_checks_currency_and_limits(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, _method, _user = _setup(
        conn, factories, cipher
    )
    service.add_coupon(
        7001,
        code="FIX25",
        discount_kind="fixed",
        value=25000,
        currency="IRR",
        min_amount=90000,
        max_uses=5,
        per_customer_limit=1,
        expires_at=iso_utc(utcnow() + timedelta(days=1)),
    )
    order = service.create_order(7101, int(plan["id"]))
    applied = service.apply_coupon(
        7101, order_id=int(order["id"]), code="FIX25"
    )
    assert int(applied["amount"]) == 75000


def test_trial_volume_and_duration_settings_drive_real_subscription(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, _plan, _method, user = _setup(
        conn, factories, cipher
    )
    service.update_growth_settings(
        7001,
        trial_enabled=True,
        trial_traffic_gb=3,
        trial_duration_days=5,
    )
    before = utcnow()
    result = service.claim_free_trial(7101)
    claim = conn.execute(
        "SELECT * FROM tenant_trial_claims "
        "WHERE tenant_id=? AND customer_id=?",
        (service.tenant_id, int(user["id"])),
    ).fetchone()
    assert claim is not None
    subscription = conn.execute(
        "SELECT * FROM tenant_subscriptions "
        "WHERE tenant_id=? AND id=?",
        (service.tenant_id, int(claim["subscription_id"])),
    ).fetchone()
    assert subscription is not None
    assert int(subscription["traffic_bytes"]) == 3 * 1024**3
    expires_at = parse_utc(str(subscription["expires_at"]))
    delta_days = (expires_at - before).total_seconds() / 86400.0
    assert 4.9 <= delta_days <= 5.1
    assert result["order_kind"] == "trial"


def test_reset_all_trials_is_strictly_tenant_scoped(
    conn, factories, cipher
) -> None:
    _ta, first, _pa, _pla, _ma, user_a = _setup(
        conn, factories, cipher, owner=7001, customer=7101
    )
    _tb, second, _pb, _plb, _mb, user_b = _setup(
        conn, factories, cipher, owner=8001, customer=8101
    )
    first.update_growth_settings(7001, trial_enabled=True)
    second.update_growth_settings(8001, trial_enabled=True)
    first.claim_free_trial(7101)
    second.claim_free_trial(8101)

    assert first.reset_all_customer_trials_admin(7001) == 1
    assert first.customer_profile_admin(
        7001, customer_id=int(user_a["id"])
    )["trial_used_at"] is None
    assert second.customer_profile_admin(
        8001, customer_id=int(user_b["id"])
    )["trial_used_at"] is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_trial_claims WHERE tenant_id=?",
        (second.tenant_id,),
    ).fetchone()[0] == 1


def test_trial_announcement_setting_roundtrips(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, _plan, _method, _user = _setup(
        conn, factories, cipher
    )
    initial = service.growth_settings(7001)
    assert bool(initial["trial_announce_enabled"]) is True

    disabled = service.update_growth_settings(
        7001,
        trial_enabled=True,
        trial_announce_enabled=False,
        trial_traffic_gb=2,
        trial_duration_days=3,
    )
    assert bool(disabled["trial_enabled"]) is True
    assert bool(disabled["trial_announce_enabled"]) is False
    assert int(disabled["trial_traffic_gb"]) == 2
    assert int(disabled["trial_duration_days"]) == 3

    enabled = service.update_growth_settings(
        7001,
        trial_announce_enabled=True,
    )
    assert bool(enabled["trial_announce_enabled"]) is True


def test_single_and_all_trial_reset_restore_eligibility(
    conn, factories, cipher
) -> None:
    _tenant, service, panel, _plan, _method, first = _setup(
        conn, factories, cipher
    )
    service.update_growth_settings(
        7001,
        trial_enabled=True,
        trial_traffic_gb=1,
        trial_duration_days=1,
    )

    first_claim = service.claim_free_trial(7101)
    assert first_claim["order_kind"] == "trial"
    assert service.customer_profile_admin(
        7001, customer_id=int(first["id"])
    )["trial_used_at"]

    reset = service.reset_customer_trial_admin(
        7001,
        customer_id=int(first["id"]),
    )
    assert reset["trial_used_at"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_trial_claims "
        "WHERE tenant_id=? AND customer_id=?",
        (service.tenant_id, int(first["id"])),
    ).fetchone()[0] == 0

    second_claim_for_first = service.claim_free_trial(7101)
    assert second_claim_for_first["order_kind"] == "trial"

    second = service.register_customer(
        7202,
        display_name="Second Trial User",
        username="second_trial",
    )
    second_claim = service.claim_free_trial(7202)
    assert second_claim["order_kind"] == "trial"
    assert panel.provision_count == 3

    changed = service.reset_all_customer_trials_admin(7001)
    assert changed == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_trial_claims WHERE tenant_id=?",
        (service.tenant_id,),
    ).fetchone()[0] == 0
    rows = conn.execute(
        "SELECT trial_used_at FROM tenant_customers "
        "WHERE tenant_id=? AND id IN (?,?) ORDER BY id",
        (service.tenant_id, int(first["id"]), int(second["id"])),
    ).fetchall()
    assert all(row["trial_used_at"] is None for row in rows)


def test_referral_trial_and_first_purchase_rewards_credit_wallet_once(
    conn, factories, cipher
) -> None:
    _tenant, service, panel, plan, method, inviter = _setup(
        conn, factories, cipher
    )
    invitee = service.register_customer(
        7202, display_name="Invitee", username="invitee"
    )
    service.update_growth_settings(
        7001,
        referral_enabled=True,
        referral_trial_reward=10000,
        referral_purchase_reward=20000,
        referral_min_purchase=50000,
        referral_max_rewards=0,
        referral_currency="IRR",
        trial_enabled=True,
        trial_traffic_gb=1,
        trial_duration_days=1,
    )
    referral = service.register_referral(
        7202, referral_code=str(inviter["referral_code"])
    )
    assert int(referral["inviter_customer_id"]) == int(inviter["id"])
    assert int(referral["invitee_customer_id"]) == int(invitee["id"])

    trial = service.claim_free_trial(7202)
    assert trial["order_kind"] == "trial"
    assert panel.provision_count == 1
    inviter_wallet = service.wallet_summary(7101)
    assert next(x for x in inviter_wallet["accounts"] if x["currency"] == "IRR")["balance"] == 10000
    with pytest.raises(TenantBusinessError):
        service.claim_free_trial(7202)

    order = service.create_order(7202, int(plan["id"]))
    receipt = service.submit_receipt(
        7202,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="first-buy",
    )
    reviewed = service.review_receipt(7001, int(receipt["id"]), approve=True)
    service.fulfill_paid_order(7001, order_id=int(reviewed["order_id"]))
    wallet_after = service.wallet_summary(7101)
    assert next(x for x in wallet_after["accounts"] if x["currency"] == "IRR")["balance"] == 30000

    # A later purchase cannot create another first-purchase reward.
    order2 = service.create_order(7202, int(plan["id"]))
    receipt2 = service.submit_receipt(
        7202,
        order_id=int(order2["id"]),
        method_id=int(method["id"]),
        reference="second-buy",
    )
    service.review_receipt(7001, int(receipt2["id"]), approve=True)
    wallet_final = service.wallet_summary(7101)
    assert next(x for x in wallet_final["accounts"] if x["currency"] == "IRR")["balance"] == 30000
    rewards = conn.execute(
        "SELECT reward_type, COUNT(*) FROM tenant_referral_rewards "
        "WHERE tenant_id=? GROUP BY reward_type ORDER BY reward_type",
        (int(_tenant["id"]),),
    ).fetchall()
    assert {row[0]: row[1] for row in rewards} == {
        "purchase": 1,
        "trial": 1,
    }


def test_failed_trial_retries_same_claim_without_issuing_second_trial(
    conn, factories, cipher
) -> None:
    _tenant, service, panel, _plan, _method, _user = _setup(
        conn, factories, cipher
    )
    service.update_growth_settings(
        7001,
        trial_enabled=True,
        trial_traffic_gb=1,
        trial_duration_days=1,
    )
    panel.fail_next_provision = True
    with pytest.raises(TenantBusinessError):
        service.claim_free_trial(7101)

    claim = conn.execute(
        "SELECT * FROM tenant_trial_claims WHERE tenant_id=?",
        (service.tenant_id,),
    ).fetchone()
    assert claim is not None
    assert claim["status"] == "failed"
    original_order = int(claim["order_id"])

    retried = service.claim_free_trial(7101)
    assert retried["order_id"] == original_order
    assert panel.provision_count == 1
    claim2 = conn.execute(
        "SELECT * FROM tenant_trial_claims WHERE tenant_id=?",
        (service.tenant_id,),
    ).fetchone()
    assert claim2["status"] == "issued"
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_trial_claims WHERE tenant_id=?",
        (service.tenant_id,),
    ).fetchone()[0] == 1


def test_referral_rejects_self_existing_and_cross_tenant_codes(
    conn, factories, cipher
) -> None:
    tenant, service, _panel, _plan, _method, inviter = _setup(
        conn, factories, cipher
    )
    service.register_customer(7202, display_name="Invitee", username="invitee")
    service.update_growth_settings(7001, referral_enabled=True)

    with pytest.raises(TenantBusinessError):
        service.register_referral(
            7101, referral_code=str(inviter["referral_code"])
        )
    first = service.register_referral(
        7202, referral_code=str(inviter["referral_code"])
    )
    duplicate = service.register_referral(
        7202, referral_code=str(inviter["referral_code"])
    )
    assert int(first["id"]) == int(duplicate["id"])

    foreign_tenant, foreign, _fp, _fplan, _fm, foreign_user = _setup(
        conn, factories, cipher, owner=8001, customer=8101
    )
    foreign.update_growth_settings(8001, referral_enabled=True)
    assert int(foreign_tenant["id"]) != int(tenant["id"])
    third = service.register_customer(
        7303, display_name="Third", username="third"
    )
    with pytest.raises(TenantBusinessError):
        service.register_referral(
            7303, referral_code=str(foreign_user["referral_code"])
        )
    assert int(third["tenant_id"]) == int(tenant["id"])


def test_zero_amount_coupon_can_finalize_without_receipt_via_wallet_checkout(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, _method, _user = _setup(
        conn, factories, cipher
    )
    service.add_coupon(
        7001,
        code="FREE100",
        discount_kind="percent",
        value=100,
        max_uses=1,
        per_customer_limit=1,
    )
    order = service.create_order(7101, int(plan["id"]))
    free = service.apply_coupon(
        7101, order_id=int(order["id"]), code="FREE100"
    )
    assert int(free["amount"]) == 0
    result = service.pay_order_with_wallet(7101, order_id=int(order["id"]))
    assert result["status"] == "active"
    wallet = service.wallet_summary(7101)
    assert [x for x in wallet["history"] if x["kind"] == "purchase"] == []

def test_wallet_topup_receipt_credits_once_and_reject_path_does_not_credit(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, _plan, method, _user = _setup(
        conn, factories, cipher
    )
    topup = service.create_wallet_topup(
        7101, amount=75000, currency="IRR"
    )
    receipt = service.submit_wallet_topup_receipt(
        7101,
        topup_id=int(topup["id"]),
        method_id=int(method["id"]),
        reference="topup-paid",
    )
    reviewed = service.review_wallet_topup_receipt(
        7001, receipt_id=int(receipt["id"]), approve=True
    )
    assert reviewed["status"] == "paid"
    wallet = service.wallet_summary(7101)
    assert next(x for x in wallet["accounts"] if x["currency"] == "IRR")["balance"] == 75000
    topup_txs = [x for x in wallet["history"] if x["kind"] == "topup"]
    assert len(topup_txs) == 1
    with pytest.raises(TenantBusinessError):
        service.review_wallet_topup_receipt(
            7001, receipt_id=int(receipt["id"]), approve=True
        )
    assert next(
        x for x in service.wallet_summary(7101)["accounts"]
        if x["currency"] == "IRR"
    )["balance"] == 75000

    rejected = service.create_wallet_topup(
        7101, amount=25000, currency="IRR"
    )
    rejected_receipt = service.submit_wallet_topup_receipt(
        7101,
        topup_id=int(rejected["id"]),
        method_id=int(method["id"]),
        reference="topup-reject",
    )
    service.review_wallet_topup_receipt(
        7001, receipt_id=int(rejected_receipt["id"]), approve=False
    )
    assert next(
        x for x in service.wallet_summary(7101)["accounts"]
        if x["currency"] == "IRR"
    )["balance"] == 75000

def test_cancel_pending_order_releases_coupon_reservation(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, _method, _user = _setup(
        conn, factories, cipher
    )
    coupon = service.add_coupon(
        7001,
        code="CANCEL20",
        discount_kind="percent",
        value=20,
        max_uses=1,
        per_customer_limit=1,
    )
    order = service.create_order(7101, int(plan["id"]))
    service.apply_coupon(
        7101, order_id=int(order["id"]), code="CANCEL20"
    )
    assert int(service.coupon(int(coupon["id"]))["used_count"]) == 1

    cancelled = service.cancel_order(7101, order_id=int(order["id"]))
    assert cancelled["status"] == "cancelled"
    assert int(service.coupon(int(coupon["id"]))["used_count"]) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_coupon_redemptions WHERE order_id=?",
        (int(order["id"]),),
    ).fetchone()[0] == 0

    with pytest.raises(TenantBusinessError):
        service.cancel_order(7101, order_id=int(order["id"]))

