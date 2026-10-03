"""Phase 6 customer account, live status, usage and order tests."""

from __future__ import annotations

from datetime import timedelta

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.UserBot import handlers as user_handlers
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import PanelTarget, PanelUserResult, UsageResult


class StatusPanel:
    def __init__(self) -> None:
        self.usage_bytes = 3 * 1024**3
        self.active = True
        self.last_online = iso_utc(utcnow() - timedelta(minutes=7))
        self.disabled: list[tuple[str, bool]] = []

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        assert secret
        return UsageResult(
            usage_bytes=int(self.usage_bytes),
            active=bool(self.active),
            last_online=self.last_online,
        )

    def set_enabled(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        enabled: bool,
    ) -> PanelUserResult:
        assert secret
        self.active = bool(enabled)
        self.disabled.append((external_ref, bool(enabled)))
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=int(self.usage_bytes),
            active=bool(enabled),
            traffic_bytes=10 * 1024**3,
            expires_at=iso_utc(utcnow() + timedelta(days=10)),
            last_online=self.last_online,
            subscription_url=f"{target.endpoint}/user/{external_ref}/all.txt",
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        return f"{target.endpoint}/user/{external_ref}/all.txt"


def _service(conn, factories, cipher):
    owner = 7001
    actor = 7101
    tenant = factories.tenant(owner_telegram_id=owner)
    panel = StatusPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server = service.add_server(
        owner,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        user_path="user",
    )
    service.set_panel_credential(
        owner,
        server_id=int(server["id"]),
        secret="panel-key",
    )
    plan = service.add_plan(
        owner,
        name="10GB Monthly",
        traffic_gb=10,
        duration_days=30,
        price=120000,
        currency="IRR",
    )
    customer = service.register_customer(
        actor,
        display_name="Status User",
        username="status_user",
    )
    order = service.create_order(
        actor,
        int(plan["id"]),
        server_id=int(server["id"]),
    )
    now = iso_utc(utcnow())
    cursor = conn.execute(
        "INSERT INTO tenant_subscriptions "
        "(tenant_id,customer_id,plan_id,order_id,server_id,external_ref,status,"
        "usage_bytes,traffic_bytes,expires_at,last_online,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?)",
        (
            service.tenant_id,
            int(customer["id"]),
            int(plan["id"]),
            int(order["id"]),
            int(server["id"]),
            "status-user-1",
            1 * 1024**3,
            10 * 1024**3,
            iso_utc(utcnow() + timedelta(days=10)),
            iso_utc(utcnow() - timedelta(hours=1)),
            now,
            now,
        ),
    )
    conn.commit()
    return service, panel, actor, int(cursor.lastrowid or 0), int(order["id"])


def test_live_customer_status_refreshes_usage_and_last_online(
    conn, factories, cipher
) -> None:
    service, panel, actor, subscription_id, _order_id = _service(
        conn, factories, cipher
    )
    item = service.customer_subscription_status(
        actor,
        subscription_id=subscription_id,
        refresh=True,
    )
    assert int(item["usage_bytes"]) == 3 * 1024**3
    assert item["status"] == "active"
    assert item["last_online"] == panel.last_online
    assert item["server_label"] == "Turkey"
    assert item["plan_name"] == "10GB Monthly"


def test_account_refresh_moves_depleted_service_to_expired(
    conn, factories, cipher
) -> None:
    service, panel, actor, _subscription_id, _order_id = _service(
        conn, factories, cipher
    )
    panel.usage_bytes = 10 * 1024**3
    summary = service.customer_account_summary(actor, refresh=True)
    assert summary["subscriptions"]["active"] == 0
    assert summary["subscriptions"]["expired"] == 1
    assert panel.disabled and panel.disabled[-1][1] is False


def test_account_summary_contains_real_order_counters(
    conn, factories, cipher
) -> None:
    service, _panel, actor, _subscription_id, order_id = _service(
        conn, factories, cipher
    )
    summary = service.customer_account_summary(actor)
    assert summary["orders_total"] == 1
    assert summary["pending_orders"] == 1
    order = service.customer_order(actor, order_id=order_id)
    assert order["plan_name"] == "10GB Monthly"
    assert int(order["plan_traffic_gb"]) == 10
    assert int(order["plan_duration_days"]) == 30


def test_show_username_changes_profile_after_subscription_detail_removed(
    conn, factories, cipher
) -> None:
    service, _panel, actor, subscription_id, _order_id = _service(
        conn, factories, cipher
    )
    summary = service.customer_account_summary(actor)

    visible = service.runtime_userbot_settings()
    profile_visible = user_handlers._user_account_text(summary, visible)
    service_menu = user_handlers._subscription_menu_text()
    assert "@status_user" in profile_visible
    assert "@status_user" not in service_menu

    service.set_userbot_setting_admin(
        7001, key="show_username", value=False
    )
    hidden = service.runtime_userbot_settings()
    assert "@status_user" not in user_handlers._user_account_text(
        summary, hidden
    )
    assert "@status_user" not in user_handlers._subscription_menu_text()


def test_phase6_runtime_has_active_expired_and_service_detail_routes() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "shop:subs:active",
        "shop:subs:expired",
        "shop:substatus:",
        "shop:orders",
        "shop:order:",
        "shop:account",
    ):
        assert callback in source
    for label in (
        "کانفیگ ها📝",
        "تمدید اشتراک♾",
        "تغییر نام اشتراک✏️",
        "تغییر لینک اشتراک🚨",
    ):
        assert label in source
    for removed in (
        "📊 میزان استفاده",
        "📥 حجم باقی‌مانده",
        "📅 تاریخ انقضا",
        "🕓 آخرین اتصال",
    ):
        assert removed not in source


def test_show_user_status_is_enforced_beyond_main_keyboard() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    status_start = source.index("async def show_status")
    callback_start = source.index("async def on_callback", status_start)
    status_source = source[status_start:callback_start]
    assert 'settings.get("show_user_status", True)' in status_source
    assert "توسط ادمین غیرفعال است" in status_source
    assert "Runtime: ready" not in status_source
    assert "لایسنس:" not in status_source


def test_status_helpers_keep_business_metrics_after_detail_ui_removed(
    conn, factories, cipher
) -> None:
    service, _panel, actor, subscription_id, _order_id = _service(
        conn, factories, cipher
    )
    item = service.customer_subscription_status(
        actor,
        subscription_id=subscription_id,
        refresh=False,
    )
    metrics = user_handlers._subscription_metrics(
        item,
        service.runtime_userbot_settings(),
    )
    assert user_handlers._subscription_status_label(item) == "🟢 فعال"
    assert metrics["usage_gb"] == 1.0
    assert metrics["limit_gb"] == 10.0
    assert metrics["remaining_gb"] == 9.0
    assert "پیش" in user_handlers._last_online_text(item.get("last_online"))


def test_customer_status_and_order_reads_are_owner_scoped(
    conn, factories, cipher
) -> None:
    service, _panel, _actor, subscription_id, order_id = _service(
        conn, factories, cipher
    )
    service.register_customer(
        7202,
        display_name="Other User",
        username="other_user",
    )
    try:
        service.customer_subscription_status(
            7202,
            subscription_id=subscription_id,
            refresh=False,
        )
    except TenantBusinessError:
        pass
    else:
        raise AssertionError("foreign subscription became readable")

    try:
        service.customer_order(7202, order_id=order_id)
    except TenantBusinessError:
        pass
    else:
        raise AssertionError("foreign order became readable")
