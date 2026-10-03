"""Phase 13 broadcast/channel parity: targeting, media bridge, audit and UI."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.AdminBot import broadcast_channel as phase13
from TenantRuntime.business import TenantBusinessService
from Tests.test_user_account_status_phase6 import _service


class FakeMessage:
    def __init__(self, *, text: str = "", text_html: str = "") -> None:
        self.text = text
        self.text_html = text_html or text
        self.caption = ""
        self.caption_html = ""
        self.photo = []
        self.video = None
        self.document = None
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.sent.append((text, kwargs))


def _insert_subscription(
    service,
    conn,
    *,
    customer_id: int,
    plan_id: int,
    order_id: int,
    expires_at: str,
    status: str = "expired",
) -> int:
    now = iso_utc(utcnow())
    cur = conn.execute(
        "INSERT INTO tenant_subscriptions "
        "(tenant_id,customer_id,plan_id,order_id,status,traffic_bytes,"
        "expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            service.tenant_id,
            customer_id,
            plan_id,
            order_id,
            status,
            10 * 1024**3,
            expires_at,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_broadcast_segments_match_sellbot_and_stay_tenant_scoped(
    conn, factories, cipher
):
    service, _panel, actor, sid, _ = _service(conn, factories, cipher)
    active = service.customer_subscription_status(
        actor, subscription_id=sid, refresh=False
    )
    owner = service.owner_telegram_id

    # Same customer also has an old expired service. Because their maximum
    # expiry is still in the future, SellBot must NOT classify them expired.
    order_old = service.create_order(
        actor,
        int(active["plan_id"]),
        server_id=int(active["server_id"]),
    )
    _insert_subscription(
        service,
        conn,
        customer_id=int(active["customer_id"]),
        plan_id=int(active["plan_id"]),
        order_id=int(order_old["id"]),
        expires_at=iso_utc(utcnow() - timedelta(days=30)),
    )

    expired_user = service.register_customer(
        7102, display_name="Expired", username=None
    )
    expired_order = service.create_order(
        7102,
        int(active["plan_id"]),
        server_id=int(active["server_id"]),
    )
    _insert_subscription(
        service,
        conn,
        customer_id=int(expired_user["id"]),
        plan_id=int(active["plan_id"]),
        order_id=int(expired_order["id"]),
        expires_at=iso_utc(utcnow() - timedelta(days=10)),
    )

    service.register_customer(
        7103, display_name="No Order", username=None
    )

    other_tenant = factories.tenant(owner_telegram_id=8001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(other_tenant["id"]),
        owner_telegram_id=8001,
        secret_cipher=cipher,
    )
    other.register_customer(
        8101, display_name="Foreign", username=None
    )

    stats = service.broadcast_stats_admin(owner)
    assert stats == {
        "total_users": 3,
        "expired_users": 1,
        "no_order_users": 1,
        "expired_1w_users": 1,
        "expired_2w_users": 0,
        "expired_4w_users": 0,
        "expired_8w_users": 0,
    }
    assert {
        int(x["telegram_user_id"])
        for x in service.broadcast_targets_admin(
            owner, segment="expired_all"
        )
    } == {7102}
    assert {
        int(x["telegram_user_id"])
        for x in service.broadcast_targets_admin(
            owner, segment="no_order"
        )
    } == {7103}
    assert 8101 not in {
        int(x["telegram_user_id"])
        for x in service.broadcast_targets_admin(owner, segment="all")
    }


def test_phase13_broadcast_audit_supports_video_and_delivery_details(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    run_id = service.start_broadcast_run_admin(
        owner,
        segment="all",
        message_kind="video",
        target_count=9,
        buttons_count=2,
    )
    service.finish_broadcast_run_admin(
        owner,
        run_id=run_id,
        sent=7,
        failed=2,
        recovered=1,
        unreachable=1,
        temporary=1,
    )
    row = conn.execute(
        "SELECT * FROM tenant_broadcast_runs "
        "WHERE tenant_id=? AND id=?",
        (service.tenant_id, run_id),
    ).fetchone()
    assert row is not None
    assert row["message_kind"] == "video"
    assert row["buttons_count"] == 2
    assert row["sent_count"] == 7
    assert row["failed_count"] == 2
    assert row["recovered_count"] == 1
    assert row["unreachable_count"] == 1
    assert row["temporary_count"] == 1
    assert row["finished_at"]


def test_broadcast_menu_shows_sellbot_stats_and_exact_segments() -> None:
    text = phase13._broadcast_stats_text({
        "total_users": 170,
        "expired_users": 31,
        "no_order_users": 7,
        "expired_1w_users": 20,
        "expired_2w_users": 15,
        "expired_4w_users": 8,
        "expired_8w_users": 2,
    })
    assert "◈ تعداد کاربران تلگرام: 170" in text
    assert "◈ تعداد کاربران منقضی: 31" in text
    markup = phase13._broadcast_segment_keyboard()
    labels = [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]
    assert labels[:-1] == list(phase13.SEGMENT_LABELS.values())
    assert labels[-1] == "🔙بازگشت"


def test_broadcast_video_crosses_admin_to_userbot_token_and_keeps_button(
    monkeypatch, conn, factories, cipher
):
    service, _panel, actor, _sid, _ = _service(
        conn, factories, cipher
    )
    factories.bot(service.tenant_id, "user", cipher)
    sent_calls: list[tuple[str, dict]] = []

    class AdminFile:
        async def download_as_bytearray(self):
            return bytearray(b"video-bytes")

    class AdminBot:
        async def get_file(self, file_id):
            assert file_id == "admin-video-id"
            return AdminFile()

    class FakeUserBot:
        def __init__(self, token):
            assert token
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def send_video(self, **kwargs):
            sent_calls.append(("video", kwargs))
            return SimpleNamespace(
                video=SimpleNamespace(file_id="userbot-video-id")
            )
        async def send_message(self, **kwargs):
            sent_calls.append(("text", kwargs))
            return SimpleNamespace(message_id=1)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(phase13, "Bot", FakeUserBot)
    monkeypatch.setattr(phase13.asyncio, "sleep", no_sleep)
    context = SimpleNamespace(bot=AdminBot())
    draft = {
        "segment": "all",
        "kind": "video",
        "text": "<b>سلام</b>",
        "file_id": "admin-video-id",
        "buttons": [{
            "text": "ورود",
            "url": "https://t.me/example_bot",
            "style": "primary",
        }],
    }

    result = asyncio.run(
        phase13._send_broadcast(
            context,
            service,
            service.owner_telegram_id,
            draft,
        )
    )
    assert result["sent"] == 1
    assert result["failed"] == 0
    assert sent_calls[0][0] == "video"
    assert hasattr(sent_calls[0][1]["video"], "read")
    markup = sent_calls[0][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "ورود"
    assert markup.inline_keyboard[0][0].url == "https://t.me/example_bot"

    audit = conn.execute(
        "SELECT * FROM tenant_broadcast_runs "
        "WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
        (service.tenant_id,),
    ).fetchone()
    assert audit["message_kind"] == "video"
    assert audit["buttons_count"] == 1
    assert audit["sent_count"] == 1


def test_broadcast_long_media_text_is_sent_separately() -> None:
    assert phase13._visible_html_length("<b>abc</b>") == 3
    long_text = "x" * (phase13.MAX_CAPTION_LENGTH + 1)
    phase13._validate_body("video", long_text)
    try:
        phase13._validate_body(
            "text", "x" * (phase13.MAX_TEXT_LENGTH + 1)
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Telegram text length guard was bypassed")


def test_channel_helpers_validate_target_and_preserve_edit_choices() -> None:
    assert phase13._normalize_channel_target("@speedll_channel") == (
        "@speedll_channel"
    )
    assert phase13._normalize_channel_target(
        "https://t.me/speedll_channel"
    ) == "@speedll_channel"
    assert phase13._normalize_channel_target(
        "-1001234567890"
    ) == "-1001234567890"
    assert phase13._normalize_channel_target("not a channel") == ""

    text_draft = {
        "kind": "text", "text": "x", "file_id": "", "buttons": []
    }
    labels = [
        button.text
        for row in phase13._channel_edit_markup(text_draft).inline_keyboard
        for button in row
    ]
    assert "🖼 افزودن عکس / ویدئو" in labels
    assert "🔄 جایگزینی کامل پست" in labels


def test_channel_replace_keeps_existing_buttons(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    message = FakeMessage(text="متن جدید", text_html="<b>متن جدید</b>")
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={
        phase13.CHANNEL_DRAFT_KEY: {
            "kind": "photo",
            "text": "قبلی",
            "file_id": "old-photo",
            "buttons": [{
                "text": "خرید",
                "url": "https://example.com",
                "style": "primary",
            }],
        },
        phase13.FLOW_KEY: {"kind": "channel_replace"},
    })

    def admin_keyboard():
        return "ADMIN"

    handled = asyncio.run(
        phase13.handle_text(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=admin_keyboard,
        )
    )
    assert handled
    draft = context.user_data[phase13.CHANNEL_DRAFT_KEY]
    assert draft["kind"] == "text"
    assert draft["text"] == "<b>متن جدید</b>"
    assert draft["buttons"][0]["text"] == "خرید"
    assert phase13.FLOW_KEY not in context.user_data


def test_result_text_distinguishes_success_partial_and_empty() -> None:
    assert phase13._broadcast_result_text({
        "sent": 0, "failed": 0
    }).startswith("ℹ️")
    assert phase13._broadcast_result_text({
        "sent": 5, "failed": 0
    }) == "✅ پیام برای 5 کاربر ارسال شد."
    partial = phase13._broadcast_result_text({
        "sent": 4,
        "failed": 2,
        "unreachable": 1,
        "temporary": 1,
    })
    assert "⚠️ ارسال همگانی به‌صورت ناقص انجام شد." in partial
    assert "✅ موفق: 4" in partial
    assert "❌ ناموفق: 2" in partial
