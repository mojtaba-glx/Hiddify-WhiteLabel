"""Phase 13: SellBot-style broadcast and channel publishing for Tenant AdminBot.

The flows are isolated here so broadcast/channel media state cannot collide with
payments, tickets or other AdminBot wizards. AdminBot-owned Telegram file IDs
are always downloaded before they cross the sibling UserBot token boundary.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape as html_unescape
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from telegram import Bot, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import ContextTypes

from Database.repositories import BotRepository
from Shared.crypto import fingerprint_token
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.button_styles import (
    inline_button as InlineKeyboardButton,
    keyboard_button as KeyboardButton,
    set_button_settings,
)

FLOW_KEY = "stage13_broadcast_channel_flow"
BROADCAST_DRAFT_KEY = "tenant_broadcast_draft"
CHANNEL_DRAFT_KEY = "tenant_channel_post_draft"
MAX_BUTTONS = 8
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
SKIP_TEXT = "⏩رد کردن"

SEGMENT_LABELS = {
    "all": "تمام کاربران",
    "expired_all": "تمام کاربران منقضی شده",
    "no_order": "کاربران بدون سفارش",
    "expired_1w": "کاربران منقضی شده بیش از یک هفته",
    "expired_2w": "کاربران منقضی شده بیش از دو هفته",
    "expired_4w": "کاربران منقضی شده بیش از چهار هفته",
    "expired_8w": "کاربران منقضی شده بیش از هشت هفته",
}


def _cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌لغو")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _skip_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(SKIP_TEXT)], [KeyboardButton("❌لغو")]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _is_cancel(value: str) -> bool:
    clean = str(value or "").strip().replace("\u200c", "")
    return clean in {"❌لغو", "❌ لغو", "لغو", "/cancel"}


def _is_skip(value: str) -> bool:
    raw = "".join(str(value or "").strip().split())
    for mark in ("\ufe0f", "\u200c", "\u200e", "\u200f"):
        raw = raw.replace(mark, "")
    return raw in {"⏩ردکردن", "ردکردن", "⏭ردکردن", "▶ردکردن"}


def _normalize_button_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        username = raw[1:].strip()
        return (
            f"https://t.me/{username}"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else ""
        )
    if raw.lower().startswith("t.me/"):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    scheme = str(parsed.scheme or "").lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return raw
    if scheme == "tg" and (parsed.netloc or parsed.path):
        return raw
    return ""


def _normalize_channel_target(value: str) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"-100[0-9]{5,20}", raw):
        return raw
    if raw.startswith("@"):
        username = raw[1:].strip()
        return (
            f"@{username}"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else ""
        )
    if raw.lower().startswith(("t.me/", "telegram.me/")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
        "t.me", "telegram.me", "www.t.me",
    }:
        username = parsed.path.strip("/").split("/", 1)[0].lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return f"@{username}"
    return ""


def _visible_html_length(value: str) -> int:
    plain = re.sub(r"<[^>]+>", "", str(value or ""))
    return len(html_unescape(plain))


def _validate_body(kind: str, text: str) -> None:
    kind = str(kind or "")
    size = _visible_html_length(text)
    if kind == "text":
        if not text.strip():
            raise ValueError("empty text")
        if size > MAX_TEXT_LENGTH:
            raise ValueError("text too long")
        return
    # A media caption longer than Telegram's caption limit is still supported:
    # it is sent as a separate message after the media.
    if size > MAX_TEXT_LENGTH:
        raise ValueError("text too long")


def _kind_label(kind: str) -> str:
    return {
        "text": "متنی",
        "photo": "عکس + کپشن",
        "video": "ویدئو + کپشن",
    }.get(str(kind or ""), "هنوز ساخته نشده")


def _markup(draft: dict[str, Any]) -> InlineKeyboardMarkup | None:
    buttons = list(draft.get("buttons") or [])
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                str(item.get("text") or "لینک"),
                url=str(item.get("url") or ""),
                style=str(item.get("style") or "primary"),
            )
        ]
        for item in buttons[:MAX_BUTTONS]
    ])


def _empty_broadcast(segment: str = "") -> dict[str, Any]:
    return {
        "segment": str(segment or ""),
        "kind": "",
        "text": "",
        "file_id": "",
        "buttons": [],
    }


def _broadcast_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    value = context.user_data.get(BROADCAST_DRAFT_KEY)
    if not isinstance(value, dict):
        value = _empty_broadcast()
        context.user_data[BROADCAST_DRAFT_KEY] = value
    return value


def _empty_channel() -> dict[str, Any]:
    return {"kind": "", "text": "", "file_id": "", "buttons": []}


def _channel_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    value = context.user_data.get(CHANNEL_DRAFT_KEY)
    if not isinstance(value, dict):
        value = _empty_channel()
        context.user_data[CHANNEL_DRAFT_KEY] = value
    return value


def _clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(FLOW_KEY, None)


def _sibling_user_bot_token(business: Any) -> str:
    if business.secret_cipher is None:
        raise TenantBusinessError("UserBot encryption is unavailable")
    row = BotRepository(business.conn).get_by_tenant_role(
        business.tenant_id, "user"
    )
    if row is None or row.get("status") != "active":
        raise TenantBusinessError("UserBot is not active")
    plain = business.secret_cipher.decrypt(str(row["encrypted_token"]))
    if fingerprint_token(plain) != str(row["token_fingerprint"]):
        raise TenantBusinessError("UserBot credential integrity failed")
    return plain


async def _download_admin_media(
    context: ContextTypes.DEFAULT_TYPE, file_id: str
) -> bytes:
    tg_file = await context.bot.get_file(str(file_id))
    data = bytes(await tg_file.download_as_bytearray())
    if not data:
        raise TenantBusinessError("Telegram media is empty")
    return data


def _message_html(message: Any, *, caption: bool = False) -> str:
    if caption:
        return str(
            getattr(message, "caption_html", None)
            or getattr(message, "caption", None)
            or ""
        )
    return str(
        getattr(message, "text_html", None)
        or getattr(message, "text", None)
        or ""
    )


def _media_from_message(message: Any) -> tuple[str, str]:
    if getattr(message, "photo", None):
        return "photo", str(message.photo[-1].file_id)
    video = getattr(message, "video", None)
    if video is not None:
        return "video", str(video.file_id)
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "photo", str(document.file_id)
        if mime.startswith("video/"):
            return "video", str(document.file_id)
    return "", ""


async def _edit_or_send(
    update: Update,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = None,
) -> None:
    query = update.callback_query
    if query is not None:
        try:
            await query.edit_message_text(
                text, reply_markup=markup, parse_mode=parse_mode
            )
            return
        except BadRequest:
            pass
        await query.message.reply_text(
            text, reply_markup=markup, parse_mode=parse_mode
        )
        return
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=parse_mode
        )


def _broadcast_stats_text(stats: dict[str, Any]) -> str:
    return (
        f"◈ تعداد کاربران تلگرام: {int(stats.get('total_users') or 0)}\n"
        f"◈ تعداد کاربران منقضی: {int(stats.get('expired_users') or 0)}\n"
        f"◈ تعداد کاربران بدون سفارش: {int(stats.get('no_order_users') or 0)}\n"
        f"◈ تعداد کاربران منقضی شده بیش از یک هفته: "
        f"{int(stats.get('expired_1w_users') or 0)}\n"
        f"◈ تعداد کاربران منقضی شده بیش از دو هفته: "
        f"{int(stats.get('expired_2w_users') or 0)}\n"
        f"◈ تعداد کاربران منقضی شده بیش از چهار هفته: "
        f"{int(stats.get('expired_4w_users') or 0)}\n"
        f"◈ تعداد کاربران منقضی شده بیش از هشت هفته: "
        f"{int(stats.get('expired_8w_users') or 0)}"
    )


def _broadcast_segment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                label,
                callback_data=f"userbot:broadcast:segment:{segment}",
            )
        ]
        for segment, label in SEGMENT_LABELS.items()
    ] + [[
        InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")
    ]])


def _broadcast_menu_markup(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if draft.get("kind"):
        rows.extend([
            [
                InlineKeyboardButton(
                    "✏️ ویرایش پیام",
                    callback_data="userbot:broadcast:edit",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔘 افزودن دکمه",
                    callback_data="userbot:broadcast:button",
                )
            ],
            [
                InlineKeyboardButton(
                    "👁 پیش‌نمایش",
                    callback_data="userbot:broadcast:preview",
                )
            ],
            [
                InlineKeyboardButton(
                    "🚀 انتشار",
                    callback_data="userbot:broadcast:publish",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧹 پاک کردن دکمه‌ها",
                    callback_data="userbot:broadcast:clear_buttons",
                    style="danger",
                )
            ],
        ])
    rows.extend([
        [
            InlineKeyboardButton(
                "👥 تغییر گروه هدف",
                callback_data="userbot:broadcast_menu",
            )
        ],
        [InlineKeyboardButton("🔙بازگشت", callback_data="userbot:menu")],
    ])
    return InlineKeyboardMarkup(rows)


def _broadcast_edit_markup(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    media_title = (
        "🖼 افزودن عکس / ویدئو"
        if str(draft.get("kind") or "") == "text"
        else "🖼 ویرایش عکس / ویدئو"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📝 ویرایش متن / کپشن",
                callback_data="userbot:broadcast:edit_text",
            )
        ],
        [
            InlineKeyboardButton(
                media_title,
                callback_data="userbot:broadcast:edit_media",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 جایگزینی کامل پیام",
                callback_data="userbot:broadcast:edit_replace",
                style="danger",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 بازگشت",
                callback_data="userbot:broadcast:draft",
            )
        ],
    ])


def _broadcast_summary(draft: dict[str, Any], target_count: int) -> str:
    segment = str(draft.get("segment") or "all")
    return (
        "📧 <b>ارسال پیام همگانی</b>\n\n"
        f"👥 گروه: <b>{SEGMENT_LABELS.get(segment, segment)}</b>\n"
        f"🎯 تعداد فعلی گیرنده‌ها: <b>{int(target_count)}</b>\n"
        f"📝 نوع پیام: <b>{_kind_label(str(draft.get('kind') or ''))}</b>\n"
        f"🔘 تعداد دکمه‌ها: <b>{len(draft.get('buttons') or [])}</b>\n\n"
        "قبل از انتشار می‌توانید پیش‌نمایش را بررسی یا پیام را ویرایش کنید."
    )


async def show_broadcast_menu(
    update: Update, business: Any, actor: int
) -> None:
    stats = business.broadcast_stats_admin(actor)
    await _edit_or_send(
        update,
        _broadcast_stats_text(stats),
        _broadcast_segment_keyboard(),
    )


async def _show_broadcast_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
) -> None:
    draft = _broadcast_draft(context)
    segment = str(draft.get("segment") or "all")
    targets = business.broadcast_targets_admin(actor, segment=segment)
    await _edit_or_send(
        update,
        _broadcast_summary(draft, len(targets)),
        _broadcast_menu_markup(draft),
        parse_mode="HTML",
    )


async def _preview_draft(message: Any, draft: dict[str, Any]) -> None:
    kind = str(draft.get("kind") or "")
    text = str(draft.get("text") or "")
    markup = _markup(draft)
    if not kind:
        await message.reply_text("❌ هنوز محتوایی ساخته نشده است.")
        return
    if kind == "text":
        await message.reply_text(
            text or " ", parse_mode="HTML", reply_markup=markup
        )
        return

    file_id = str(draft.get("file_id") or "")
    long_caption = _visible_html_length(text) > MAX_CAPTION_LENGTH
    if kind == "photo":
        await message.reply_photo(
            photo=file_id,
            caption=None if long_caption else (text or None),
            parse_mode="HTML",
            reply_markup=None if long_caption else markup,
        )
    elif kind == "video":
        await message.reply_video(
            video=file_id,
            caption=None if long_caption else (text or None),
            parse_mode="HTML",
            reply_markup=None if long_caption else markup,
        )
    if long_caption and text:
        await message.reply_text(
            text, parse_mode="HTML", reply_markup=markup
        )


async def _send_with_retry(factory: Any, *, max_attempts: int = 3):
    retries = 0
    last_error: Exception | None = None
    for attempt in range(max(1, int(max_attempts))):
        try:
            return True, await factory(), "", retries, None
        except RetryAfter as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                return False, None, "temporary", retries, exc
            retries += 1
            wait = getattr(exc, "retry_after", 1)
            if hasattr(wait, "total_seconds"):
                wait = wait.total_seconds()
            try:
                seconds = float(wait)
            except (TypeError, ValueError):
                seconds = 1.0
            await asyncio.sleep(min(max(seconds + 0.25, 0.5), 30.0))
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                return False, None, "temporary", retries, exc
            retries += 1
            await asyncio.sleep(0.75 * (attempt + 1))
        except (Forbidden, BadRequest) as exc:
            return False, None, "unreachable", retries, exc
        except TelegramError as exc:
            return False, None, "telegram", retries, exc
        except Exception as exc:
            return False, None, "other", retries, exc
    return False, None, "other", retries, last_error


async def _send_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
    draft: dict[str, Any],
) -> dict[str, int]:
    segment = str(draft.get("segment") or "all")
    targets = business.broadcast_targets_admin(actor, segment=segment)
    kind = str(draft.get("kind") or "text")
    text = str(draft.get("text") or "")
    _validate_body(kind, text)
    buttons = list(draft.get("buttons") or [])
    run_id = business.start_broadcast_run_admin(
        actor,
        segment=segment,
        message_kind=kind,
        target_count=len(targets),
        buttons_count=len(buttons),
    )
    if not targets:
        business.finish_broadcast_run_admin(
            actor, run_id=run_id, sent=0, failed=0
        )
        return {
            "target": 0, "sent": 0, "failed": 0, "recovered": 0,
            "unreachable": 0, "temporary": 0, "telegram": 0, "other": 0,
        }

    media_bytes = b""
    if kind in {"photo", "video"}:
        media_bytes = await _download_admin_media(
            context, str(draft.get("file_id") or "")
        )

    markup = _markup(draft)
    sent = failed = recovered = 0
    unreachable = temporary = telegram_error = other = 0
    reusable_file_id = ""
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            for target in targets:
                chat_id = int(target["telegram_user_id"])
                recipient_retried = False
                category = ""
                ok = False
                long_caption = (
                    kind in {"photo", "video"}
                    and _visible_html_length(text) > MAX_CAPTION_LENGTH
                )

                if kind == "text":
                    async def send_text():
                        return await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )

                    ok, _, category, retries, _ = await _send_with_retry(
                        send_text
                    )
                    recipient_retried = retries > 0
                else:
                    async def send_media():
                        if reusable_file_id:
                            media: Any = reusable_file_id
                        else:
                            media = BytesIO(media_bytes)
                            media.name = (
                                "broadcast.jpg"
                                if kind == "photo"
                                else "broadcast.mp4"
                            )
                        kwargs = {
                            "chat_id": chat_id,
                            "caption": None if long_caption else (text or None),
                            "parse_mode": "HTML",
                            "reply_markup": None if long_caption else markup,
                        }
                        if kind == "photo":
                            return await bot.send_photo(photo=media, **kwargs)
                        return await bot.send_video(video=media, **kwargs)

                    ok, sent_media, category, retries, _ = (
                        await _send_with_retry(send_media)
                    )
                    recipient_retried = retries > 0
                    if ok and not reusable_file_id:
                        if kind == "photo":
                            photos = list(
                                getattr(sent_media, "photo", None) or []
                            )
                            if photos:
                                reusable_file_id = str(photos[-1].file_id)
                        else:
                            video = getattr(sent_media, "video", None)
                            if video is not None:
                                reusable_file_id = str(video.file_id)

                    if ok and long_caption and text:
                        async def send_long_text():
                            return await bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )

                        ok, _, category, retries2, _ = (
                            await _send_with_retry(send_long_text)
                        )
                        recipient_retried = (
                            recipient_retried or retries2 > 0
                        )

                if ok:
                    sent += 1
                    if recipient_retried:
                        recovered += 1
                else:
                    failed += 1
                    if category == "unreachable":
                        unreachable += 1
                    elif category == "temporary":
                        temporary += 1
                    elif category == "telegram":
                        telegram_error += 1
                    else:
                        other += 1
                await asyncio.sleep(0.06)
    finally:
        token = ""

    business.finish_broadcast_run_admin(
        actor,
        run_id=run_id,
        sent=sent,
        failed=failed,
        recovered=recovered,
        unreachable=unreachable,
        temporary=temporary,
        telegram_error=telegram_error,
        other=other,
    )
    return {
        "target": len(targets),
        "sent": sent,
        "failed": failed,
        "recovered": recovered,
        "unreachable": unreachable,
        "temporary": temporary,
        "telegram": telegram_error,
        "other": other,
    }


def _broadcast_result_text(result: dict[str, int]) -> str:
    sent = max(0, int(result.get("sent") or 0))
    failed = max(0, int(result.get("failed") or 0))
    total = sent + failed
    if total == 0:
        return "ℹ️ کاربری در گروه انتخاب‌شده برای ارسال پیدا نشد."
    recovered = max(0, int(result.get("recovered") or 0))
    if failed == 0:
        text = f"✅ پیام برای {sent} کاربر ارسال شد."
        if recovered:
            text += (
                f"\n🔁 {recovered} ارسال موقتاً خطا داشت "
                "و با تلاش مجدد موفق شد."
            )
        return text

    if sent == 0:
        lines = [f"❌ ارسال پیام برای هر {failed} کاربر ناموفق بود."]
    else:
        lines = [
            "⚠️ ارسال همگانی به‌صورت ناقص انجام شد.",
            f"✅ موفق: {sent}",
            f"❌ ناموفق: {failed}",
        ]
    details = [
        ("unreachable", "🚫 غیرقابل دسترس/مسدود"),
        ("temporary", "🌐 خطای موقت پس از تلاش مجدد"),
        ("telegram", "⚠️ سایر خطاهای تلگرام"),
        ("other", "🛠 سایر خطاها"),
        ("recovered", "🔁 بازیابی‌شده با تلاش مجدد"),
    ]
    for key, label in details:
        value = max(0, int(result.get(key) or 0))
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _channel_admin_menu(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            "➕ ساخت پست جدید",
            callback_data="channelpost:new",
            style="success",
        )
    ]]
    if draft.get("kind"):
        rows.extend([
            [
                InlineKeyboardButton(
                    "✏️ ویرایش پست",
                    callback_data="channelpost:edit",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔘 افزودن دکمه",
                    callback_data="channelpost:button",
                )
            ],
            [
                InlineKeyboardButton(
                    "👁 پیش‌نمایش",
                    callback_data="channelpost:preview",
                )
            ],
            [
                InlineKeyboardButton(
                    "🚀 انتشار در کانال",
                    callback_data="channelpost:publish",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧹 پاک کردن دکمه‌ها",
                    callback_data="channelpost:clear_buttons",
                    style="danger",
                )
            ],
        ])
    rows.extend([
        [
            InlineKeyboardButton(
                "⚙️ تنظیم کانال مقصد",
                callback_data="channelpost:set",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 بازگشت به مدیریت ربات کاربران",
                callback_data="userbot:menu",
            )
        ],
    ])
    return InlineKeyboardMarkup(rows)


def _channel_edit_markup(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    media_title = (
        "🖼 افزودن عکس / ویدئو"
        if str(draft.get("kind") or "") == "text"
        else "🖼 ویرایش عکس / ویدئو"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📝 ویرایش متن / کپشن",
                callback_data="channelpost:edit_text",
            )
        ],
        [
            InlineKeyboardButton(
                media_title,
                callback_data="channelpost:edit_media",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 جایگزینی کامل پست",
                callback_data="channelpost:edit_replace",
                style="danger",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 بازگشت",
                callback_data="channelpost:menu",
            )
        ],
    ])


async def _show_channel_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
) -> None:
    settings = business.userbot_settings_admin(actor)
    draft = _channel_draft(context)
    target = str(settings.get("channel_id") or "").strip() or "تنظیم نشده"
    text = (
        "📢 <b>مدیریت پست کانال</b>\n\n"
        f"📝 نوع پست: <b>{_kind_label(str(draft.get('kind') or ''))}</b>\n"
        f"🔘 تعداد دکمه‌ها: <b>{len(draft.get('buttons') or [])}</b>\n"
        f"🎯 مقصد: <code>{target}</code>\n\n"
        "پیام از ربات ادمین ساخته می‌شود و هنگام انتشار با توکن "
        "ربات کاربران به کانال ارسال می‌شود."
    )
    await _edit_or_send(
        update, text, _channel_admin_menu(draft), parse_mode="HTML"
    )


async def _publish_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    business: Any,
    actor: int,
) -> bool:
    draft = _channel_draft(context)
    settings = business.userbot_settings_admin(actor)
    target = str(settings.get("channel_id") or "").strip()
    if not target:
        await update.effective_message.reply_text(
            "❌ کانال مقصد تنظیم نشده است."
        )
        return False
    kind = str(draft.get("kind") or "")
    if not kind:
        await update.effective_message.reply_text(
            "❌ ابتدا یک پست بساز."
        )
        return False
    text = str(draft.get("text") or "")
    _validate_body(kind, text)
    markup = _markup(draft)
    token = _sibling_user_bot_token(business)
    try:
        async with Bot(token=token) as bot:
            if kind == "text":
                sent = await bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                media_bytes = await _download_admin_media(
                    context, str(draft.get("file_id") or "")
                )
                stream = BytesIO(media_bytes)
                stream.name = (
                    "channel.jpg" if kind == "photo" else "channel.mp4"
                )
                long_caption = (
                    _visible_html_length(text) > MAX_CAPTION_LENGTH
                )
                if kind == "photo":
                    sent = await bot.send_photo(
                        chat_id=target,
                        photo=stream,
                        caption=None if long_caption else (text or None),
                        parse_mode="HTML",
                        reply_markup=None if long_caption else markup,
                    )
                else:
                    sent = await bot.send_video(
                        chat_id=target,
                        video=stream,
                        caption=None if long_caption else (text or None),
                        parse_mode="HTML",
                        reply_markup=None if long_caption else markup,
                    )
                if long_caption and text:
                    sent = await bot.send_message(
                        chat_id=target,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
    except Exception as exc:
        await update.effective_message.reply_text(
            "❌ انتشار ناموفق بود. دسترسی ادمین ربات کاربران به کانال "
            "و شناسه کانال را بررسی کن.\n"
            f"خطا: <code>{type(exc).__name__}</code>",
            parse_mode="HTML",
        )
        return False
    finally:
        token = ""

    context.user_data.pop(CHANNEL_DRAFT_KEY, None)
    _clear_flow(context)
    await update.effective_message.reply_text(
        "✅ پست با موفقیت توسط ربات کاربران در کانال منتشر شد.\n"
        f"🆔 Message ID: <code>{int(sent.message_id)}</code>",
        parse_mode="HTML",
    )
    return True


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    query = update.callback_query
    if query is None:
        return False
    data = str(query.data or "")
    if not (
        data == "userbot:broadcast_menu"
        or data.startswith("userbot:broadcast:")
        or data.startswith("channelpost:")
    ):
        return False

    set_button_settings(business.runtime_userbot_settings())
    await query.answer()

    if data == "userbot:broadcast_menu":
        _clear_flow(context)
        await show_broadcast_menu(update, business, actor)
        return True

    if data.startswith("userbot:broadcast:segment:"):
        segment = data.rsplit(":", 1)[1]
        if segment not in SEGMENT_LABELS:
            await query.answer("گروه ارسال نامعتبر است.", show_alert=True)
            return True
        context.user_data[BROADCAST_DRAFT_KEY] = _empty_broadcast(segment)
        context.user_data[FLOW_KEY] = {
            "kind": "broadcast",
            "step": "wait_text",
        }
        await query.message.reply_text(
            f"✍ لطفا پیام خود را برای ارسال به "
            f"«{SEGMENT_LABELS[segment]}» وارد کنید:",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "userbot:broadcast:draft":
        _clear_flow(context)
        await _show_broadcast_draft(
            update, context, business, actor
        )
        return True

    if data == "userbot:broadcast:edit":
        draft = _broadcast_draft(context)
        if not draft.get("kind"):
            raise TenantBusinessError("broadcast is empty")
        await _edit_or_send(
            update,
            "✏️ <b>ویرایش پیام همگانی</b>\n\n"
            "بخشی که می‌خواهید تغییر کند را انتخاب کنید.",
            _broadcast_edit_markup(draft),
            parse_mode="HTML",
        )
        return True

    if data == "userbot:broadcast:edit_text":
        context.user_data[FLOW_KEY] = {
            "kind": "broadcast_edit_text"
        }
        await query.message.reply_text(
            "📝 متن یا کپشن جدید را بفرستید.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "userbot:broadcast:edit_media":
        context.user_data[FLOW_KEY] = {
            "kind": "broadcast_edit_media"
        }
        await query.message.reply_text(
            "🖼 عکس یا ویدئوی جدید را بفرستید.\n"
            "متن و دکمه‌های فعلی تغییر نمی‌کنند.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "userbot:broadcast:edit_replace":
        context.user_data[FLOW_KEY] = {
            "kind": "broadcast_replace"
        }
        await query.message.reply_text(
            "🔄 نسخه کامل جدید پیام را بفرستید؛ متن، عکس + کپشن "
            "یا ویدئو + کپشن. دکمه‌های فعلی حفظ می‌شوند.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "userbot:broadcast:button":
        draft = _broadcast_draft(context)
        if not draft.get("kind"):
            raise TenantBusinessError("broadcast is empty")
        if len(draft.get("buttons") or []) >= MAX_BUTTONS:
            await query.message.reply_text(
                f"❌ حداکثر {MAX_BUTTONS} دکمه مجاز است."
            )
            return True
        context.user_data[FLOW_KEY] = {
            "kind": "broadcast_button_text"
        }
        await query.message.reply_text(
            "🔘 عنوان دکمه را بفرستید؛ مثلاً: 🛒 خرید سرویس",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "userbot:broadcast:preview":
        await _preview_draft(query.message, _broadcast_draft(context))
        return True

    if data == "userbot:broadcast:clear_buttons":
        _broadcast_draft(context)["buttons"] = []
        await query.message.reply_text("✅ همه دکمه‌ها پاک شدند.")
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if data == "userbot:broadcast:publish":
        draft = _broadcast_draft(context)
        result = await _send_broadcast(context, business, actor, draft)
        context.user_data.pop(BROADCAST_DRAFT_KEY, None)
        _clear_flow(context)
        await query.message.reply_text(
            _broadcast_result_text(result),
            reply_markup=admin_main_keyboard(),
        )
        return True

    # Channel management.
    if data == "channelpost:menu":
        _clear_flow(context)
        await _show_channel_menu(update, context, business, actor)
        return True

    if data == "channelpost:set":
        context.user_data[FLOW_KEY] = {"kind": "channel_set"}
        await query.message.reply_text(
            "📢 @channel یا شناسه -100... را ارسال کنید:",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:new":
        context.user_data[CHANNEL_DRAFT_KEY] = _empty_channel()
        context.user_data[FLOW_KEY] = {"kind": "channel_content"}
        await query.message.reply_text(
            "📝 متن پست را بفرست، یا عکس/ویدئو را همراه کپشن ارسال کن.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:edit":
        draft = _channel_draft(context)
        if not draft.get("kind"):
            raise TenantBusinessError("channel post is empty")
        await _edit_or_send(
            update,
            "✏️ <b>ویرایش پست</b>\n\n"
            "بخشی که می‌خواهید تغییر کند را انتخاب کنید.\n"
            "متن، رسانه و دکمه‌ها مستقل از هم نگه داشته می‌شوند.",
            _channel_edit_markup(draft),
            parse_mode="HTML",
        )
        return True

    if data == "channelpost:edit_text":
        context.user_data[FLOW_KEY] = {"kind": "channel_edit_text"}
        await query.message.reply_text(
            "📝 متن یا کپشن جدید را بفرستید.\n"
            "برای پاک‌کردن کامل متن، عدد 0 را بفرستید.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:edit_media":
        context.user_data[FLOW_KEY] = {"kind": "channel_edit_media"}
        await query.message.reply_text(
            "🖼 عکس یا ویدئوی جدید را بفرستید.\n"
            "متن/کپشن و دکمه‌های فعلی تغییر نمی‌کنند.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:edit_replace":
        context.user_data[FLOW_KEY] = {"kind": "channel_replace"}
        await query.message.reply_text(
            "🔄 نسخه کامل جدید پست را بفرستید.\n"
            "می‌تواند متن، عکس + کپشن یا ویدئو + کپشن باشد.\n"
            "🔘 دکمه‌های فعلی حفظ می‌شوند.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:button":
        draft = _channel_draft(context)
        if not draft.get("kind"):
            raise TenantBusinessError("channel post is empty")
        if len(draft.get("buttons") or []) >= MAX_BUTTONS:
            await query.message.reply_text(
                f"❌ حداکثر {MAX_BUTTONS} دکمه مجاز است."
            )
            return True
        context.user_data[FLOW_KEY] = {"kind": "channel_button_text"}
        await query.message.reply_text(
            "🔘 عنوان دکمه را بفرستید؛ مثلاً: 🛒 خرید سرویس",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data == "channelpost:preview":
        await _preview_draft(query.message, _channel_draft(context))
        return True

    if data == "channelpost:clear_buttons":
        _channel_draft(context)["buttons"] = []
        await query.message.reply_text(
            "✅ همه دکمه‌های پیش‌نویس پاک شدند."
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    if data == "channelpost:publish":
        published = await _publish_channel(
            update, context, business, actor
        )
        if published:
            await query.message.reply_text(
                "🏠 منوی ادمین",
                reply_markup=admin_main_keyboard(),
            )
        return True

    return False


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    flow = context.user_data.get(FLOW_KEY)
    if not isinstance(flow, dict):
        return False
    message = update.effective_message
    if message is None:
        return False
    text = str(message.text or "").strip()

    if _is_cancel(text):
        _clear_flow(context)
        await message.reply_text(
            "❌ لغو شد.", reply_markup=admin_main_keyboard()
        )
        return True

    kind = str(flow.get("kind") or "")
    if kind == "broadcast":
        draft = _broadcast_draft(context)
        step = str(flow.get("step") or "wait_text")
        if step == "wait_text":
            body = _message_html(message).strip()
            _validate_body("text", body)
            draft.update(kind="text", text=body, file_id="")
            flow["step"] = "wait_media"
            await message.reply_text(
                "🖼️ عکس یا ویدئو را ارسال کنید، یا روی "
                "[⏩رد کردن] بزنید:",
                reply_markup=_skip_cancel_keyboard(),
            )
            return True
        if step == "wait_media" and _is_skip(text):
            _clear_flow(context)
            await message.reply_text(
                "✅ پیام آماده شد.",
                reply_markup=admin_main_keyboard(),
            )
            await _show_broadcast_draft(
                update, context, business, actor
            )
            return True
        await message.reply_text(
            "❌ عکس/ویدئو ارسال کنید یا «⏩رد کردن» را بزنید.",
            reply_markup=_skip_cancel_keyboard(),
        )
        return True

    if kind == "broadcast_edit_text":
        body = _message_html(message).strip()
        draft = _broadcast_draft(context)
        _validate_body(str(draft.get("kind") or "text"), body)
        draft["text"] = body
        _clear_flow(context)
        await message.reply_text(
            "✅ متن/کپشن پیام ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind == "broadcast_replace":
        body = _message_html(message).strip()
        _validate_body("text", body)
        draft = _broadcast_draft(context)
        buttons = list(draft.get("buttons") or [])
        segment = str(draft.get("segment") or "all")
        draft.clear()
        draft.update(
            segment=segment,
            kind="text",
            text=body,
            file_id="",
            buttons=buttons,
        )
        _clear_flow(context)
        await message.reply_text(
            "✅ پیام جایگزین شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind == "broadcast_button_text":
        if not text or len(text) > 64:
            raise ValueError("button text")
        flow["label"] = text
        flow["kind"] = "broadcast_button_url"
        await message.reply_text(
            "🔗 حالا لینک دکمه را بفرستید؛ لینک کامل یا @username.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if kind == "broadcast_button_url":
        url = _normalize_button_url(text)
        if not url:
            raise ValueError("button url")
        draft = _broadcast_draft(context)
        buttons = list(draft.get("buttons") or [])
        if len(buttons) >= MAX_BUTTONS:
            raise TenantBusinessError("broadcast button limit reached")
        buttons.append({
            "text": str(flow.get("label") or "لینک"),
            "url": url,
            "style": "primary",
        })
        draft["buttons"] = buttons
        _clear_flow(context)
        await message.reply_text(
            "✅ دکمه اضافه شد.", reply_markup=admin_main_keyboard()
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind == "channel_set":
        target = _normalize_channel_target(text)
        if not target:
            raise ValueError("channel target")
        business.set_userbot_setting_admin(
            actor, key="channel_id", value=target
        )
        _clear_flow(context)
        await message.reply_text(
            "✅ کانال ذخیره شد.", reply_markup=admin_main_keyboard()
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    if kind in {"channel_content", "channel_replace"}:
        body = _message_html(message).strip()
        _validate_body("text", body)
        draft = _channel_draft(context)
        buttons = list(draft.get("buttons") or [])
        draft.clear()
        draft.update(
            kind="text",
            text=body,
            file_id="",
            buttons=buttons if kind == "channel_replace" else [],
        )
        _clear_flow(context)
        await message.reply_text(
            "✅ محتوای پست ذخیره/ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    if kind == "channel_edit_text":
        draft = _channel_draft(context)
        body = _message_html(message)
        if text in {"0", "-", "—"}:
            body = ""
        if str(draft.get("kind") or "") == "text" and not body.strip():
            raise ValueError("text post cannot be empty")
        _validate_body(str(draft.get("kind") or "text"), body)
        draft["text"] = body
        _clear_flow(context)
        await message.reply_text(
            "✅ متن/کپشن پست ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    if kind == "channel_button_text":
        if not text or len(text) > 64:
            raise ValueError("button text")
        flow["label"] = text
        flow["kind"] = "channel_button_url"
        await message.reply_text(
            "🔗 حالا لینک دکمه را بفرستید؛ لینک کامل یا @username.",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if kind == "channel_button_url":
        url = _normalize_button_url(text)
        if not url:
            raise ValueError("button url")
        draft = _channel_draft(context)
        buttons = list(draft.get("buttons") or [])
        if len(buttons) >= MAX_BUTTONS:
            raise TenantBusinessError("channel button limit reached")
        buttons.append({
            "text": str(flow.get("label") or "لینک"),
            "url": url,
            "style": "primary",
        })
        draft["buttons"] = buttons
        _clear_flow(context)
        await message.reply_text(
            "✅ دکمه اضافه شد.", reply_markup=admin_main_keyboard()
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    return False


async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    flow = context.user_data.get(FLOW_KEY)
    if not isinstance(flow, dict):
        return False
    message = update.effective_message
    if message is None:
        return False
    kind = str(flow.get("kind") or "")
    media_kind, file_id = _media_from_message(message)
    if not media_kind:
        return False
    caption = _message_html(message, caption=True)
    _validate_body(media_kind, caption)

    if kind == "broadcast" and str(flow.get("step") or "") == "wait_media":
        draft = _broadcast_draft(context)
        draft["kind"] = media_kind
        draft["file_id"] = file_id
        # The text was collected in the preceding SellBot-style text step.
        _clear_flow(context)
        await message.reply_text(
            "✅ رسانه پیام ذخیره شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind == "broadcast_edit_media":
        draft = _broadcast_draft(context)
        draft["kind"] = media_kind
        draft["file_id"] = file_id
        _clear_flow(context)
        await message.reply_text(
            "✅ عکس/ویدئوی پیام ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind == "broadcast_replace":
        draft = _broadcast_draft(context)
        buttons = list(draft.get("buttons") or [])
        segment = str(draft.get("segment") or "all")
        draft.clear()
        draft.update(
            segment=segment,
            kind=media_kind,
            text=caption,
            file_id=file_id,
            buttons=buttons,
        )
        _clear_flow(context)
        await message.reply_text(
            "✅ پیام جایگزین شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_broadcast_draft(update, context, business, actor)
        return True

    if kind in {"channel_content", "channel_replace"}:
        draft = _channel_draft(context)
        buttons = list(draft.get("buttons") or [])
        draft.clear()
        draft.update(
            kind=media_kind,
            text=caption,
            file_id=file_id,
            buttons=buttons if kind == "channel_replace" else [],
        )
        _clear_flow(context)
        await message.reply_text(
            "✅ محتوای پست ذخیره/ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    if kind == "channel_edit_media":
        draft = _channel_draft(context)
        draft["kind"] = media_kind
        draft["file_id"] = file_id
        _clear_flow(context)
        await message.reply_text(
            "✅ عکس/ویدئوی پست ویرایش شد.",
            reply_markup=admin_main_keyboard(),
        )
        await _show_channel_menu(update, context, business, actor)
        return True

    return False


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    business: Any,
    actor: int,
    admin_main_keyboard: Any,
) -> bool:
    message = update.effective_message
    document = getattr(message, "document", None) if message else None
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if not document or not (
        mime.startswith("image/") or mime.startswith("video/")
    ):
        return False
    return await handle_media(
        update,
        context,
        business=business,
        actor=actor,
        admin_main_keyboard=admin_main_keyboard,
    )
