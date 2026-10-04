"""SellBot-style server menus backed by live, tenant-owned panel operations."""

from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta
from html import escape
from io import BytesIO

import qrcode
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from TenantRuntime.button_styles import inline_button as Button
from TenantRuntime.server_admin import ServerAdminService, user_status, DEFAULT_SALES
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.panels import PanelError
from TenantRuntime.smart_subscription import decode_subscription_lines
from Shared.timeutils import iso_utc, utcnow, format_tehran, parse_utc

FLOW = "server_admin_flow"
STATUS_LABELS = {
    "active": "🟢 فعال",
    "disabled": "⚫ غیرفعال",
    "expired": "🔴 منقضی",
    "pending": "🟠 در انتظار",
}
ROUTES = {
    "page",
    "user",
    "useract",
    "view",
    "users",
    "puser",
    "pedit",
    "pfield",
    "ptoggle",
    "preset",
    "presetok",
    "pconfigs",
    "prenew",
    "pdelete",
    "pdeleteok",
    "userops",
    "usersearch",
    "useradd",
    "useraddplan",
    "plans",
    "plan",
    "planadd",
    "planfield",
    "plantoggle",
    "planarchive",
    "planarchiveok",
    "plancategory",
    "categories",
    "categoryadd",
    "category",
    "categoryfield",
    "categorytoggle",
    "settings",
    "salesfield",
    "mode",
    "discounts",
    "discounttoggle",
    "discountedit",
    "domains",
    "domain",
    "domainadd",
    "domainedit",
    "domainselect",
    "domaindelete",
    "domaindeleteok",
    "nodes",
    "node",
    "nodeadd",
    "nodepick",
    "nodeedit",
    "nodedel",
    "nodedelok",
    "nodestatus",
    "sync",
    "syncrun",
    "frozen",
    "frozenuser",
    "frozenrepair",
    "frozenclear",
    "delete",
    "deleteok",
}


def button(label, data):
    return [Button(label, callback_data=data)]


def back(sid, section="view"):
    return button("بازگشت🔙", f"srv:{section}:{sid}")


def markup(rows):
    return InlineKeyboardMarkup(rows)


def local_time(raw):
    if not raw:
        return "نامشخص"
    try:
        return format_tehran(parse_utc(raw))
    except (ValueError, TypeError):
        return str(raw)


_PERSIAN_DIGITS_TRANS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _normalize_digit_text(value):
    return str(value or "").translate(_PERSIAN_DIGITS_TRANS)


def _format_discount_tiers(tiers):
    normalized = sorted(
        (
            {"gb": int(item["gb"]), "percent": int(item["percent"])}
            for item in (tiers or [])
            if int(item.get("gb") or 0) > 0 and int(item.get("percent") or 0) > 0
        ),
        key=lambda item: item["gb"],
    )
    if not normalized:
        return "غیرفعال"
    return " | ".join(
        f"از {item['gb']} گیگ: {item['percent']}٪" for item in normalized
    )


def _parse_discount_tiers_text(text):
    raw = _normalize_digit_text(text).strip()
    if raw in {"0", "خاموش", "غیرفعال", "-"}:
        return []
    items = []
    normalized = raw.replace("،", ",").replace("\n", ",").replace("؛", ",")
    for part in normalized.split(","):
        part = part.strip()
        if not part:
            continue
        separator = next((sep for sep in (":", "=", "-") if sep in part), None)
        if not separator:
            raise ValueError("invalid discount tier")
        gb_text, percent_text = part.split(separator, 1)
        gb = int(
            _normalize_digit_text(
                gb_text.replace("گیگ", "").replace("gb", "").replace("GB", "")
            )
            .replace(",", "")
            .strip()
        )
        percent = int(
            _normalize_digit_text(percent_text)
            .replace("%", "")
            .replace("٪", "")
            .replace(",", "")
            .strip()
        )
        if gb <= 0 or not 0 < percent <= 100:
            raise ValueError("invalid discount tier")
        items.append({"gb": gb, "percent": percent})
    if not items or len({item["gb"] for item in items}) != len(items):
        raise ValueError("invalid discount tier")
    return sorted(items, key=lambda item: item["gb"])


def _plan_mode_title(mode):
    return {
        "fixed": "فقط پلن‌های ثابت",
        "dynamic": "فقط پلن پویا",
        "mixed": "حالت ترکیبی (ثابت + پویا)",
    }.get(mode, "نامشخص")


def _plans_root_view(business, service, sid):
    sales = service.sales(sid)
    mode = sales["mode"]
    rows = []
    if mode in {"fixed", "mixed"}:
        rows.append(button("📂 لیست دسته‌های پلن", f"srv:categories:{sid}"))
    rows.append(button("⚙️تنظیمات پلن‌ها", f"srv:settings:{sid}"))
    if mode in {"dynamic", "mixed"}:
        rows.append(button("🎛 مدیریت حرفه‌ای تخفیف‌ها", f"srv:discounts:{sid}"))
    rows.append(back(sid))
    return (
        f"مدیریت پلن‌ها برای سرور 🖥 {business.server(sid)['label']}\n"
        "━━━━━━━━━━━━━━\n"
        f"حالت نمایش فعلی در ربات کاربران: {_plan_mode_title(mode)}\n\n"
        "یکی از گزینه‌های زیر را انتخاب کنید:",
        rows,
    )


def _plans_settings_view(sid):
    return (
        "⚙️تنظیمات پلن‌ها\n\nیکی از گزینه‌های زیر را انتخاب کنید:",
        [
            button("نوع نمایش پلن‌ها📋", f"srv:mode:{sid}"),
            button("تنظیم پلن پویا📈", f"srv:settings:{sid}:dynamic"),
            back(sid, "plans"),
        ],
    )


def _plan_mode_view(service, sid):
    current = service.sales(sid)["mode"]

    def mode_button(value, title):
        selected = current == value
        return Button(
            ("✅ " if selected else "❌ ") + title,
            callback_data=f"srv:mode:{sid}:{value}",
            style="success" if selected else "danger",
        )

    return (
        "⚙️تنظیمات پلن‌ها\n\nحالت نمایش پلن‌ها را انتخاب کنید:",
        [
            [
                mode_button("fixed", "ثابت"),
                mode_button("dynamic", "پویا"),
                mode_button("mixed", "ترکیبی"),
            ],
            back(sid, "settings"),
        ],
    )


def _dynamic_settings_view(service, sid):
    sales = service.sales(sid)
    tiers = sales.get("discount_tiers") or []
    if tiers:
        discount_line = f"🎚 تخفیف پلاکانی: {_format_discount_tiers(tiers)}"
    else:
        discount_line = (
            f"🎁 تخفیف حجمی ساده: هر {sales['discount_step_gb']} گیگ "
            f"+{sales['discount_percent_step']}٪ تا سقف "
            f"{sales['discount_percent_max']}٪"
        )
    text = "\n".join(
        [
            "📈 تنظیم مقادیر پلن پویا",
            "",
            f"💰 قیمت هر گیگ: {int(sales['price_gb']):,} تومان",
            f"💰 قیمت هر ماه: {int(sales['price_month']):,} تومان",
            "",
            (
                f"📊 حجم قابل فروش: از {sales['min_gb']} تا {sales['max_gb']} "
                f"گیگ (گام: {sales['step_gb']})"
            ),
            (
                f"⌛ زمان اشتراک: از {sales['min_month']} تا "
                f"{sales['max_month']} ماه (گام: {sales['step_month']})"
            ),
            "",
            discount_line,
            "",
            "برای تغییر هر مقدار از دکمه‌های زیر استفاده کنید.",
            (
                "برای مدیریت و ویرایش تنظیمات تخفیف‌ها، "
                "از دکمه‌ی اختصاصی استفاده کنید."
            ),
        ]
    )
    rows = [
        button("💰 قیمت هر گیگ", f"srv:salesfield:{sid}:price_gb"),
        button("💰 قیمت هر ماه", f"srv:salesfield:{sid}:price_month"),
        button("📊 حداقل/حداکثر حجم و گام", f"srv:salesfield:{sid}:volume_range"),
        button("⌛ حداقل/حداکثر زمان و گام", f"srv:salesfield:{sid}:time_range"),
        back(sid, "settings"),
    ]
    return text, rows


def _discount_timer_line(sales, kind, label):
    raw = sales.get(f"discount_{kind}_until")
    if not raw:
        return ""
    try:
        end = parse_utc(raw)
        remaining = int((end - utcnow()).total_seconds())
    except (ValueError, TypeError):
        return ""
    if remaining <= 0:
        return ""
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    remaining_text = " و ".join(parts) if parts else "کمتر از یک دقیقه"
    return (
        f"⏱ تایمر {label}: {remaining_text} مانده "
        f"(پایان: {local_time(raw)})"
    )


