"""SellBot parity for the per-server plans section (fixed/dynamic/mixed).

Covers mode switching persistence, the sample-plans seed (SellBot
add_sample_plans parity), the conditional seed button in the plans root,
and the exact SellBot empty-catalog texts in the UserBot buy flow.
"""

from __future__ import annotations

import pytest

from TenantRuntime.business import TenantBusinessService
from TenantRuntime.server_admin import ServerAdminService
from TenantRuntime.AdminBot import server_actions


@pytest.fixture()
def business(conn, factories, cipher) -> TenantBusinessService:
    tenant = factories.tenant(owner_telegram_id=7001)
    return TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
    )


@pytest.fixture()
def server(business) -> dict:
    return business.add_server(
        7001,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        user_path="user",
    )


def _labels(rows):
    return [[button.text for button in row] for row in rows]


def test_plan_mode_switching_persists(business, server) -> None:
    service = ServerAdminService(business)
    sid = int(server["id"])
    assert service.sales(sid)["mode"] == "fixed"
    for mode in ("dynamic", "mixed", "fixed"):
        service.set_sales(7001, sid, {"mode": mode})
        assert service.sales(sid)["mode"] == mode
    with pytest.raises(ValueError):
        service.set_sales(7001, sid, {"mode": "unknown"})


def test_seed_sample_plans_creates_sellbot_catalog_once(business, server) -> None:
    sid = int(server["id"])
    report = business.seed_sample_plans(7001, server_id=sid)
    assert report["created_categories"] == 2
    assert report["created_plans"] == 3
    assert [p["name"] for p in report["plans"]] == [
        "30 گیگ / 30 روز",
        "60 گیگ / 30 روز",
        "150 گیگ / 90 روز",
    ]
    plans = business.list_plans(public=False, server_id=sid)
    assert len(plans) == 3
    assert all(int(p["price"] or 0) > 0 for p in plans)

    again = business.seed_sample_plans(7001, server_id=sid)
    assert again == {"created_categories": 0, "created_plans": 0, "plans": []}
    assert len(business.list_plans(public=False, server_id=sid)) == 3


def test_plans_root_offers_seed_only_when_fixed_empty(business, server) -> None:
    service = ServerAdminService(business)
    sid = int(server["id"])

    service.set_sales(7001, sid, {"mode": "fixed"})
    _, rows = server_actions._plans_root_view(business, service, 7001, sid)
    assert any(
        button.callback_data == f"srv:seedplans:{sid}"
        for row in rows
        for button in row
    )

    business.seed_sample_plans(7001, server_id=sid)
    _, rows = server_actions._plans_root_view(business, service, 7001, sid)
    assert all(
        button.callback_data != f"srv:seedplans:{sid}"
        for row in rows
        for button in row
    )

    service.set_sales(7001, sid, {"mode": "dynamic"})
    _, rows = server_actions._plans_root_view(business, service, 7001, sid)
    assert all(
        button.callback_data != f"srv:seedplans:{sid}"
        for row in rows
        for button in row
    )


def test_seed_route_is_registered() -> None:
    assert "seedplans" in server_actions.ROUTES


def test_userbot_empty_catalog_texts_match_sellbot() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    assert "❌ پلن ثابت برای این سرور یافت نشد." in source
    assert "❌ دسته‌بندی برای این سرور یافت نشد." in source
