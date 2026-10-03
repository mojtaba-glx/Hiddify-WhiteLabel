"""Phase 5 subscription-link separation and UserBot parity tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from TenantRuntime.panels import PanelError
from TenantRuntime.smart_subscription import smart_subscription_url


class LinkPanel:
    def subscription_link(self, *, target, external_ref: str) -> str:
        del target
        return f"https://native.example/sub/{external_ref}"

    def subscription_content(self, *, target, secret: str, external_ref: str) -> str:
        del target, secret
        return f"vless://{external_ref}@example.com:443?type=ws"


def _service(conn, factories, cipher):
    tenant = factories.tenant(owner_telegram_id=7001)
    panel = LinkPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    server = service.add_server(
        7001,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://panel.example",
        user_path="user",
    )
    plan = service.add_plan(
        7001,
        name="50G",
        traffic_gb=50,
        duration_days=30,
        price=100000,
        currency="IRR",
    )
    customer = service.register_customer(
        7101,
        display_name="Link User",
        username="link_user",
    )
    order = service.create_order(
        7101,
        int(plan["id"]),
        server_id=int(server["id"]),
    )
    now = iso_utc(utcnow())
    cursor = conn.execute(
        "INSERT INTO tenant_subscriptions "
        "(tenant_id,customer_id,plan_id,order_id,server_id,external_ref,status,"
        "usage_bytes,traffic_bytes,expires_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
        (
            service.tenant_id,
            int(customer["id"]),
            int(plan["id"]),
            int(order["id"]),
            int(server["id"]),
            "sub-user-1",
            5 * 1024**3,
            50 * 1024**3,
            iso_utc(utcnow() + timedelta(days=20)),
            now,
            now,
        ),
    )
    conn.commit()
    return service, int(cursor.lastrowid or 0)


def test_native_panel_and_smart_links_are_independent(
    conn, factories, cipher
) -> None:
    service, subscription_id = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        7001,
        key="smart_base_url",
        value="https://smart.example/base",
    )

    panel_page = service.panel_user_page_link(
        7101, subscription_id=subscription_id
    )
    native = service.subscription_link(
        7101, subscription_id=subscription_id
    )
    native_b64 = service.subscription_link_b64(
        7101, subscription_id=subscription_id
    )
    automatic = service.automatic_subscription_link(
        7101, subscription_id=subscription_id
    )
    smart = service.smart_subscription_link(
        7101, subscription_id=subscription_id, base64_output=False
    )
    smart_b64 = service.smart_subscription_link(
        7101, subscription_id=subscription_id, base64_output=True
    )

    assert panel_page == "https://panel.example/user/sub-user-1"
    assert native == "https://native.example/sub/sub-user-1"
    assert native_b64 == "https://native.example/sub/sub-user-1?base64=1"
    assert automatic == (
        "https://panel.example/user/sub-user-1/sub/?asn=unknown"
    )
    assert smart.startswith("https://smart.example/base/sub/")
    assert smart.endswith("/all.txt")
    assert smart_b64.startswith("https://smart.example/base/sub/")
    assert smart_b64.endswith("/all.b64")
    assert native != smart


def test_subscription_identity_is_available_to_runtime(
    conn, factories, cipher
) -> None:
    service, _subscription_id = _service(conn, factories, cipher)
    item = service.list_subscriptions(7101)[0]
    assert item["customer_username"] == "link_user"
    assert item["customer_display_name"] == "Link User"


def test_smart_base_url_validation_is_not_applied_to_native_link(
    conn, factories, cipher
) -> None:
    service, subscription_id = _service(conn, factories, cipher)
    with pytest.raises(ValueError):
        service.set_userbot_setting_admin(
            7001,
            key="smart_base_url",
            value="javascript:alert(1)",
        )
    assert service.subscription_link(
        7101, subscription_id=subscription_id
    ) == "https://native.example/sub/sub-user-1"


def test_smart_url_builder_has_distinct_text_and_b64_outputs() -> None:
    plain = smart_subscription_url(
        "https://smart.example",
        "abc",
        base64_output=False,
    )
    encoded = smart_subscription_url(
        "https://smart.example",
        "abc",
        base64_output=True,
    )
    assert plain == "https://smart.example/sub/abc/all.txt"
    assert encoded == "https://smart.example/sub/abc/all.b64"


def test_delivery_output_respects_independent_visibility_switches(
    conn, factories, cipher
) -> None:
    from TenantRuntime.UserBot import handlers as user_handlers

    service, subscription_id = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        7001,
        key="smart_base_url",
        value="https://smart.example",
    )
    result = {
        "id": subscription_id,
        "subscription_url": "https://must-not-be-read-directly.example",
    }

    settings = service.runtime_userbot_settings()
    visible = user_handlers._delivery_access_text(
        service, 7101, result, settings
    )
    assert "https://native.example/sub/sub-user-1" in visible
    assert "https://must-not-be-read-directly.example" not in visible
    assert "کانفیگ مستقیم" in visible

    for key in (
        "show_user_page_link",
        "show_direct_config",
        "show_auto_sub_link",
        "show_sub_link",
        "show_sub_link_b64",
        "show_multi_server",
        "show_multi_server_b64",
    ):
        service.set_userbot_setting_admin(7001, key=key, value=False)

    hidden = user_handlers._delivery_access_text(
        service,
        7101,
        result,
        service.runtime_userbot_settings(),
    )
    assert "https://" not in hidden
    assert "غیرفعال است" in hidden


def test_userbot_runtime_wires_all_subscription_link_controls() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    for key in (
        "show_direct_config",
        "show_auto_sub_link",
        "show_sub_link",
        "show_sub_link_b64",
        "show_multi_server",
        "show_multi_server_b64",
        "show_user_page_link",
        "show_username",
    ):
        assert key in source
    for callback in (
        "shop:configmenu:",
        "shop:configs:",
        "shop:autosub:",
        "shop:sublink:",
        "shop:subb64:",
        "shop:smart:",
        "shop:smartb64:",
    ):
        assert callback in source
    assert "show_smart_link" not in source


def test_admin_has_all_sellbot_subscription_visibility_controls() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "sub_link_status:show_direct_config",
        "sub_link_status:show_auto_sub_link",
        "sub_link_status:show_sub_link",
        "sub_link_status:show_sub_link_b64",
        "sub_link_status:show_multi_server",
        "sub_link_status:show_multi_server_b64",
    ):
        assert callback in source
    assert "این مقدار فقط روی Smart Link اثر دارد" in source
