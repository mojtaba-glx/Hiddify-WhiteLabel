"""Phase 19 missing-service sweep: probe states, 7-day TTL, vpn names."""

from __future__ import annotations

import asyncio
import re

import pytest

from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import PanelError


class FakePanel:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.deleted: list[str] = []

    def get_user(self, *, target, secret: str, external_ref: str):
        if self.mode == "missing":
            raise PanelError("panel returned HTTP 404")
        if self.mode == "offline":
            raise PanelError("panel connection failed")
        return object()

    def delete_user(self, *, target, secret: str, external_ref: str) -> None:
        if self.mode == "missing":
            raise PanelError("panel returned HTTP 404")
        if self.mode == "offline":
            raise PanelError("panel connection failed")
        self.deleted.append(str(external_ref))


def _service(conn, factories, cipher, owner=7901):
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = FakePanel()
    service = TenantBusinessService(
        conn, tenant_id=int(tenant["id"]), owner_telegram_id=owner,
        secret_cipher=cipher, panel_adapter=panel,
    )
    return tenant, service, panel


def _provisioned_sub(conn, service, panel, owner=7901, customer_tg=7911):
    server = service.add_server(
        owner, label="TR", panel_kind="hiddify",
        endpoint="https://panel.example",
    )
    service.set_panel_credential(owner, server_id=int(server["id"]), secret="k")
    plan = service.add_plan(
        owner, name="Plan", traffic_gb=10, duration_days=30,
        price=100000, currency="IRR",
    )
    method = service.add_payment_method(
        owner, kind="card", title="Card", currency="IRR", destination="6037"
    )
    service.register_customer(customer_tg, display_name="Buyer", username=None)
    order = service.create_order(customer_tg, int(plan["id"]))
    receipt = service.submit_receipt(
        customer_tg, order_id=int(order["id"]),
        method_id=int(method["id"]), reference="r",
    )
    service.review_receipt(owner, int(receipt["id"]), approve=True)
    sub = service.list_subscriptions(customer_tg)[0]
    conn.execute(
        "UPDATE tenant_subscriptions SET server_id=?, external_ref=?,"
        " status='active' WHERE id=?",
        (int(server["id"]), "11111111-2222-3333-4444-555555555555",
         int(sub["id"])),
    )
    conn.execute(
        "INSERT INTO tenant_subscription_nodes (tenant_id, subscription_id,"
        " server_id, external_ref, is_primary, status, created_at, updated_at)"
        " VALUES (?,?,?,?,1,'active','2026-01-01','2026-01-01')",
        (service.tenant_id, int(sub["id"]), int(server["id"]),
         "11111111-2222-3333-4444-555555555555"),
    )
    conn.commit()
    return sub


def test_probe_states_exist_missing_unreachable() -> None:
    import TenantRuntime.UserBot.handlers as handlers

    class Target:
        pass

    assert handlers._probe_subscription_state(
        FakePanel("ok"), [(Target(), "s", "ref")]) == "exists"
    assert handlers._probe_subscription_state(
        FakePanel("missing"), [(Target(), "s", "ref")]) == "missing"
    assert handlers._probe_subscription_state(
        FakePanel("offline"), [(Target(), "s", "ref")]) == "unreachable"
    # Found anywhere wins over missing elsewhere.
    assert handlers._probe_subscription_state(
        FakePanel("ok"), [(Target(), "s", "r1")]) == "exists"


def test_missing_streak_and_seven_day_purge(conn, factories, cipher) -> None:
    _, service, panel = _service(conn, factories, cipher)
    sub = _provisioned_sub(conn, service, panel)
    sid = int(sub["id"])

    info = service.mark_service_missing(sid)
    assert info["missing_streak"] == 1
    info = service.mark_service_missing(sid)
    assert info["missing_streak"] == 2
    assert service._older_than_days(info["first_missing_at"], 7) is False

    service.mark_service_seen(sid)
    row = conn.execute(
        "SELECT missing_streak, first_missing_at FROM tenant_service_probe"
        " WHERE subscription_id=?", (sid,),
    ).fetchone()
    assert int(row["missing_streak"]) == 0
    assert row["first_missing_at"] is None

    # Backdate the streak past the TTL, then purge like the sweeper does.
    conn.execute(
        "UPDATE tenant_service_probe SET first_missing_at='2020-01-01 00:00:00',"
        " missing_streak=9 WHERE subscription_id=?", (sid,),
    )
    conn.commit()
    info = service.mark_service_missing(sid)
    assert service._older_than_days(info["first_missing_at"], 7) is True
    result = service.purge_missing_subscription(sid)
    assert result["status"] == "deleted"
    assert conn.execute(
        "SELECT id FROM tenant_subscriptions WHERE id=?", (sid,)
    ).fetchone() is None
    # Money history survives the purge.
    assert conn.execute("SELECT COUNT(*) FROM tenant_orders").fetchone()[0] >= 1


