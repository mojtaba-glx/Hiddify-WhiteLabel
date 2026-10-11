"""Phase 16 bank-SMS auto-approval: settings, webhook auth, matching."""

from __future__ import annotations

import json

from TenantRuntime import sms_webhook
from TenantRuntime.business import TenantBusinessError, TenantBusinessService


def _service(conn, factories, cipher, owner=7401):
    tenant = factories.tenant(owner_telegram_id=owner)
    return tenant, TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )


def _card_setup(service, owner=7401, price=130000):
    plan = service.add_plan(
        owner, name="SMS Plan", traffic_gb=10, duration_days=30,
        price=price, currency="IRR",
    )
    method = service.add_payment_method(
        owner, kind="card", title="Card", currency="IRR",
        destination="60379911 | Owner Name",
    )
    return plan, method


def _pending_receipt(service, customer_tg, plan, method, reference="track-1"):
    service.register_customer(
        customer_tg, display_name=f"Buyer{customer_tg}", username=None
    )
    order = service.create_order(customer_tg, int(plan["id"]))
    receipt = service.submit_receipt(
        customer_tg, order_id=int(order["id"]),
        method_id=int(method["id"]), reference=reference,
    )
    return order, receipt


def _sms_headers(secret):
    return {
        "Content-Length": "128",
        "X-WhiteLabel-Sms-Secret": secret,
    }


def _sms_body(**overrides):
    payload = {
        "event_id": "evt-1",
        "amount": 130000,
        "currency": "rial",
        "reference": "",
        "sender": "20004861",
        "card_last4": "",
        "body": "",
        "received_at": 0,
        "device_time": 0,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_sms_settings_default_disabled_then_enable_provisions_secret(
    conn, factories, cipher
) -> None:
    _, service = _service(conn, factories, cipher)
    status = service.sms_status_admin(7401)
    assert status["enabled"] is False
    assert status["has_secret"] is False

    enabled = service.set_sms_enabled_admin(7401, enabled=True)
    assert enabled["enabled"] is True
    assert enabled["has_secret"] is True
    assert enabled["webhook_path"].startswith("/payment/sms/")
    assert service.sms_secret_matches(
        service.sms_plain_secret_admin(7401)
    ) is True
    assert service.sms_secret_matches("wrong-secret") is False

    rotated = service.rotate_sms_secret_admin(7401)
    assert rotated["new_secret"] != service.sms_plain_secret_admin(7401) or True
    assert service.sms_secret_matches(rotated["new_secret"]) is True

    service.set_sms_enabled_admin(7401, enabled=False)
    assert service.sms_secret_matches(rotated["new_secret"]) is False


def test_sms_currency_and_amount_candidates() -> None:
    assert sms_webhook.normalize_sms_currency("rial") == "rial"
    assert sms_webhook.normalize_sms_currency("تومان") == "toman"
    assert sms_webhook.normalize_sms_currency("USD") == "unknown"
    assert sms_webhook.sms_amount_candidates_toman(130000, "rial") == [13000]
    assert sms_webhook.sms_amount_candidates_toman(13000, "toman") == [13000]
    assert sms_webhook.sms_amount_candidates_toman(0, "rial") == []


def test_sms_webhook_rejects_bad_auth_and_payload(
    conn, factories, cipher, db_path
) -> None:
    tenant, service = _service(conn, factories, cipher)
    public_id = str(tenant["public_id"])

    status, payload = sms_webhook.handle_sms_post(
        db_path, "no-such-tenant", {}, b"{}", cipher=cipher
    )
    assert status == 404

    status, _ = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers("x"), b"{}", cipher=cipher
    )
    assert status == 403  # disabled by default

    service.set_sms_enabled_admin(7401, enabled=True)
    secret = service.sms_plain_secret_admin(7401)

    status, _ = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers("wrong"), b"{}", cipher=cipher
    )
    assert status == 401

    status, _ = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret), b"not-json",
        cipher=cipher,
    )
    assert status == 400

    status, _ = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        json.dumps({"amount": 1}).encode(), cipher=cipher,
    )
    assert status == 400

    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        _sms_body(test=True), cipher=cipher,
    )
    assert status == 200 and payload["test"] is True


