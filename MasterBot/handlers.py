"""Telegram handlers for the owner-only MasterBot interface."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from MasterBot.service import MasterService, MasterServiceError, NotFoundError
from MasterBot.customer_handlers import (
    handle_customer_callback,
    handle_customer_photo,
    handle_customer_text,
    show_customer_home,
)
from MasterBot.customer_service import CustomerPortalError, CustomerPortalService
from Provisioning.service import PreparedBot, ProvisioningError
from MasterBot.views import (
    audit_text,
    back_keyboard,
    backup_settings_view,
    confirm_keyboard,
    license_detail,
    licenses_view,
    main_menu_keyboard,
    financial_dashboard_view,
    order_detail_view,
    orders_view,
    payment_history_view,
    payment_method_detail,
    payment_receipt_detail,
    platform_customer_detail,
    platform_customers_view,
    platform_settings_view,
    plan_detail,
    plans_view,
    tenant_detail,
    tenants_view,
    trial_plan_picker,
    warnings_text,
)
from Shared.access import is_master_admin
from Shared.redaction import get_logger, safe_format_exception
from Shared.settings import load_database_path

logger = get_logger(__name__)

MASTER_ROOT = Path(__file__).resolve().parents[1]
MAX_RESTORE_UPLOAD_BYTES = 256 * 1024 * 1024


def _master_env_file() -> Path:
    return MASTER_ROOT / ".env"


def _master_database_path() -> Path:
    env_path = _master_env_file()
    values: dict[str, str] = {}
    try:
        if env_path.is_file():
            values = {
                str(key): str(value or "")
                for key, value in dotenv_values(env_path).items()
            }
    except Exception:
        values = {}
    configured = load_database_path(values)
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else (MASTER_ROOT / candidate)


def _backup_passphrase() -> str:
    raw = str(os.environ.get("BACKUP_PASSPHRASE", "") or "")
    if not raw:
        try:
            env_path = _master_env_file()
            if env_path.is_file():
                raw = str(dotenv_values(env_path).get("BACKUP_PASSPHRASE", "") or "")
        except Exception:
            raw = ""
    return raw


def _read_version() -> str:
    try:
        return (MASTER_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _actor_id(update: Update) -> Optional[int]:
    return int(update.effective_user.id) if update.effective_user else None


def _service(context: ContextTypes.DEFAULT_TYPE) -> MasterService:
    service = context.application.bot_data.get("master_service")
    if not isinstance(service, MasterService):
        raise RuntimeError("master service is unavailable")
    return service


def _portal(context: ContextTypes.DEFAULT_TYPE) -> CustomerPortalService:
    portal = context.application.bot_data.get("customer_portal_service")
    if not isinstance(portal, CustomerPortalService):
        raise RuntimeError("customer portal service is unavailable")
    return portal


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return is_master_admin(_actor_id(update), _service(context).master_admin_id)


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Role firewall: customer routes are public, owner routes are not."""
    service = _service(context)
    if is_master_admin(_actor_id(update), service.master_admin_id):
        return
    if _actor_id(update) is None or int(_actor_id(update) or 0) <= 0:
        raise ApplicationHandlerStop
    # Customer callback payloads are deliberately in their own namespace.
    # A copied owner callback can never enter the owner handler below.
    if update.callback_query and str(update.callback_query.data or "").startswith("customer:"):
        return
    if update.callback_query:
        await update.callback_query.answer("دسترسی مجاز نیست.", show_alert=True)
        raise ApplicationHandlerStop
    if update.effective_message:
        text = str(getattr(update.effective_message, "text", "") or "").strip()
        if text.startswith("/") and text.split(maxsplit=1)[0].split("@", 1)[0] not in {
            "/start", "/menu", "/cancel"
        }:
            await update.effective_message.reply_text("دسترسی مجاز نیست.")
            raise ApplicationHandlerStop
        # Customer text, image receipts and the three public commands proceed.
        return
    raise ApplicationHandlerStop


async def _render(update: Update, text: str, keyboard: Any) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text=text, reply_markup=keyboard)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        await show_customer_home(update, context)
        return
    context.user_data.clear()
    await _render(
        update,
        "🧭 پنل مدیریت سیستم فروش ربات\n\n"
        "از این بخش کاربران، ربات‌ها، لایسنس‌ها، پرداخت‌ها و تنظیمات کل سیستم را مدیریت می‌کنید.",
        main_menu_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.user_data.get("flow"), dict):
        await _remove_flow_keyboard(update, "❌ عملیات لغو شد.")
    await show_main(update, context)


def _parse_int(raw: str, label: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


FLOW_CANCEL_TEXT = "❌ لغو"


def _flow_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(FLOW_CANCEL_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _prompt_flow(update: Update, text: str) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            text, reply_markup=_flow_cancel_keyboard()
        )
        return
    if update.effective_chat is not None:
        await update.effective_chat.send_message(
            text, reply_markup=_flow_cancel_keyboard()
        )


async def _remove_flow_keyboard(update: Update, text: str = "✅ انجام شد.") -> None:
    if update.effective_chat is not None:
        await update.effective_chat.send_message(
            text, reply_markup=ReplyKeyboardRemove()
        )


async def _show_license_tenant_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page_number: int = 0,
) -> None:
    actor = int(_actor_id(update) or 0)
    page = _service(context).list_tenants(
        actor, page=page_number, page_size=8
    )
    rows: list[list[InlineKeyboardButton]] = []
    for tenant in page.items:
        rows.append([
            InlineKeyboardButton(
                f"🤖 #{int(tenant['id'])} · {str(tenant['name'])[:28]}",
                callback_data=f"license:new:tenant:{int(tenant['id'])}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page.has_previous:
        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"license:new:tenant-page:{page.page - 1}",
            )
        )
    if page.has_next:
        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"license:new:tenant-page:{page.page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    if not rows:
        rows.append([
            InlineKeyboardButton(
                "↩️ لایسنس‌ها",
                callback_data="flow:cancel:licenses",
            )
        ])
    text = (
        "👤 مشتری/رباتی که می‌خواهید لایسنس برایش صادر شود را انتخاب کنید:"
        if page.items
        else "❌ هنوز مشتری‌ای ثبت نشده است. ابتدا یک ربات مشتری ایجاد کنید."
    )
    keyboard = InlineKeyboardMarkup(rows)
    if update.callback_query is not None:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=keyboard
        )
    elif update.effective_chat is not None:
        await update.effective_chat.send_message(
            text, reply_markup=keyboard
        )


async def _show_license_plan_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tenant: dict[str, Any],
) -> None:
    actor = int(_actor_id(update) or 0)
    plans = _service(context).list_active_plans(actor)
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        rows.append([
            InlineKeyboardButton(
                f"📦 #{int(plan['id'])} · {str(plan['name'])[:24]} · "
                f"{int(plan['duration_days'])} روز",
                callback_data=f"license:new:plan:{int(plan['id'])}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "➕ ساخت پلن جدید",
            callback_data="license:new:create-plan",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ تغییر مشتری",
            callback_data="license:new:tenant-page:0",
        )
    ])
    text = (
        f"✅ مشتری انتخاب شد: {tenant['name']} (#{int(tenant['id'])})\n\n"
        + (
            "📦 پلن لایسنس را انتخاب کنید:"
            if plans
            else "❌ هیچ پلن فعالی وجود ندارد. با «➕ ساخت پلن جدید» یکی بسازید."
        )
    )
    keyboard = InlineKeyboardMarkup(rows)
    if update.callback_query is not None:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=keyboard
        )
    elif update.effective_chat is not None:
        await update.effective_chat.send_message(
            text, reply_markup=keyboard
        )