def _discount_settings_view(service, sid):
    sales = service.sales(sid)
    simple_enabled = service.discount_active(sales, "simple")
    tiered_enabled = service.discount_active(sales, "tiered")
    tiers = sales.get("discount_tiers") or []
    lines = [
        "🎛 مدیریت حرفه‌ای تخفیف‌ها",
        "",
        f"🎁 تخفیف حجمی ساده: {'فعال ✅' if simple_enabled else 'غیرفعال ❌'}",
        f"🎚 تخفیف پلاکانی: {'فعال ✅' if tiered_enabled else 'غیرفعال ❌'}",
        "",
        (
            "در این بخش می‌توانی تنظیمات ذخیره‌شده هر نوع تخفیف را ببینی "
            "و تنها در صورت نیاز آن را تغییر بدهی."
        ),
    ]
    simple_timer = _discount_timer_line(
        sales, "simple", "تخفیف حجمی ساده"
    )
    tiered_timer = _discount_timer_line(
        sales, "tiered", "تخفیف پلاکانی"
    )
    if simple_timer:
        lines.append(simple_timer)
    if tiered_timer:
        lines.append(tiered_timer)
    if simple_enabled:
        lines.append(
            f"• تخفیف حجمی ساده: از {sales['discount_step_gb']} گیگ به بالا، "
            f"{sales['discount_percent_step']}٪ تا سقف "
            f"{sales['discount_percent_max']}٪"
        )
    elif (
        int(sales.get("discount_step_gb") or 0) > 0
        and int(sales.get("discount_percent_step") or 0) > 0
    ):
        lines.append(
            f"• تنظیمات ذخیره‌شده تخفیف حجمی ساده: از "
            f"{sales['discount_step_gb']} گیگ به بالا، "
            f"{sales['discount_percent_step']}٪ تا سقف "
            f"{sales['discount_percent_max']}٪ (غیرفعال)"
        )
    if tiered_enabled:
        lines.append(f"• پله‌های تخفیف پلاکانی: {_format_discount_tiers(tiers)}")
    elif tiers:
        lines.append(
            "• پله‌های تخفیف پلاکانی ذخیره شده: "
            f"{_format_discount_tiers(tiers)} (غیرفعال)"
        )
    rows = [
        [
            Button(
                ("خاموش کن" if simple_enabled else "روشن کن")
                + " تخفیف حجمی ساده",
                callback_data=(
                    f"srv:discounttoggle:{sid}:simple:"
                    f"{'off' if simple_enabled else 'on'}"
                ),
                style="danger" if simple_enabled else "success",
            )
        ],
        [
            Button(
                ("خاموش کن" if tiered_enabled else "روشن کن")
                + " تخفیف پلاکانی",
                callback_data=(
                    f"srv:discounttoggle:{sid}:tiered:"
                    f"{'off' if tiered_enabled else 'on'}"
                ),
                style="danger" if tiered_enabled else "success",
            )
        ],
        button("✏️ ویرایش تخفیف حجمی ساده", f"srv:discountedit:{sid}:simple"),
        button("✏️ ویرایش تخفیف پله‌ای", f"srv:discountedit:{sid}:tiered"),
        button(
            "⏱ تنظیم تایمر تخفیف حجمی ساده",
            f"srv:discountedit:{sid}:simple_timer",
        ),
        button(
            "⏱ تنظیم تایمر تخفیف پلاکانی",
            f"srv:discountedit:{sid}:tiered_timer",
        ),
        back(sid, "plans"),
    ]
    return "\n".join(lines), rows