def test_filter_hides_missing_keeps_unreachable_and_pending(
    conn, factories, cipher, monkeypatch
) -> None:
    import TenantRuntime.panels as panels
    import TenantRuntime.UserBot.handlers as handlers

    _, service, panel = _service(conn, factories, cipher)
    _provisioned_sub(conn, service, panel)
    sub = service.list_subscriptions(7911)[0]
    settings = {"missing_service_delete_days": 7}

    service.panel_adapter = FakePanel("missing")
    visible = asyncio.run(handlers._filter_existing_subscriptions(
        service, 7911, [dict(sub)], settings
    ))
    assert visible == []
    row = conn.execute(
        "SELECT missing_streak FROM tenant_service_probe WHERE subscription_id=?",
        (int(sub["id"]),),
    ).fetchone()
    assert int(row["missing_streak"]) == 1

    service.panel_adapter = FakePanel("offline")
    visible = asyncio.run(handlers._filter_existing_subscriptions(
        service, 7911, [dict(sub)], settings
    ))
    assert visible == []

    service.panel_adapter = FakePanel("ok")
    visible = asyncio.run(handlers._filter_existing_subscriptions(
        service, 7911, [dict(sub)], settings
    ))
    assert [int(x["id"]) for x in visible] == [int(sub["id"])]

    # Pending/unprovisioned rows always stay visible.
    pending = {"id": 999, "status": "pending_provisioning"}
    visible = asyncio.run(handlers._filter_existing_subscriptions(
        service, 7911, [pending], settings
    ))
    assert visible == [pending]


def test_new_subscriptions_get_vpn_random_names(conn, factories, cipher) -> None:
    _, service, _ = _service(conn, factories, cipher)
    plan = service.add_plan(
        7901, name="Plan", traffic_gb=10, duration_days=30,
        price=1000, currency="IRR",
    )
    method = service.add_payment_method(
        7901, kind="card", title="Card", currency="IRR", destination="1"
    )
    names = set()
    for tg in (7921, 7922):
        service.register_customer(tg, display_name="B", username=None)
        order = service.create_order(tg, int(plan["id"]))
        receipt = service.submit_receipt(
            tg, order_id=int(order["id"]),
            method_id=int(method["id"]), reference="r",
        )
        service.review_receipt(7901, int(receipt["id"]), approve=True)
        sub = service.list_subscriptions(tg)[0]
        name = str(sub.get("service_name") or "")
        assert re.fullmatch(r"vpn-\d{6}", name), name
        names.add(name)
    assert len(names) == 2


def test_admin_panel_detail_helpers() -> None:
    from TenantRuntime.AdminBot import handlers as admin_handlers

    assert admin_handlers._format_usage_current(0) == "0.0"
    assert admin_handlers._format_usage_current(1.5) == "1.5"
    assert admin_handlers._format_usage_limit(20.0) == "20.0"
    from datetime import date

    expected_days = (date(2099, 1, 1) - date.today()).days
    assert admin_handlers._panel_expiry_line(
        "2099-01-01T00:00:00+00:00"
    ) == f"📆انقضا: {expected_days} روز دیگر"
    assert "منقضی شده" in admin_handlers._panel_expiry_line(
        "2020-01-01T00:00:00+00:00"
    )
    assert admin_handlers._panel_expiry_line(None) == "📆انقضا: نامشخص"
    from datetime import datetime, timedelta, timezone

    two_hours_ago = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    ).strftime("%Y-%m-%d %H:%M:%S")
    assert "ساعت پیش" in admin_handlers._relative_last_online(two_hours_ago)
    assert "نامشخص" in admin_handlers._relative_last_online("")
    assert admin_handlers._display_safe_note(
        "HiddifyBot:6119169885"
    ) == "HiddifyBot:۶۱۱۹۱۶۹۸۸۵"
    assert admin_handlers._display_safe_note("") == "—"
    assert admin_handlers._panel_user_page_url(
        {"endpoint": "https://user.speedll.ir", "admin_path": "bn9IfiVD8d"},
        "a24674c6-0391-42bd-9d1d-b15a37f609e0",
    ) == "https://user.speedll.ir/bn9IfiVD8d/a24674c6-0391-42bd-9d1d-b15a37f609e0/"
