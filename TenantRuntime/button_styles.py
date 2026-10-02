from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from telegram import (
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    KeyboardButton as TelegramKeyboardButton,
)

VALID_BUTTON_STYLES = {"primary", "success", "danger"}

_SETTINGS: ContextVar[dict[str, Any]] = ContextVar(
    "tenant_button_style_settings",
    default={"colored_buttons": True, "button_theme": "smart"},
)

_DANGER_TOKENS = (
    "❌", "🗑", "🚫", "لغو", "حذف", "بستن", "غیرفعال",
    "disable", "delete", "remove", "reject", "cancel", "close",
)
_SUCCESS_TOKENS = (
    "✅", "➕", "💳", "💰", "🎁", "🔥", "تایید", "تأیید",
    "پرداخت", "خرید", "تمدید", "افزودن", "ارسال", "ساخت", "فعال",
    "approve", "confirm", "pay", "buy", "renew", "add", "send", "enable",
)
_STRONG_SUCCESS_TOKENS = (
    "✅", "تایید", "تأیید", "پرداخت کردم", "تایید و پرداخت",
    "ارسال", "افزودن", "approve", "confirm", "send", "add",
)
_SHOP_TOKENS = (
    "💳", "💰", "🎁", "🔥", "🏷", "خرید", "تمدید", "پرداخت",
    "کیف پول", "شارژ", "کارت", "کوپن", "هدیه", "پلن", "بسته",
    "قیمت", "wallet", "coupon", "gift", "plan", "price",
)
_PRIMARY_TOKENS = (
    "🔙", "↩️", "➡️", "⬅️", "◀️", "▶️", "📊", "📈", "📋",
    "📁", "⚙️", "🌐", "🔗", "🔄", "🖥", "👤", "📧", "📢",
    "بازگشت", "وضعیت", "لیست", "تنظیم", "راهنما", "جستجو",
    "مدیریت", "گزارش", "noop", "back", "status", "list",
    "settings", "menu", "guide", "search",
)


def set_button_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    clean = dict(settings or {})
    _SETTINGS.set(clean)
    return clean


def current_button_settings() -> dict[str, Any]:
    return dict(_SETTINGS.get())


def normalize_button_theme(value: Any) -> str:
    theme = str(value or "smart").strip().lower()
    return theme if theme in {"smart", "shop", "pro", "minimal"} else "smart"


def _contains_any(haystack: str, tokens: tuple[str, ...]) -> bool:
    return any(token in haystack for token in tokens)


def infer_button_style(
    text: Any,
    callback_data: Any = None,
    *,
    theme: str | None = None,
) -> str | None:
    settings = current_button_settings()
    selected = normalize_button_theme(theme or settings.get("button_theme"))
    haystack = f"{str(text or '')} {str(callback_data or '')}".lower()

    if _contains_any(haystack, _DANGER_TOKENS):
        return "danger"
    if selected == "minimal":
        return (
            "success"
            if _contains_any(haystack, _STRONG_SUCCESS_TOKENS)
            else None
        )
    if selected == "pro":
        if _contains_any(haystack, _STRONG_SUCCESS_TOKENS):
            return "success"
        return "primary" if _contains_any(haystack, _PRIMARY_TOKENS) else None
    if selected == "shop":
        if (
            _contains_any(haystack, _SUCCESS_TOKENS)
            or _contains_any(haystack, _SHOP_TOKENS)
        ):
            return "success"
        return "primary"
    if _contains_any(haystack, _SUCCESS_TOKENS):
        return "success"
    return "primary" if _contains_any(haystack, _PRIMARY_TOKENS) else "primary"


def _selected_style(
    text: Any,
    callback_data: Any,
    explicit_style: str | None,
    settings: dict[str, Any] | None,
) -> str | None:
    current = dict(settings) if settings is not None else current_button_settings()
    if not bool(current.get("colored_buttons", True)):
        return None
    style = str(explicit_style or "").strip().lower() or infer_button_style(
        text,
        callback_data,
        theme=str(current.get("button_theme") or "smart"),
    )
    return style if style in VALID_BUTTON_STYLES else None


def inline_button(
    text: str,
    *args: Any,
    settings: dict[str, Any] | None = None,
    style: str | None = None,
    **kwargs: Any,
) -> TelegramInlineKeyboardButton:
    selected = _selected_style(
        text,
        kwargs.get("callback_data"),
        style,
        settings,
    )
    api_kwargs = dict(kwargs.pop("api_kwargs", None) or {})
    if selected:
        try:
            return TelegramInlineKeyboardButton(
                text,
                *args,
                style=selected,
                api_kwargs=api_kwargs or None,
                **kwargs,
            )
        except TypeError:
            api_kwargs.setdefault("style", selected)
    if api_kwargs:
        kwargs["api_kwargs"] = api_kwargs
    return TelegramInlineKeyboardButton(text, *args, **kwargs)


def keyboard_button(
    text: str,
    *args: Any,
    settings: dict[str, Any] | None = None,
    style: str | None = None,
    **kwargs: Any,
) -> TelegramKeyboardButton:
    selected = _selected_style(text, None, style, settings)
    api_kwargs = dict(kwargs.pop("api_kwargs", None) or {})
    if selected:
        try:
            return TelegramKeyboardButton(
                text,
                *args,
                style=selected,
                api_kwargs=api_kwargs or None,
                **kwargs,
            )
        except TypeError:
            api_kwargs.setdefault("style", selected)
    if api_kwargs:
        kwargs["api_kwargs"] = api_kwargs
    return TelegramKeyboardButton(text, *args, **kwargs)
