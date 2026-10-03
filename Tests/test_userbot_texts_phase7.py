"""Phase 7 AdminBot-managed UserBot texts and invite banner parity."""

from __future__ import annotations

from types import SimpleNamespace

from TenantRuntime.UserBot import handlers as user_handlers
from TenantRuntime.business import (
    USERBOT_SETTING_DEFAULTS,
    TenantBusinessService,
)


def _service(conn, factories, cipher):
    owner = 7001
    actor = 7101
    tenant = factories.tenant(owner_telegram_id=owner)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    service.register_customer(
        actor,
        display_name="Text User",
        username="text_user",
    )
    return service, owner, actor


def test_phase7_defaults_cover_every_admin_managed_userbot_text() -> None:
    expected = {
        "welcome_message",
        "faq_text",
        "guide_text",
        "guide_android_text",
        "guide_ios_text",
        "guide_windows_text",
        "guide_mac_text",
        "guide_linux_text",
        "servers_list_text",
        "plans_list_text",
        "ticket_panel_text",
        "invite_text",
        "invite_info_text",
        "invite_banner_text",
        "invite_banner_photo_id",
    }
    assert expected.issubset(USERBOT_SETTING_DEFAULTS)
    for key in expected - {"invite_banner_photo_id"}:
        assert str(USERBOT_SETTING_DEFAULTS[key]).strip()


def test_admin_text_setting_changes_runtime_and_zero_restores_default(
    conn, factories, cipher
) -> None:
    service, owner, _actor = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        owner,
        key="faq_text",
        value="FAQ CUSTOM",
    )
    assert service.runtime_userbot_settings()["faq_text"] == "FAQ CUSTOM"

    service.set_userbot_setting_admin(
        owner,
        key="faq_text",
        value="0",
    )
    assert (
        service.runtime_userbot_settings()["faq_text"]
        == USERBOT_SETTING_DEFAULTS["faq_text"]
    )


def test_welcome_template_supports_real_user_placeholders() -> None:
    settings = {
        "welcome_message": (
            "سلام {full_name} | {username} | {id} | "
            "{tenant_name} | ورود {visits}"
        )
    }
    rendered = user_handlers._setting_text(
        settings,
        "welcome_message",
        "fallback",
        full_name="Mojtaba",
        username="@mojtaba",
        id=12345,
        tenant_name="Speed",
        visits=4,
    )
    assert rendered == "سلام Mojtaba | @mojtaba | 12345 | Speed | ورود 4"


def test_bad_template_placeholder_never_breaks_userbot() -> None:
    rendered = user_handlers._format_text_template(
        "سلام {unknown_placeholder}",
        full_name="User",
    )
    assert rendered == "سلام {unknown_placeholder}"


def test_ticket_panel_text_is_used_as_real_panel_intro() -> None:
    body = user_handlers._ticket_panel_body(
        {"ticket_panel_text": "CUSTOM SUPPORT PANEL"},
        [
            {
                "id": 11,
                "subject": "Connection",
                "status": "open",
                "admin_reply": None,
            }
        ],
    )
    assert body.startswith("CUSTOM SUPPORT PANEL")
    assert "#11" in body
    assert "Connection" in body


def test_invite_texts_and_banner_are_rendered_from_admin_settings(
    conn, factories, cipher
) -> None:
    service, owner, actor = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        owner,
        key="invite_info_text",
        value="INFO {trial_reward} / {purchase_reward}",
    )
    service.set_userbot_setting_admin(
        owner,
        key="invite_text",
        value="LINK={invite_link}",
    )
    service.set_userbot_setting_admin(
        owner,
        key="invite_banner_text",
        value="BANNER {invite_link}",
    )
    service.set_userbot_setting_admin(
        owner,
        key="invite_banner_photo_id",
        value="telegram-photo-id",
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(username="speed_test_bot")
    )
    rendered = user_handlers._referral_content(
        service,
        actor,
        context,
        service.runtime_userbot_settings(),
    )
    assert "INFO " in rendered["body"]
    assert "LINK=https://t.me/speed_test_bot?start=ref_" in rendered["body"]
    assert rendered["banner_text"].startswith(
        "BANNER https://t.me/speed_test_bot?start=ref_"
    )
    assert rendered["photo_id"] == "telegram-photo-id"


def test_invite_banner_photo_can_be_cleared_with_zero(
    conn, factories, cipher
) -> None:
    service, owner, _actor = _service(conn, factories, cipher)
    service.set_userbot_setting_admin(
        owner,
        key="invite_banner_photo_id",
        value="photo-id",
    )
    service.set_userbot_setting_admin(
        owner,
        key="invite_banner_photo_id",
        value="0",
    )
    assert service.runtime_userbot_settings()["invite_banner_photo_id"] == ""


def test_all_phase7_texts_are_connected_to_real_userbot_routes() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()

    for key in (
        "welcome_message",
        "faq_text",
        "guide_text",
        "guide_android_text",
        "guide_ios_text",
        "guide_windows_text",
        "guide_mac_text",
        "guide_linux_text",
        "servers_list_text",
        "plans_list_text",
        "ticket_panel_text",
        "invite_text",
        "invite_info_text",
        "invite_banner_text",
        "invite_banner_photo_id",
    ):
        assert key in source

    assert 'callback_data="shop:invitebanner"' in source
    assert 'data == "shop:invitebanner"' in source
    assert 'data == "shop:tickets"' in source
    assert 'data == "shop:guide"' in source
    assert 'data.startswith("shop:guide:")' in source
    assert 'settings.get("servers_list_text")' in source
    assert 'settings.get("plans_list_text")' in source


def test_adminbot_exposes_invite_text_and_photo_management() -> None:
    source = open(
        "TenantRuntime/AdminBot/userbot_management.py",
        encoding="utf-8",
    ).read()
    for callback in (
        "userbot:settings:texts:edit:welcome_message",
        "userbot:settings:texts:edit:faq_text",
        "userbot:settings:texts:guide_menu",
        "userbot:settings:texts:edit:servers_list_text",
        "userbot:settings:texts:edit:plans_list_text",
        "userbot:settings:texts:edit:ticket_panel_text",
        "userbot:settings:texts:invite_menu",
        "userbot:settings:texts:edit:invite_text",
        "userbot:settings:texts:edit:invite_info_text",
        "userbot:settings:texts:edit:invite_banner_text",
        "userbot:settings:texts:invite_photo",
        "userbot:settings:texts:invite_photo_remove",
    ):
        assert callback in source
    assert 'kind=="invite_banner_photo"' in source
    assert 'key="invite_banner_photo_id"' in source


def test_guide_and_faq_do_not_force_duplicate_fixed_titles() -> None:
    source = open(
        "TenantRuntime/UserBot/handlers.py",
        encoding="utf-8",
    ).read()
    # Admin content is the actual page body; UserBot should not always prepend
    # another fixed title over it.
    assert '"💡 راهنما\\n" + (guide' not in source
    assert '"📕 سوالات متداول\\n" + (faq' not in source
