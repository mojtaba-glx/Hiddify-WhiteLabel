"""SellBot parity for the per-server plans section (fixed/dynamic/mixed).

Covers mode switching persistence, the sample-plans seed (SellBot
add_sample_plans parity), the conditional seed button in the plans root,
and the exact SellBot empty-catalog texts in the UserBot buy flow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from TenantRuntime.business import TenantBusinessService
from TenantRuntime.server_admin import ServerAdminService
from TenantRuntime.AdminBot import server_actions
from TenantRuntime.UserBot import handlers as user_handlers
from TenantRuntime.UserBot import dynamic_plans
from Tests.test_server_connection_parity import Messages, Query
from Tests.test_user_account_status_phase6 import _service


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


def test_dynamic_direct_mode_opens_builder_for_dynamic_and_mixed() -> None:
    priced_dynamic = {
        "mode": "dynamic", "price_gb": 1000, "price_day": 0,
        "price_month": 30000, "pricing_model": "sellbot_month",
    }
    assert dynamic_plans._dynamic_direct_mode(priced_dynamic) is True
    assert dynamic_plans._dynamic_direct_mode({**priced_dynamic, "mode": "mixed"}) is True
    assert dynamic_plans._dynamic_direct_mode({**priced_dynamic, "mode": "fixed"}) is False
    assert dynamic_plans._dynamic_direct_mode(
        {**priced_dynamic, "price_gb": 0, "price_month": 0}
    ) is False


def _userbot_ui(monkeypatch, business):
    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7101),
        effective_message=message,
        effective_chat=message,
        callback_query=None,
    )
    monkeypatch.setattr(
        user_handlers,
        "_services",
        lambda _: (SimpleNamespace(role="user"), None, None, business),
    )
    return update, context, message


async def _userbot_click(update, context, data):
    update.callback_query = Query(data, update.effective_message)
    await user_handlers.on_callback(update, context)
    update.callback_query = None


def _flat_callbacks(message):
    return [
        button.callback_data
        for text, kwargs in message.sent
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


def test_mixed_location_opens_builder_with_ready_plans_row(
    monkeypatch, conn, factories, cipher
) -> None:
    """SellBot mixed screen: builder + ready-plans row under confirm."""
    service, _, _, _, _ = _service(conn, factories, cipher)
    server = service.list_servers()[0]
    sid = int(server["id"])
    admin = ServerAdminService(service)
    admin.set_sales(7001, sid, {"mode": "mixed", "price_gb": 1000})

    update, context, message = _userbot_ui(monkeypatch, service)

    async def run():
        await _userbot_click(update, context, f"shop:buyloc:{sid}")

    asyncio.run(run())
    text, kwargs = message.sent[-1]
    # SellBot mixed header, not the plain dynamic builder header.
    assert text.startswith("🛒 **خرید اشتراک**")
    assert "بسته دلخواه خود را بسازید یا از پلن‌های آماده استفاده کنید" in text
    rows = kwargs["reply_markup"].inline_keyboard
    labels = [[button.text for button in row] for row in rows]
    callbacks = [[button.callback_data for button in row] for row in rows]
    flat_labels = [label for row in labels for label in row]
    flat_callbacks = [cb for row in callbacks for cb in row]
    # Confirm row, then ready-plans row, then back row (SellBot order).
    confirm_at = flat_labels.index("💳 تایید و خرید")
    ready_at = flat_labels.index("📋 پلن های آماده")
    back_at = flat_labels.index("🔙 بازگشت")
    assert confirm_at < ready_at < back_at
    assert flat_callbacks[ready_at] == f"shop:mixed:{sid}:fixed"
    assert flat_callbacks[back_at] == "shop:buy"


def test_mixed_ready_plans_opens_flat_fixed_list(
    monkeypatch, conn, factories, cipher
) -> None:
    service, _, _, _, _ = _service(conn, factories, cipher)
    server = service.list_servers()[0]
    sid = int(server["id"])
    admin = ServerAdminService(service)
    admin.set_sales(7001, sid, {"mode": "mixed", "price_gb": 1000})

    update, context, message = _userbot_ui(monkeypatch, service)

    async def run():
        await _userbot_click(update, context, f"shop:mixed:{sid}:fixed")

    asyncio.run(run())
    text, kwargs = message.sent[-1]
    assert "پلن" in text
    callbacks = _flat_callbacks(message)
    assert any(cb.startswith("shop:plan:") for cb in callbacks)
    assert "shop:buy" in callbacks
    assert not any(cb.startswith("shop:dynamic:") for cb in callbacks)


def test_mixed_ready_plans_alerts_when_not_mixed(
    monkeypatch, conn, factories, cipher
) -> None:
    service, _, _, _, _ = _service(conn, factories, cipher)
    server = service.list_servers()[0]
    sid = int(server["id"])
    admin = ServerAdminService(service)
    admin.set_sales(7001, sid, {"mode": "fixed"})

    answers: list[tuple] = []

    class AlertQuery(Query):
        async def answer(self, *args, **kwargs):
            answers.append((args, kwargs))

    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7101),
        effective_message=message,
        effective_chat=message,
        callback_query=None,
    )
    monkeypatch.setattr(
        user_handlers,
        "_services",
        lambda _: (SimpleNamespace(role="user"), None, None, service),
    )
    settings = service.runtime_userbot_settings()

    async def run():
        update.callback_query = AlertQuery(f"shop:mixed:{sid}:fixed", message)
        handled = await dynamic_plans.handle_callback(
            update, context, business=service, actor=7101, settings=settings
        )
        assert handled is True

    asyncio.run(run())
    assert answers and answers[0][1].get("show_alert") is True
    assert "ترکیبی" in answers[0][0][0]


def test_dynamic_location_still_opens_builder_directly(
    monkeypatch, conn, factories, cipher
) -> None:
    service, _, _, _, _ = _service(conn, factories, cipher)
    server = service.list_servers()[0]
    sid = int(server["id"])
    admin = ServerAdminService(service)
    admin.set_sales(7001, sid, {"mode": "dynamic", "price_gb": 1000})

    update, context, message = _userbot_ui(monkeypatch, service)

    async def run():
        await _userbot_click(update, context, f"shop:buyloc:{sid}")

    asyncio.run(run())
    text, _ = message.sent[-1]
    assert text == "📦بسته مورد نیاز خود را جهت خرید تنظیم کنید"


def test_fixed_location_hides_dynamic_entry(
    monkeypatch, conn, factories, cipher
) -> None:
    service, _, _, _, _ = _service(conn, factories, cipher)
    server = service.list_servers()[0]
    sid = int(server["id"])
    admin = ServerAdminService(service)
    admin.set_sales(7001, sid, {"mode": "fixed"})

    update, context, message = _userbot_ui(monkeypatch, service)

    async def run():
        await _userbot_click(update, context, f"shop:buyloc:{sid}")

    asyncio.run(run())
    callbacks = _flat_callbacks(message)
    assert not any(cb.startswith("shop:dynamic:") for cb in callbacks)
    assert any(cb.startswith("shop:plan:") or cb.startswith("shop:buycat:") for cb in callbacks)