def _format_gb(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _qr_image(data: str) -> BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(str(data))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


def _expiry_text(raw) -> str:
    if not raw:
        return "نامشخص"
    try:
        seconds = (parse_utc(raw) - utcnow()).total_seconds()
    except (ValueError, TypeError):
        return local_time(raw)
    if seconds < 0:
        days = max(0, math.ceil(abs(seconds) / 86400))
        return f"منقضی شده ({days} روز پیش)"
    days = max(1, math.ceil(seconds / 86400))
    return f"{days} روز دیگر"


def _panel_native_link(business, sid: int, external_ref: str) -> str:
    try:
        server = business.server(int(sid))
        target = business._panel_target(server)
        return str(
            business.panel_adapter.subscription_link(
                target=target, external_ref=str(external_ref)
            )
            or ""
        ).strip()
    except (TenantBusinessError, PanelError, ValueError, TypeError):
        return ""


def _panel_user_page_link(business, sid: int, external_ref: str) -> str:
    link = _panel_native_link(business, sid, external_ref)
    if not link:
        return ""
    kind = str(business.server(int(sid)).get("panel_kind") or "").lower()
    if kind == "hiddify" and link.rstrip("/").endswith("/all.txt"):
        return link.rstrip("/")[:-len("all.txt")].rstrip("/") + "/"
    return link


def _cluster_labels(service, actor: int, sid: int, uid: int) -> list[str]:
    try:
        _, targets = service.related_targets(actor, sid, uid)
    except (TenantBusinessError, ValueError, TypeError):
        targets = [(int(sid), "")]
    labels: list[str] = []
    for target_sid, _ in targets:
        try:
            label = str(service.b.server(int(target_sid)).get("label") or "").strip()
        except TenantBusinessError:
            continue
        if label and label not in labels:
            labels.append(label)
    return labels


def _user_detail_text(service, actor: int, sid: int, uid: int, row: dict) -> str:
    limit = int(row.get("traffic_bytes") or 0) / 1024**3
    usage = int(row.get("usage_bytes") or 0) / 1024**3
    labels = _cluster_labels(service, actor, sid, uid)
    name = escape(str(row.get("name") or row.get("external_ref") or "کاربر"))
    user_page = _panel_user_page_link(service.b, sid, str(row.get("external_ref") or ""))
    header = (
        f'👤 کاربر: <a href="{escape(user_page, quote=True)}">{name}</a>'
        if user_page
        else f"👤 کاربر: {name}"
    )
    location_line = (
        "◇ سرویس لوکیشن: " + "، ".join(escape(x) for x in labels)
        if labels
        else f"⬖ سرور: {escape(str(service.b.server(sid).get('label') or 'نامشخص'))}"
    )
    return "\n".join(
        [
            header,
            "❖⬩╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍⬩❖",
            location_line,
            (
                f"📊مصرف: {usage:.1f} از {limit:.1f} گیگابایت"
                if limit
                else f"📊مصرف: {usage:.1f} گیگابایت (نامحدود)"
            ),
            f"📆انقضا: {escape(_expiry_text(row.get('expires_at')))}",
            f"📶 وضعیت حساب: {STATUS_LABELS.get(user_status(row),'در انتظار')}",
            f"📶 آخرین اتصال: {escape(local_time(row.get('last_online')))}",
            f"📝یادداشت: {escape(row.get('comment') or '—')}",
        ]
    )


def _user_detail_rows(sid: int, uid: int):
    return [
        [Button("کانفیگ ها📄", callback_data=f"srv:pconfigs:{sid}:{uid}")],
        [Button("ویرایش کاربر✏️", callback_data=f"srv:pedit:{sid}:{uid}")],
        [Button("تمدید اشتراک♾️", callback_data=f"srv:prenew:{sid}:{uid}")],
        [Button("حذف کاربر🗑️", callback_data=f"srv:pdelete:{sid}:{uid}")],
        [Button("بازگشت به لیست کاربران", callback_data=f"srv:users:{sid}")],
    ]


def _config_menu_rows(business, sid: int, uid: int, row: dict):
    kind = str(business.server(int(sid)).get("panel_kind") or "").lower()
    ref = str(row.get("external_ref") or "")
    rows = [
        [Button("📄 کانفیگ‌های مستقیم", callback_data=f"srv:pconfigs:{sid}:{uid}:direct", style="primary")],
    ]
    if kind == "xnet":
        rows.extend(
            [
                [Button("🔗 لینک اشتراک X-NET", callback_data=f"srv:pconfigs:{sid}:{uid}:sub", style="primary")],
                [Button("🌐 اشتراک هوشمند", callback_data=f"srv:pconfigs:{sid}:{uid}:multi", style="success")],
                [Button("🌐 اشتراک هوشمند Base64", callback_data=f"srv:pconfigs:{sid}:{uid}:multi_b64", style="success")],
            ]
        )
    else:
        rows.extend(
            [
                [Button("🔄 اشتراک خودکار", callback_data=f"srv:pconfigs:{sid}:{uid}:auto_sub", style="primary")],
                [Button("🔗 لینک اشتراک اصلی", callback_data=f"srv:pconfigs:{sid}:{uid}:sub", style="primary")],
                [Button("🧬 لینک اشتراک Base64", callback_data=f"srv:pconfigs:{sid}:{uid}:sub_b64", style="primary")],
                [Button("🌐 اشتراک هوشمند", callback_data=f"srv:pconfigs:{sid}:{uid}:multi", style="success")],
                [Button("🌐 اشتراک هوشمند Base64", callback_data=f"srv:pconfigs:{sid}:{uid}:multi_b64", style="success")],
            ]
        )
        panel_link = _panel_user_page_link(business, sid, ref)
        if panel_link:
            rows.append(
                [Button("🚪 ورود به پنل کاربر", url=panel_link, style="success")]
            )
        else:
            rows.append(
                [Button("🚪 ورود به پنل کاربر", callback_data=f"srv:pconfigs:{sid}:{uid}:bot_link", style="success")]
            )
    rows.append(
        [Button("🔙 برگشت به جزئیات کاربر", callback_data=f"srv:puser:{sid}:{uid}", style="primary")]
    )
    return rows


def _creation_summary(flow: dict) -> str:
    count = int(flow.get("count") or 1)
    if count == 1:
        return (
            "لطفاً اطلاعات را تایید کنید:\n"
            f"👤 کاربر: {flow['name']}\n"
            f"📊 مصرف: {_format_gb(flow['gb'])} گیگابایت\n"
            f"📅 مدت: {int(flow['days'])} روز"
        )
    return (
        "لطفاً اطلاعات را تایید کنید:\n"
        f"👥 تعداد کاربران: {count}\n"
        f"👤 پیشوند نام: {flow['name']}\n"
        f"📊 حجم هر کاربر: {_format_gb(flow['gb'])} گیگابایت\n"
        f"📅 مدت: {int(flow['days'])} روز"
    )


async def edit(query, text, rows, *, html=False):
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup(rows),
            parse_mode="HTML" if html else None,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def paged_edit(query, context, sid, text, rows, *, html=False, page=0, key=None):
    if len(rows) <= 24 and len(text) <= 3500 and key is None:
        return await edit(query, text, rows, html=html)
    state_key = f"server_pages:{sid}"
    if key is None:
        key = uuid.uuid4().hex[:8]
        context.user_data[state_key] = dict(key=key, text=text, rows=rows, html=html)
    state = context.user_data.get(state_key)
    if not isinstance(state, dict) or state["key"] != key:
        raise TenantBusinessError("list expired")
    rows, text, html = state["rows"], state["text"], state["html"]
    items, footer = rows[:-1], rows[-1:]
    page = max(0, min(int(page), max(0, (len(items) - 1) // 20)))
    visible = items[page * 20 : (page + 1) * 20]
    nav = []
    if page:
        nav.append(Button("◀️ قبلی", callback_data=f"srv:page:{sid}:{key}:{page-1}"))
    if (page + 1) * 20 < len(items):
        nav.append(Button("بعدی ▶️", callback_data=f"srv:page:{sid}:{key}:{page+1}"))
    if nav:
        visible = visible + [nav]
    if len(text) > 3500:
        # Every item remains reachable in the paginated keyboard.
        text = text.split("\n", 1)[0] + f"\nتعداد گزینه‌ها: {len(items)}"
        html = False
    await edit(query, text + f"\n📄 صفحه {page+1}", visible + footer, html=html)


async def prompt(update, context, flow, text):
    from TenantRuntime.AdminBot.handlers import cancel_keyboard

    context.user_data[FLOW] = flow
    await update.effective_message.reply_text(text, reply_markup=cancel_keyboard())


def list_user_status(row, *, now):
    """Presentation status only; never change account or quota enforcement."""
    state = user_status(row)
    if state == "expired":
        return "expired"
    if state != "active":
        return "disabled"
    extra = json.loads(row.get("extra_json") or "{}")
    # X-UI/X-NET report live connections independently from last-seen history.
    if extra.get("online") is False:
        return "offline"
    last_seen = (
        row.get("last_synced_at")
        if extra.get("online") is True
        else row.get("last_online")
    )
    try:
        seconds = (now - parse_utc(last_seen)).total_seconds()
        if -120 <= seconds <= 90:
            return "online"
    except (ValueError, TypeError, AttributeError):
        pass
    return "offline"


async def send_list(query, service, actor, sid, *, page=0, status="all"):
    warning = ""
    try:
        await service.refresh_users(actor, sid)
    except TenantBusinessError:
        warning = (
            "\n\n⚠️ اتصال به پنل برقرار نیست؛ آخرین فهرست ذخیره‌شده نمایش داده می‌شود."
        )
    items = [
        r
        for r in service.users(actor, sid, status=status)
        if r.get("state") != "pending"
    ]
    # Preserve the panel's order, including when falling back to its last snapshot.
    items.sort(
        key=lambda r: (
            json.loads(r.get("extra_json") or "{}").get("list_position", r["id"]),
            r["id"],
        )
    )
    frozen_ids = {r["user_id"] for r in service.frozen(actor, sid)}
    now = utcnow()
    states = {r["id"]: list_user_status(r, now=now) for r in items}
    counts = {
        state: sum(value == state for value in states.values())
        for state in ("online", "offline", "disabled", "expired")
    }
    total = len(items)
    pages = max(1, (total + 19) // 20)
    page = max(0, min(int(page), pages - 1))
    if not items:
        text = "[📋 لیست کاربران]\nهنوز هیچ کاربری برای این سرور ثبت نشده است.\n\n"
    else:
        text = (
            "[📋 لیست کاربران]\n"
            "❕ شما می‌توانید لیست کاربران و اطلاعات آن‌ها را اینجا مشاهده کنید.\n"
            f"📄 صفحه: {page+1}/{pages}\n"
        )
    text += (
        f"👥 تعداد کاربران: {total}\n"
        f"🔵 آنلاین: {counts['online']}\n"
        f"🟡 آفلاین: {counts['offline']}\n"
        f"⚫ غیرفعال: {counts['disabled']}\n"
        f"🔴 منقضی شده: {counts['expired']}\n"
        f"❄️ یخ‌زده (نود قطع): {sum(r['id'] in frozen_ids for r in items)}" + warning
    )
    icons = {"online": "🔵", "offline": "🟡", "disabled": "⚫", "expired": "🔴"}
    buttons = []
    for row in items[page * 20 : (page + 1) * 20]:
        label = (
            ("❄️" if row["id"] in frozen_ids else "")
            + icons[states[row["id"]]]
            + row["name"]
        )
        buttons.append(
            Button(label, callback_data=f'srv:puser:{sid}:{row["id"]}', style="primary")
        )
    rows = [list(reversed(buttons[i : i + 3])) for i in range(0, len(buttons), 3)]
    if items:
        nav = []
        if page:
            nav.append(Button("➡️", callback_data=f"srv:users:{sid}:{status}:{page-1}"))
        nav.append(Button(f"{page+1}/{pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(Button("⬅️", callback_data=f"srv:users:{sid}:{status}:{page+1}"))
        rows.append(nav)
    rows.append(button("بازگشت", f"srv:view:{sid}"))
    await edit(query, text, rows)


def user_text(row, server):
    """Compatibility formatter for list/search callers without service context."""
    limit = int(row.get("traffic_bytes") or 0) / 1024**3
    usage = int(row.get("usage_bytes") or 0) / 1024**3
    return "\n".join(
        [
            f"👤 کاربر: {escape(row['name'])}",
            "❖⬩╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍⬩❖",
            f"⬖ سرور: {escape(server['label'])}",
            (
                f"📊مصرف: {usage:.1f} از {limit:.1f} گیگابایت"
                if limit
                else f"📊مصرف: {usage:.1f} گیگابایت (نامحدود)"
            ),
            f"📆انقضا: {escape(_expiry_text(row.get('expires_at')))}",
            f"📶 وضعیت حساب: {STATUS_LABELS.get(user_status(row),'در انتظار')}",
            f"📶 آخرین اتصال: {escape(local_time(row.get('last_online')))}",
            f"📝یادداشت: {escape(row.get('comment') or '—')}",
        ]
    )


async def user_detail(query, service, actor, sid, uid, *, refresh=True, editing=False):
    warning = ""
    try:
        row = (
            await service.live_user(actor, sid, uid)
            if refresh
            else service.user(actor, sid, uid)
        )
    except TenantBusinessError:
        row = service.user(actor, sid, uid)
        warning = (
            "⚠️ دریافت اطلاعات زنده ممکن نیست؛ اطلاعات ذخیره‌شده نمایش داده می‌شود.\n\n"
        )
    if editing:
        rows = [
            button(
                "کاربر فعال 🟢" if row["active"] else "کاربر غیرفعال 🔴",
                f"srv:ptoggle:{sid}:{uid}",
            ),
            [
                Button("بازنشانی حجم🔄", callback_data=f"srv:preset:{sid}:{uid}:usage"),
                Button("ویرایش حجم📊", callback_data=f"srv:pfield:{sid}:{uid}:volume"),
            ],
            [
                Button("بازنشانی مدت🔄", callback_data=f"srv:preset:{sid}:{uid}:days"),
                Button("ویرایش مدت📅", callback_data=f"srv:pfield:{sid}:{uid}:days"),
            ],
            button("ویرایش یادداشت📝", f"srv:pfield:{sid}:{uid}:comment"),
            button("تغییرنام اشتراک✏️", f"srv:pfield:{sid}:{uid}:name"),
            button("بازگشت🔙", f"srv:puser:{sid}:{uid}"),
        ]
    else:
        rows = _user_detail_rows(sid, uid)
    await edit(
        query,
        warning + _user_detail_text(service, actor, sid, uid, row),
        rows,
        html=True,
    )


async def _send_created_user_detail(message, service, actor, sid, uid):
    warning = ""
    try:
        row = await service.live_user(actor, sid, uid)
    except TenantBusinessError:
        row = service.user(actor, sid, uid)
        warning = (
            "⚠️ دریافت اطلاعات زنده ممکن نیست؛ اطلاعات ذخیره‌شده نمایش داده می‌شود.\n\n"
        )
    await message.reply_text(
        warning + _user_detail_text(service, actor, sid, uid, row),
        reply_markup=markup(_user_detail_rows(sid, uid)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def plan_list(query, context, service, actor, sid, category=None):
    plans = service.plans(actor, sid)
    if category is not None:
        plans = [p for p in plans if int(p.get("category_id") or 0) == int(category)]
    rows = [
        button(
            f"{p['name']} · {p['price']:,} {p['currency']}"[:60],
            f'srv:plan:{sid}:{p["id"]}',
        )
        for p in plans
    ]
    rows.extend([button("➕ افزودن پلن", f"srv:planadd:{sid}"), back(sid)])
    await paged_edit(
        query,
        context,
        sid,
        (
            "📋 لیست پلن‌های موجود\nپلن موردنظر را انتخاب کنید."
            if plans
            else "📋 برای این بخش هنوز پلنی ثبت نشده است."
        ),
        rows,
    )


async def handle_callback(update, context, *, business, actor):
    data = str(update.callback_query.data or "")
    if data == "noop":
        business._admin(actor)
        await update.callback_query.answer()
        return True
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "srv" or parts[1] not in ROUTES:
        return False
    action, sid = parts[1], int(parts[2])
    service = ServerAdminService(business)
    service.authorize(actor, sid)
    query = update.callback_query
    await query.answer()
    context.user_data.pop("biz_flow", None)
    if action not in {
        "presetok",
        "pdeleteok",
        "nodedelok",
        "domaindeleteok",
        "planarchiveok",
        "deleteok",
    }:
        context.user_data.pop("server_action_confirmation", None)
    context.user_data.pop(FLOW, None)

    async def show(text, rows, *, html=False):
        await paged_edit(query, context, sid, text, rows, html=html)

    if action == "page":
        await paged_edit(query, context, sid, "", [], key=parts[3], page=int(parts[4]))
    elif action in {"user", "useract"}:
        # Old messages resolve to the current panel user and its ownership scope.
        sub = business._admin_subscription(actor, int(parts[3]))
        mapping = business.conn.execute(
            "SELECT external_ref FROM tenant_subscription_nodes WHERE tenant_id=? AND subscription_id=? AND server_id=?",
            (business.tenant_id, sub["id"], sid),
        ).fetchone()
        ref = (
            mapping["external_ref"]
            if mapping
            else sub["external_ref"] if int(sub["server_id"]) == sid else None
        )
        if not ref:
            raise TenantBusinessError("user not on server")
        await service.refresh_users(actor, sid)
        user = next(
            (r for r in service.users(actor, sid) if r["external_ref"] == ref), None
        )
        if not user:
            raise TenantBusinessError("user not on server")
        await user_detail(query, service, actor, sid, user["id"])
    elif action == "view":
        from TenantRuntime.AdminBot.handlers import _server_detail_view

        note = ""
        try:
            inventory = await service.refresh_users(actor, sid)
            count = len(inventory)
        except TenantBusinessError:
            cached = service.users(actor, sid)
            count = len(cached) if cached else None
            note = (
                "\n\n⚠️ ارتباط با پنل برقرار نیست؛ اطلاعات ذخیره‌شده نمایش داده می‌شود."
            )
        text, kb = _server_detail_view(business, actor, sid, users_count=count)
        await show(text + note, kb.inline_keyboard, html=True)
    elif action == "users":
        status = (
            parts[3]
            if len(parts) > 3 and parts[3] in {"all", "active", "disabled", "expired"}
            else "all"
        )
        page = int(parts[4]) if len(parts) > 4 else 0
        await send_list(query, service, actor, sid, page=page, status=status)
    elif action in {"puser", "pedit"}:
        await user_detail(
            query, service, actor, sid, int(parts[3]), editing=action == "pedit"
        )
    elif action == "userops":
        await show(
            "عملیات کاربری🛡️\nدر این بخش می‌توانید کاربران جدید اضافه کنید یا بین کاربران جستجو کنید.",
            [
                button("افزودن کاربر➕", f"srv:useradd:{sid}:single"),
                button("افزودن چندین کاربر➕", f"srv:useradd:{sid}:multi"),
                button("افزودن کاربر با پلن➕", f"srv:useraddplan:{sid}"),
                button("جستجوی کاربر🔍", f"srv:usersearch:{sid}"),
                back(sid),
            ],
        )
    elif action == "usersearch":
        await prompt(
            update,
            context,
            dict(kind="search", sid=sid),
            "🔍 جستجوی هوشمند کاربر در این سرور\nنام کاربر، UUID یا لینک کانفیگ را ارسال کنید.",
        )
    elif action == "useradd":
        multi = len(parts) > 3 and parts[3] == "multi"
        await prompt(
            update,
            context,
            dict(
                kind="create_count" if multi else "create_name",
                sid=sid,
                count=1,
                operation_key=uuid.uuid4().hex,
            ),
            (
                "➕ افزودن چندین کاربر\n👥 چند کاربر می‌خواهید اضافه کنید؟\nمثال: 5"
                if multi
                else "لطفاً نام کاربر را وارد کنید:"
            ),
        )
    elif action == "useraddplan":
        if len(parts) > 3:
            plan = service.plan(actor, sid, int(parts[3]))
            if plan["status"] != "active":
                raise TenantBusinessError("plan is disabled")
            await prompt(
                update,
                context,
                dict(
                    kind="create_name",
                    sid=sid,
                    count=1,
                    gb=plan["traffic_gb"],
                    days=plan["duration_days"],
                    operation_key=uuid.uuid4().hex,
                ),
                "لطفاً نام کاربر را وارد کنید:",
            )
        else:
            plans = [p for p in service.plans(actor, sid) if p["status"] == "active"]
            await paged_edit(
                query,
                context,
                sid,
                (
                    "📋 لیست پلن‌های موجود"
                    if plans
                    else "❌ برای این سرور هنوز هیچ پلنی ثبت نشده است."
                ),
                [
                    button(
                        f"{p['name']} | {p['duration_days']} روز | {p['traffic_gb']} گیگ"[
                            :60
                        ],
                        f'srv:useraddplan:{sid}:{p["id"]}',
                    )
                    for p in plans
                ]
                + [back(sid, "userops")],
            )
    elif action == "pfield":
        uid = int(parts[3])
        field = parts[4]
        row = service.user(actor, sid, uid)
        prompts = {
            "name": "نام جدید اشتراک را وارد کنید:",
            "comment": "یادداشت جدید را وارد کنید؛ برای پاک‌کردن «-» بفرستید.",
            "volume": "حجم جدید را به گیگابایت وارد کنید:",
            "days": "مدت جدید اشتراک را به روز وارد کنید:",
        }
        if field not in prompts:
            raise ValueError("invalid field")
        await prompt(
            update,
            context,
            dict(kind="user_field", sid=sid, uid=uid, field=field),
            prompts[field],
        )
    elif action == "ptoggle":
        await service.toggle_user(actor, sid, int(parts[3]))
        await user_detail(query, service, actor, sid, int(parts[3]), editing=True)
    elif action in {"preset", "presetok", "pdelete", "pdeleteok"}:
        uid = int(parts[3])
        row = service.user(actor, sid, uid)
        operation = parts[4] if len(parts) > 4 else "all"
        if action in {"preset", "pdelete"}:
            token = f"{sid}:{uid}:{operation}:{action}"
            context.user_data["server_action_confirmation"] = token
            if action == "preset":
                rows = [
                    button(
                        "✅ تایید بازنشانی", f"srv:presetok:{sid}:{uid}:{operation}"
                    ),
                    button("لغو❌", f"srv:pedit:{sid}:{uid}"),
                ]
                text = (
                    "آیا از بازنشانی "
                    + ("حجم مصرف‌شده" if operation == "usage" else "مدت اشتراک")
                    + " مطمئن هستید؟"
                )
            else:
                rows = [
                    button("🗑 حذف کامل از همه سرورها", f"srv:pdeleteok:{sid}:{uid}:all")
                ]
                # Single-server deletion is available for independent users or child replicas.
                sub = (
                    business._admin_subscription(actor, row["subscription_id"])
                    if row["subscription_id"]
                    else None
                )
                if not sub or int(sub["server_id"]) != sid:
                    rows.insert(
                        0,
                        button("🧹 فقط همین سرور", f"srv:pdeleteok:{sid}:{uid}:single"),
                    )
                rows.append(button("لغو❌", f"srv:puser:{sid}:{uid}"))
                text = "⚠️ نوع حذف را انتخاب کنید. حذف کامل، کاربر را از سرور اصلی و نودهای مرتبط حذف می‌کند."
            await show(text, rows)
        else:
            expected = f'{sid}:{uid}:{operation}:{"preset" if action=="presetok" else "pdelete"}'
            token = context.user_data.pop("server_action_confirmation", None)
            if action == "pdeleteok":
                if operation not in {"all", "single"}:
                    raise ValueError("invalid deletion scope")
                valid = token == f"{sid}:{uid}:all:pdelete"
            else:
                valid = token == expected
            if not valid:
                raise TenantBusinessError("confirmation expired")
            if action == "presetok":
                if operation not in {"usage", "days"}:
                    raise ValueError("invalid reset")
                changes = (
                    {"reset_usage": True}
                    if operation == "usage"
                    else {
                        "reset_days": True,
                        "expires_at": iso_utc(
                            utcnow()
                            + timedelta(
                                days=max(
                                    1,
                                    service.reset_duration(actor, sid, uid),
                                )
                            )
                        ),
                    }
                )
                await service.edit_user(actor, sid, uid, changes)
                await user_detail(query, service, actor, sid, uid, editing=True)
            else:
                await service.delete_user(
                    actor, sid, uid, all_targets=operation == "all"
                )
                await show("✅ کاربر حذف شد.", [back(sid, "users")])
    elif action == "prenew":
        uid = int(parts[3])
        service.user(actor, sid, uid)
        await prompt(
            update,
            context,
            dict(kind="renew_gb", sid=sid, uid=uid, operation_key=uuid.uuid4().hex),
            "🎛 تمدید اشتراک\nحجم جدید را به گیگابایت وارد کنید:",
        )
    elif action == "pconfigs":
        uid = int(parts[3])
        row = service.user(actor, sid, uid)
        scoped_row, targets = service.related_targets(actor, sid, uid)
        rows = _config_menu_rows(business, sid, uid, row)
        cfg_type = parts[4] if len(parts) > 4 else ""

        if not cfg_type:
            await show(
                _user_detail_text(service, actor, sid, uid, row),
                rows,
                html=True,
            )
        elif cfg_type == "direct" and len(parts) == 5:
            await show(
                "📄 کانفیگ مستقیم\n"
                f"👤 کاربر: {escape(str(row.get('name') or 'کاربر'))}\n"
                "━━━━━━━━━━━━━━\n"
                "پروتکل موردنظر را انتخاب کنید:",
                [
                    [Button("🟢 VLESS", callback_data=f"srv:pconfigs:{sid}:{uid}:direct:vless")],
                    [Button("🔵 VMESS", callback_data=f"srv:pconfigs:{sid}:{uid}:direct:vmess")],
                    [Button("🟠 TROJAN", callback_data=f"srv:pconfigs:{sid}:{uid}:direct:trojan")],
                    [Button("🔙 بازگشت", callback_data=f"srv:pconfigs:{sid}:{uid}")],
                ],
                html=True,
            )
        elif cfg_type == "direct" and len(parts) >= 6:
            proto = str(parts[5] or "").lower()
            if proto not in {"vless", "vmess", "trojan"}:
                raise ValueError("invalid direct protocol")
            links: list[str] = []
            seen: set[str] = set()
            errors = 0
            for target_sid, ref in targets:
                try:
                    raw = str(
                        await service.call(
                            target_sid,
                            "subscription_content",
                            external_ref=ref,
                        )
                    )
                except TenantBusinessError:
                    errors += 1
                    continue
                for line in decode_subscription_lines(raw):
                    low = line.lower()
                    if not low.startswith(proto + "://") or line in seen:
                        continue
                    seen.add(line)
                    links.append(line)
            if not links:
                await show(
                    f"❌ کانفیگ مستقیم {proto.upper()} یافت نشد.\n"
                    "برای این کاربر هیچ کانفیگ مناسبی یافت نشد.",
                    [[Button("🔙 بازگشت به منوی کانفیگ‌ها", callback_data=f"srv:pconfigs:{sid}:{uid}")]],
                )
            else:
                payload = "\n".join(links)
                body = (
                    f"🔗 کانفیگ‌های {proto.upper()}\n"
                    "برای کپی، کل باکس زیر را یکجا کپی کنید:\n"
                    f"<pre><code>{escape(payload)}</code></pre>"
                )
                back_rows = [[Button("🔙 بازگشت به منوی کانفیگ‌ها", callback_data=f"srv:pconfigs:{sid}:{uid}")]]
                if errors:
                    body += f"\n⚠️ دریافت کانفیگ از {errors} سرور ممکن نشد."
                if len(body) <= 3900:
                    await show(body, back_rows, html=True)
                else:
                    await update.effective_message.reply_document(
                        BytesIO(payload.encode()),
                        filename=f"{proto}-configs-{uid}.txt",
                    )
                    await show("📄 کانفیگ‌ها در فایل ارسال شدند.", back_rows)
        else:
            kind = str(business.server(int(sid)).get("panel_kind") or "").lower()
            native = _panel_native_link(
                business, sid, str(row.get("external_ref") or "")
            )
            panel_page = _panel_user_page_link(
                business, sid, str(row.get("external_ref") or "")
            )
            url = ""
            caption = ""

            if cfg_type == "auto_sub":
                if not native:
                    raise TenantBusinessError("subscription link is unavailable")
                url = (
                    panel_page.rstrip("/") + "/sub/?asn=unknown"
                    if kind == "hiddify" and panel_page
                    else native
                )
                caption = "لینک اشتراک خودکار"
            elif cfg_type in {"sub", "link"}:
                if not native:
                    raise TenantBusinessError("subscription link is unavailable")
                url = native
                caption = "لینک اشتراک X-NET" if kind == "xnet" else "لینک اشتراک"
            elif cfg_type == "sub_b64":
                if not native:
                    raise TenantBusinessError("subscription link is unavailable")
                sep = "&" if "?" in native else "?"
                url = native + sep + "base64=1"
                caption = "لینک اشتراک b64"
            elif cfg_type in {"multi", "multi_b64"}:
                base64_output = cfg_type == "multi_b64"
                try:
                    if scoped_row.get("subscription_id"):
                        url = business.admin_smart_subscription_link(
                            actor,
                            subscription_id=int(scoped_row["subscription_id"]),
                            base64_output=base64_output,
                        )
                    else:
                        source_uid = int(scoped_row.get("_source_user_id") or uid)
                        url = business.admin_panel_smart_subscription_link(
                            actor,
                            panel_user_id=source_uid,
                            base64_output=base64_output,
                        )
                except TenantBusinessError:
                    fallback_rows = []
                    if panel_page:
                        fallback_rows.append(
                            [Button("🚪 ورود به پنل کاربر", url=panel_page, style="success")]
                        )
                    fallback_rows.append(
                        [Button("🔙 برگشت به منوی لینک‌ها", callback_data=f"srv:pconfigs:{sid}:{uid}")]
                    )
                    await show(
                        (
                            "❌ لینک اشتراک هوشمند برای این کاربر هنوز آماده نیست.\n"
                            "آدرس عمومی Smart Subscription را در تنظیمات سرور WhiteLabel ثبت کنید."
                        ),
                        fallback_rows,
                    )
                    return True
                caption = (
                    "لینک اشتراک هوشمند b64"
                    if base64_output
                    else "لینک اشتراک هوشمند"
                )
            elif cfg_type == "bot_link":
                if not panel_page:
                    raise TenantBusinessError("panel user link is unavailable")
                await show(
                    f"🌐 لینک پنل کاربر\n{escape(panel_page)}",
                    [
                        [Button("🚪 باز کردن پنل کاربر", url=panel_page, style="success")],
                        [Button("🔙 برگشت به منوی لینک‌ها", callback_data=f"srv:pconfigs:{sid}:{uid}")],
                    ],
                    html=True,
                )
                return True
            else:
                raise ValueError("invalid config action")

            if not url:
                raise TenantBusinessError("subscription link is unavailable")
            bot = getattr(context, "bot", None)
            if bot is None:
                raise TenantBusinessError("telegram bot is unavailable")
            chat_id = getattr(update.effective_chat, "id", None) or actor
            await bot.send_photo(
                chat_id=chat_id,
                photo=_qr_image(url),
                caption=f"{caption}\n{url}",
                reply_markup=markup(
                    [[Button("🔙 بازگشت به منوی کانفیگ‌ها", callback_data=f"srv:pconfigs:{sid}:{uid}")]]
                ),
            )
    elif action == "plans":
        text, rows = _plans_root_view(business, service, sid)
        await show(text, rows)
    elif action == "plan":
        if len(parts) == 3:
            await plan_list(query, context, service, actor, sid)
        else:
            pid = int(parts[3])
            p = service.plan(actor, sid, pid)
            rows = [
                button("✏️ نام پلن", f"srv:planfield:{sid}:{pid}:name"),
                [
                    Button(
                        "📊 حجم", callback_data=f"srv:planfield:{sid}:{pid}:traffic_gb"
                    ),
                    Button(
                        "📅 مدت",
                        callback_data=f"srv:planfield:{sid}:{pid}:duration_days",
                    ),
                ],
                [
                    Button("💰 قیمت", callback_data=f"srv:planfield:{sid}:{pid}:price"),
                    Button(
                        "💱 واحد پول",
                        callback_data=f"srv:planfield:{sid}:{pid}:currency",
                    ),
                ],
                button("📂 دسته‌بندی", f"srv:plancategory:{sid}:{pid}"),
                button("🔢 اولویت", f"srv:planfield:{sid}:{pid}:priority"),
                button(
                    "🟢 فعال" if p["status"] == "active" else "🔴 غیرفعال",
                    f"srv:plantoggle:{sid}:{pid}",
                ),
                button("🗑 حذف پلن", f"srv:planarchive:{sid}:{pid}"),
                back(sid, "plan"),
            ]
            scope = (
                "تمام سرورها"
                if p.get("server_id") is None
                else business.server(sid)["label"]
            )
            await show(
                f"📋 {p['name']}\n📊 حجم: {p['traffic_gb']} گیگ\n📅 مدت: {p['duration_days']} روز\n💰 قیمت: {p['price']:,} {p['currency']}\n📡 محدوده: {scope}\n📂 دسته: {p.get('category_title') or 'بدون دسته'}\nوضعیت: {p['status']}",
                rows,
            )
    elif action in {"planadd", "planfield"}:
        if action == "planadd":
            await prompt(
                update,
                context,
                dict(kind="plan_name", sid=sid),
                "📋 نام پلن جدید این سرور را وارد کنید:",
            )
        else:
            pid = int(parts[3])
            field = parts[4]
            service.plan(actor, sid, pid)
            if field not in {
                "name",
                "traffic_gb",
                "duration_days",
                "price",
                "currency",
                "priority",
            }:
                raise ValueError("invalid plan field")
            await prompt(
                update,
                context,
                dict(kind="plan_field", sid=sid, pid=pid, field=field),
                "مقدار جدید "
                + {
                    "name": "نام پلن",
                    "traffic_gb": "حجم (گیگابایت)",
                    "duration_days": "مدت (روز)",
                    "price": "قیمت",
                    "currency": "واحد پول",
                    "priority": "اولویت",
                }[field]
                + " را وارد کنید:",
            )
    elif action == "plantoggle":
        pid = int(parts[3])
        p = service.plan(actor, sid, pid)
        service.edit_plan(
            actor,
            sid,
            pid,
            {"status": "disabled" if p["status"] == "active" else "active"},
        )
        await show(
            "✅ وضعیت پلن ذخیره شد.", [button("بازگشت🔙", f"srv:plan:{sid}:{pid}")]
        )
    elif action in {"planarchive", "planarchiveok"}:
        pid = int(parts[3])
        service.plan(actor, sid, pid)
        if action == "planarchive":
            context.user_data["server_action_confirmation"] = f"plan:{sid}:{pid}"
            await show(
                "❓ پلن از فروش حذف شود؟ اشتراک‌ها و سفارش‌های قبلی حفظ می‌شوند.",
                [
                    button("✅ حذف پلن", f"srv:planarchiveok:{sid}:{pid}"),
                    button("لغو❌", f"srv:plan:{sid}:{pid}"),
                ],
            )
        else:
            if (
                context.user_data.pop("server_action_confirmation", None)
                != f"plan:{sid}:{pid}"
            ):
                raise TenantBusinessError("confirmation expired")
            service.edit_plan(actor, sid, pid, {"status": "archived"})
            await plan_list(query, context, service, actor, sid)
    elif action == "plancategory":
        pid = int(parts[3])
        service.plan(actor, sid, pid)
        if len(parts) > 4:
            cid = int(parts[4])
            service.edit_plan(actor, sid, pid, {"category_id": cid or None})
            await show(
                "✅ دسته پلن ذخیره شد.", [button("بازگشت🔙", f"srv:plan:{sid}:{pid}")]
            )
        else:
            await show(
                "📂 دسته پلن را انتخاب کنید:",
                [
                    button(c["title"], f'srv:plancategory:{sid}:{pid}:{c["id"]}')
                    for c in business.list_plan_categories(public=False)
                ]
                + [
                    button("بدون دسته", f"srv:plancategory:{sid}:{pid}:0"),
                    button("بازگشت🔙", f"srv:plan:{sid}:{pid}"),
                ],
            )
    elif action == "categories":
        await show(
            "📂 لیست دسته‌های پلن",
            [
                button(c["title"], f'srv:category:{sid}:{c["id"]}')
                for c in business.list_plan_categories(public=False)
            ]
            + [
                button("سایر پلن‌ها", f"srv:category:{sid}:0"),
                button("➕ افزودن دسته", f"srv:categoryadd:{sid}"),
                back(sid, "plans"),
            ],
        )
    elif action == "categoryadd":
        await prompt(
            update,
            context,
            dict(kind="category_add", sid=sid),
            "📂 عنوان دسته جدید را وارد کنید:",
        )
    elif action == "category":
        cid = int(parts[3])
        c = business.plan_category(cid, public=False) if cid else None
        plans = [
            p
            for p in service.plans(actor, sid)
            if int(p.get("category_id") or 0) == cid
        ]
        rows = [button(p["name"], f'srv:plan:{sid}:{p["id"]}') for p in plans]
        if c:
            rows.extend(
                [
                    button("✏️ ویرایش عنوان", f"srv:categoryfield:{sid}:{cid}:title"),
                    button("🔢 اولویت", f"srv:categoryfield:{sid}:{cid}:priority"),
                    button(
                        "🟢 فعال" if c["status"] == "active" else "🔴 غیرفعال",
                        f"srv:categorytoggle:{sid}:{cid}",
                    ),
                ]
            )
        rows.extend(
            [button("➕ افزودن پلن", f"srv:planadd:{sid}"), back(sid, "categories")]
        )
        await show("📂 " + (c["title"] if c else "سایر پلن‌ها"), rows)
    elif action == "categoryfield":
        cid = int(parts[3])
        field = parts[4]
        business.plan_category(cid, public=False)
        if field not in {"title", "priority"}:
            raise ValueError("invalid category field")
        await prompt(
            update,
            context,
            dict(kind="category_field", sid=sid, cid=cid, field=field),
            "مقدار جدید دسته را وارد کنید:",
        )
    elif action == "categorytoggle":
        cid = int(parts[3])
        c = business.plan_category(cid, public=False)
        business.update_plan_category_admin(
            actor,
            category_id=cid,
            status="disabled" if c["status"] == "active" else "active",
        )
        await show("✅ وضعیت دسته ذخیره شد.", [back(sid, "categories")])
    elif action == "settings":
        sales = service.sales(sid)
        names = {
            "min_gb": "حداقل حجم",
            "max_gb": "حداکثر حجم",
            "step_gb": "گام حجم",
            "min_days": "حداقل مدت",
            "max_days": "حداکثر مدت",
            "step_days": "گام مدت",
            "price_gb": "قیمت هر گیگ",
            "price_day": "قیمت هر روز",
            "currency": "واحد پول",
            "discount_percent": "درصد تخفیف",
        }
        rows = (
            [
                button(
                    "حالت نمایش: "
                    + {"fixed": "ثابت", "dynamic": "پویا", "mixed": "ترکیبی"}[
                        sales["mode"]
                    ],
                    f"srv:mode:{sid}",
                )
            ]
            + [
                button(f"{v}: {sales[k]}", f"srv:salesfield:{sid}:{k}")
                for k, v in names.items()
            ]
            + [back(sid, "plans")]
        )
        await show(
            "⚙️ تنظیمات پلن‌ها\nقیمت پلن پویا = (حجم × قیمت هر گیگ + مدت × قیمت هر روز) پس از تخفیف.\nبرای فعال‌شدن خرید پویا، قیمت و محدوده‌ها را تنظیم کنید.",
            rows,
        )
    elif action == "mode":
        if len(parts) > 3:
            service.set_sales(actor, sid, {"mode": parts[3]})
            await show("✅ حالت نمایش ذخیره شد.", [back(sid, "settings")])
        else:
            await show(
                "حالت نمایش پلن‌ها در ربات کاربران:",
                [
                    button("فقط پلن‌های ثابت", f"srv:mode:{sid}:fixed"),
                    button("فقط پلن پویا", f"srv:mode:{sid}:dynamic"),
                    button("ترکیبی (ثابت + پویا)", f"srv:mode:{sid}:mixed"),
                    back(sid, "settings"),
                ],
            )
    elif action == "salesfield":
        field = parts[3]
        if field not in {
            "min_gb",
            "max_gb",
            "step_gb",
            "min_days",
            "max_days",
            "step_days",
            "price_gb",
            "price_day",
            "currency",
            "discount_percent",
        }:
            raise ValueError("invalid pricing field")
        await prompt(
            update,
            context,
            dict(kind="sales_field", sid=sid, field=field),
            "مقدار جدید تنظیم پلن را وارد کنید:",
        )
    elif action == "discounts":
        sales = service.sales(sid)
        rows = []
        lines = [
            "🎛 مدیریت حرفه‌ای تخفیف‌ها",
            "بیشترین تخفیف قابل‌استفاده روی قیمت پلن پویا اعمال می‌شود.",
        ]
        for kind, title in (("simple", "حجمی ساده"), ("tiered", "پله‌ای")):
            active = service.discount_active(sales, kind)
            lines.append(f"🎁 تخفیف {title}: {'فعال ✅' if active else 'غیرفعال ❌'}")
            lines.append(
                "⏱ پایان: " + local_time(sales.get(f"discount_{kind}_until"))
                if sales.get(f"discount_{kind}_until")
                else "⏱ بدون محدودیت زمانی"
            )
            rows.extend(
                [
                    button(
                        ("خاموش کن" if active else "روشن کن") + " تخفیف " + title,
                        f'srv:discounttoggle:{sid}:{kind}:{"off" if active else "on"}',
                    ),
                    button(
                        "✏️ ویرایش تخفیف " + title, f"srv:discountedit:{sid}:{kind}"
                    ),
                    button(
                        "⏱ تنظیم تایمر تخفیف " + title,
                        f"srv:discountedit:{sid}:{kind}_timer",
                    ),
                ]
            )
        lines.append(
            f"حجمی ساده: هر {sales['discount_step_gb']} گیگ، {sales['discount_percent_step']}٪ تا سقف {sales['discount_percent_max']}٪"
        )
        lines.append(
            "پله‌ها: "
            + (
                " · ".join(
                    f"{t['gb']} گیگ: {t['percent']}٪" for t in sales["discount_tiers"]
                )
                or "ثبت نشده"
            )
        )
        rows.extend(
            [
                button(
                    f"تخفیف عمومی: {sales['discount_percent']}٪",
                    f"srv:salesfield:{sid}:discount_percent",
                ),
                back(sid, "plans"),
            ]
        )
        await show("\n".join(lines), rows)
    elif action == "discounttoggle":
        kind, state = parts[3], parts[4]
        if kind not in {"simple", "tiered"} or state not in {"on", "off"}:
            raise ValueError("invalid discount toggle")
        changes = {f"discount_{kind}_enabled": state == "on"}
        # An explicit enable starts a new untimed offer if the old timer expired.
        sales = service.sales(sid)
        until = sales.get(f"discount_{kind}_until")
        if state == "on" and until and parse_utc(until) <= utcnow():
            changes[f"discount_{kind}_until"] = None
        service.set_sales(actor, sid, changes)
        await show("✅ وضعیت تخفیف ذخیره شد.", [back(sid, "discounts")])
    elif action == "discountedit":
        kind = parts[3]
        prompts = {
            "simple": "گام حجم، درصد هر گام و سقف تخفیف را با فاصله بفرستید.\nمثال: 50 5 30",
            "tiered": "پله‌ها را به شکل «حجم:درصد» با فاصله بفرستید.\nمثال: 50:5 100:10 200:20\nبرای پاک‌کردن «-» بفرستید.",
            "simple_timer": "زمان تخفیف حجمی ساده را به دقیقه وارد کنید؛ 0 یعنی بدون محدودیت.",
            "tiered_timer": "زمان تخفیف پله‌ای را به دقیقه وارد کنید؛ 0 یعنی بدون محدودیت.",
        }
        if kind not in prompts:
            raise ValueError("invalid discount setting")
        await prompt(
            update,
            context,
            dict(kind="discount_field", sid=sid, field=kind),
            prompts[kind],
        )
    elif action == "domains":
        rows = service.domains(actor, sid)
        text = "🔗 مدیریت دامنه‌ها\n❖ • -------------------------- • ❖\nدامنه‌هایی که اینجا ثبت می‌کنی برای لینک‌های اشتراک و کانفیگ‌ها استفاده می‌شوند.\n\n"
        text += (
            "\n".join(
                f"{'⭐' if d['is_primary'] else '🌐'} {d['title']} · {d['origin']}"
                for d in rows
            )
            or "در حال حاضر هیچ دامنه‌ای ثبت نشده است؛ لینک‌ها از تنظیمات اصلی سرور استفاده می‌کنند."
        )
        await show(
            text,
            [button(d["title"], f'srv:domain:{sid}:{d["id"]}') for d in rows]
            + [button("➕ افزودن دامنه", f"srv:domainadd:{sid}"), back(sid)],
        )
    elif action == "domain":
        did = int(parts[3])
        d = service.domain(actor, sid, did)
        await show(
            f"🔗 {d['title']}\n🌐 {d['origin']}\nوضعیت: {'دامنه پیش‌فرض لینک اشتراک' if d['is_primary'] else 'جایگزین'}",
            [
                button("✏️ ویرایش دامنه", f"srv:domainedit:{sid}:{did}"),
                button(
                    "⭐ استفاده برای لینک‌های اشتراک", f"srv:domainselect:{sid}:{did}"
                ),
                button("🗑 حذف دامنه", f"srv:domaindelete:{sid}:{did}"),
                back(sid, "domains"),
            ],
        )
    elif action in {"domainadd", "domainedit"}:
        did = int(parts[3]) if len(parts) > 3 else None
        if did:
            service.domain(actor, sid, did)
        await prompt(
            update,
            context,
            dict(kind="domain_title", sid=sid, did=did),
            "عنوان دامنه را وارد کنید:",
        )
    elif action == "domainselect":
        service.select_domain(actor, sid, int(parts[3]))
        await show("✅ دامنه پیش‌فرض لینک‌های اشتراک ذخیره شد.", [back(sid, "domains")])
    elif action in {"domaindelete", "domaindeleteok"}:
        did = int(parts[3])
        service.domain(actor, sid, did)
        if action == "domaindelete":
            context.user_data["server_action_confirmation"] = f"domain:{sid}:{did}"
            await show(
                "❓ دامنه از فهرست حذف شود؟",
                [
                    button("✅ حذف دامنه", f"srv:domaindeleteok:{sid}:{did}"),
                    back(sid, "domains"),
                ],
            )
        else:
            if (
                context.user_data.pop("server_action_confirmation", None)
                != f"domain:{sid}:{did}"
            ):
                raise TenantBusinessError("confirmation expired")
            service.delete_domain(actor, sid, did)
            await show("✅ دامنه حذف شد.", [back(sid, "domains")])
    elif action == "nodes":
        nodes = [
            n
            for n in business.list_nodes(parent_server_id=sid)
            if n.get("server_id") != sid
        ]
        await show(
            "⚙️ مدیریت نودها\n⬇️ لیست نود های شما\n\n"
            + (
                "\n".join("• " + n["label"] for n in nodes)
                or "در حال حاضر هیچ نودی ثبت نشده است."
            ),
            [button(n["label"], f'srv:node:{sid}:{n["id"]}') for n in nodes]
            + [button("➕ افزودن", f"srv:nodeadd:{sid}"), back(sid)],
        )
    elif action == "node":
        nid = int(parts[3])
        n = service.node(actor, sid, nid)
        target = int(n["server_id"])
        await show(
            f"✏️ ویرایش نود\n🖥 سرور: {n['label']}\nموقعیت: {n.get('location') or '—'}\nوضعیت: {n['status']}",
            [
                button("👤لیست کاربران", f"srv:users:{target}"),
                button("🛡️عملیات کاربری", f"srv:userops:{target}"),
                button("✏️ ویرایش عنوان نود", f"srv:nodeedit:{sid}:{nid}:label"),
                button("🌍 ویرایش موقعیت", f"srv:nodeedit:{sid}:{nid}:location"),
                button(
                    "🟢 فعال" if n["status"] == "active" else "⚫ غیرفعال",
                    f"srv:nodestatus:{sid}:{nid}",
                ),
                button("🔌 ویرایش اتصال پنل", f"srv:edit:{target}"),
                button("🗑 حذف نود", f"srv:nodedel:{sid}:{nid}"),
                back(sid, "nodes"),
            ],
        )
    elif action == "nodeadd":
        candidates = [
            s
            for s in business.list_servers()
            if s["id"] != sid and s["status"] == "active"
        ]
        await show(
            (
                "➕ سرور نود را انتخاب کنید."
                if candidates
                else "ابتدا یک سرور دیگر اضافه کنید."
            ),
            [button(s["label"], f'srv:nodepick:{sid}:{s["id"]}') for s in candidates]
            + [button("➕ افزودن سرور جدید", "srv:add"), back(sid, "nodes")],
        )
    elif action == "nodepick":
        service.attach_node(actor, sid, int(parts[3]))
        await show(
            "✅ نود اضافه شد. برای ساخت کاربران موجود، از «همگام سازی نودها» استفاده کنید.",
            [back(sid, "nodes"), back(sid, "sync")],
        )
    elif action == "nodeedit":
        nid = int(parts[3])
        field = parts[4]
        service.node(actor, sid, nid)
        if field not in {"label", "location"}:
            raise ValueError("invalid node field")
        await prompt(
            update,
            context,
            dict(kind="node_field", sid=sid, nid=nid, field=field),
            "مقدار جدید نود را وارد کنید:",
        )
    elif action == "nodestatus":
        nid = int(parts[3])
        n = service.node(actor, sid, nid)
        # Disabling an attachment also disables its own replicas through status sync.
        desired = "disabled" if n["status"] == "active" else "active"
        await service.set_node_enabled(actor, sid, nid, desired == "active")
        await show(
            "✅ وضعیت اتصال نود ذخیره شد.",
            [button("بازگشت🔙", f"srv:node:{sid}:{nid}")],
        )
    elif action in {"nodedel", "nodedelok"}:
        nid = int(parts[3])
        service.node(actor, sid, nid)
        if action == "nodedel":
            context.user_data["server_action_confirmation"] = f"node:{sid}:{nid}"
            await show(
                "❓ نود حذف شود؟ کاربران وابسته به همین سرور اصلی از پنل نود حذف می‌شوند؛ کاربران سرور اصلی و کاربران مستقل نود حفظ می‌شوند.",
                [
                    button("✅ حذف نود", f"srv:nodedelok:{sid}:{nid}"),
                    back(sid, "nodes"),
                ],
            )
        else:
            if (
                context.user_data.pop("server_action_confirmation", None)
                != f"node:{sid}:{nid}"
            ):
                raise TenantBusinessError("confirmation expired")
            await service.remove_node(actor, sid, nid)
            await show("✅ نود حذف شد.", [back(sid, "nodes")])
    elif action == "sync":
        modes = {
            "report": "📊 فقط بررسی و گزارش",
            "missing": "🧩 ساخت کاربران جاافتاده",
            "details": "🔁 همسان‌سازی مشخصات موجودها",
            "status": "🔒 همسان‌سازی وضعیت فعال/غیرفعال",
            "full": "✅ اجرای کامل امن",
            "extra": "👁 نمایش کاربران اضافی",
            "migrate": "🔄 ثبت سرویس کاربران قدیمی ادمین",
        }
        await show(
            "🔄 همگام‌سازی نودها\n\nاز این بخش می‌توانید بعد از اضافه کردن نود جدید، کاربران موجود سرور اصلی را روی نودها بسازید و مشخصات حجم/زمان را همسان کنید.\n\n🔐 اجرای کامل و همسان‌سازی مشخصات موجودها به نام، مصرف فعلی و وضعیت فعال/غیرفعال دست نمی‌زند.\nبرای تغییر وضعیت کاربران موجود، فقط از دکمه «🔒 همسان‌سازی وضعیت فعال/غیرفعال» استفاده کنید.\n\nکاربران اضافه روی نودها حذف نمی‌شوند و فقط گزارش داده می‌شوند.",
            [button(v, f"srv:syncrun:{sid}:{k}") for k, v in modes.items()]
            + [back(sid)],
        )
    elif action == "syncrun":
        report = await service.sync_nodes(actor, sid, parts[3])
        text = f"🔄 گزارش همگام‌سازی\n👥 کاربران اصلی: {report['source']}\n⚙️ نودها: {report['nodes']}\n🧩 جاافتاده: {report['missing']}\n✅ موجود: {report['existing']}\n➕ ساخته‌شده: {report['created']}\n🔁 همسان‌شده: {report['updated']}\n❌ خطا: {report['errors']}\n👁 کاربران اضافی: {len(report['extras'])}"
        if parts[3] == "migrate":
            text = f"✅ کاربران قدیمی در مدیریت سرور ثبت شدند.\n👤 کاربران اصلی: {report['registered']}\n🔗 ارتباط نودهای موجود: {report['existing']}\n❌ خطا: {report['errors']}"
        rows = [
            button(
                f"{x['name']} · {business.server(x['server_id'])['label']}"[:60],
                f'srv:puser:{x["server_id"]}:{x["id"]}',
            )
            for x in report["extras"]
        ] + [back(sid, "sync")]
        await show(text, rows)
    elif action == "frozen":
        try:
            await service.refresh_users(actor, sid)
        except TenantBusinessError:
            pass
        frozen = service.frozen(actor, sid)
        rows = [
            button(
                f"{x['name']} · {x['server_label']}"[:60],
                f'srv:frozenuser:{sid}:{x["user_id"]}',
            )
            for x in frozen
        ] + [back(sid)]
        await show(
            "❄️ مدیریت کاربران یخ‌زده این سرور\n\n"
            + (
                "\n".join(
                    f"• {x['name']} · {x['server_label']} · تلاش ناموفق: {x['fail_count']}"
                    for x in frozen
                )
                or "✅ رکورد یخ‌زده‌ای برای این سرور وجود ندارد."
            ),
            rows,
        )
    elif action == "frozenuser":
        uid = int(parts[3])
        service.user(actor, sid, uid)
        rows = [x for x in service.frozen(actor, sid) if x["user_id"] == uid]
        await show(
            "❄️ جزئیات رکورد یخ‌زده\n"
            + "\n".join(
                f"{x['server_label']} · {x.get('last_error') or 'خطای اتصال'} · {local_time(x.get('frozen_at'))}"
                for x in rows
            ),
            [
                button("🔄 ترمیم نودهای کاربر", f"srv:frozenrepair:{sid}:{uid}"),
                button("🧹 پاک‌کردن فقط داده یخ‌زدگی", f"srv:frozenclear:{sid}:{uid}"),
                button("👤 جزئیات کاربر", f"srv:puser:{sid}:{uid}"),
                button("🗑 حذف کامل", f"srv:pdelete:{sid}:{uid}"),
                back(sid, "frozen"),
            ],
        )
    elif action == "frozenrepair":
        uid = int(parts[3])
        service.user(actor, sid, uid)
        report = await service.sync_nodes(actor, sid, "full", only_user=uid)
        await show(
            f"🔄 ترمیم انجام شد. ساخته‌شده: {report['created']} · خطا: {report['errors']}",
            [back(sid, "frozen")],
        )
    elif action == "frozenclear":
        service.clear_frozen(actor, sid, int(parts[3]))
        await show(
            "✅ داده یخ‌زدگی پاک شد. وضعیت فعال/غیرفعال کاربر تغییری نکرد.",
            [back(sid, "frozen")],
        )
    elif action == "delete":
        context.user_data["server_action_confirmation"] = f"server:{sid}"
        await show(
            f"❓ سرور «{business.server(sid)['label']}» حذف شود؟\nسرور دارای کاربران پنل، اشتراک مرتبط یا سفارش باز قابل حذف نیست.",
            [button("✅ حذف سرور", f"srv:deleteok:{sid}"), back(sid)],
        )
    elif action == "deleteok":
        if context.user_data.pop("server_action_confirmation", None) != f"server:{sid}":
            raise TenantBusinessError("confirmation expired")
        await service.refresh_users(actor, sid)
        deleted = business.delete_server(actor, server_id=sid)
        from TenantRuntime.AdminBot.handlers import _server_list_view

        text, kb = _server_list_view(business)
        await show(
            f"✅ سرور «{deleted['label']}» حذف شد.\n\n{text}", kb.inline_keyboard
        )
    return True


async def handle_text(update, context, *, business, actor):
    flow = context.user_data.get(FLOW)
    if not isinstance(flow, dict):
        return False
    from TenantRuntime.AdminBot.handlers import (
        ADMIN_MAIN_BUTTONS,
        admin_main_keyboard,
        cancel_keyboard,
        confirm_add_user_keyboard,
    )

    text = str(update.effective_message.text or "").strip()
    if text in ADMIN_MAIN_BUTTONS:
        context.user_data.pop(FLOW, None)
        return False
    if text in {"❌ لغو", "لغو", "/cancel"}:
        context.user_data.pop(FLOW, None)
        await update.effective_message.reply_text(
            "❌ عملیات لغو شد.", reply_markup=admin_main_keyboard()
        )
        await update.effective_message.reply_text(
            "↩️ مدیریت سرور", reply_markup=markup([back(int(flow["sid"]))])
        )
        return True
    service = ServerAdminService(business)
    sid = int(flow["sid"])
    service.authorize(actor, sid)
    kind = flow["kind"]
    next_prompt = None
    result = "✅ ذخیره شد."
    section = "view"

    async def finish_create():
        return await service.create_users(
            actor,
            sid,
            name=flow["name"],
            gb=flow["gb"],
            days=flow["days"],
            count=flow["count"],
            operation_key=flow["operation_key"],
        )

    try:
        if kind == "search":
            await service.refresh_users(actor, sid)
            # Resolve UUIDs embedded inside subscription/config URLs, too.
            matches = service.users(actor, sid, query=text)
            if not matches:
                matches = [
                    r
                    for r in service.users(actor, sid)
                    if r["external_ref"].casefold() in text.casefold()
                ]
            context.user_data.pop(FLOW, None)
            await update.effective_message.reply_text(
                "🔍 جستجو انجام شد.", reply_markup=admin_main_keyboard()
            )
            result = await update.effective_message.reply_text("🔍 نتایج جستجو")
            # A message supports edit_text rather than query.edit_message_text.
            if result is None:
                await update.effective_message.reply_text(
                    f"✅ {len(matches)} نتیجه پیدا شد.",
                    reply_markup=markup(
                        [
                            button(r["name"], f'srv:puser:{sid}:{r["id"]}')
                            for r in matches
                        ]
                        + [back(sid, "userops")]
                    ),
                )
            else:

                class MessageQuery:
                    async def edit_message_text(self, *args, **kwargs):
                        return await result.edit_text(*args, **kwargs)

                await paged_edit(
                    MessageQuery(),
                    context,
                    sid,
                    (
                        f"✅ {len(matches)} نتیجه پیدا شد."
                        if matches
                        else "❌ کاربری پیدا نشد."
                    ),
                    [button(r["name"], f'srv:puser:{sid}:{r["id"]}') for r in matches]
                    + [back(sid, "userops")],
                )
            return True
        if kind == "create_count":
            count = int(text)
            if not 1 <= count <= 100:
                raise ValueError("count outside bounds")
            flow.update(count=count, kind="create_name")
            next_prompt = "پیشوند نام کاربران را وارد کنید:"
        elif kind == "create_name":
            if not 1 <= len(text) <= 64:
                raise ValueError("name outside bounds")
            flow["name"] = text
            if "gb" in flow and "days" in flow:
                flow["kind"] = "create_confirm"
                await update.effective_message.reply_text(
                    _creation_summary(flow),
                    reply_markup=confirm_add_user_keyboard(),
                )
                return True
            flow["kind"] = "create_gb"
            next_prompt = "حجم اشتراک را به گیگابایت وارد کنید:"
        elif kind == "create_gb":
            gb = float(text)
            if not 0 < gb <= 1000000:
                raise ValueError("quota outside bounds")
            flow.update(gb=gb, kind="create_days")
            next_prompt = "مدت اشتراک را به روز وارد کنید:"
        elif kind == "create_days":
            days = int(text)
            if not 1 <= days <= 36500:
                raise ValueError("days outside bounds")
            flow.update(days=days, kind="create_confirm")
            await update.effective_message.reply_text(
                _creation_summary(flow),
                reply_markup=confirm_add_user_keyboard(),
            )
            return True
        elif kind == "create_confirm":
            if text not in {"✅ تایید", "✅تایید", "تایید", "تأیید"}:
                await update.effective_message.reply_text(
                    "لطفاً با دکمه‌های «✅ تایید» یا «❌ لغو» پاسخ دهید.",
                    reply_markup=confirm_add_user_keyboard(),
                )
                return True

            report = await finish_create()
            users = list(report.get("users") or [])
            errors = int(report.get("errors") or 0)
            details = list(report.get("error_details") or [])
            success_nodes = list(report.get("node_success_labels") or [])
            failed_nodes = list(report.get("node_failed_labels") or [])
            context.user_data.pop(FLOW, None)

            if int(flow.get("count") or 1) == 1 and len(users) == 1:
                await update.effective_message.reply_text(
                    "✅ کاربر جدید با موفقیت ساخته شد.\n"
                    f"👤 نام: {flow['name']}\n"
                    f"📊 حجم: {_format_gb(flow['gb'])} گیگابایت\n"
                    f"📅 مدت: {int(flow['days'])} روز",
                    reply_markup=admin_main_keyboard(),
                )
            else:
                lines = [
                    "📦 نتیجه افزودن چندین کاربر",
                    f"✅ موفق: {len(users)}",
                    f"❌ ناموفق: {errors}",
                ]
                if users:
                    lines.extend(["", "کاربران ساخته‌شده:"])
                    lines.extend(f"• {u['name']}" for u in users[:12])
                    if len(users) > 12:
                        lines.append(f"... و {len(users)-12} کاربر دیگر")
                if details:
                    lines.extend(["", "خطاها:"])
                    lines.extend(f"• {item}" for item in details[:8])
                await update.effective_message.reply_text(
                    "\n".join(lines),
                    reply_markup=admin_main_keyboard(),
                )

            if success_nodes:
                await update.effective_message.reply_text(
                    "✅ ساخته شد روی نودها: " + "، ".join(success_nodes)
                )
            if failed_nodes:
                await update.effective_message.reply_text(
                    "⚠️ ساخت روی این نودها کامل نشد: " + "، ".join(failed_nodes)
                )

            if not users:
                await update.effective_message.reply_text(
                    "❌ خطا در ایجاد کاربر روی سرور"
                    + (
                        "\n" + "\n".join(f"• {item}" for item in details[:5])
                        if details
                        else ""
                    ),
                    reply_markup=admin_main_keyboard(),
                )
                return True

            detail_limit = 20
            for index, user in enumerate(users):
                if index >= detail_limit:
                    break
                await _send_created_user_detail(
                    update.effective_message,
                    service,
                    actor,
                    sid,
                    int(user["id"]),
                )
                if int(flow.get("count") or 1) > 1:
                    native = _panel_native_link(
                        business,
                        sid,
                        str(user.get("external_ref") or ""),
                    )
                    bot = getattr(context, "bot", None)
                    if native and bot is not None:
                        chat_id = getattr(update.effective_chat, "id", None) or actor
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=_qr_image(native),
                            caption=(
                                f"🔗 لینک اشتراک {user.get('name')}:\n{native}"
                            ),
                        )
            if len(users) > detail_limit:
                await update.effective_message.reply_text(
                    f"ℹ️ جزئیات فقط برای {detail_limit} کاربر اول ارسال شد."
                )
            return True
        elif kind == "user_field":
            uid = int(flow["uid"])
            field = flow["field"]
            if field == "volume":
                gb = float(text)
                if not 0 <= gb <= 1000000:
                    raise ValueError("quota outside bounds")
                changes = {"traffic_bytes": int(gb * 1024**3)}
            elif field == "days":
                days = int(text)
                if not 1 <= days <= 36500:
                    raise ValueError("days outside bounds")
                changes = {
                    "expires_at": iso_utc(utcnow() + timedelta(days=days)),
                    "reset_days": True,
                }
            else:
                changes = {field: "" if text == "-" and field == "comment" else text}
            await service.edit_user(actor, sid, uid, changes)
            section = f"pedit:{uid}"
        elif kind == "renew_gb":
            gb = int(text)
            if not 0 < gb <= 1000000:
                raise ValueError("quota outside bounds")
            flow.update(gb=gb, kind="renew_days")
            next_prompt = "مدت تمدید را به روز وارد کنید:"
        elif kind == "renew_days":
            days = int(text)
            if not 1 <= days <= 36500:
                raise ValueError("days outside bounds")
            user = service.user(actor, sid, int(flow["uid"]))
            await service.renew_user(
                actor,
                sid,
                user["id"],
                gb=int(flow["gb"]),
                days=days,
                operation_key=f'server-renew:{sid}:{flow["uid"]}:{flow["operation_key"]}',
            )
            section = f'puser:{user["id"]}'
            result = "✅ اشتراک تمدید شد."
        elif kind == "plan_name":
            if not 1 <= len(text) <= 80:
                raise ValueError("invalid plan name")
            flow.update(name=text, kind="plan_gb")
            next_prompt = "حجم پلن را به گیگابایت وارد کنید:"
        elif kind == "plan_gb":
            value = int(text)
            if value <= 0:
                raise ValueError("invalid quota")
            flow.update(gb=value, kind="plan_days")
            next_prompt = "مدت پلن را به روز وارد کنید:"
        elif kind == "plan_days":
            value = int(text)
            if value <= 0:
                raise ValueError("invalid days")
            flow.update(days=value, kind="plan_price")
            next_prompt = "قیمت پلن را وارد کنید:"
        elif kind == "plan_price":
            value = int(text.replace(",", ""))
            if value < 0:
                raise ValueError("invalid price")
            flow.update(price=value, kind="plan_currency")
            next_prompt = "واحد پول را وارد کنید (IRR / IRT / USD / USDT / EUR):"
        elif kind == "plan_currency":
            currency = text.upper()
            if currency not in {"IRR", "IRT", "USD", "USDT", "EUR"}:
                raise ValueError("invalid currency")
            p = business.add_plan(
                actor,
                name=flow["name"],
                traffic_gb=flow["gb"],
                duration_days=flow["days"],
                price=flow["price"],
                currency=currency,
                server_id=sid,
            )
            section = f'plan:{p["id"]}'
        elif kind == "plan_field":
            field = flow["field"]
            value = (
                int(text.replace(",", ""))
                if field in {"price", "priority", "traffic_gb", "duration_days"}
                else text.upper() if field == "currency" else text
            )
            if field == "currency" and value not in {
                "IRR",
                "IRT",
                "USD",
                "USDT",
                "EUR",
            }:
                raise ValueError("invalid currency")
            service.edit_plan(actor, sid, flow["pid"], {field: value})
            section = f'plan:{flow["pid"]}'
        elif kind == "category_add":
            business.add_plan_category_admin(actor, title=text)
            section = "categories"
        elif kind == "category_field":
            field = flow["field"]
            value = int(text) if field == "priority" else text
            business.update_plan_category_admin(
                actor, category_id=flow["cid"], **{field: value}
            )
            section = f'category:{flow["cid"]}'
        elif kind == "sales_field":
            key = flow["field"]
            value = text.upper() if key == "currency" else int(text.replace(",", ""))
            service.set_sales(actor, sid, {key: value})
            section = "settings"
        elif kind == "discount_field":
            field = flow["field"]
            if field == "simple":
                step, percent, cap = map(int, text.split())
                changes = {
                    "discount_step_gb": step,
                    "discount_percent_step": percent,
                    "discount_percent_max": cap,
                }
            elif field == "tiered":
                tiers = []
                if text != "-":
                    for part in text.split():
                        gb, percent = map(int, part.split(":"))
                        tiers.append(dict(gb=gb, percent=percent))
                changes = {"discount_tiers": tiers}
            else:
                minutes = int(text)
                if not 0 <= minutes <= 525600:
                    raise ValueError("invalid discount timer")
                name = field.removesuffix("_timer")
                changes = {
                    f"discount_{name}_until": (
                        iso_utc(utcnow() + timedelta(minutes=minutes))
                        if minutes
                        else None
                    )
                }
            service.set_sales(actor, sid, changes)
            section = "discounts"
        elif kind == "domain_title":
            if not 1 <= len(text) <= 80:
                raise ValueError("invalid title")
            flow.update(title=text, kind="domain_origin")
            next_prompt = "آدرس دامنه عمومی را با http/https وارد کنید:"
        elif kind == "domain_origin":
            service.save_domain(
                actor, sid, title=flow["title"], origin=text, did=flow.get("did")
            )
            section = "domains"
        elif kind == "node_field":
            service.node(actor, sid, flow["nid"])
            field = flow["field"]
            if not 1 <= len(text) <= 80:
                raise ValueError("invalid node value")
            business.conn.execute(
                f"UPDATE tenant_nodes SET {field}=?,updated_at=? WHERE tenant_id=? AND id=?",
                (text, iso_utc(utcnow()), business.tenant_id, int(flow["nid"])),
            )
            business.conn.commit()
            section = f'node:{flow["nid"]}'
        else:
            return False
        if next_prompt:
            await update.effective_message.reply_text(
                next_prompt, reply_markup=cancel_keyboard()
            )
            return True
        context.user_data.pop(FLOW, None)
        await update.effective_message.reply_text(
            result, reply_markup=admin_main_keyboard()
        )
        parts = section.split(":")
        target = f"srv:{parts[0]}:{sid}" + (":" + parts[1] if len(parts) > 1 else "")
        await update.effective_message.reply_text(
            "↩️ ادامه مدیریت", reply_markup=markup([button("بازگشت🔙", target)])
        )
    except (ValueError, TypeError, TenantBusinessError):
        await update.effective_message.reply_text(
            "❌ عملیات کامل نشد یا مقدار معتبر نیست. دوباره تلاش کنید؛ برای بازگشت، لغو را بزنید.",
            reply_markup=cancel_keyboard(),
        )
    return True
