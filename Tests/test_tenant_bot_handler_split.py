from pathlib import Path

from telegram import ReplyKeyboardMarkup

from TenantRuntime import handlers as dispatcher
from TenantRuntime.AdminBot import handlers as admin_handlers
from TenantRuntime.UserBot import handlers as user_handlers


ROOT = Path(__file__).resolve().parents[1]


def test_admin_and_user_handlers_are_physically_split() -> None:
    admin = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(encoding="utf-8")
    user = (ROOT / "TenantRuntime" / "UserBot" / "handlers.py").read_text(encoding="utf-8")
    root = (ROOT / "TenantRuntime" / "handlers.py").read_text(encoding="utf-8")

    assert "biz:dashboard" in admin
    assert "shop:buy" not in admin
    assert "shop:buy" in user
    assert "biz:dashboard" not in user
    assert "biz:" not in root
    assert "shop:" not in root


def test_role_packages_export_their_own_registration() -> None:
    assert admin_handlers.register_admin_handlers.__module__ == "TenantRuntime.AdminBot.handlers"
    assert user_handlers.register_user_handlers.__module__ == "TenantRuntime.UserBot.handlers"
    assert dispatcher.register_runtime_handlers.__module__ == "TenantRuntime.handlers"


def test_photo_receipts_stay_in_userbot_while_admin_media_is_management_only() -> None:
    admin_source = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(encoding="utf-8")
    user_source = (ROOT / "TenantRuntime" / "UserBot" / "handlers.py").read_text(encoding="utf-8")
    management_source = (
        ROOT / "TenantRuntime" / "AdminBot" / "userbot_management.py"
    ).read_text(encoding="utf-8")

    assert "receipt_photo" not in admin_source
    assert "filters.PHOTO" in user_source
    assert "receipt_photo" in user_source

    # AdminBot may accept media only for the SellBot-compatible management
    # flows (broadcast/channel posts), never as a customer receipt.
    assert "userbot_admin_media" in admin_source
    assert "filters.PHOTO | filters.VIDEO" in admin_source
    assert 'kind=="broadcast"' in management_source
    assert 'kind in {"channel_content","channel_edit_media"}' in management_source
    assert "submit_receipt(" not in management_source



def test_admin_main_menu_matches_sellbot_layout() -> None:
    keyboard = admin_handlers.admin_main_keyboard()
    assert isinstance(keyboard, ReplyKeyboardMarkup)
    labels = [[button.text for button in row] for row in keyboard.keyboard]
    assert labels == [
        ["🖥 مدیریت سرورها"],
        ["🔍 جستجوی کاربر", "📊 گزارش روزانه"],
        ["🤖 مدیریت ربات کاربران"],
        ["📊 وضعیت سرور", "🏢 نمایندگی", "📫 دریافت بکاپ"],
    ]
    assert keyboard.resize_keyboard is True
    assert keyboard.selective is True



def test_admin_server_menu_uses_sellbot_labels() -> None:
    source = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(
        encoding="utf-8"
    )
    for label in (
        "‏🖥 مدیریت سرورها",
        "⬇️ لیست سرور های شما",
        "افزودن سرور➕",
        "👤لیست کاربران",
        "🛡️عملیات کاربری",
        "📋پلن ها",
        "🔗لیست دامنه‌ها",
        "✏️ویرایش سرور",
        "🗑️حذف سرور",
        "⚙️لیست نودها",
        "🔄همگام سازی نودها",
        "❄️ کاربران یخ‌زده این سرور",
    ):
        assert label in source



def test_admin_search_menu_matches_sellbot_sections() -> None:
    source = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(
        encoding="utf-8"
    )
    for label in (
        "🔍 جستجوی هوشمند کاربر",
        "📊پیگیری اشتراک",
        "⚠️ لیست کاربران منقضی شده",
        "♻️ اشتراک‌های منقضی‌شده",
        "🧹 بررسی رکوردهای مشکوک/قدیمی",
        "کانفیگ ها📄",
        "ویرایش کاربر✏️",
        "تمدید اشتراک♾️",
        "حذف کاربر🗑️",
        "📊 <b>گزارش کامل روزانه فروش</b>",
    ):
        assert label in source



def test_smart_search_prompt_matches_sellbot_and_uses_bottom_cancel_keyboard() -> None:
    source = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(
        encoding="utf-8"
    )
    assert "نام کاربر، UUID یا لینک کانفیگ را ارسال کنید." in source
    assert "نام کاربر، یوزرنیم، Telegram ID، شناسه سرویس یا شناسه پنل را ارسال کنید." not in source
    assert '[[KeyboardButton("❌ لغو")]]' in source
    assert "reply_markup=cancel_keyboard()" in source
    assert "await msg.delete()" in source
    assert '"❌ جستجو لغو شد."' in source