async def _show_orders(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    actor = int(_actor_id(update) or 0)
    status = str(context.user_data.get("order_status_filter") or "all")
    query = str(context.user_data.get("order_query") or "")
    page = _portal(context).list_all_orders(
        actor, page=page_number, status=status, query=query
    )
    text, keyboard = orders_view(page, status=status, query=query)
    await _render(update, text, keyboard)


async def _show_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    order = _portal(context).get_order_admin(actor, int(order_id))
    text, keyboard = order_detail_view(order)
    await _render(update, text, keyboard)


async def _show_platform_customers(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    actor = int(_actor_id(update) or 0)
    query = str(context.user_data.get("platform_customer_query") or "")
    page = _portal(context).list_platform_customers(
        actor, page=page_number, query=query
    )
    text, keyboard = platform_customers_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_platform_customer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, customer_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    customer = _portal(context).get_platform_customer_admin(actor, int(customer_id))
    text, keyboard = platform_customer_detail(customer)
    await _render(update, text, keyboard)


async def _show_tenants(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("tenant_query") or "")
    page = _service(context).list_tenants(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = tenants_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_tenant(update: Update, context: ContextTypes.DEFAULT_TYPE, tenant_id: int) -> None:
    service = _service(context)
    actor = int(_actor_id(update) or 0)
    row = service.get_tenant(actor, tenant_id)
    state = service.provisioning_status(actor, tenant_id)
    readiness = {
        "admin": state.admin_bot,
        "user": state.user_bot,
        "ready": state.ready,
        "runtime_status": state.runtime_status,
    }
    text, keyboard = tenant_detail(row, readiness)
    await _render(update, text, keyboard)


async def _show_plans(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("plan_query") or "")
    page = _service(context).list_plans(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = plans_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_plan(
    update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    row = _service(context).get_plan(actor, int(plan_id))
    settings = _portal(context).get_platform_settings(actor)
    text, keyboard = plan_detail(
        row, trial_plan_id=settings.get("trial_plan_id")
    )
    await _render(update, text, keyboard)


async def _show_licenses(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    query = str(context.user_data.get("license_query") or "")
    page = _service(context).list_licenses(
        int(_actor_id(update) or 0), page=page_number, query=query
    )
    text, keyboard = licenses_view(page, query=query)
    await _render(update, text, keyboard)


async def _show_license(
    update: Update, context: ContextTypes.DEFAULT_TYPE, license_id: int
) -> None:
    row = _service(context).get_license(int(_actor_id(update) or 0), license_id)
    timezone_name = str(context.application.bot_data.get("display_timezone") or "Asia/Tehran")
    text, keyboard = license_detail(row, timezone_name=timezone_name)
    await _render(update, text, keyboard)


async def _show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = int(_actor_id(update) or 0)
    settings = _portal(context).get_platform_settings(actor)
    text, keyboard = platform_settings_view(settings)
    await _render(update, text, keyboard)


async def _show_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = int(_actor_id(update) or 0)
    settings = _portal(context).get_platform_settings(actor)
    text, keyboard = backup_settings_view(
        settings, has_passphrase=bool(_backup_passphrase())
    )
    await _render(update, text, keyboard)


async def _do_full_backup(
    update: Update, context: ContextTypes.DEFAULT_TYPE, passphrase: str
) -> None:
    """Create an encrypted full backup and deliver it to the owner chat."""
    from Ops.backup import BackupError, create_backup

    actor = int(_actor_id(update) or 0)
    if len(passphrase.encode("utf-8")) < 12:
        await _render(
            update,
            "❌ رمز بکاپ باید حداقل ۱۲ کاراکتر باشد.",
            back_keyboard("menu:backup"),
        )
        return
    database = _master_database_path()
    if not database.is_file():
        await _render(
            update,
            "❌ فایل دیتابیس پیدا نشد؛ بکاپ ساخته نشد.",
            back_keyboard("menu:backup"),
        )
        return
    try:
        output = create_backup(
            database_path=database,
            environment_path=_master_env_file()
            if _master_env_file().is_file()
            else None,
            output_directory=MASTER_ROOT / "backups",
            passphrase=passphrase,
            version=_read_version(),
        )
    except BackupError as exc:
        logger.warning("MasterBot full backup failed: %s", safe_format_exception(exc))
        await _render(
            update,
            "❌ ساخت بکاپ ناموفق بود. دوباره تلاش کنید.",
            back_keyboard("menu:backup"),
        )
        return
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    _portal(context).record_backup_completed(
        actor, filename=output.name, sha256=digest
    )
    caption = (
        "✅ بکاپ کامل ساخته شد\n\n"
        f"📁 {output.name}\n"
        f"🔑 SHA256: {digest[:16]}…\n"
        "این فایل را در جای امن خارج از سرور نگه دارید؛ "
        "رمز بکاپ جداگانه و امن نگهداری شود."
    )
    if update.effective_chat is not None:
        with open(output, "rb") as handle:
            await update.effective_chat.send_document(
                document=handle, filename=output.name, caption=caption
            )
    await _show_backup(update, context)


def _summarize_backup_manifest(manifest: dict[str, Any]) -> str:
    files = manifest.get("files") if isinstance(manifest, dict) else None
    names = ", ".join(sorted(files)) if isinstance(files, dict) else "—"
    return (
        f"نسخه: {manifest.get('version', '—')}\n"
        f"زمان ساخت: {manifest.get('created_at', '—')}\n"
        f"فایل‌ها: {names}"
    )


async def _verify_restored_backup(
    update: Update, passphrase: str, backup_file: str
) -> None:
    from Ops.backup import BackupError, _decrypt_archive

    path = Path(str(backup_file or ""))
    if not path.is_file():
        await _render(
            update,
            "❌ فایل بکاپ پیدا نشد؛ دوباره آپلود کنید.",
            back_keyboard("menu:backup"),
        )
        return
    if len(passphrase.encode("utf-8")) < 12:
        await _render(
            update,
            "❌ رمز بکاپ باید حداقل ۱۲ کاراکتر باشد.",
            back_keyboard("menu:backup"),
        )
        return
    try:
        content = _decrypt_archive(path, passphrase)
        manifest = json.loads(content["manifest.json"].decode("utf-8"))
    except BackupError:
        await _render(
            update,
            "❌ رمز نادرست است یا فایل بکاپ خراب است.",
            back_keyboard("menu:backup"),
        )
        return
    except Exception as exc:
        logger.warning("MasterBot backup verify failed: %s", safe_format_exception(exc))
        await _render(
            update,
            "❌ بررسی فایل بکاپ ناموفق بود.",
            back_keyboard("menu:backup"),
        )
        return
    await _render_backup_summary(update, path, manifest)


async def _render_backup_summary(
    update: Update, path: Path, manifest: dict[str, Any]
) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    await _render(
        update,
        "✅ فایل بکاپ معتبر است\n\n"
        f"{_summarize_backup_manifest(manifest)}\n"
        f"🔑 SHA256: {digest[:16]}…\n\n"
        "برای بازیابی نهایی (بعد از توقف سرویس‌ها):\n"
        f"`python3 scripts/restore.py \"{path}\" --yes`\n\n"
        "⚠️ بازیابی از تلگرام انجام نمی‌شود؛ حتماً از ترمینال و با سرویس‌های متوقف اجرا کنید.",
        back_keyboard("menu:backup"),
    )


async def _try_stored_passphrase_verify(update: Update, path: Path) -> bool:
    """Verify an uploaded backup with the stored passphrase, if any."""
    from Ops.backup import BackupError, _decrypt_archive

    stored = _backup_passphrase()
    if len(stored.encode("utf-8")) < 12:
        return False
    try:
        content = _decrypt_archive(path, stored)
        manifest = json.loads(content["manifest.json"].decode("utf-8"))
    except BackupError:
        return False
    except Exception as exc:
        logger.warning("MasterBot stored-passphrase verify failed: %s", safe_format_exception(exc))
        return False
    finally:
        stored = ""
    await _render_backup_summary(update, path, manifest)
    return True


async def handle_backup_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_owner(update, context):
        return
    flow = context.user_data.get("flow")
    if not isinstance(flow, dict) or str(flow.get("kind") or "") != "backup_restore_upload":
        return
    message = update.effective_message
    if message is None or message.document is None:
        return
    document = message.document
    raw_name = str(getattr(document, "file_name", "") or "uploaded.wlbak")
    safe_name = "".join(
        ch for ch in Path(raw_name).name if ch.isalnum() or ch in "._-"
    ).strip("._") or "uploaded.wlbak"
    if not safe_name.endswith(".wlbak"):
        await message.reply_text(
            "❌ فقط فایل `.wlbak` قبول است.", reply_markup=back_keyboard("menu:backup")
        )
        return
    size = int(getattr(document, "file_size", 0) or 0)
    if size > MAX_RESTORE_UPLOAD_BYTES:
        await message.reply_text(
            "❌ حجم فایل بیش از حد مجاز است.", reply_markup=back_keyboard("menu:backup")
        )
        return
    incoming = MASTER_ROOT / "backups" / "incoming"
    try:
        incoming.mkdir(parents=True, mode=0o700, exist_ok=True)
    except Exception as exc:
        logger.warning("MasterBot backup incoming dir failed: %s", safe_format_exception(exc))
        await message.reply_text(
            "❌ آماده‌سازی پوشه بکاپ ناموفق بود.", reply_markup=back_keyboard("menu:backup")
        )
        return
    target = incoming / safe_name
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(str(target))
        os.chmod(target, 0o600)
    except Exception as exc:
        logger.warning("MasterBot backup download failed: %s", safe_format_exception(exc))
        await message.reply_text(
            "❌ دانلود فایل بکاپ ناموفق بود.", reply_markup=back_keyboard("menu:backup")
        )
        return
    context.user_data["flow"] = {"kind": "backup_restore_passphrase", "backup_file": str(target)}
    if await _try_stored_passphrase_verify(update, target):
        context.user_data.pop("flow", None)
        return
    await message.reply_text(
        f"📥 فایل `{safe_name}` دریافت شد.\n\n"
        "🔐 حالا رمز همان بکاپ را بفرستید؛ پیام بلافاصله حذف می‌شود.",
        reply_markup=back_keyboard("menu:backup"),
    )


async def _show_payment_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_number: int = 0
) -> None:
    actor = int(_actor_id(update) or 0)
    status = str(context.user_data.get("receipt_status_filter") or "all")
    query = str(context.user_data.get("receipt_query") or "")
    page = _portal(context).list_receipts_admin(
        actor, page=page_number, status=status, query=query
    )
    text, keyboard = payment_history_view(page, status=status, query=query)
    await _render(update, text, keyboard)


async def _show_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    receipt_id: int,
    *,
    send_image: bool = False,
) -> None:
    actor = int(_actor_id(update) or 0)
    receipt = _portal(context).get_receipt_admin(actor, int(receipt_id))
    text, keyboard = payment_receipt_detail(receipt)
    await _render(update, text, keyboard)
    if send_image and receipt.get("telegram_file_id") and update.effective_chat:
        await update.effective_chat.send_photo(
            photo=str(receipt["telegram_file_id"]),
            caption=f"تصویر رسید #{int(receipt_id)}",
        )


async def _show_payment_method(
    update: Update, context: ContextTypes.DEFAULT_TYPE, method_id: int
) -> None:
    actor = int(_actor_id(update) or 0)
    method = _portal(context).get_payment_method_admin(actor, int(method_id))
    text, keyboard = payment_method_detail(method)
    await _render(update, text, keyboard)


async def _show_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = int(_actor_id(update) or 0)
    portal = _portal(context)
    methods = portal.list_all_payment_methods(actor)
    receipts = portal.list_pending_receipts(actor, limit=15)
    lines = ["💳 مدیریت پرداخت‌ها", "", f"رسیدهای در انتظار: {len(receipts)}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for receipt in receipts:
        lines.append(
            f"#{int(receipt['id'])} · {receipt['display_name']} · "
            f"{int(receipt['amount']):,} {receipt['currency']} · {receipt['method_title']}"
        )
        rows.append([InlineKeyboardButton(
            f"🧾 بررسی رسید #{int(receipt['id'])}", callback_data=f"payment:receipt:{int(receipt['id'])}"
        )])
    if not receipts:
        lines.append("رسیدی برای بررسی وجود ندارد.")
    lines.extend(["", "روش‌های فعال/غیرفعال:"])
    if not methods:
        lines.append("هنوز روشی ثبت نشده است.")
    for method in methods:
        state = "🟢" if method["status"] == "active" else "⚫"
        lines.append(f"{state} #{int(method['id'])} · {method['kind']} · {method['title']} · {method['currency']}")
        rows.append([InlineKeyboardButton(
            f"{state} {method['title']} · {method['currency']}",
            callback_data=f"payment:view:{int(method['id'])}",
        )])
    rows.extend([
        [InlineKeyboardButton("📚 تاریخچه پرداخت‌ها", callback_data="payment:history")],
        [
            InlineKeyboardButton("➕ کارت‌به‌کارت", callback_data="payment:add:card"),
            InlineKeyboardButton("➕ ارز دیجیتال", callback_data="payment:add:crypto"),
        ],
        [InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")],
    ])
    await _render(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = str(query.data or "")
    if data.startswith("customer:"):
        await handle_customer_callback(update, context, data)
        return
    actor = int(_actor_id(update) or 0)
    service = _service(context)
    try:
        if data == "noop":
            return
        if data == "menu:main":
            await show_main(update, context)
            return
        if data.startswith("flow:cancel:"):
            target = data.split(":", 2)[2]
            context.user_data.pop("flow", None)
            context.user_data.pop("confirm", None)
            await _remove_flow_keyboard(update, "❌ عملیات لغو شد.")
            if target == "tenants":
                await _show_tenants(update, context)
            elif target == "plans":
                await _show_plans(update, context)
            elif target == "licenses":
                await _show_licenses(update, context)
            else:
                await show_main(update, context)
            return
        if data == "menu:orders":
            context.user_data["order_status_filter"] = "all"
            context.user_data.pop("order_query", None)
            await _show_orders(update, context)
            return
        if data.startswith("orderadmin:filter:"):
            status = data.split(":", 2)[2]
            context.user_data["order_status_filter"] = status
            context.user_data.pop("order_query", None)
            await _show_orders(update, context)
            return
        if data.startswith("orderadmin:page:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid order page callback")
            context.user_data["order_status_filter"] = parts[2]
            await _show_orders(update, context, _parse_int(parts[3], "page"))
            return
        if data == "orderadmin:search":
            context.user_data["flow"] = {"kind": "order_search"}
            await _render(
                update,
                "🔎 شناسه سفارش، نام، یوزرنیم یا شناسه تلگرام مشتری را بفرستید:",
                back_keyboard("menu:orders"),
            )
            return
        if data.startswith("orderadmin:view:"):
            await _show_order(
                update, context, _parse_int(data.rsplit(":", 1)[1], "order id")
            )
            return
        if data == "payment:history":
            context.user_data["receipt_status_filter"] = "all"
            context.user_data.pop("receipt_query", None)
            await _show_payment_history(update, context)
            return
        if data.startswith("payment:history:filter:"):
            status = data.split(":", 3)[3]
            context.user_data["receipt_status_filter"] = status
            context.user_data.pop("receipt_query", None)
            await _show_payment_history(update, context)
            return
        if data.startswith("payment:history:page:"):
            parts = data.split(":")
            if len(parts) != 5:
                raise ValueError("invalid receipt page callback")
            context.user_data["receipt_status_filter"] = parts[3]
            await _show_payment_history(update, context, _parse_int(parts[4], "page"))
            return
        if data == "payment:history:search":
            context.user_data["flow"] = {"kind": "receipt_search"}
            await _render(
                update,
                "🔎 شناسه پرداخت، سفارش، نام، یوزرنیم یا کد پیگیری را بفرستید:",
                back_keyboard("payment:history"),
            )
            return
        if data.startswith("payment:history:view:"):
            await _show_receipt(
                update,
                context,
                _parse_int(data.rsplit(":", 1)[1], "receipt id"),
                send_image=True,
            )
            return
        if data == "menu:payments":
            await _show_payments(update, context)
            return
        if data.startswith("payment:view:"):
            await _show_payment_method(
                update, context, _parse_int(data.rsplit(":", 1)[1], "method id")
            )
            return
        if data.startswith("payment:edit:"):
            method_id = _parse_int(data.rsplit(":", 1)[1], "method id")
            method = _portal(context).get_payment_method_admin(actor, method_id)
            context.user_data["flow"] = {
                "kind": "payment_method_edit",
                "method_id": method_id,
                "payment_kind": str(method["kind"]),
            }
            instructions = (
                "عنوان | ارز | شماره کارت | نام صاحب کارت | توضیح اختیاری"
                if method["kind"] == "card" else
                "عنوان | ارز | آدرس کیف پول | شبکه | توضیح اختیاری"
            )
            await _render(
                update,
                f"✏️ ویرایش روش پرداخت #{method_id}\n\n{instructions}",
                back_keyboard(f"payment:view:{method_id}"),
            )
            return
        if data.startswith("payment:add:"):
            kind = data.rsplit(":", 1)[1]
            if kind not in ("card", "crypto"):
                raise ValueError("invalid payment kind")
            context.user_data["flow"] = {"kind": "payment_method_new", "payment_kind": kind}
            instructions = (
                "عنوان | ارز | شماره کارت | نام صاحب کارت | توضیح اختیاری"
                if kind == "card" else
                "عنوان | ارز | آدرس کیف پول | شبکه | توضیح اختیاری"
            )
            await _render(update, f"➕ ثبت روش پرداخت\n\n{instructions}", back_keyboard("menu:payments"))
            return
        if data.startswith("payment:method:"):
            _, _, method_raw, status = data.split(":", 3)
            method_id = _parse_int(method_raw, "method id")
            _portal(context).set_payment_method_status(actor, method_id, status)
            await _show_payment_method(update, context, method_id)
            return
        if data.startswith("payment:receipt:"):
            receipt_id = _parse_int(data.rsplit(":", 1)[1], "receipt id")
            await _show_receipt(update, context, receipt_id, send_image=True)
            return
        if data.startswith("payment:approve:") or data.startswith("payment:reject:"):
            parts = data.split(":")
            action, receipt_raw = parts[1], parts[2]
            receipt_id = _parse_int(receipt_raw, "receipt id")
            if action == "reject":
                context.user_data["flow"] = {
                    "kind": "payment_reject_note",
                    "receipt_id": receipt_id,
                }
                await _render(
                    update,
                    "❌ دلیل رد پرداخت را بنویسید.\n"
                    "اگر توضیحی ندارید، فقط - بفرستید.",
                    back_keyboard(f"payment:receipt:{receipt_id}"),
                )
                return
            context.user_data["confirm"] = {
                "kind": "payment_review",
                "receipt_id": receipt_id,
                "approve": True,
                "review_note": "",
            }
            await _render(
                update,
                "✅ پرداخت تأیید شود؟ این عملیات فقط یک‌بار انجام می‌شود.",
                confirm_keyboard("payment_review", f"payment:receipt:{receipt_id}"),
            )
            return
        if data == "menu:customers":
            context.user_data.pop("platform_customer_query", None)
            await _show_platform_customers(update, context)
            return
        if data.startswith("customeradmin:page:"):
            await _show_platform_customers(
                update, context, _parse_int(data.rsplit(":", 1)[1], "page")
            )
            return
        if data == "customeradmin:search":
            context.user_data["flow"] = {"kind": "platform_customer_search"}
            await _render(
                update,
                "🔎 نام، یوزرنیم، شناسه تلگرام یا شناسه داخلی کاربر را بفرستید:",
                back_keyboard("customeradmin:page:0"),
            )
            return
        if data.startswith("customeradmin:view:"):
            await _show_platform_customer(
                update, context, _parse_int(data.rsplit(":", 1)[1], "customer id")
            )
            return
        if data.startswith("customeradmin:wallet:"):
            parts = data.split(":")
            if len(parts) != 4:
                raise ValueError("invalid wallet adjustment callback")
            direction = parts[2]
            customer_id = _parse_int(parts[3], "customer id")
            if direction not in ("add", "subtract"):
                raise ValueError("invalid wallet adjustment direction")
            _portal(context).get_platform_customer_admin(actor, customer_id)
            context.user_data["flow"] = {
                "kind": "customer_wallet_adjust",
                "customer_id": customer_id,
                "direction": direction,
            }
            label = "افزایش" if direction == "add" else "کاهش"
            await _render(
                update,
                f"👛 {label} موجودی کیف پول\n\n"
                "اطلاعات را بفرستید:\n"
                "ارز | مبلغ | یادداشت اختیاری\n\n"
                "مثال: IRR | 500000 | اصلاح پرداخت",
                back_keyboard(f"customeradmin:view:{customer_id}"),
            )
            return
        if data.startswith("customeradmin:status:"):
            _, _, customer_raw, status = data.split(":", 3)
            customer_id = _parse_int(customer_raw, "customer id")
            if status not in ("active", "blocked"):
                raise ValueError("invalid customer status")
            context.user_data["confirm"] = {
                "kind": "platform_customer_status",
                "customer_id": customer_id,
                "status": status,
            }
            await _render(
                update,
                "تغییر وضعیت این کاربر را تأیید می‌کنید؟",
                confirm_keyboard(
                    "platform_customer_status",
                    f"customeradmin:view:{customer_id}",
                ),
            )
            return
        if data == "menu:tenants":
            context.user_data.pop("tenant_query", None)
            await _show_tenants(update, context)
            return
        if data.startswith("tenant:page:"):
            await _show_tenants(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "tenant:new":
            context.user_data["flow"] = {
                "kind": "tenant_new_name",
                "cancel_target": "tenants",
            }
            await _prompt_flow(update, "🏪 نام فروشگاه را وارد کنید:")
            return
        if data == "tenant:provision":
            context.user_data["flow"] = {
                "kind": "provision_name",
                "cancel_target": "tenants",
            }
            await _prompt_flow(update, "🏪 نام فروشگاه را وارد کنید:")
            return
        if data == "tenant:search":
            context.user_data["flow"] = {"kind": "tenant_search"}
            await _render(update, "🔎 نام، شناسه انگلیسی یا شناسه مالک را بفرستید:", back_keyboard("tenant:page:0"))
            return
        if data.startswith("tenant:view:"):
            await _show_tenant(update, context, _parse_int(data.rsplit(":", 1)[1], "tenant id"))
            return
        if data.startswith("tenant:edit:"):
            tenant_id = _parse_int(data.rsplit(":", 1)[1], "tenant id")
            service.get_tenant(actor, tenant_id)
            context.user_data["flow"] = {"kind": "tenant_edit", "tenant_id": tenant_id}
            await _render(
                update,
                "✏️ اطلاعات جدید را بفرستید:\nنام | شناسه انگلیسی | شناسه عددی مالک",
                back_keyboard(f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:bot:"):
            _, _, tenant_raw, role = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if role not in ("admin", "user"):
                raise ValueError("invalid bot role")
            context.user_data["flow"] = {
                "kind": "bot_token", "tenant_id": tenant_id, "role": role
            }
            await _render(
                update,
                "🔐 توکن ربات را ارسال کنید. پیام توکن پس از دریافت حذف می‌شود.",
                back_keyboard(f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:webhook:"):
            _, _, tenant_raw, role = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if role not in ("admin", "user"):
                raise ValueError("invalid bot role")
            context.user_data["confirm"] = {
                "kind": "webhook_rotate",
                "tenant_id": tenant_id,
                "role": role,
            }
            await _render(
                update,
                "Secret فعلی بلافاصله نامعتبر می‌شود. Secret جدید ساخته شود؟",
                confirm_keyboard("webhook_rotate", f"tenant:view:{tenant_id}"),
            )
            return
        if data.startswith("tenant:status:"):
            _, _, tenant_raw, status = data.split(":", 3)
            tenant_id = _parse_int(tenant_raw, "tenant id")
            service.get_tenant(actor, tenant_id)
            if status not in ("active", "suspended", "disabled"):
                raise ValueError("invalid tenant status")
            context.user_data["confirm"] = {
                "kind": "tenant_status", "tenant_id": tenant_id, "status": status
            }
            await _render(
                update,
                f"آیا تغییر وضعیت مشتری به {status} تأیید می‌شود؟",
                confirm_keyboard("tenant_status", f"tenant:view:{tenant_id}"),
            )
            return

        if data == "menu:plans":
            context.user_data.pop("plan_query", None)
            await _show_plans(update, context)
            return
        if data.startswith("plan:page:"):
            await _show_plans(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "plan:new":
            context.user_data["flow"] = {
                "kind": "plan_new_name",
                "cancel_target": "plans",
            }
            await _prompt_flow(update, "📦 نام پلن را وارد کنید:")
            return
        if data == "plan:search":
            context.user_data["flow"] = {"kind": "plan_search"}
            await _render(update, "🔎 نام، وضعیت یا شناسه پلن را بفرستید:", back_keyboard("plan:page:0"))
            return
        if data.startswith("plan:view:"):
            await _show_plan(
                update, context, _parse_int(data.rsplit(":", 1)[1], "plan id")
            )
            return
        if data.startswith("plan:edit:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            service.get_plan(actor, plan_id)
            context.user_data["flow"] = {"kind": "plan_edit", "plan_id": plan_id}
            await _render(
                update,
                "✏️ پلن جدید را بفرستید:\nنام | روز | قیمت | حداکثر سرور | حداکثر کاربر",
                back_keyboard(f"plan:view:{plan_id}"),
            )
            return
        if data.startswith("plan:commerce:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            service.get_plan(actor, plan_id)
            context.user_data["flow"] = {"kind": "plan_commerce", "plan_id": plan_id}
            await _render(
                update,
                "💰 تنظیمات فروش پلن\n\n"
                "ارز و تعداد روز لایسنس تست را بفرستید:\n"
                "ارز | روزهای تست\n\n"
                "مثال: IRR | 3\n"
                "برای غیرفعال بودن تست، تعداد روز را 0 بزنید.",
                back_keyboard(f"plan:view:{plan_id}"),
            )
            return
        if data.startswith("plan:public:"):
            _, _, plan_raw, public_raw = data.split(":", 3)
            plan_id = _parse_int(plan_raw, "plan id")
            public_value = _parse_int(public_raw, "public flag")
            if public_value not in (0, 1):
                raise ValueError("invalid public flag")
            row = service.get_plan(actor, plan_id)
            _portal(context).configure_plan_commerce(
                actor,
                plan_id,
                currency=str(row.get("currency") or "USD"),
                is_public=bool(public_value),
                trial_days=int(row.get("trial_days") or 0),
            )
            await _show_plan(update, context, plan_id)
            return
        if data.startswith("plan:trial:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            _portal(context).set_trial_plan(actor, plan_id)
            await _show_plan(update, context, plan_id)
            return
        if data.startswith("plan:status:"):
            _, _, plan_raw, status = data.split(":", 3)
            plan_id = _parse_int(plan_raw, "plan id")
            service.get_plan(actor, plan_id)
            if status not in ("active", "archived", "disabled"):
                raise ValueError("invalid plan status")
            context.user_data["confirm"] = {
                "kind": "plan_status", "plan_id": plan_id, "status": status
            }
            await _render(
                update,
                f"آیا تغییر وضعیت پلن به {status} تأیید می‌شود؟",
                confirm_keyboard("plan_status", f"plan:view:{plan_id}"),
            )
            return

        if data == "menu:licenses":
            context.user_data.pop("license_query", None)
            await _show_licenses(update, context)
            return
        if data.startswith("license:page:"):
            await _show_licenses(update, context, _parse_int(data.rsplit(":", 1)[1], "page"))
            return
        if data == "license:new":
            context.user_data["flow"] = {
                "kind": "license_new_tenant",
                "cancel_target": "licenses",
            }
            await _prompt_flow(
                update,
                "برای خروج از ساخت لایسنس، «❌ لغو» را بزنید.",
            )
            await _show_license_tenant_picker(update, context)
            return
        if data.startswith("license:new:tenant-page:"):
            flow = context.user_data.get("flow")
            if not isinstance(flow, dict) or not str(flow.get("kind") or "").startswith(
                "license_new_"
            ):
                raise ValueError("license creation flow expired")
            page_number = _parse_int(data.rsplit(":", 1)[1], "page")
            flow["kind"] = "license_new_tenant"
            flow.pop("tenant_id", None)
            flow.pop("plan_id", None)
            await _show_license_tenant_picker(
                update, context, page_number=page_number
            )
            return
        if data.startswith("license:new:tenant:"):
            flow = context.user_data.get("flow")
            if not isinstance(flow, dict) or str(flow.get("kind") or "") != "license_new_tenant":
                raise ValueError("license tenant selection expired")
            tenant_id = _parse_int(data.rsplit(":", 1)[1], "tenant id")
            tenant = service.get_tenant(actor, tenant_id)
            flow["tenant_id"] = tenant_id
            flow["kind"] = "license_new_plan"
            await _show_license_plan_picker(
                update, context, tenant=tenant
            )
            return
        if data.startswith("license:new:plan:"):
            flow = context.user_data.get("flow")
            if not isinstance(flow, dict) or str(flow.get("kind") or "") != "license_new_plan":
                raise ValueError("license plan selection expired")
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            plan = service.get_plan(actor, plan_id)
            if str(plan.get("status") or "") != "active":
                raise ValueError("plan must be active")
            tenant = service.get_tenant(actor, int(flow["tenant_id"]))
            flow["plan_id"] = plan_id
            flow["kind"] = "license_new_grace"
            await query.edit_message_text(
                "✅ مشتری: "
                f"{tenant['name']} (#{int(tenant['id'])})\n"
                f"✅ پلن: {plan['name']} · {int(plan['duration_days'])} روز\n\n"
                "⏱ تعداد روز مهلت بعد از پایان لایسنس را وارد کنید.\n"
                "برای بدون مهلت، 0 بفرستید:"
            )
            return
        if data == "license:new:create-plan":
            flow = context.user_data.get("flow")
            if not isinstance(flow, dict) or str(flow.get("kind") or "") != "license_new_plan":
                raise ValueError("license plan selection expired")
            tenant = service.get_tenant(actor, int(flow.get("tenant_id") or 0))
            context.user_data["flow"] = {
                "kind": "plan_new_name",
                "cancel_target": "licenses",
                "license_tenant_id": int(tenant["id"]),
            }
            await _prompt_flow(update, "📦 نام پلن را وارد کنید:")
            return
        if data == "license:search":
            context.user_data["flow"] = {"kind": "license_search"}
            await _render(
                update,
                "🔎 شناسه لایسنس/مشتری/پلن یا نام مشتری/پلن را بفرستید:",
                back_keyboard("license:page:0"),
            )
            return
        if data.startswith("license:view:"):
            await _show_license(update, context, _parse_int(data.rsplit(":", 1)[1], "license id"))
            return
        if data.startswith("license:renew:"):
            license_id = _parse_int(data.rsplit(":", 1)[1], "license id")
            service.get_license(actor, license_id)
            context.user_data["flow"] = {"kind": "license_renew", "license_id": license_id}
            await _render(
                update,
                "♻️ مدت تمدید را بفرستید:\nتعداد روز | روز مهلت (اختیاری)",
                back_keyboard(f"license:view:{license_id}"),
            )
            return
        if data.startswith("license:suspend:") or data.startswith("license:reactivate:"):
            parts = data.split(":")
            kind = parts[1]
            license_id = _parse_int(parts[2], "license id")
            service.get_license(actor, license_id)
            context.user_data["confirm"] = {"kind": kind, "license_id": license_id}
            await _render(
                update,
                "این عملیات روی دسترسی مشتری اثر می‌گذارد. تأیید می‌کنید؟",
                confirm_keyboard(kind, f"license:view:{license_id}"),
            )
            return

        if data.startswith("confirm:"):
            expected = data.split(":", 1)[1]
            pending = context.user_data.pop("confirm", None)
            if not isinstance(pending, dict) or pending.get("kind") != expected:
                raise ValueError("confirmation expired")
            if expected == "wallet_adjust":
                result = _portal(context).adjust_customer_wallet(
                    actor,
                    int(pending["customer_id"]),
                    currency=str(pending["currency"]),
                    amount=int(pending["amount"]),
                    note=str(pending.get("note") or ""),
                )
                await update.effective_chat.send_message(
                    f"✅ کیف پول اصلاح شد.\n"
                    f"تغییر: {int(result['amount']):+,} {result['currency']}\n"
                    f"موجودی جدید: {int(result['balance']):,} {result['currency']}"
                )
                await _show_platform_customer(
                    update, context, int(result["customer_id"])
                )
                return
            if expected == "platform_customer_status":
                row = _portal(context).set_platform_customer_status(
                    actor, int(pending["customer_id"]), str(pending["status"])
                )
                await _show_platform_customer(update, context, int(row["id"]))
                return
            if expected == "tenant_status":
                row = service.set_tenant_status(
                    actor, int(pending["tenant_id"]), str(pending["status"])
                )
                await _show_tenant(update, context, int(row["id"]))
                return
            if expected == "webhook_rotate":
                tenant_id = int(pending["tenant_id"])
                enrollment = service.rotate_tenant_webhook_secret(
                    actor, tenant_id, role=str(pending["role"])
                )
                await update.effective_chat.send_message(
                    f"🔑 Webhook secret جدید {enrollment.bot.role}:\n"
                    f"{enrollment.webhook_secret}\n\n"
                    "پس از استفاده این پیام را حذف کنید.",
                    protect_content=True,
                )
                await _show_tenant(update, context, tenant_id)
                return
            if expected == "plan_status":
                row = service.set_plan_status(
                    actor, int(pending["plan_id"]), str(pending["status"])
                )
                await _show_plan(update, context, int(row["id"]))
                return
            if expected == "renew":
                row = service.renew(
                    actor,
                    int(pending["license_id"]),
                    extra_days=int(pending["extra_days"]),
                    grace_days=int(pending["grace_days"]),
                )
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "suspend":
                row = service.suspend(actor, int(pending["license_id"]))
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "reactivate":
                row = service.reactivate(actor, int(pending["license_id"]))
                await _show_license(update, context, int(row["id"]))
                return
            if expected == "payment_review":
                approved = bool(pending["approve"])
                reviewed = _portal(context).review_receipt(
                    actor,
                    int(pending["receipt_id"]),
                    approve=approved,
                    review_note=str(pending.get("review_note") or ""),
                )
                if not approved:
                    note = str(reviewed.get("review_note") or "").strip()
                    notification = f"❌ پرداخت سفارش {reviewed['public_id']} تأیید نشد."
                    if note:
                        notification += f"\n\nعلت: {note}"
                    notification += "\n\nدر صورت نیاز با پشتیبانی تماس بگیرید."
                elif reviewed["order_kind"] == "wallet_topup":
                    notification = (
                        f"✅ شارژ کیف پول {reviewed['public_id']} تأیید شد.\n"
                        f"مبلغ: {int(reviewed['amount']):,} {reviewed['currency']}"
                    )
                elif reviewed["order_kind"] == "renewal":
                    notification = (
                        f"✅ تمدید {reviewed['public_id']} با موفقیت انجام شد.\n"
                        f"مبلغ: {int(reviewed['amount']):,} {reviewed['currency']}"
                    )
                elif reviewed["order_status"] == "paid":
                    notification = (
                        f"✅ پرداخت سفارش {reviewed['public_id']} تأیید شد.\n\n"
                        "🔑 اکنون از بخش «راه‌اندازی ربات» ادامه دهید."
                    )
                else:
                    notification = f"✅ پرداخت سفارش {reviewed['public_id']} تأیید شد."
                try:
                    await context.bot.send_message(
                        chat_id=int(reviewed["telegram_user_id"]), text=notification
                    )
                except Exception as exc:
                    logger.warning(
                        "Payment notification could not be sent: %s",
                        safe_format_exception(exc),
                    )
                await _show_payments(update, context)
                return
            raise ValueError("unknown confirmation")

        if data == "menu:stats":
            stats = service.statistics(actor)
            finance = _portal(context).financial_summary(actor)
            text, keyboard = financial_dashboard_view(stats, finance)
            await _render(update, text, keyboard)
            return
        if data == "menu:warnings" or data.startswith("warning:page:"):
            page_no = 0 if data == "menu:warnings" else _parse_int(data.rsplit(":", 1)[1], "page")
            page = service.list_warnings(actor, page=page_no)
            buttons = []
            if page.has_previous:
                buttons.append(InlineKeyboardButton("⬅️", callback_data=f"warning:page:{page.page-1}"))
            if page.has_next:
                buttons.append(InlineKeyboardButton("➡️", callback_data=f"warning:page:{page.page+1}"))
            rows = [buttons] if buttons else []
            rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
            await _render(update, warnings_text(page.items), InlineKeyboardMarkup(rows))
            return
        if data == "menu:audit" or data.startswith("audit:page:"):
            page_no = 0 if data == "menu:audit" else _parse_int(data.rsplit(":", 1)[1], "page")
            page = service.list_audit(actor, page=page_no)
            buttons = []
            if page.has_previous:
                buttons.append(InlineKeyboardButton("⬅️", callback_data=f"audit:page:{page.page-1}"))
            if page.has_next:
                buttons.append(InlineKeyboardButton("➡️", callback_data=f"audit:page:{page.page+1}"))
            rows = [buttons] if buttons else []
            rows.append([InlineKeyboardButton("↩️ منوی اصلی", callback_data="menu:main")])
            await _render(update, audit_text(page.items), InlineKeyboardMarkup(rows))
            return
        if data == "menu:settings":
            await _show_settings(update, context)
            return
        if data == "settings:trialplan":
            settings = _portal(context).get_platform_settings(actor)
            plans = _portal(context).list_trial_plan_candidates(actor)
            text, keyboard = trial_plan_picker(
                plans, selected_plan_id=settings.get("trial_plan_id")
            )
            await _render(update, text, keyboard)
            return
        if data == "settings:trialplan:auto":
            _portal(context).set_trial_plan(actor, None)
            await _show_settings(update, context)
            return
        if data.startswith("settings:trialplan:set:"):
            plan_id = _parse_int(data.rsplit(":", 1)[1], "plan id")
            _portal(context).set_trial_plan(actor, plan_id)
            await _show_settings(update, context)
            return
        if data.startswith("settings:edit:"):
            key = data.rsplit(":", 1)[1]
            prompts = {
                "store_name": "🏷 نام جدید فروشگاه را بفرستید:",
                "support_contact": "☎️ آیدی یا راه ارتباطی پشتیبانی را بفرستید. برای حذف، - ارسال کنید.",
                "maintenance_message": "📝 پیام زمان توقف فروش را بفرستید:",
            }
            if key not in prompts:
                raise ValueError("invalid setting key")
            context.user_data["flow"] = {"kind": "platform_setting_text", "setting_key": key}
            await _render(update, prompts[key], back_keyboard("menu:settings"))
            return
        if data == "settings:toggle:sales":
            current = _portal(context).get_platform_settings(actor)
            _portal(context).set_platform_bool_setting(
                actor, "sales_enabled", not bool(current["sales_enabled"])
            )
            await _show_settings(update, context)
            return
        if data == "settings:toggle:trial":
            current = _portal(context).get_platform_settings(actor)
            _portal(context).set_platform_bool_setting(
                actor, "trial_enabled", not bool(current["trial_enabled"])
            )
            await _show_settings(update, context)
            return
        if data == "menu:backup":
            await _show_backup(update, context)
            return
        if data == "backup:auto:toggle":
            status = _portal(context).get_backup_status(actor)
            _portal(context).set_backup_auto(
                actor, not bool(status["auto_backup_enabled"])
            )
            await _show_backup(update, context)
            return
        if data.startswith("backup:interval:"):
            try:
                hours = int(data.rsplit(":", 1)[1])
                _portal(context).set_backup_interval(actor, hours)
            except (ValueError, TypeError):
                pass
            await _show_backup(update, context)
            return
        if data == "backup:now":
            passphrase = _backup_passphrase()
            if passphrase:
                await _do_full_backup(update, context, passphrase)
                passphrase = ""
                return
            context.user_data["flow"] = {"kind": "backup_passphrase"}
            await _render(
                update,
                "🔐 رمز بکاپ را بفرستید (حداقل ۱۲ کاراکتر).\n"
                "پیام شما بلافاصله حذف می‌شود و رمز هیچ‌جا ذخیره نمی‌شود.",
                back_keyboard("menu:backup"),
            )
            return
        if data == "backup:restore":
            context.user_data["flow"] = {"kind": "backup_restore_upload"}
            await _render(
                update,
                "📥 فایل بکاپ (`.wlbak`) را همین‌جا ارسال کنید.\n\n"
                "اگر رمز بکاپ را قبلاً با دکمه «🔐 تنظیم رمز بکاپ» ذخیره کرده باشید،\n"
                "فایل خودکار بررسی می‌شود؛ وگرنه بعد از آپلود رمز پرسیده می‌شود.\n"
                "بازیابی نهایی باید از ترمینال و با سرویس‌های متوقف انجام شود.",
                back_keyboard("menu:backup"),
            )
            return
        if data == "backup:passphrase":
            context.user_data["flow"] = {"kind": "backup_set_passphrase"}
            await _render(
                update,
                "🔐 رمز جدید بکاپ را بفرستید (حداقل ۱۲ کاراکتر).\n"
                "پیام شما بلافاصله حذف می‌شود و رمز فقط در فایل امن سرور ذخیره می‌شود.\n"
                "از این به بعد بکاپ فوری و بازیابی با همین رمز کار می‌کنند.",
                back_keyboard("menu:backup"),
            )
            return
        raise ValueError("unknown callback")
    except (
        ValueError,
        sqlite3.IntegrityError,
        MasterServiceError,
        NotFoundError,
        ProvisioningError,
    ) as exc:
        logger.warning("MasterBot callback rejected: %s", safe_format_exception(exc))
        await _render(update, "❌ درخواست معتبر نیست یا دیگر قابل اجرا نیست.", back_keyboard())


def _split_fields(text: str, minimum: int, maximum: int) -> list[str]:
    parts = [part.strip() for part in str(text or "").split("|")]
    if not minimum <= len(parts) <= maximum or any(not part for part in parts[:minimum]):
        raise ValueError("invalid input format")
    return parts


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        if await handle_customer_text(update, context):
            return
        await show_customer_home(update, context)
        return
    flow = context.user_data.get("flow")
    if not isinstance(flow, dict) or update.effective_message is None:
        await show_main(update, context)
        return
    kind = str(flow.get("kind") or "")
    actor = int(_actor_id(update) or 0)
    service = _service(context)
    text = str(update.effective_message.text or "").strip()

    if text == FLOW_CANCEL_TEXT:
        target = str(flow.get("cancel_target") or "")
        if not target:
            if kind.startswith(("tenant_new_", "provision_")):
                target = "tenants"
            elif kind.startswith("plan_new_"):
                target = "plans"
            elif kind.startswith("license_new_"):
                target = "licenses"
            else:
                target = "main"
        context.user_data.pop("flow", None)
        context.user_data.pop("confirm", None)
        await _remove_flow_keyboard(update, "❌ عملیات لغو شد.")
        if target == "tenants":
            await _show_tenants(update, context)
        elif target == "plans":
            await _show_plans(update, context)
        elif target == "licenses":
            await _show_licenses(update, context)
        else:
            await show_main(update, context)
        return

    try:
        if kind in ("tenant_new_name", "provision_name"):
            name = text.strip()
            if not name:
                raise ValueError("shop name is required")
            flow["name"] = name[:120]
            flow["kind"] = (
                "tenant_new_owner" if kind == "tenant_new_name" else "provision_owner"
            )
            await update.effective_message.reply_text(
                "👤 شناسه عددی تلگرام مالک فروشگاه را وارد کنید:",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind in ("tenant_new_owner", "provision_owner"):
            owner_id = _parse_int(text, "owner id")
            if owner_id <= 0:
                raise ValueError("owner id must be positive")
            flow["owner_telegram_id"] = owner_id
            flow["kind"] = (
                "tenant_new_slug" if kind == "tenant_new_owner" else "provision_slug"
            )
            await update.effective_message.reply_text(
                "🔤 شناسه انگلیسی فروشگاه را وارد کنید.\n"
                "مثال: speedtest",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind in ("tenant_new_slug", "provision_slug"):
            slug = text.strip()
            if not slug:
                raise ValueError("slug is required")
            flow["slug"] = slug
            if kind == "tenant_new_slug":
                row = service.create_tenant(
                    actor,
                    name=str(flow["name"]),
                    slug=str(flow["slug"]),
                    owner_telegram_id=int(flow["owner_telegram_id"]),
                )
                context.user_data.pop("flow", None)
                await _remove_flow_keyboard(update, "✅ فروشگاه ثبت شد.")
                await _show_tenant(update, context, int(row["id"]))
                return
            flow["kind"] = "provision_admin_token"
            await update.effective_message.reply_text(
                "🔐 توکن ربات مدیریت مشتری را بفرستید؛ پیام بلافاصله حذف می‌شود.",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind in ("provision_admin_token", "provision_user_token"):
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete a provisioning token message: %s",
                    safe_format_exception(exc),
                )
            role = "admin" if kind == "provision_admin_token" else "user"
            prepared = await service.prepare_tenant_bot(
                actor, role=role, plain_token=text
            )
            text = ""
            if role == "admin":
                flow["admin_bot"] = prepared
                flow["kind"] = "provision_user_token"
                await update.effective_chat.send_message(
                    "✅ ربات مدیریت تأیید شد. اکنون توکن ربات کاربران را بفرستید.",
                    reply_markup=_flow_cancel_keyboard(),
                )
                return
            admin_bot = flow.get("admin_bot")
            if not isinstance(admin_bot, PreparedBot):
                raise ProvisioningError("provisioning draft expired")
            result = service.provision_tenant_prepared(
                actor,
                name=str(flow["name"]),
                slug=str(flow["slug"]),
                owner_telegram_id=int(flow["owner_telegram_id"]),
                admin_bot=admin_bot,
                user_bot=prepared,
            )
            context.user_data.pop("flow", None)
            await _remove_flow_keyboard(update, "✅ راه‌اندازی فروشگاه تکمیل شد.")
            await update.effective_chat.send_message(
                "🔐 کلیدهای یک‌بارمصرف وب‌هوک\n\n"
                f"ربات مدیریت: {result.webhook_secrets.admin}\n"
                f"ربات کاربران: {result.webhook_secrets.user}\n\n"
                "پس از ثبت وب‌هوک این پیام را حذف کنید؛ فقط نسخه هش‌شده در دیتابیس ذخیره شده است.",
                protect_content=True,
            )
            row = service.get_tenant(actor, result.tenant_id)
            state = service.provisioning_status(actor, result.tenant_id)
            detail, keyboard = tenant_detail(
                row,
                {
                    "admin": state.admin_bot,
                    "user": state.user_bot,
                    "ready": state.ready,
                    "runtime_status": state.runtime_status,
                },
            )
            await update.effective_chat.send_message(detail, reply_markup=keyboard)
            return
        if kind == "tenant_edit":
            fields = _split_fields(text, 3, 3)
            row = service.update_tenant(
                actor,
                int(flow["tenant_id"]),
                name=fields[0],
                slug=fields[1],
                owner_telegram_id=_parse_int(fields[2], "owner id"),
            )
            context.user_data.pop("flow", None)
            await _show_tenant(update, context, int(row["id"]))
            return
        if kind == "payment_reject_note":
            receipt_id = int(flow["receipt_id"])
            note = "" if text == "-" else text[:300]
            context.user_data.pop("flow", None)
            context.user_data["confirm"] = {
                "kind": "payment_review",
                "receipt_id": receipt_id,
                "approve": False,
                "review_note": note,
            }
            await update.effective_message.reply_text(
                "❌ رد این پرداخت تأیید شود؟"
                + (f"\n\nعلت: {note}" if note else ""),
                reply_markup=confirm_keyboard(
                    "payment_review", f"payment:receipt:{receipt_id}"
                ),
            )
            return
        if kind == "customer_wallet_adjust":
            fields = _split_fields(text, 2, 3)
            currency = fields[0].upper()
            amount = _parse_int(fields[1].replace(",", ""), "amount")
            if amount <= 0:
                raise ValueError("amount must be positive")
            direction = str(flow.get("direction") or "")
            delta = amount if direction == "add" else -amount
            customer_id = int(flow["customer_id"])
            note = fields[2] if len(fields) == 3 else ""
            context.user_data.pop("flow", None)
            context.user_data["confirm"] = {
                "kind": "wallet_adjust",
                "customer_id": customer_id,
                "currency": currency,
                "amount": delta,
                "note": note,
            }
            label = "افزایش" if delta > 0 else "کاهش"
            await update.effective_message.reply_text(
                f"👛 {label} {amount:,} {currency} برای کیف پول این مشتری تأیید شود؟",
                reply_markup=confirm_keyboard(
                    "wallet_adjust", f"customeradmin:view:{customer_id}"
                ),
            )
            return
        if kind == "receipt_search":
            context.user_data.pop("flow", None)
            context.user_data["receipt_query"] = text[:100]
            await _show_payment_history(update, context)
            return
        if kind == "order_search":
            context.user_data.pop("flow", None)
            context.user_data["order_query"] = text[:100]
            await _show_orders(update, context)
            return
        if kind == "platform_customer_search":
            context.user_data.pop("flow", None)
            context.user_data["platform_customer_query"] = text[:100]
            await _show_platform_customers(update, context)
            return
        if kind == "tenant_search":
            context.user_data.pop("flow", None)
            context.user_data["tenant_query"] = text[:100]
            await _show_tenants(update, context)
            return
        if kind == "plan_search":
            context.user_data.pop("flow", None)
            context.user_data["plan_query"] = text[:100]
            await _show_plans(update, context)
            return
        if kind == "license_search":
            context.user_data.pop("flow", None)
            context.user_data["license_query"] = text[:100]
            await _show_licenses(update, context)
            return
        if kind == "plan_new_name":
            name = text.strip()
            if not name:
                raise ValueError("plan name is required")
            flow["name"] = name[:120]
            flow["kind"] = "plan_new_days"
            await update.effective_message.reply_text(
                "⏳ مدت پلن را به روز وارد کنید.\nمثال: 30",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind == "plan_new_days":
            days = _parse_int(text, "days")
            if days <= 0:
                raise ValueError("days must be positive")
            flow["duration_days"] = days
            flow["kind"] = "plan_new_price"
            await update.effective_message.reply_text(
                "💰 قیمت پلن را وارد کنید.\nبرای پلن رایگان عدد 0 را بفرستید.",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind == "plan_new_price":
            flow["price"] = _parse_int(text.replace(",", ""), "price")
            flow["kind"] = "plan_new_max_servers"
            await update.effective_message.reply_text(
                "🖥 حداکثر تعداد سرور مجاز این پلن را وارد کنید:",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind == "plan_new_max_servers":
            flow["max_servers"] = _parse_int(text, "max servers")
            flow["kind"] = "plan_new_max_users"
            await update.effective_message.reply_text(
                "👥 حداکثر تعداد کاربر مجاز این پلن را وارد کنید:",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind == "plan_new_max_users":
            max_users = _parse_int(text, "max users")
            license_tenant_id = int(flow.get("license_tenant_id") or 0)
            row = service.create_plan(
                actor,
                name=str(flow["name"]),
                duration_days=int(flow["duration_days"]),
                price=int(flow["price"]),
                max_servers=int(flow["max_servers"]),
                max_users=max_users,
            )
            if license_tenant_id:
                # Came from license issuance: auto-select the new plan and
                # continue the flow instead of dropping the tenant context.
                tenant = service.get_tenant(actor, license_tenant_id)
                context.user_data["flow"] = {
                    "kind": "license_new_grace",
                    "tenant_id": license_tenant_id,
                    "plan_id": int(row["id"]),
                    "cancel_target": "licenses",
                }
                await _remove_flow_keyboard(update, "✅ پلن ثبت شد.")
                await update.effective_message.reply_text(
                    "✅ مشتری: "
                    f"{tenant['name']} (#{license_tenant_id})\n"
                    f"✅ پلن: {row['name']} · {int(row['duration_days'])} روز\n\n"
                    "⏱ تعداد روز مهلت بعد از پایان لایسنس را وارد کنید.\n"
                    "برای بدون مهلت، 0 بفرستید:"
                )
                return
            context.user_data.pop("flow", None)
            await _remove_flow_keyboard(update, "✅ پلن ثبت شد.")
            await _show_plan(update, context, int(row["id"]))
            return
        if kind == "plan_edit":
            fields = _split_fields(text, 5, 5)
            kwargs: dict[str, Any] = {
                "name": fields[0],
                "duration_days": _parse_int(fields[1], "days"),
                "price": _parse_int(fields[2], "price"),
                "max_servers": _parse_int(fields[3], "max servers"),
                "max_users": _parse_int(fields[4], "max users"),
            }
            row = service.update_plan(actor, int(flow["plan_id"]), **kwargs)
            context.user_data.pop("flow", None)
            await _show_plan(update, context, int(row["id"]))
            return
        if kind == "plan_commerce":
            fields = _split_fields(text, 2, 2)
            plan_id = int(flow["plan_id"])
            current = service.get_plan(actor, plan_id)
            _portal(context).configure_plan_commerce(
                actor,
                plan_id,
                currency=fields[0],
                is_public=bool(int(current.get("is_public", 1))),
                trial_days=_parse_int(fields[1], "trial days"),
            )
            context.user_data.pop("flow", None)
            await _show_plan(update, context, plan_id)
            return
        if kind in ("payment_method_new", "payment_method_edit"):
            fields = _split_fields(text, 4, 5)
            payment_kind = str(flow.get("payment_kind") or "")
            if payment_kind not in ("card", "crypto"):
                raise ValueError("invalid payment kind")
            if payment_kind == "card":
                title, currency, destination, recipient = fields[:4]
                network = None
            else:
                title, currency, destination, network = fields[:4]
                recipient = None
            instructions = fields[4] if len(fields) == 5 else ""
            if kind == "payment_method_new":
                row = _portal(context).add_payment_method(
                    actor, kind=payment_kind, title=title, currency=currency,
                    destination=destination, recipient=recipient, network=network,
                    instructions=instructions,
                )
            else:
                row = _portal(context).update_payment_method(
                    actor, int(flow["method_id"]), title=title, currency=currency,
                    destination=destination, recipient=recipient, network=network,
                    instructions=instructions,
                )
            context.user_data.pop("flow", None)
            await _show_payment_method(update, context, int(row["id"]))
            return
        if kind == "platform_setting_text":
            key = str(flow.get("setting_key") or "")
            _portal(context).set_platform_text_setting(actor, key, text)
            context.user_data.pop("flow", None)
            await _show_settings(update, context)
            return
        if kind == "backup_passphrase":
            passphrase = text
            text = ""
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete the submitted backup passphrase: %s",
                    safe_format_exception(exc),
                )
            context.user_data.pop("flow", None)
            try:
                await _do_full_backup(update, context, passphrase)
            finally:
                passphrase = ""
            return
        if kind == "backup_restore_passphrase":
            passphrase = text
            text = ""
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete the submitted restore passphrase: %s",
                    safe_format_exception(exc),
                )
            backup_file = str(flow.get("backup_file") or "")
            context.user_data.pop("flow", None)
            try:
                await _verify_restored_backup(update, passphrase, backup_file)
            finally:
                passphrase = ""
            return
        if kind == "backup_set_passphrase":
            passphrase = text
            text = ""
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete the submitted backup passphrase: %s",
                    safe_format_exception(exc),
                )
            if len(passphrase.encode("utf-8")) < 12:
                await _render(
                    update,
                    "❌ رمز بکاپ باید حداقل ۱۲ کاراکتر باشد. دوباره بفرستید.",
                    back_keyboard("menu:backup"),
                )
                passphrase = ""
                return
            context.user_data.pop("flow", None)
            try:
                _portal(context).set_backup_passphrase(
                    actor, passphrase, env_path=str(_master_env_file())
                )
            except (ValueError, OSError) as exc:
                logger.warning(
                    "MasterBot backup passphrase save failed: %s",
                    safe_format_exception(exc),
                )
                await _render(
                    update,
                    "❌ ذخیره رمز ناموفق بود. دوباره تلاش کنید.",
                    back_keyboard("menu:backup"),
                )
                return
            finally:
                passphrase = ""
            if update.effective_chat is not None:
                await update.effective_chat.send_message(
                    "✅ رمز بکاپ ذخیره شد.\n"
                    "از این به بعد بکاپ فوری و بازیابی با همین رمز کار می‌کنند."
                )
            await _show_backup(update, context)
            return
        if kind in ("license_new_tenant", "license_new_plan"):
            await update.effective_message.reply_text(
                "👆 مشتری و پلن را فقط با دکمه‌های پیام بالا انتخاب کنید.",
                reply_markup=_flow_cancel_keyboard(),
            )
            return
        if kind == "license_new_grace":
            grace_days = _parse_int(text, "grace days")
            row = service.create_license(
                actor,
                tenant_id=int(flow["tenant_id"]),
                plan_id=int(flow["plan_id"]),
                grace_days=grace_days,
            )
            context.user_data.pop("flow", None)
            await _remove_flow_keyboard(update, "✅ لایسنس ثبت شد.")
            await _show_license(update, context, int(row["id"]))
            return
        if kind == "license_renew":
            fields = _split_fields(text, 1, 2)
            days = _parse_int(fields[0], "days")
            if days <= 0:
                raise ValueError("days must be positive")
            grace = _parse_int(fields[1], "grace days") if len(fields) == 2 else 0
            context.user_data.pop("flow", None)
            context.user_data["confirm"] = {
                "kind": "renew",
                "license_id": int(flow["license_id"]),
                "extra_days": days,
                "grace_days": grace,
            }
            await update.effective_message.reply_text(
                f"تمدید {days} روزه با {grace} روز مهلت تأیید شود؟",
                reply_markup=confirm_keyboard("renew", f"license:view:{int(flow['license_id'])}"),
            )
            return
        if kind == "bot_token":
            # Never keep the raw token in user_data, logs or an exception.
            try:
                await update.effective_message.delete()
            except Exception as exc:
                logger.warning(
                    "Could not delete the submitted token message: %s",
                    safe_format_exception(exc),
                )
            enrollment = await service.register_tenant_bot_with_secret(
                actor,
                int(flow["tenant_id"]),
                role=str(flow["role"]),
                plain_token=text,
            )
            text = ""
            context.user_data.pop("flow", None)
            await update.effective_chat.send_message(
                f"✅ ربات @{enrollment.bot.telegram_username or 'unknown'} ثبت شد.\n"
                f"کلید وب‌هوک: {enrollment.webhook_secret}\n"
                "این کلید فقط همین یک‌بار نمایش داده می‌شود؛ پس از استفاده پیام را حذف کنید.",
                reply_markup=back_keyboard(f"tenant:view:{int(flow['tenant_id'])}"),
                protect_content=True,
            )
            return
        raise ValueError("unknown input flow")
    except (
        ValueError,
        sqlite3.IntegrityError,
        MasterServiceError,
        NotFoundError,
        ProvisioningError,
        CustomerPortalError,
    ) as exc:
        # Flow remains active so the owner can correct the input. The actual
        # message content is deliberately absent from this log entry.
        logger.warning("MasterBot input rejected: %s", safe_format_exception(exc))
        error_text = "❌ مقدار واردشده معتبر نیست. دوباره تلاش کنید."
        if kind in ("bot_token", "provision_admin_token", "provision_user_token"):
            keyboard = (
                _flow_cancel_keyboard()
                if kind in ("provision_admin_token", "provision_user_token")
                else back_keyboard()
            )
            await update.effective_chat.send_message(
                error_text, reply_markup=keyboard
            )
        else:
            if kind.startswith(("tenant_new_", "provision_")):
                keyboard = _flow_cancel_keyboard()
            elif kind.startswith("plan_new_"):
                keyboard = _flow_cancel_keyboard()
            elif kind.startswith("license_new_"):
                keyboard = _flow_cancel_keyboard()
            else:
                keyboard = back_keyboard()
            await update.effective_message.reply_text(
                error_text, reply_markup=keyboard
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled MasterBot error: %s", safe_format_exception(context.error or RuntimeError()))


def register_handlers(application: Application) -> None:
    # Group -1 is an application-wide role firewall.  Customer traffic is
    # restricted to its namespaced callbacks while all owner callbacks remain
    # blocked before their handlers run.
    application.add_handler(TypeHandler(Update, access_gate), group=-1)
    application.add_handler(CommandHandler("start", show_main), group=0)
    application.add_handler(CommandHandler("menu", show_main), group=0)
    application.add_handler(CommandHandler("cancel", cancel), group=0)
    application.add_handler(CallbackQueryHandler(on_callback), group=0)
    application.add_handler(MessageHandler(filters.PHOTO, handle_customer_photo), group=0)
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_backup_document), group=0
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=0)
    application.add_error_handler(on_error)
