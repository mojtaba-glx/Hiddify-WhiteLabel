"""Phase 13 parity tests against the active Hiddify-SellBot flows."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.AdminBot import broadcast_channel as phase13
from TenantRuntime.business import TenantBusinessService
from Tests.test_user_account_status_phase6 import _service


class FakeMessage:
    def __init__(
        self,
        *,
        text: str = "",
        text_html: str = "",
        caption: str = "",
        caption_html: str = "",
        photo=None,
        video=None,
        document=None,
    ) -> None:
        self.text = text
        self.text_html = text_html or text
        self.caption = caption
        self.caption_html = caption_html or caption
        self.photo = list(photo or [])
        self.video = video
        self.document = document
        self.sent: list[tuple[str, str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.sent.append(("text", text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def reply_photo(self, photo, **kwargs):
        self.sent.append(("photo", str(photo), kwargs))
        return SimpleNamespace(
            message_id=len(self.sent),
            photo=[SimpleNamespace(file_id="preview-photo")],
        )

    async def reply_video(self, video, **kwargs):
        self.sent.append(("video", str(video), kwargs))
        return SimpleNamespace(
            message_id=len(self.sent),
            video=SimpleNamespace(file_id="preview-video"),
        )


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


def test_phase13_broadcast_audit_is_tenant_scoped_and_records_delivery(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    run_id = service.start_broadcast_run_admin(
        owner,
        segment="all",
        message_kind="photo",
        target_count=9,
        buttons_count=0,
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
    assert row["message_kind"] == "photo"
    assert row["buttons_count"] == 0
    assert row["sent_count"] == 7
    assert row["failed_count"] == 2
    assert row["recovered_count"] == 1
    assert row["unreachable_count"] == 1
    assert row["temporary_count"] == 1
    assert row["finished_at"]


def test_broadcast_menu_matches_sellbot_segments_and_has_no_draft_editor() -> None:
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

    source = open(
        "TenantRuntime/AdminBot/broadcast_channel.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:broadcast:preview",
        "userbot:broadcast:edit",
        "userbot:broadcast:button",
        "userbot:broadcast:publish",
        "userbot:broadcast:clear_buttons",
    ):
        assert f'callback_data="{callback}"' not in source


def test_broadcast_skip_keyboard_matches_sellbot() -> None:
    markup = phase13._skip_cancel_keyboard()
    labels = [
        button.text
        for row in markup.keyboard
        for button in row
    ]
    assert labels == ["⏩رد کردن", "❌لغو"]
    assert markup.one_time_keyboard is True


def test_broadcast_skip_sends_directly_without_preview(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    draft = {
        "segment": "all",
        "kind": "text",
        "text": "متن تست",
        "file_id": "",
        "buttons": [],
    }
    message = FakeMessage(text="⏩رد کردن")
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={
        phase13.BROADCAST_DRAFT_KEY: draft,
        phase13.FLOW_KEY: {
            "kind": "broadcast",
            "step": "wait_media",
        },
    })
    calls = []

    async def fake_send(_context, _business, _actor, current):
        calls.append(dict(current))
        return {
            "target": 2,
            "sent": 2,
            "failed": 0,
            "recovered": 0,
            "unreachable": 0,
            "temporary": 0,
            "telegram": 0,
            "other": 0,
        }

    monkeypatch.setattr(phase13, "_send_broadcast", fake_send)
    handled = asyncio.run(
        phase13.handle_text(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )

    assert handled
    assert calls and calls[0]["text"] == "متن تست"
    assert calls[0]["kind"] == "text"
    assert phase13.FLOW_KEY not in context.user_data
    assert phase13.BROADCAST_DRAFT_KEY not in context.user_data
    assert message.sent[-1][1] == "✅ پیام برای 2 کاربر ارسال شد."
    assert message.sent[-1][2]["reply_markup"] == "ADMIN"


def test_broadcast_photo_sends_directly_and_video_is_rejected(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    calls = []

    async def fake_send(_context, _business, _actor, current):
        calls.append(dict(current))
        return {
            "target": 1,
            "sent": 1,
            "failed": 0,
            "recovered": 0,
            "unreachable": 0,
            "temporary": 0,
            "telegram": 0,
            "other": 0,
        }

    monkeypatch.setattr(phase13, "_send_broadcast", fake_send)

    photo_message = FakeMessage(
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
    )
    photo_update = SimpleNamespace(
        effective_message=photo_message,
        callback_query=None,
    )
    photo_context = SimpleNamespace(user_data={
        phase13.BROADCAST_DRAFT_KEY: {
            "segment": "all",
            "kind": "text",
            "text": "همراه عکس",
            "file_id": "",
            "buttons": [],
        },
        phase13.FLOW_KEY: {
            "kind": "broadcast",
            "step": "wait_media",
        },
    })
    assert asyncio.run(
        phase13.handle_media(
            photo_update,
            photo_context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert calls[-1]["kind"] == "photo"
    assert calls[-1]["file_id"] == "large"
    assert phase13.FLOW_KEY not in photo_context.user_data

    video_message = FakeMessage(
        video=SimpleNamespace(file_id="video-id")
    )
    video_update = SimpleNamespace(
        effective_message=video_message,
        callback_query=None,
    )
    video_context = SimpleNamespace(user_data={
        phase13.BROADCAST_DRAFT_KEY: {
            "segment": "all",
            "kind": "text",
            "text": "نباید ویدئو شود",
            "file_id": "",
            "buttons": [],
        },
        phase13.FLOW_KEY: {
            "kind": "broadcast",
            "step": "wait_media",
        },
    })
    assert asyncio.run(
        phase13.handle_media(
            video_update,
            video_context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert len(calls) == 1
    assert video_context.user_data[phase13.FLOW_KEY]["step"] == "wait_media"
    assert "لطفا عکس ارسال کنید" in video_message.sent[-1][1]


def test_broadcast_image_document_is_accepted(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    calls = []

    async def fake_send(_context, _business, _actor, current):
        calls.append(dict(current))
        return {
            "target": 1,
            "sent": 1,
            "failed": 0,
            "recovered": 0,
            "unreachable": 0,
            "temporary": 0,
            "telegram": 0,
            "other": 0,
        }

    monkeypatch.setattr(phase13, "_send_broadcast", fake_send)
    message = FakeMessage(
        document=SimpleNamespace(
            file_id="image-document-id",
            mime_type="image/jpeg",
        )
    )
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={
        phase13.BROADCAST_DRAFT_KEY: {
            "segment": "all",
            "kind": "text",
            "text": "عکس فایل",
            "file_id": "",
            "buttons": [],
        },
        phase13.FLOW_KEY: {
            "kind": "broadcast",
            "step": "wait_media",
        },
    })
    assert asyncio.run(
        phase13.handle_document(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert calls[-1]["kind"] == "photo"
    assert calls[-1]["file_id"] == "image-document-id"


def test_broadcast_stale_preview_state_recovers_to_photo_step(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    message = FakeMessage(text="anything")
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={
        phase13.BROADCAST_DRAFT_KEY: {
            "segment": "all",
            "kind": "text",
            "text": "قدیمی",
            "file_id": "",
            "buttons": [],
        },
        phase13.FLOW_KEY: {
            "kind": "broadcast",
            "step": "preview",
        },
    })

    assert asyncio.run(
        phase13.handle_text(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert context.user_data[phase13.FLOW_KEY]["step"] == "wait_media"
    assert "مسیر قدیمی پیش‌نمایش حذف شده" in message.sent[-1][1]


def test_broadcast_photo_crosses_tokens_and_reuses_userbot_file_id(
    monkeypatch, conn, factories, cipher
):
    service, _panel, actor, _sid, _ = _service(
        conn, factories, cipher
    )
    service.register_customer(
        7104, display_name="Second", username=None
    )
    factories.bot(service.tenant_id, "user", cipher)
    sent_calls: list[dict] = []

    class AdminFile:
        async def download_as_bytearray(self):
            return bytearray(b"photo-bytes")

    class AdminBot:
        async def get_file(self, file_id):
            assert file_id == "admin-photo-id"
            return AdminFile()

    class FakeUserBot:
        def __init__(self, token):
            assert token

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_photo(self, **kwargs):
            sent_calls.append(kwargs)
            return SimpleNamespace(
                photo=[SimpleNamespace(file_id="userbot-photo-id")]
            )

        async def send_message(self, **kwargs):
            raise AssertionError("short caption must stay on the photo")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(phase13, "Bot", FakeUserBot)
    monkeypatch.setattr(phase13.asyncio, "sleep", no_sleep)
    context = SimpleNamespace(bot=AdminBot())
    draft = {
        "segment": "all",
        "kind": "photo",
        "text": "سلام",
        "file_id": "admin-photo-id",
        "buttons": [],
    }

    result = asyncio.run(
        phase13._send_broadcast(
            context,
            service,
            service.owner_telegram_id,
            draft,
        )
    )
    assert result["sent"] == 2
    assert result["failed"] == 0
    assert hasattr(sent_calls[0]["photo"], "read")
    assert sent_calls[1]["photo"] == "userbot-photo-id"

    audit = conn.execute(
        "SELECT * FROM tenant_broadcast_runs "
        "WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
        (service.tenant_id,),
    ).fetchone()
    assert audit["message_kind"] == "photo"
    assert audit["buttons_count"] == 0
    assert audit["sent_count"] == 2


def test_broadcast_long_photo_text_is_sent_as_separate_message(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    factories.bot(service.tenant_id, "user", cipher)
    calls: list[tuple[str, dict]] = []

    class AdminFile:
        async def download_as_bytearray(self):
            return bytearray(b"photo")

    class AdminBot:
        async def get_file(self, _file_id):
            return AdminFile()

    class FakeUserBot:
        def __init__(self, token):
            assert token

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_photo(self, **kwargs):
            calls.append(("photo", kwargs))
            return SimpleNamespace(
                photo=[SimpleNamespace(file_id="user-photo")]
            )

        async def send_message(self, **kwargs):
            calls.append(("text", kwargs))
            return SimpleNamespace(message_id=2)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(phase13, "Bot", FakeUserBot)
    monkeypatch.setattr(phase13.asyncio, "sleep", no_sleep)
    body = "x" * (phase13.MAX_CAPTION_LENGTH + 1)
    result = asyncio.run(
        phase13._send_broadcast(
            SimpleNamespace(bot=AdminBot()),
            service,
            service.owner_telegram_id,
            {
                "segment": "all",
                "kind": "photo",
                "text": body,
                "file_id": "admin-photo",
                "buttons": [],
            },
        )
    )
    assert result["sent"] == 1
    assert calls[0][0] == "photo"
    assert "caption" not in calls[0][1]
    assert calls[1][0] == "text"
    assert calls[1][1]["text"] == body


def test_retry_helper_recovers_transient_network_error(monkeypatch) -> None:
    attempts = {"count": 0}

    async def factory():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise phase13.NetworkError("temporary")
        return "ok"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(phase13.asyncio, "sleep", no_sleep)
    ok, result, category, retries, error = asyncio.run(
        phase13._send_with_retry(factory)
    )
    assert ok
    assert result == "ok"
    assert category == ""
    assert retries == 1
    assert error is None


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
    assert "🚫 غیرقابل دسترس/مسدود: 1" in partial
    assert "🌐 خطای موقت پس از تلاش مجدد: 1" in partial


def test_channel_menu_and_edit_actions_match_sellbot() -> None:
    draft = {
        "kind": "text",
        "text": "x",
        "file_id": "",
        "buttons": [],
    }
    labels = [
        button.text
        for row in phase13._channel_admin_menu(draft).inline_keyboard
        for button in row
    ]
    assert labels == [
        "➕ ساخت پست جدید",
        "✏️ ویرایش پست",
        "🔘 افزودن دکمه",
        "👁 پیش‌نمایش",
        "🚀 انتشار در کانال",
        "🧹 پاک کردن دکمه‌ها",
        "🔙 بازگشت به مدیریت ربات کاربران",
    ]
    assert "⚙️ تنظیم کانال مقصد" not in labels
    last = phase13._channel_admin_menu(draft).inline_keyboard[-1][0]
    assert last.callback_data == "channelpost:back_userbot"

    edit_labels = [
        button.text
        for row in phase13._channel_edit_markup(draft).inline_keyboard
        for button in row
    ]
    assert edit_labels == [
        "📝 ویرایش متن / کپشن",
        "🖼 افزودن عکس / ویدئو",
        "🔄 جایگزینی کامل پست",
        "🔙 بازگشت",
    ]


def test_channel_replace_keeps_existing_buttons(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    message = FakeMessage(
        text="متن جدید",
        text_html="<b>متن جدید</b>",
    )
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

    handled = asyncio.run(
        phase13.handle_text(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert handled
    draft = context.user_data[phase13.CHANNEL_DRAFT_KEY]
    assert draft["kind"] == "text"
    assert draft["text"] == "<b>متن جدید</b>"
    assert draft["buttons"][0]["text"] == "خرید"
    assert phase13.FLOW_KEY not in context.user_data


def test_channel_preview_supports_photo_and_video() -> None:
    photo_message = FakeMessage()
    asyncio.run(
        phase13._preview_draft(
            photo_message,
            {
                "kind": "photo",
                "text": "کپشن",
                "file_id": "admin-photo",
                "buttons": [],
            },
        )
    )
    assert photo_message.sent[0][0] == "photo"
    assert photo_message.sent[0][2]["caption"] == "کپشن"

    video_message = FakeMessage()
    asyncio.run(
        phase13._preview_draft(
            video_message,
            {
                "kind": "video",
                "text": "کپشن",
                "file_id": "admin-video",
                "buttons": [],
            },
        )
    )
    assert video_message.sent[0][0] == "video"
    assert video_message.sent[0][2]["caption"] == "کپشن"


def test_channel_caption_limit_and_edit_media_guard(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    too_long = "x" * (phase13.MAX_CAPTION_LENGTH + 1)

    message = FakeMessage(
        photo=[SimpleNamespace(file_id="new-photo")]
    )
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    draft = {
        "kind": "text",
        "text": too_long,
        "file_id": "",
        "buttons": [],
    }
    context = SimpleNamespace(user_data={
        phase13.CHANNEL_DRAFT_KEY: draft,
        phase13.FLOW_KEY: {"kind": "channel_edit_media"},
    })

    handled = asyncio.run(
        phase13.handle_media(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert handled
    assert draft["kind"] == "text"
    assert draft["file_id"] == ""
    assert phase13.FLOW_KEY in context.user_data
    assert "متن فعلی برای کپشن طولانی است" in message.sent[-1][1]


def test_channel_publish_bridges_admin_media_and_uses_force_join_fallback(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    factories.bot(service.tenant_id, "user", cipher)
    owner = service.owner_telegram_id
    service.set_userbot_setting_admin(
        owner,
        key="channel_id",
        value="",
    )
    service.set_userbot_setting_admin(
        owner,
        key="force_join_channel",
        value="@speedll_channel",
    )
    calls = []

    class AdminFile:
        async def download_as_bytearray(self):
            return bytearray(b"channel-photo-bytes")

    class AdminBot:
        async def get_file(self, file_id):
            assert file_id == "admin-photo-id"
            return AdminFile()

    class FakeUserBot:
        def __init__(self, token):
            assert token

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_photo(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=88)

    monkeypatch.setattr(phase13, "Bot", FakeUserBot)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(
        bot=AdminBot(),
        user_data={
            phase13.CHANNEL_DRAFT_KEY: {
                "kind": "photo",
                "text": "<b>پست</b>",
                "file_id": "admin-photo-id",
                "buttons": [{
                    "text": "ورود",
                    "url": "https://t.me/example_bot",
                    "style": "primary",
                }],
            }
        },
    )

    ok = asyncio.run(
        phase13._publish_channel(
            update,
            context,
            service,
            owner,
            lambda: "ADMIN",
        )
    )
    assert ok
    assert calls[0]["chat_id"] == "@speedll_channel"
    assert hasattr(calls[0]["photo"], "read")
    assert calls[0]["reply_markup"].inline_keyboard[0][0].text == "ورود"
    assert phase13.CHANNEL_DRAFT_KEY not in context.user_data
    assert "Message ID: <code>88</code>" in message.sent[-1][1]
    assert message.sent[-1][2]["reply_markup"] == "ADMIN"


def test_channel_publish_failure_keeps_draft_for_retry(
    monkeypatch, conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    factories.bot(service.tenant_id, "user", cipher)
    owner = service.owner_telegram_id
    service.set_userbot_setting_admin(
        owner, key="channel_id", value="@speedll_channel"
    )

    class FailingUserBot:
        def __init__(self, token):
            assert token

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_message(self, **kwargs):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(phase13, "Bot", FailingUserBot)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    draft = {
        "kind": "text",
        "text": "retry me",
        "file_id": "",
        "buttons": [],
    }
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        user_data={phase13.CHANNEL_DRAFT_KEY: draft},
    )

    ok = asyncio.run(
        phase13._publish_channel(
            update,
            context,
            service,
            owner,
            lambda: "ADMIN",
        )
    )
    assert not ok
    assert context.user_data[phase13.CHANNEL_DRAFT_KEY] is draft
    assert "❌ انتشار ناموفق بود" in message.sent[-1][1]


def test_channel_cancel_returns_to_manager_and_keeps_draft(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(
        conn, factories, cipher
    )
    owner = service.owner_telegram_id
    draft = {
        "kind": "photo",
        "text": "پست قبلی",
        "file_id": "photo-id",
        "buttons": [{
            "text": "خرید",
            "url": "https://example.com",
            "style": "primary",
        }],
    }
    message = FakeMessage(text="❌لغو")
    update = SimpleNamespace(
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={
        phase13.CHANNEL_DRAFT_KEY: draft,
        phase13.FLOW_KEY: {"kind": "channel_edit_text"},
    })

    handled = asyncio.run(
        phase13.handle_text(
            update,
            context,
            business=service,
            actor=owner,
            admin_main_keyboard=lambda: "ADMIN",
        )
    )
    assert handled
    assert phase13.FLOW_KEY not in context.user_data
    assert context.user_data[phase13.CHANNEL_DRAFT_KEY] is draft
    assert any(
        "📢 <b>مدیریت پست کانال</b>" in item[1]
        for item in message.sent
    )


def test_channel_url_normalization_matches_sellbot() -> None:
    assert phase13._normalize_button_url(
        "@user_speedl_bot"
    ) == "https://t.me/user_speedl_bot"
    assert phase13._normalize_button_url(
        "t.me/user_speedl_bot"
    ) == "https://t.me/user_speedl_bot"
    assert phase13._normalize_button_url(
        "tg://resolve?domain=user_speedl_bot"
    ) == "tg://resolve?domain=user_speedl_bot"
    assert phase13._normalize_button_url("@bad-name") == ""


def test_userbot_management_routes_all_phase13_input_types() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for route in (
        "broadcast_channel.handle_callback",
        "broadcast_channel.handle_text",
        "broadcast_channel.handle_media",
        "broadcast_channel.handle_document",
    ):
        assert route in source