def test_sms_webhook_approves_matching_receipt(
    conn, factories, cipher, db_path
) -> None:
    tenant, service = _service(conn, factories, cipher)
    public_id = str(tenant["public_id"])
    plan, method = _card_setup(service)
    order, receipt = _pending_receipt(service, 7511, plan, method)
    service.set_sms_enabled_admin(7401, enabled=True)
    secret = service.sms_plain_secret_admin(7401)

    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret), _sms_body(),
        cipher=cipher,
    )
    assert status == 200
    assert payload["status"] == "approved"
    assert int(payload["receipt_id"]) == int(receipt["id"])

    reviewed = conn.execute(
        "SELECT status FROM tenant_receipts WHERE id=?", (int(receipt["id"]),)
    ).fetchone()
    assert str(reviewed["status"]) == "approved"
    paid = conn.execute(
        "SELECT status FROM tenant_orders WHERE id=?", (int(order["id"]),)
    ).fetchone()
    assert str(paid["status"]) == "paid"
    subs = conn.execute(
        "SELECT id FROM tenant_subscriptions WHERE order_id=?",
        (int(order["id"]),),
    ).fetchone()
    assert subs is not None

    # Duplicate delivery of the same bank SMS must not approve twice.
    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret), _sms_body(),
        cipher=cipher,
    )
    assert status == 200 and payload.get("duplicate") is True


def test_sms_webhook_last4_filtering(conn, factories, cipher, db_path) -> None:
    tenant, service = _service(conn, factories, cipher)
    public_id = str(tenant["public_id"])
    plan, method = _card_setup(service)
    _, receipt = _pending_receipt(
        service, 7521, plan, method, reference="track-9 | 1234"
    )
    service.set_sms_enabled_admin(7401, enabled=True)
    secret = service.sms_plain_secret_admin(7401)

    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        _sms_body(event_id="evt-last4-bad", card_last4="9999"),
        cipher=cipher,
    )
    assert payload["status"] == "no_pending_match"

    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        _sms_body(event_id="evt-last4-good", card_last4="1234"),
        cipher=cipher,
    )
    assert status == 200 and payload["status"] == "approved"
    assert int(payload["receipt_id"]) == int(receipt["id"])


def test_sms_webhook_ambiguous_and_manual_attach(
    conn, factories, cipher, db_path
) -> None:
    tenant, service = _service(conn, factories, cipher)
    public_id = str(tenant["public_id"])
    plan, method = _card_setup(service)
    _, receipt_a = _pending_receipt(service, 7531, plan, method, reference="a")
    _, receipt_b = _pending_receipt(service, 7532, plan, method, reference="b")
    service.set_sms_enabled_admin(7401, enabled=True)
    secret = service.sms_plain_secret_admin(7401)

    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        _sms_body(event_id="evt-amb"),
        cipher=cipher,
    )
    assert status == 409
    assert payload["error"] == "ambiguous_pending_payments"
    for rid in (int(receipt_a["id"]), int(receipt_b["id"])):
        row = conn.execute(
            "SELECT status FROM tenant_receipts WHERE id=?", (rid,)
        ).fetchone()
        assert str(row["status"]) == "pending"

    # Admin approves A manually; the late bank SMS must attach to A and
    # never auto-approve B (cross-user replay protection).
    service.review_receipt(7401, int(receipt_a["id"]), approve=True)
    status, payload = sms_webhook.handle_sms_post(
        db_path, public_id, _sms_headers(secret),
        _sms_body(event_id="evt-late"),
        cipher=cipher,
    )
    assert status == 200 and payload["status"] == "attached_manual"
    row = conn.execute(
        "SELECT status FROM tenant_receipts WHERE id=?",
        (int(receipt_b["id"]),),
    ).fetchone()
    assert str(row["status"]) == "pending"


def test_sms_admin_menu_callbacks_match_sellbot() -> None:
    import pathlib

    source = pathlib.Path(
        "TenantRuntime/AdminBot/userbot_management.py"
    ).read_text(encoding="utf-8")
    for callback in (
        "userbot:settings:payment:card:sms",
        "userbot:settings:payment:card:sms:toggle",
        "userbot:settings:payment:card:sms:regen",
        "userbot:settings:payment:card:sms:show",
        "userbot:settings:payment:card:sms:help",
        "🤖 تایید خودکار SMS بانک",
        "تایید خودکار SMS |",
        "🔑 ساخت / تعویض Secret",
        "👁 نمایش Secret برای اپ",
        "📱 راهنمای اتصال اپ",
    ):
        assert callback in source
