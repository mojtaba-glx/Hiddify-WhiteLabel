"""Phase 10 referral parity: independent rewards, invite text and manual rewards."""

from __future__ import annotations

from types import SimpleNamespace

from TenantRuntime.UserBot import handlers as user_handlers
from Tests.test_tenant_sales_growth import _setup


def _currency_balance(service, actor: int, currency: str = "IRR") -> int:
    return sum(
        int(x.get("balance") or 0)
        for x in service.wallet_summary(actor).get("accounts", [])
        if str(x.get("currency") or "") == currency
    )


def _approve_first_purchase(
    service, actor: int, admin: int, plan_id: int, method_id: int, ref: str
) -> int:
    order = service.create_order(actor, plan_id)
    receipt = service.submit_receipt(
        actor,
        order_id=int(order["id"]),
        method_id=method_id,
        reference=ref,
    )
    reviewed = service.review_receipt(admin, int(receipt["id"]), approve=True)
    service.fulfill_paid_order(admin, order_id=int(reviewed["order_id"]))
    return int(reviewed["order_id"])


def test_independent_trial_and_purchase_reward_switches_are_enforced(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method, inviter = _setup(
        conn, factories, cipher
    )
    service.register_customer(
        7202, display_name="Invitee", username="invitee"
    )
    service.update_growth_settings(
        7001,
        referral_enabled=True,
        referral_trial_reward_enabled=False,
        referral_trial_reward=10000,
        referral_purchase_reward_enabled=True,
        referral_purchase_reward=20000,
        referral_min_purchase=0,
        referral_max_rewards=0,
        referral_currency="IRR",
        trial_enabled=True,
    )
    service.register_referral(
        7202, referral_code=str(inviter["referral_code"])
    )

    service.claim_free_trial(7202)
    assert _currency_balance(service, 7101) == 0

    _approve_first_purchase(
        service,
        7202,
        7001,
        int(plan["id"]),
        int(method["id"]),
        "phase10-buy",
    )
    assert _currency_balance(service, 7101) == 20000
    rewards = service.referral_rewards_admin(7001)
    assert [
        x["reward_type"]
        for x in rewards
        if x["source"] == "automatic"
    ] == ["purchase"]


def test_referral_cap_is_per_reward_type_like_sellbot(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, plan, method, inviter = _setup(
        conn, factories, cipher
    )
    service.register_customer(
        7202, display_name="Invitee", username="invitee"
    )
    service.update_growth_settings(
        7001,
        referral_enabled=True,
        referral_trial_reward_enabled=True,
        referral_trial_reward=10000,
        referral_purchase_reward_enabled=True,
        referral_purchase_reward=20000,
        referral_min_purchase=0,
        referral_max_rewards=1,
        referral_currency="IRR",
        trial_enabled=True,
    )
    service.register_referral(
        7202, referral_code=str(inviter["referral_code"])
    )

    service.claim_free_trial(7202)
    _approve_first_purchase(
        service,
        7202,
        7001,
        int(plan["id"]),
        int(method["id"]),
        "phase10-cap-buy",
    )
    assert _currency_balance(service, 7101) == 30000
    automatic = [
        x for x in service.referral_rewards_admin(7001)
        if x["source"] == "automatic"
    ]
    assert {x["reward_type"] for x in automatic} == {"trial", "purchase"}


def test_custom_invite_text_and_manual_reward_reach_userbot_and_reports(
    conn, factories, cipher
) -> None:
    _tenant, service, _panel, _plan, _method, inviter = _setup(
        conn, factories, cipher
    )
    custom = (
        "دعوت اختصاصی\n"
        "{invite_link}\n"
        "TEST={trial_reward}\n"
        "BUY={purchase_reward}\n"
        "COUNT={referred_count}"
    )
    settings = service.update_growth_settings(
        7001,
        referral_enabled=True,
        referral_trial_reward_enabled=False,
        referral_trial_reward=15000,
        referral_purchase_reward_enabled=True,
        referral_purchase_reward=25000,
        referral_currency="IRR",
        referral_invite_text=custom,
    )
    assert settings["referral_invite_text"] == custom

    reward = service.grant_manual_referral_reward_admin(
        7001,
        customer_id=int(inviter["id"]),
        amount=5000,
        currency="IRR",
        note="phase 10",
    )
    assert reward["reward_type"] == "manual"
    assert _currency_balance(service, 7101) == 5000

    context = SimpleNamespace(
        bot=SimpleNamespace(username="speed_phase10_bot")
    )
    content = user_handlers._referral_content(
        service,
        7101,
        context,
        service.runtime_userbot_settings(),
    )
    assert "https://t.me/speed_phase10_bot?start=ref_" in content["body"]
    assert "TEST=غیرفعال" in content["body"]
    assert "BUY=25,000 IRR" in content["body"]
    assert "پاداش دستی" in content["body"]
    assert "پاداش دستی رفرال" in user_handlers._wallet_text(
        service.wallet_summary(7101)
    )

    rows = service.referral_rewards_admin(7001)
    assert any(
        x["source"] == "manual" and int(x["amount"]) == 5000
        for x in rows
    )
    stats = service.referral_admin_stats(7001)
    assert int(stats["manual_rewards_count"]) == 1
    assert int(stats["manual_rewards_amount"]) == 5000


def test_phase10_admin_referral_controls_are_real() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    business = open(
        "TenantRuntime/business.py",
        encoding="utf-8",
    ).read()
    runtime = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()

    for callback in (
        "userbot:referral:toggle:trial_reward_enabled",
        "userbot:referral:toggle:purchase_reward_enabled",
        "userbot:referral:edit:invite_text",
        "userbot:referral:list:",
        "userbot:referral:rewards:",
        "userbot:referral:manual",
    ):
        assert callback in source
    assert "grant_manual_referral_reward_admin" in business
    assert "referral_invite_text" in runtime
    assert "referral_trial_reward_enabled" in runtime
    assert "referral_purchase_reward_enabled" in runtime
