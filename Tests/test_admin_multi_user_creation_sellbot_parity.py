"""Tests for SellBot-parity multi-user creation flow and safe note display in AdminBot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from TenantRuntime.AdminBot import handlers as admin_handlers
from TenantRuntime.AdminBot import server_actions
from TenantRuntime.server_admin import ServerAdminService
from Tests.test_server_management_complete import setup, ui, click, send


def test_display_safe_note_strips_whitelabel_marker_and_formats_hiddifybot() -> None:
    # 1. Bare WhiteLabel marker (legacy or internal) must not leak to UI, displays dash
    assert admin_handlers._display_safe_note("WhiteLabel tenant=1 subscription=-14107") == "—"
    assert server_actions._display_safe_note("WhiteLabel tenant=1 subscription=-14107") == "—"

    # 2. HiddifyBot with WhiteLabel marker must strip marker and translate digits to Persian
    assert (
        admin_handlers._display_safe_note("HiddifyBot:9001 | WhiteLabel tenant=1 subscription=-14107")
        == "HiddifyBot:۹۰۰۱"
    )
    assert (
        server_actions._display_safe_note("HiddifyBot:9001 | WhiteLabel tenant=1 subscription=-14107")
        == "HiddifyBot:۹۰۰۱"
    )

    # 3. Test flag preserved
    assert (
        admin_handlers._display_safe_note("HiddifyBot:9001|test | WhiteLabel tenant=1 subscription=-14107")
        == "HiddifyBot:۹۰۰۱|test"
    )

    # 4. Plain HiddifyBot note
    assert admin_handlers._display_safe_note("HiddifyBot:123456789") == "HiddifyBot:۱۲۳۴۵۶۷۸۹"

    # 5. Empty or None displays dash
    assert admin_handlers._display_safe_note("") == "—"
    assert admin_handlers._display_safe_note(None) == "—"

    # 6. Custom note preserved
    assert admin_handlers._display_safe_note("یادداشت تستی کاربر") == "یادداشت تستی کاربر"


def test_number_parsers_sellbot_parity() -> None:
    # Persian digits and decimals
    assert server_actions._parse_positive_int("۵") == 5
    assert server_actions._parse_positive_int("100") == 100
    assert server_actions._parse_positive_int("0") is None
    assert server_actions._parse_positive_int("-5") is None
    assert server_actions._parse_positive_int("abc") is None

    assert server_actions._parse_positive_float("۰٫۵") == 0.5
    assert server_actions._parse_positive_float("0,5") == 0.5
    assert server_actions._parse_positive_float("5.5") == 5.5
    assert server_actions._parse_positive_float("۵") == 5.0
    assert server_actions._parse_positive_float("0") is None
    assert server_actions._parse_positive_float("invalid") is None


def test_multi_user_creation_full_flow_sellbot_parity(monkeypatch, conn, factories, cipher) -> None:
    b, p, s, source, child, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)

    class PhotoBot:
        def __init__(self):
            self.photos = []

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    context.bot = PhotoBot()
    orig_provision = p.provision

    def provision_with_comment(*, target, secret, request):
        res = orig_provision(target=target, secret=secret, request=request)
        from TenantRuntime.panels import panel_comment
        p.users[target.endpoint, res.external_ref]["comment"] = panel_comment(request)
        return res

    p.provision = provision_with_comment

    async def run():
        # 1. Click "افزودن چندین کاربر➕"
        await click(update, context, f'srv:useradd:{source["id"]}:multi')
        prompt_text = str(message.sent[-1][0])
        assert "➕ افزودن چندین کاربر" in prompt_text
        assert "👥 چند کاربر می‌خواهید اضافه کنید؟" in prompt_text
        assert "مثال: 5" in prompt_text

        # 2. Test invalid count validation
        await send(update, context, "abc")
        assert "❌ لطفاً تعداد معتبر وارد کنید.\nمثال: 5" in str(message.sent[-1][0])

        # 3. Test count > 100 validation
        await send(update, context, "150")
        assert "❌ حداکثر 100 کاربر را در هر مرحله می‌توانید اضافه کنید." in str(message.sent[-1][0])

        # 4. Valid count with Persian digit "۳"
        await send(update, context, "۳")
        assert "✅ تعداد کاربران ثبت شد." in str(message.sent[-1][0])
        assert "📝 لطفاً نام پایه را وارد کنید." in str(message.sent[-1][0])
        assert "مثال: تست" in str(message.sent[-1][0])

        # 5. Base name "تست"
        await send(update, context, "تست")
        assert "📊 لطفاً حجم هر کاربر (GB) را وارد کنید:\nمثال: 5 یا 0.5" in str(message.sent[-1][0])

        # 6. Invalid volume validation
        await send(update, context, "0")
        assert "❌ لطفاً یک عدد معتبر بزرگ‌تر از صفر برای حجم وارد کنید." in str(message.sent[-1][0])

        # 7. Valid volume with Persian decimal "۱٫۵"
        await send(update, context, "۱٫۵")
        assert "📅 لطفاً مدت هر کاربر (روز) را وارد کنید:\nمثال: 30" in str(message.sent[-1][0])

        # 8. Invalid days validation
        await send(update, context, "invalid")
        assert "❌ لطفاً یک عدد صحیح معتبر برای روز وارد کنید.\nمثال: 30" in str(message.sent[-1][0])

        # 9. Valid days with Persian digit "۳۰"
        await send(update, context, "۳۰")
        confirm_text = str(message.sent[-1][0])
        assert "لطفاً اطلاعات را تایید کنید:" in confirm_text
        assert "👥 تعداد: 3 کاربر" in confirm_text
        assert "👤 نام پایه: تست" in confirm_text
        assert "📊 حجم هر کاربر: 1.5 گیگابایت" in confirm_text
        assert "📅 مدت هر کاربر: 30 روز" in confirm_text
        assert "🧾 نمونه نام‌ها: تست، تست1، تست2" in confirm_text

        # 10. Confirm creation with "✅ تایید"
        await send(update, context, "✅ تایید")

        texts = [str(item[0]) for item in message.sent]
        assert any("📦 نتیجه افزودن چندین کاربر" in t for t in texts)
        assert any("✅ موفق: 3" in t for t in texts)
        assert any("کاربران ساخته‌شده:\n• تست\n• تست1\n• تست2" in t for t in texts)

        # Assert no QR barcode photos or extra link messages were sent
        assert len(context.bot.photos) == 0
        assert not any("🔗 لینک اشتراک تست:" in t for t in texts)

        # 11. Check user detail note: must show HiddifyBot:۷۰۰۱ and NOT WhiteLabel tenant=...
        user_detail_texts = [t for t in texts if "❖⬩╍╍" in t]
        assert len(user_detail_texts) >= 3
        for u_text in user_detail_texts:
            assert "📝یادداشت: HiddifyBot:۷۰۰۱" in u_text
            assert "WhiteLabel" not in u_text

        # 12. Check in database and panel users
        users = s.users(7001, source["id"])
        user_names = {u["name"] for u in users}
        assert "تست" in user_names
        assert "تست1" in user_names
        assert "تست2" in user_names

    asyncio.run(run())


def test_multi_user_creation_cancel_flow(monkeypatch, conn, factories, cipher) -> None:
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)

    async def run():
        # 1. Start multi-user wizard
        await click(update, context, f'srv:useradd:{source["id"]}:multi')
        assert context.user_data.get(server_actions.FLOW) is not None

        # 2. Cancel at count prompt
        await send(update, context, "❌ لغو")
        assert context.user_data.get(server_actions.FLOW) is None
        assert any("❌ عملیات لغو شد." in str(item[0]) for item in message.sent)

        # 3. Start again and cancel at confirm prompt
        await click(update, context, f'srv:useradd:{source["id"]}:multi')
        await send(update, context, "2")
        await send(update, context, "کاربر")
        await send(update, context, "5")
        await send(update, context, "10")
        assert context.user_data[server_actions.FLOW]["kind"] == "create_confirm"

        await send(update, context, "❌ لغو")
        assert context.user_data.get(server_actions.FLOW) is None
        assert any("❌ عملیات لغو شد." in str(item[0]) for item in message.sent)

    asyncio.run(run())


def test_legacy_whitelabel_comment_in_detail_text_displays_dash(monkeypatch, conn, factories, cipher) -> None:
    b, p, s, source, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, b)

    # Seed a user whose comment on the panel is purely the legacy WhiteLabel marker
    p.seed(
        source["endpoint"],
        "legacy-user-uuid",
        name="کاربر قدیمی",
        usage=0,
        traffic=10 * 1024**3,
    )
    p.users[source["endpoint"], "legacy-user-uuid"]["comment"] = "WhiteLabel tenant=1 subscription=-14107"

    async def run():
        # Refresh users to pull into local DB
        await s.refresh_users(7001, source["id"])
        users = s.users(7001, source["id"])
        legacy_u = next(u for u in users if u["external_ref"] == "legacy-user-uuid")

        # View user detail
        await click(update, context, f'srv:puser:{source["id"]}:{legacy_u["id"]}')
        detail_text = str(message.sent[-1][0])
        assert "📝یادداشت: —" in detail_text
        assert "WhiteLabel" not in detail_text

    asyncio.run(run())
