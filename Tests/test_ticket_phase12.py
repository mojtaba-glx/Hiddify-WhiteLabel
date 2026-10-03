"""Phase 12 tickets: threaded support, media, preview wiring and lifecycle."""

from __future__ import annotations

import pytest

from TenantRuntime.business import TenantBusinessError, TenantBusinessService


def _service(conn, factories, owner=8401):
    tenant = factories.tenant(owner_telegram_id=owner)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
    )
    return tenant, service


def test_ticket_thread_preserves_media_and_user_reply_lifecycle(conn, factories) -> None:
    _, service = _service(conn, factories)
    service.register_customer(501, display_name="Buyer", username="buyer")

    ticket = service.create_ticket(
        501,
        subject="Connection",
        body="My service is not connecting",
        media=b"first-image",
        media_mime="image/jpeg",
    )
    assert ticket["status"] == "open"

    messages = service.ticket_messages(501, ticket_id=int(ticket["id"]))
    assert len(messages) == 1
    assert messages[0]["sender_type"] == "user"
    assert messages[0]["has_media"] is True
    media = service.ticket_message_media(
        501,
        ticket_id=int(ticket["id"]),
        message_id=int(messages[0]["id"]),
    )
    assert media is not None
    assert media["media"] == b"first-image"

    ticket = service.reply_ticket(
        501,
        ticket_id=int(ticket["id"]),
        reply="More details",
        media=b"second-image",
        media_mime="image/jpeg",
    )
    assert ticket["status"] == "open"
    assert len(service.ticket_messages(501, ticket_id=int(ticket["id"]))) == 2

    service.close_ticket(501, ticket_id=int(ticket["id"]))
    with pytest.raises(TenantBusinessError):
        service.reply_ticket(
            501,
            ticket_id=int(ticket["id"]),
            reply="should fail",
        )
    reopened = service.reopen_ticket(501, ticket_id=int(ticket["id"]))
    assert reopened["status"] == "open"


def test_admin_reply_moves_ticket_to_open_bucket_and_can_reopen(conn, factories) -> None:
    _, service = _service(conn, factories)
    customer = service.register_customer(
        502, display_name="Support User", username=None
    )
    ticket = service.create_ticket(
        502,
        subject="Billing",
        body="Please check my payment",
    )
    tid = int(ticket["id"])

    pending = service.list_tickets_admin(8401, status="pending")
    assert [int(x["id"]) for x in pending] == [tid]

    answered = service.reply_ticket_admin(
        8401,
        ticket_id=tid,
        reply="Checked and fixed",
        media=b"admin-image",
        media_mime="image/jpeg",
        admin_name="Support",
    )
    assert answered["status"] == "answered"
    assert service.list_tickets_admin(8401, status="pending") == []
    open_items = service.list_tickets_admin(8401, status="open")
    assert [int(x["id"]) for x in open_items] == [tid]

    messages = service.ticket_messages_admin(8401, ticket_id=tid)
    assert [x["sender_type"] for x in messages] == ["user", "admin"]
    admin_media = service.ticket_message_media_admin(
        8401,
        ticket_id=tid,
        message_id=int(messages[-1]["id"]),
    )
    assert admin_media is not None
    assert admin_media["media"] == b"admin-image"

    closed = service.set_ticket_status_admin(
        8401, ticket_id=tid, status="closed"
    )
    assert closed["status"] == "closed"
    reopened = service.set_ticket_status_admin(
        8401, ticket_id=tid, status="open"
    )
    assert reopened["status"] == "open"
    assert int(reopened["customer_id"]) == int(customer["id"])


def test_ticket_access_is_tenant_scoped(conn, factories) -> None:
    _, first = _service(conn, factories, owner=8401)
    _, second = _service(conn, factories, owner=8402)
    first.register_customer(601, display_name="First", username=None)
    second.register_customer(602, display_name="Second", username=None)
    ticket = first.create_ticket(601, subject="A", body="tenant one")

    with pytest.raises(TenantBusinessError):
        second.ticket_admin(8402, ticket_id=int(ticket["id"]))


def test_ticket_ui_wires_sellbot_preview_edit_send_and_support_text() -> None:
    user_source = open(
        "TenantRuntime/UserBot/handlers.py", encoding="utf-8"
    ).read()
    admin_source = open(
        "TenantRuntime/AdminBot/userbot_management.py", encoding="utf-8"
    ).read()

    for value in (
        "ticket_panel_text",
        "📬تیکت‌های من",
        "📩ایجاد تیکت",
        "▶️رد کردن",
        "✅ارسال",
        "✏️ویرایش",
        'callback_data=f"shop:ticketflow:{clean}:send"',
        'callback_data=f"shop:ticketflow:{clean}:edit"',
        "shop:ticketreply:",
        "shop:ticketclose:",
        "shop:ticketmedia:",
    ):
        assert value in user_source

    for value in (
        "📨تیکت‌های در انتظار",
        "📬تیکت‌های باز",
        "📩تیکت‌های بسته",
        "📪 بستن تیکت",
        "📬 باز کردن تیکت",
        "userbot:ticketreply:skip",
        "userbot:ticketreply:send",
        "userbot:ticketreply:edit",
        "ticket_message_media_admin",
    ):
        assert value in admin_source
