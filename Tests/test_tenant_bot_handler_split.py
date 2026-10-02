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


def test_only_userbot_registers_photo_receipts() -> None:
    admin_source = (ROOT / "TenantRuntime" / "AdminBot" / "handlers.py").read_text(encoding="utf-8")
    user_source = (ROOT / "TenantRuntime" / "UserBot" / "handlers.py").read_text(encoding="utf-8")

    assert "filters.PHOTO" not in admin_source
    assert "filters.PHOTO" in user_source



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
