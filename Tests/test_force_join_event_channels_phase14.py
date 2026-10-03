"""Phase 14: SellBot Force Join and tenant event-channel parity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from TenantRuntime.AdminBot import userbot_management as admin_ui
from TenantRuntime.UserBot import handlers as user_ui
from TenantRuntime.business import TenantBusinessService
from Tests.test_user_account_status_phase6 import _service


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


class FakeQuery:
    def __init__(self, data: str, message: FakeMessage | None = None) -> None:
        self.data = data
        self.message = message or FakeMessage()
        self.answers: list[tuple[tuple, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.edits))


def test_phase14_defaults_are_independent_and_legacy_event_is_preserved(
    conn, factories, cipher
):
    service, _panel, _actor, _sid, _ = _service(conn, factories, cipher)
    owner = service.owner_telegram_id

    service.set_userbot_setting_admin(
        owner,
        key="event_channel_enabled",
        value=True,
    )
    service.set_userbot_setting_admin(
        owner,
        key="event_channel_id",
        value="@legacy_events",
    )
    settings = service.runtime_userbot_settings()
    assert settings["purchase_event_channel_enabled"] is True
    assert settings["purchase_event_channel_id"] == "@legacy_events"
    assert settings["payment_event_channel_enabled"] is False
    assert settings["payment_event_channel_id"] == ""
    assert settings["system_event_channel_enabled"] is False
    assert settings["system_event_channel_id"] == ""

    service.set_userbot_setting_admin(
        owner,
        key="purchase_event_channel_id",
        value="@purchases",
    )
    settings = service.runtime_userbot_settings()
    assert settings["purchase_event_channel_id"] == "@purchases"


def test_phase14_event_settings_are_tenant_scoped(conn, factories, cipher):
    service, _panel, _actor, _sid, _ = _service(conn, factories, cipher)
    owner = service.owner_telegram_id
    service.set_userbot_setting_admin(
        owner,
        key="payment_event_channel_enabled",
        value=True,
    )
    service.set_userbot_setting_admin(
        owner,
        key="payment_event_channel_id",
        value="@tenant_one_payments",
    )

    tenant2 = factories.tenant(owner_telegram_id=99001)
    other = TenantBusinessService(
        conn,
        tenant_id=int(tenant2["id"]),
        owner_telegram_id=99001,
        secret_cipher=cipher,
    )
    s1 = service.runtime_userbot_settings()
    s2 = other.runtime_userbot_settings()
    assert s1["payment_event_channel_enabled"] is True
    assert s1["payment_event_channel_id"] == "@tenant_one_payments"
    assert s2["payment_event_channel_enabled"] is False
    assert s2["payment_event_channel_id"] == ""


def test_force_join_legacy_target_is_normalized(conn, factories, cipher):
    service, _panel, _actor, _sid, _ = _service(conn, factories, cipher)
    owner = service.owner_telegram_id
    service.set_userbot_setting_admin(
        owner,
        key="force_join_channel",
        value="@speedl_support",
    )
    settings = service.runtime_userbot_settings()
    assert settings["force_join_channel_username"] == "speedl_support"
    assert settings["force_join_channel_link"] == "https://t.me/speedl_support"
    target, url, guide = user_ui._force_join_config(settings)
    assert target == "@speedl_support"
    assert url == "https://t.me/speedl_support"
    assert "بررسی عضویت" in guide


def test_force_join_keyboard_matches_sellbot() -> None:
    markup = user_ui._force_join_markup("https://t.me/speedl_support")
    labels = [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]
    assert labels == ["📢 عضویت در کانال", "✅ بررسی عضویت"]
    assert markup.inline_keyboard[-1][0].callback_data == "forcejoin:check"


def test_force_join_membership_fail_open_on_bad_channel() -> None:
    class BrokenBot:
        async def get_chat_member(self, **_kwargs):
            raise RuntimeError("bot is not admin / bad chat")

    context = SimpleNamespace(bot=BrokenBot())
    result = asyncio.run(
        user_ui._force_join_membership(
            context,
            user_id=123,
            settings={
                "force_join_channel_username": "missing_channel",
            },
        )
    )
    assert result is None


def test_force_join_definite_non_member_is_blocked_with_sellbot_guide() -> None:
    class Bot:
        async def get_chat_member(self, **_kwargs):
            return SimpleNamespace(status="left")

    class Business:
        def runtime_userbot_settings(self):
            return {
                "force_join_enabled": True,
                "force_join_channel_username": "speedl_support",
                "force_join_channel_id": "",
                "force_join_channel_link": "https://t.me/speedl_support",
                "force_join_guide_text": (
                    "🔒 برای استفاده از ربات، ابتدا در کانال پشتیبانی عضو شوید.\n"
                    "پس از عضویت روی «✅ بررسی عضویت» بزنید."
                ),
                "button_theme": "smart",
                "colored_buttons": True,
            }

    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_message=message,
        callback_query=None,
        effective_chat=None,
    )
    allowed = asyncio.run(
        user_ui._force_join_allowed(
            update,
            SimpleNamespace(bot=Bot()),
            Business(),
        )
    )
    assert allowed is False
    assert "کانال پشتیبانی" in message.sent[-1][0]
    markup = message.sent[-1][1]["reply_markup"]
    assert markup.inline_keyboard[-1][0].callback_data == "forcejoin:check"


def test_force_join_restricted_status_matches_sellbot_and_does_not_pass() -> None:
    class Bot:
        async def get_chat_member(self, **_kwargs):
            return SimpleNamespace(status="restricted")

    result = asyncio.run(
        user_ui._force_join_membership(
            SimpleNamespace(bot=Bot()),
            user_id=123,
            settings={"force_join_channel_username": "speedl_support"},
        )
    )
    assert result is False


def test_start_persists_referral_before_force_join_can_stop(monkeypatch) -> None:
    calls: list[tuple] = []

    class Business:
        def register_customer(self, user_id, **kwargs):
            calls.append(("customer", user_id, kwargs))

        def register_referral(self, user_id, *, referral_code):
            calls.append(("referral", user_id, referral_code))

    class State:
        def load(self, _user_id):
            raise AssertionError("home state must not continue after force join blocks")

    async def blocked(_update, _context, _business):
        calls.append(("force_join",))
        return False

    monkeypatch.setattr(
        user_ui,
        "_services",
        lambda _context: (
            SimpleNamespace(role="user", tenant_name="Tenant"),
            None,
            State(),
            Business(),
        ),
    )
    monkeypatch.setattr(user_ui, "_force_join_allowed", blocked)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(
            id=777,
            full_name="Referral User",
            username="ref_user",
        ),
        callback_query=None,
        effective_message=FakeMessage("/start"),
    )
    context = SimpleNamespace(
        args=["ref_ABC123"],
        user_data={},
    )
    asyncio.run(user_ui.show_home(update, context))

    names = [x[0] for x in calls]
    assert names == ["customer", "referral", "force_join"]
    assert calls[1] == ("referral", 777, "ABC123")


def test_channel_parser_accepts_forwarded_channel_and_manual_targets() -> None:
    forwarded = SimpleNamespace(
        forward_from_chat=SimpleNamespace(
            type="channel",
            id=-100123456,
            username="speedl_support",
            title="SpeedL Support",
        ),
        forward_origin=None,
    )
    target, title, link = admin_ui._channel_target_from_message(forwarded, "")
    assert target == "@speedl_support"
    assert title == "SpeedL Support"
    assert link == "https://t.me/speedl_support"

    blank = SimpleNamespace(forward_from_chat=None, forward_origin=None)
    assert admin_ui._channel_target_from_message(
        blank, "-100987654321"
    )[0] == "-100987654321"
    assert admin_ui._channel_target_from_message(
        blank, "@speedl_support"
    )[0] == "@speedl_support"
    assert admin_ui._channel_target_from_message(blank, "not-a-channel")[0] == ""


def test_purchase_event_uses_tenant_admin_bot(monkeypatch) -> None:
    calls: list[dict] = []

    class Business:
        def runtime_userbot_settings(self):
            return {
                "purchase_event_channel_enabled": True,
                "purchase_event_channel_id": "@purchases",
            }

    class FakeBot:
        def __init__(self, token):
            assert token == "admin-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_message(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        user_ui,
        "_sibling_admin_bot_token",
        lambda _business: "admin-token",
    )
    monkeypatch.setattr(user_ui, "Bot", FakeBot)

    asyncio.run(
        user_ui._send_purchase_event_report(
            Business(),
            telegram_id=123,
            display_name="Ali",
            order={
                "id": 55,
                "operation": "purchase",
                "plan_name": "10GB",
                "selected_server_label": "Turkey",
                "traffic_gb": 10,
                "duration_days": 30,
                "amount": 120000,
                "currency": "TOMAN",
            },
            result={"operation": "purchase", "id": 99},
        )
    )
    assert calls[0]["chat_id"] == "@purchases"
    assert "📣 گزارش رویداد اشتراک" in calls[0]["text"]
    assert "خرید اشتراک" in calls[0]["text"]


def test_payment_event_uses_tenant_userbot_and_is_best_effort(monkeypatch) -> None:
    calls: list[dict] = []

    class Business:
        def userbot_settings_admin(self, _actor):
            return {
                "payment_event_channel_enabled": True,
                "payment_event_channel_id": "@payments",
            }

        def payment_admin(self, _actor, *, payment_key):
            assert payment_key == "order:9"
            return {
                "payment_key": payment_key,
                "display_name": "Sara",
                "username": "sara",
                "telegram_user_id": 456,
                "amount": 90000,
                "currency": "TOMAN",
            }

    class FakeBot:
        def __init__(self, token):
            assert token == "user-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_message(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        admin_ui,
        "_sibling_user_bot_token",
        lambda _business: "user-token",
    )
    monkeypatch.setattr(admin_ui, "Bot", FakeBot)

    asyncio.run(
        admin_ui._send_payment_event_report(
            Business(),
            7001,
            payment_key="order:9",
        )
    )
    assert calls[0]["chat_id"] == "@payments"
    assert "📣 گزارش رویداد پرداخت" in calls[0]["text"]
    assert "✅ وضعیت: تایید شده" in calls[0]["text"]


def test_system_event_failure_never_breaks_admin_operation() -> None:
    class Business:
        def userbot_settings_admin(self, _actor):
            return {
                "system_event_channel_enabled": True,
                "system_event_channel_id": "@system",
            }

    class BrokenBot:
        async def send_message(self, **_kwargs):
            raise RuntimeError("telegram unavailable")

    asyncio.run(
        admin_ui._send_system_event(
            SimpleNamespace(bot=BrokenBot()),
            Business(),
            7001,
            text="♻️ بازیابی بکاپ انجام شد.",
        )
    )


def test_phase14_admin_callbacks_match_sellbot_surfaces() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:force_join:help",
        "userbot:settings:force_join:toggle",
        "userbot:settings:force_join:set_channel",
        "userbot:settings:buy_renew:event_channel_enabled",
        "userbot:settings:buy_renew:event_channel_set",
        "userbot:settings:payment:event_channel_toggle",
        "userbot:settings:payment:event_channel_set",
        "userbot:settings:backup_restore:event_toggle",
        "userbot:settings:backup_restore:event_set",
    ):
        assert callback in source
