"""SellBot-style manual ZIP: this tenant's data plus native panel backups."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile

from telegram.error import TelegramError

from Database.connection import transaction
from TenantRuntime.backup import (
    build_tenant_snapshot,
    encode_tenant_backup,
    MAX_BACKUP_BYTES,
)
from TenantRuntime.panel_backups import PanelBackup, safe_name

MAX_TELEGRAM_BACKUP_BYTES = 48_000_000


def build_full_archive(snapshot, panels, errors):
    bot = encode_tenant_backup(snapshot)
    with zipfile.ZipFile(io.BytesIO(bot.data)) as source:
        payload = source.read("tenant.json")
        manifest = json.loads(source.read("manifest.json"))
    manifest.update(backup_type="full", panel_backups=[], panel_errors=errors)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tenant.json", payload)
        for server, artifact in panels:
            path = (
                f"PanelBackups/{int(server['id'])}_{safe_name(server['label'])}/"
                f"{safe_name(artifact.filename, 'backup.bin')}"
            )
            archive.writestr(path, artifact.content)
            manifest["panel_backups"].append(
                {
                    "path": path,
                    "server_id": int(server["id"]),
                    "server_title": server["label"],
                    "panel_kind": server["panel_kind"],
                    "size": len(artifact.content),
                    "sha256": hashlib.sha256(artifact.content).hexdigest(),
                }
            )
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    data = buf.getvalue()
    if len(data) > MAX_TELEGRAM_BACKUP_BYTES:
        raise ValueError("backup exceeds Telegram document limit")
    filename = (
        f"Backup_All_Tenant_{bot.tenant_id}_{safe_name(snapshot['created_at'])}.zip"
    )
    return filename, data


async def send_full_backup(update, context, business, actor):
    business._admin(actor)
    # Do not send a tenant's private panel data to a group from a menu click.
    if int(update.effective_chat.id) != int(actor):
        await update.effective_message.reply_text(
            "📫 برای دریافت بکاپ، در گفت‌وگوی خصوصی ربات اقدام کنید."
        )
        return
    key = "full_backup_running"
    if context.user_data.get(key):
        await update.effective_message.reply_text(
            "⏳ بکاپ قبلی هنوز در حال آماده‌سازی است."
        )
        return
    context.user_data[key] = True
    try:
        await update.effective_message.reply_text(
            "⏳ در حال تهیه بکاپ کامل (ربات + سرورها/نودها)..."
        )
        # SQLite must stay on its owning event-loop thread. Compression and
        # network work run off-thread after the tenant-only snapshot is taken.
        with transaction(business.conn):
            snapshot = build_tenant_snapshot(
                business.conn, tenant_id=business.tenant_id
            )
        panels, errors = [], []
        total = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        for server in business.list_servers():
            try:
                _, target, secret = business._panel_material(
                    server["id"], allow_inactive=True
                )
                method = getattr(business.panel_adapter, "download_backup", None)
                if not callable(method):
                    raise ValueError("unsupported panel")
                try:
                    artifact = await asyncio.to_thread(
                        method, target=target, secret=secret
                    )
                finally:
                    secret = ""
                if not isinstance(artifact, PanelBackup) or not artifact.content:
                    raise ValueError("invalid panel backup")
                # Leave headroom for the manifest and avoid an unrestorable ZIP.
                if total + len(artifact.content) > MAX_BACKUP_BYTES - 1024 * 1024:
                    raise ValueError("full archive size limit")
                panels.append((server, artifact))
                total += len(artifact.content)
            except Exception:
                # Never include raw HTTP errors, admin URLs or credentials.
                errors.append(
                    {
                        "server_id": int(server["id"]),
                        "server_title": server["label"],
                        "message": "دریافت بکاپ پنل انجام نشد؛ اتصال، دسترسی و حجم فایل را بررسی کنید.",
                    }
                )
        try:
            filename, data = await asyncio.to_thread(
                build_full_archive, snapshot, panels, errors
            )
        except ValueError:
            await update.effective_message.reply_text(
                "❌ حجم بکاپ از حد مجاز ارسال تلگرام بیشتر است. بکاپ داده‌های ربات را از تنظیمات بکاپ دریافت کنید."
            )
            return
        caption = (
            "📬 فایل بکاپ کامل آماده شد\n🤖 بکاپ ربات: ✅\n"
            f"🖥️ بکاپ سرورها/نودها: {len(panels)} مورد\n⚠️ خطاها: {len(errors)} مورد"
        )
        try:
            await update.effective_chat.send_document(
                document=io.BytesIO(data),
                filename=filename,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
        except TelegramError:
            await update.effective_message.reply_text(
                "❌ ارسال فایل بکاپ ناموفق بود؛ دوباره «دریافت بکاپ» را بزنید."
            )
            return
        if errors:
            preview = "\n".join(
                f"• {e['server_title']} (id={e['server_id']})" for e in errors[:10]
            )
            if len(errors) > 10:
                preview += f"\n... و {len(errors) - 10} سرور دیگر"
            await update.effective_message.reply_text(
                "⚠️ بکاپ این پنل‌ها دریافت نشد؛ فایل ارسالی شامل بکاپ ربات و پنل‌های موفق است:\n"
                + preview
            )
    except Exception:
        await update.effective_message.reply_text(
            "❌ تهیهٔ بکاپ انجام نشد؛ دوباره تلاش کنید."
        )
    finally:
        context.user_data.pop(key, None)
