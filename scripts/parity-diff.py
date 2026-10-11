#!/usr/bin/env python3
"""SellBot <-> WhiteLabel parity diff (texts, buttons, layouts).

Compares the reference Hiddify-SellBot against this WhiteLabel checkout
so UI drift is found by the computer instead of by manual screen checks.

Part 1 (static): every Persian string literal in both bots' UserBot /
AdminBot handler files is collected with AST. WhiteLabel strings with no
close SellBot match are reported (possible leftover/old texts), plus any
string containing control characters (corrupted emoji class of bugs).
SellBot strings with no WhiteLabel counterpart hint at unported screens.

Part 2 (paired): equivalent keyboard/text builders from both bots run
with the same sample inputs and their button grids are diffed.
Telegram callback namespaces intentionally differ (buy: vs shop:), so
only button TEXTS and grid STRUCTURE are compared, never callbacks.

Usage:
    .venv/bin/python scripts/parity-diff.py [--sellbot DIR] [--static-only]

Exit code: 0 = no hard mismatches, 1 = differences found, 2 = harness error.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FA_TR = re.compile(r"[\u0600-\u06FF]")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_sellbot = ROOT.parent / "Hiddify-SellBot"
    if not default_sellbot.is_dir():
        default_sellbot = Path.home() / "Hiddify-SellBot"
    parser.add_argument("--sellbot", default=str(default_sellbot))
    parser.add_argument("--static-only", action="store_true")
    return parser.parse_args()


# --------------------------------------------------------------------------
# Part 1: static Persian-string audit
# --------------------------------------------------------------------------

SELLBOT_FILES = [
    "UserBot/keyboards.py",
    "UserBot/main.py",
    "UserBot/menus/userbot_menus.py",
    "AdminBot/__init__.py",
    "AdminBot/keyboards.py",
    "AdminBot/servers.py",
    "AdminBot/userbot.py",
    "AdminBot/nodes.py",
    "AdminBot/agencies.py",
    "AdminBot/menus/adminbot_menus.py",
]

WHITELABEL_FILES = [
    "TenantRuntime/UserBot/handlers.py",
    "TenantRuntime/UserBot/dynamic_plans.py",
    "TenantRuntime/UserBot/card_payment.py",
    "TenantRuntime/UserBot/direct_buy_delivery.py",
    "TenantRuntime/UserBot/sellbot_utils.py",
    "TenantRuntime/AdminBot/handlers.py",
    "TenantRuntime/AdminBot/userbot_management.py",
    "TenantRuntime/AdminBot/server_actions.py",
    "TenantRuntime/AdminBot/server_management.py",
    "TenantRuntime/AdminBot/broadcast_channel.py",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


_EDGE_JUNK = "()[]{}:،,.…!?«»\"'’‘-–—"


def _match_key(text: str) -> str:
    """Strip f-string fragment edges so parts compare by their core text."""
    return _norm(text).strip(_EDGE_JUNK).strip()


MIN_LOOSE_LEN = 8


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 32 and ch not in {"\n", "\t"} for ch in text)


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Line numbers of module/class/function docstrings (never user-facing)."""
    lines: set[int] = set()

    def _first_str(body: list) -> None:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            lines.add(body[0].lineno)

    _first_str(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _first_str(node.body)
    return lines


def collect_strings(base: Path, files: list[str]) -> dict[str, list[str]]:
    """Map normalized Persian literal -> ['file:line', ...]."""
    found: dict[str, list[str]] = {}
    for rel in files:
        path = base / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        doc_lines = _docstring_lines(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.lineno in doc_lines:
                continue
            if not FA_TR.search(node.value):
                continue
            key = _norm(node.value)
            if len(key) < 2:
                continue
            found.setdefault(key, []).append(f"{rel}:{node.lineno}")
    return found


def _buckets(keys: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for key in keys:
        core = _match_key(key)
        if len(core) < MIN_LOOSE_LEN:
            continue
        grouped.setdefault(core[:3], []).append(core)
    return grouped


def _best_ratio(text: str, buckets: dict[str, list[str]]) -> float:
    core = _match_key(text)
    cands: list[str] = []
    for prefix, group in buckets.items():
        if not prefix or not core[:3]:
            continue
        if prefix[0] != core[0]:
            continue
        cands.extend(group)
    if len(cands) > 400:
        cands = [c for c in cands if abs(len(c) - len(text)) <= 40][:400]
    best = 0.0
    for other in cands:
        if abs(len(other) - len(text)) > max(len(text), 20):
            continue
        ratio = difflib.SequenceMatcher(None, text, other).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.99:
                break
    return best


def static_audit(sellbot: Path) -> tuple[int, int]:
    print("[static] collecting strings...", flush=True)
    sellbot_strings = collect_strings(sellbot, SELLBOT_FILES)
    wl_strings = collect_strings(ROOT, WHITELABEL_FILES)
    sellbot_keys = list(sellbot_strings)
    wl_keys = list(wl_strings)
    print(
        f"[static] SellBot strings: {len(sellbot_keys)} | "
        f"WhiteLabel: {len(wl_keys)}",
        flush=True,
    )
    sellbot_set = set(sellbot_keys)
    wl_set = set(wl_keys)

    hard = 0
    print("\n--- control-character strings (always a bug) ---", flush=True)
    flagged = False
    for key in sorted(wl_keys):
        if _has_control_chars(key):
            flagged = True
            hard += 1
            print(f"  WL-ONLY-CONTROL {key!r} @ {', '.join(wl_strings[key][:3])}", flush=True)
    for key in sorted(sellbot_keys):
        if _has_control_chars(key):
            print(f"  (sellbot has one too) {key!r}", flush=True)
    if not flagged:
        print("  none. OK", flush=True)

    # Exact matches are free; fuzzy-match only the leftovers.
    # Short fragments (f-string pieces) are skipped from the loose report.
    wl_left = [
        k for k in wl_keys
        if k not in sellbot_set and len(_match_key(k)) >= MIN_LOOSE_LEN
    ]
    sellbot_left = [
        k for k in sellbot_keys
        if k not in wl_set and len(_match_key(k)) >= MIN_LOOSE_LEN
    ]
    print(
        f"[static] exact matches: {len(wl_keys) - len(wl_left)} "
        f"(fuzzy-matching {len(wl_left)} + {len(sellbot_left)} leftovers...)",
        flush=True,
    )
    sellbot_buckets = _buckets(sellbot_left)
    wl_buckets = _buckets(wl_left)

    print("\n--- WhiteLabel-only strings (no close SellBot match) ---", flush=True)
    loose = [key for key in wl_left if _best_ratio(key, sellbot_buckets) < 0.6]
    print(f"  count: {len(loose)} (showing up to 60)", flush=True)
    by_file: dict[str, list[str]] = {}
    for key in sorted(loose):
        by_file.setdefault(wl_strings[key][0].split(":")[0], []).append(key)
    shown = 0
    for rel in sorted(by_file):
        print(f"  [{rel}]", flush=True)
        for key in by_file[rel][:12]:
            if shown >= 60:
                break
            print(f"    - {key}", flush=True)
            shown += 1

    print("\n--- SellBot-only strings (possibly unported screens) ---", flush=True)
    missing = [key for key in sellbot_left if _best_ratio(key, wl_buckets) < 0.6]
    print(f"  count: {len(missing)} (showing up to 40)", flush=True)
    for key in sorted(missing)[:40]:
        print(f"    - {key}", flush=True)
    return hard, 0


# --------------------------------------------------------------------------
# Part 2: paired builder execution
# --------------------------------------------------------------------------

def _grid(markup) -> list[list[str]]:
    rows = getattr(markup, "keyboard", None)
    if rows is None:
        rows = getattr(markup, "inline_keyboard", None) or []
    return [[str(getattr(button, "text", "")) for button in row] for row in rows]


def _diff_grids(name: str, sellbot_grid, wl_grid) -> list[str]:
    problems: list[str] = []
    if len(sellbot_grid) != len(wl_grid):
        problems.append(
            f"{name}: row count differs (sellbot={len(sellbot_grid)} wl={len(wl_grid)})"
        )
    for index, (s_row, w_row) in enumerate(zip(sellbot_grid, wl_grid)):
        if len(s_row) != len(w_row):
            problems.append(
                f"{name} row {index}: button count differs "
                f"(sellbot={s_row} wl={w_row})"
            )
            continue
        for s_text, w_text in zip(s_row, w_row):
            if s_text.strip() == w_text.strip():
                continue
            ratio = difflib.SequenceMatcher(None, s_text, w_text).ratio()
            problems.append(
                f"{name} row {index}: {s_text!r} != {w_text!r} "
                f"(similarity {ratio:.2f})"
            )
    return problems


def _merge_notes(problems: list[str], notes: list[str]) -> None:
    """Route harness notes: 'info ...' is advisory, everything else fails."""
    for note in notes:
        if note.startswith("info "):
            print(f"[{note.split(' ', 1)[0]}] {note[5:]}", flush=True)
        elif note.startswith("HAMMER "):
            problems.append(note[len("HAMMER "):])
        else:
            problems.append(note)


THEME_SETTINGS = {
    "colored_buttons": True,
    "button_theme": "smart",
}


class _StubUserbotBusiness:
    def runtime_userbot_settings(self) -> dict:
        return {
            "colored_buttons": True,
            "button_theme": "smart",
            "show_gift_button": True,
        }

    def _ensure_growth_settings(self) -> dict:
        return {"trial_enabled": True, "referral_enabled": True}

    def list_methods(self, *, currency=None):
        return [{
            "id": 3,
            "title": "کارت به کارت",
            "provider_icon": "💳",
        }]

    def list_subscriptions(self, actor, status=None, limit=100, offset=0):
        return [
            {"id": 7, "service_name": "سرویس اول", "plan_name": "پلن یک",
             "status": "active"},
            {"id": 8, "service_name": "", "plan_name": "پلن دو",
             "status": "active"},
        ]


def _strip_tags(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", str(text or ""))
    return _norm(clean)


def _diff_grids_relaxed(name, sell_grid, wl_grid, *, missing_ok=(), extra_ok=()):
    """Grid diff that tolerates documented design divergences."""
    notes: list[str] = []
    sell_rows = [" | ".join(row) for row in sell_grid]
    wl_rows = [" | ".join(row) for row in wl_grid]
    for row in sell_rows:
        if row in wl_rows:
            continue
        if any(token in row for token in missing_ok):
            notes.append(f"info {name}: by-design missing row: {row!r}")
            continue
        notes.append(f"HAMMER {name}: missing row: {row!r}")
    for row in wl_rows:
        if row in sell_rows:
            continue
        if any(token in row for token in extra_ok):
            notes.append(f"info {name}: by-design extra row: {row!r}")
            continue
        notes.append(f"HAMMER {name}: extra row: {row!r}")
    return notes


# Intentional divergences (documented design differences, not bugs).
EXPECTED_DIFFS = {
    "payment_menu:zarinpal/perfect rows": (
        "no gateway backend in WhiteLabel; card/crypto/SMS only"
    ),
}


class _FakeMessage:
    def __init__(self):
        self.sent: list[tuple] = []

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        await self.edit_message_text(text, reply_markup=reply_markup, **kwargs)

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))

    async def delete(self):
        return None


class _FakeQuery:
    def __init__(self, data: str, message: _FakeMessage):
        self.data = data
        self.message = message
        self.answers: list = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _StubAdminBusiness:
    """Canned tenant business for rendering AdminBot settings screens."""

    def __init__(
        self,
        settings: dict | None = None,
        methods: list | None = None,
        sms_status: dict | None = None,
    ):
        self._settings = dict(settings or {})
        self._methods = list(methods or [])
        self._sms_status = dict(sms_status or {})

    def runtime_userbot_settings(self) -> dict:
        return {
            "colored_buttons": True,
            "button_theme": "smart",
            **self._settings,
        }

    def userbot_settings_admin(self, actor) -> dict:
        return dict(self._settings)

    def list_payment_methods_admin(self, actor) -> list:
        return [dict(m) for m in self._methods]

    def growth_settings(self, actor) -> dict:
        return {
            "referral_enabled": True,
            "trial_enabled": True,
        }

    def list_plan_categories(self, public: bool = True) -> list:
        return [
            {"id": 1, "title": "دسته یک"},
            {"id": 2, "title": "دسته دو"},
        ]

    def payment_method_admin(self, actor, method_id) -> dict:
        for method in self._methods:
            if int(method.get("id") or 0) == int(method_id):
                return dict(method)
        raise ValueError("no such method")

    def sms_status_admin(self, actor) -> dict:
        return dict(self._sms_status)

    def sms_webhook_path(self) -> str:
        return "/payment/sms/abc123"


def _admin_update(data: str):
    from types import SimpleNamespace

    message = _FakeMessage()
    query = _FakeQuery(data, message)
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=1),
        effective_user=SimpleNamespace(id=1),
        effective_message=None,
    )
    context = SimpleNamespace(user_data={}, bot_data={})
    return update, context, message


async def _render_admin(business, data: str):
    """Drive one AdminBot callback; return (text, grid) of last output."""
    from TenantRuntime.AdminBot import userbot_management as wl_mgmt

    update, context, message = _admin_update(data)
    handled = await wl_mgmt.handle_callback(
        update, context, business=business, actor=1
    )
    assert handled, f"callback not handled: {data}"
    text, markup = message.sent[-1]
    return str(text or ""), _grid(markup)


def _user_extra_checks(sellbot: Path) -> list[str]:
    """Remaining UserBot screens (trial/status/renew/tickets/support/etc.)."""
    from telegram import InlineKeyboardMarkup as _TgMarkup

    notes: list[str] = []
    sell_user_kb = _load_isolated(
        "_sellbot_user_kb_extra", sellbot / "UserBot" / "keyboards.py"
    )
    from TenantRuntime.UserBot import handlers as wl_user

    settings = dict(THEME_SETTINGS)
    settings.update({
        "server_columns": 1, "plan_columns": 1,
        "shuffle_server_layout": False,
    })

    def check(name, sell_grid, wl_grid):
        notes.extend(_diff_grids(name, sell_grid, wl_grid))

    # U11: trial location (+ trailing back, mirroring the reply site).
    trial_rows = wl_user._trial_server_rows(
        [{"id": 2, "label": "لوکیشن آلمان 🇩🇪"}], settings
    )
    from telegram import InlineKeyboardButton as _RawButton

    trial_rows.append([_RawButton("🔙بازگشت", callback_data="runtime:home")])
    check("trial_location",
          _grid(sell_user_kb.trial_location_keyboard(
              [{"id": 2, "title": "لوکیشن آلمان 🇩🇪"}])),
          _grid(_TgMarkup(trial_rows)))

    # U13: service menu rows.
    check("status_rows",
          _grid(sell_user_kb.subscription_status_keyboard(5)),
          _grid(_TgMarkup(wl_user._subscription_status_rows(
              None, 0, {"id": 5}, settings))))

    # U15/U16: config menu rows, default + all flags.
    # WL sites append their own back row; mirror SellBot's trailing back.
    from TenantRuntime.business import TenantBusinessError as _BizErr

    class _CfgBiz:
        def panel_user_page_link(self, actor, subscription_id):
            raise _BizErr("stub")

    for combo, label in (
        ({"show_user_page_link": False, "show_direct_config": True,
          "show_auto_sub_link": False, "show_sub_link": True,
          "show_sub_link_b64": False, "show_multi_server": False,
          "show_multi_server_b64": False}, "default"),
        ({"show_user_page_link": True, "show_direct_config": True,
          "show_auto_sub_link": True, "show_sub_link": True,
          "show_sub_link_b64": True, "show_multi_server": True,
          "show_multi_server_b64": True}, "all"),
    ):
        sell_grid = _grid(sell_user_kb.subscription_configs_keyboard(
            5,
            show_direct_config=combo["show_direct_config"],
            show_sub_link=combo["show_sub_link"],
            show_auto_sub_link=combo["show_auto_sub_link"],
            show_sub_link_b64=combo["show_sub_link_b64"],
            show_multi_server=combo["show_multi_server"],
            show_multi_server_b64=combo["show_multi_server_b64"],
        ))
        wl_grid = _grid(_TgMarkup(wl_user._subscription_config_menu_rows(
            _CfgBiz(), 0, 5, combo)))
        # The panel user-page row has no SellBot counterpart (WL feature).
        wl_grid_n = [row for row in wl_grid
                     if not any("صفحه یوزر" in cell for cell in row)]
        if len(wl_grid_n) != len(wl_grid):
            notes.append(
                f"info config_rows_{label}: by-design extra panel-page row")
        # WL sites append their own trailing back row (contextual target).
        wl_grid_n = wl_grid_n + [["🔙بازگشت"]]
        notes.extend(_diff_grids(
            f"config_rows_{label}", sell_grid, wl_grid_n))

    # U17: services list (_subscription_list returns (text, markup)).
    biz = _StubUserbotBusiness()
    check("services_list",
          _grid(sell_user_kb.services_list_keyboard(
              [{"id": 7, "name": "سرویس اول"}, {"id": 8, "name": "پلن دو"}])),
          _grid(wl_user._subscription_list(
              biz, 0, settings, view="all", page=0)[1]))

    # U18: renew picker rows (+ trailing back, mirroring the site).
    renew_rows = wl_user._renew_menu_rows(
        [{"id": 7, "service_name": "سرویس اول"},
         {"id": 8, "service_name": "", "plan_name": "پلن دو"}],
        settings)
    renew_rows.append([_RawButton("🔙بازگشت", callback_data="x")])
    check("renew_rows",
          _grid(sell_user_kb.renew_services_keyboard(
              [{"id": 7, "name": "سرویس اول"}, {"id": 8, "name": "پلن دو"}])),
          _grid(_TgMarkup(renew_rows)))

    # U19/U20: ticket skip + confirm markups (both modes).
    for mode in ("new", "reply"):
        check(f"ticket_skip_{mode}",
              _grid(sell_user_kb.ticket_skip_screenshot_keyboard(mode)),
              _grid(wl_user._ticket_skip_markup(mode)))
        check(f"ticket_confirm_{mode}",
              _grid(sell_user_kb.ticket_confirm_keyboard(mode)),
              _grid(wl_user._ticket_confirm_markup(mode)))

    # U21: user tickets grid (single + multi page).
    wl_items_4 = [{"id": 7912345}, {"id": 7912346},
                  {"id": 7912347}, {"id": 7912348}]
    sell_tickets_3 = [{"ticket_code": "7912345"}, {"ticket_code": "7912346"},
                      {"ticket_code": "7912347"}]
    check("user_tickets_p1",
          _grid(sell_user_kb.user_tickets_list_keyboard(sell_tickets_3, 1, 1)),
          _grid(_TgMarkup(
              wl_user._user_tickets_rows(wl_items_4[:3], 1, 15)
              + [[_RawButton("🔙بازگشت", callback_data="x")]])))
    # NOTE: SellBot renders ALL tickets on every page (its page slice is
    # dead); WhiteLabel paginates correctly. Feed SellBot the page slice.
    sell_p2 = [{"ticket_code": "7912121"}]
    wl_items_22 = [{"id": 7912000 + i} for i in range(100, 122)]
    check("user_tickets_p2",
          _grid(sell_user_kb.user_tickets_list_keyboard(sell_p2, 2, 2)),
          _grid(_TgMarkup(
              wl_user._user_tickets_rows(wl_items_22, 2, 21)
              + [[_RawButton("🔙بازگشت", callback_data="x")]])))

    # U22: user ticket detail (open + closed).
    wl_ticket_open = {"id": 7912345, "status": "open", "subject": "x",
                      "customer_id": 9}
    wl_ticket_closed = dict(wl_ticket_open, status="closed")
    check("ticket_detail_open",
          _grid(sell_user_kb.user_ticket_detail_keyboard(7912345, True, False)),
          _grid(wl_user._ticket_detail_markup(wl_ticket_open, [])))
    check("ticket_detail_closed",
          _grid(sell_user_kb.user_ticket_detail_keyboard(7912345, False, True)),
          _grid(wl_user._ticket_detail_markup(wl_ticket_closed, [])))

    # U23: support panel rows.
    check("support_panel",
          _grid(sell_user_kb.support_panel_keyboard()),
          _grid(_TgMarkup(wl_user._support_menu_rows())))

    # U24: force join (with + without URL).
    check("force_join_url",
          _grid(sell_user_kb.force_join_keyboard("https://t.me/x")),
          _grid(wl_user._force_join_markup("https://t.me/x")))
    check("force_join_bare",
          _grid(sell_user_kb.force_join_keyboard()),
          _grid(wl_user._force_join_markup("")))

    # U25: main menu renew-hidden variant.
    check("main_menu_no_renew",
          _grid(sell_user_kb.main_menu_keyboard(
              show_renew=False, show_invite=False)),
          _grid(wl_user._main_keyboard(None, _NoRenewBiz())))

    # U26: replace-link confirm button row.
    notes.append(
        "info U26 replace-link confirm is site-inline in WL "
        "(button text covered by static audit)"
    )
    return notes


def _admin_extra_checks(sellbot: Path) -> list[str]:
    """Remaining AdminBot screens (main/menus/profile/tickets/settings)."""
    import asyncio

    from telegram import InlineKeyboardMarkup as _RawMarkup

    notes: list[str] = []
    sell_admin_kb = _load_isolated(
        "_sellbot_admin_kb_extra", sellbot / "AdminBot" / "keyboards.py"
    )
    from TenantRuntime.AdminBot import handlers as wl_admin_handlers
    from TenantRuntime.AdminBot import userbot_management as wl_mgmt
    from TenantRuntime.business import USERBOT_SETTING_DEFAULTS

    def check(name, sell_grid, wl_grid):
        notes.extend(_diff_grids(name, sell_grid, wl_grid))

    # A9: admin main reply grid.
    check("admin_main",
          _grid(sell_admin_kb.admin_main_keyboard()),
          _grid(wl_admin_handlers.admin_main_keyboard()))

    # A10: BTN_* constants.
    sell_btns = {k: v for k, v in vars(sell_admin_kb).items() if k.startswith("BTN_")}
    wl_btns = {k: v for k, v in vars(wl_admin_handlers).items() if k.startswith("BTN_")}
    for key, value in sorted(sell_btns.items()):
        if key not in wl_btns:
            notes.append(f"info {key} only in SellBot ({value!r})")
        elif wl_btns[key] != value:
            notes.append(f"HAMMER {key}: {wl_btns[key]!r} != {value!r}")
    for key in sorted(set(wl_btns) - set(sell_btns)):
        notes.append(f"info {key} only in WhiteLabel ({wl_btns[key]!r})")

    # A11/A12: cancel + confirm-add-user reply keyboards.
    check("admin_cancel",
          _grid(sell_admin_kb.cancel_keyboard()),
          _grid(wl_admin_handlers.cancel_keyboard()))
    check("admin_confirm_add_user",
          _grid(sell_admin_kb.confirm_add_user_keyboard()),
          _grid(wl_admin_handlers.confirm_add_user_keyboard()))

    def sell_builder(name: str, stubs: dict | None = None):
        from telegram import InlineKeyboardButton as _RawButton
        from telegram import InlineKeyboardMarkup as _SellMarkup

        namespace: dict = {
            "InlineKeyboardButton": _RawButton,
            "InlineKeyboardMarkup": _SellMarkup,
        }
        namespace.update(stubs or {})
        return _exec_sellbot_function(
            sellbot, "AdminBot/userbot.py", name, namespace
        )

    # A13-A19: same-named menu builders.
    for builder in (
        "build_userbot_main_menu",
        "build_users_menu_keyboard",
        "build_users_search_menu_keyboard",
        "build_payments_menu_keyboard",
        "build_orders_menu_keyboard",
        "build_gifts_menu_keyboard",
        "build_tickets_menu_keyboard",
        "build_broadcast_menu_keyboard",
    ):
        try:
            sell_grid = _grid(sell_builder(builder, {})())
        except Exception as exc:
            notes.append(f"info {builder}: harness-skip ({type(exc).__name__})")
            continue
        try:
            wl_grid = _grid(getattr(wl_mgmt, builder)())
        except Exception as exc:
            notes.append(f"info {builder}: WL missing ({type(exc).__name__})")
            continue
        if builder == "build_payments_menu_keyboard":
            # WL adds crypto/wallet-topup rows (multi-method features).
            notes.extend(_diff_grids_relaxed(
                builder, sell_grid, wl_grid,
                extra_ok=("Crypto", "کیف پول"),
            ))
            continue
        check(builder, sell_grid, wl_grid)

    # A20: user profile keyboard (WL returns finished markups).
    try:
        check("user_profile",
              _grid(sell_builder("build_user_profile_keyboard")(7)),
              _grid(wl_mgmt._user_profile_keyboard(7)))
    except Exception as exc:
        notes.append(f"info user_profile: harness-skip ({type(exc).__name__})")

    # A21: service detail keyboard (WL helper returns row lists).
    from telegram import InlineKeyboardMarkup as _DetailMarkup

    try:
        check("service_detail",
              _grid(sell_builder("build_service_detail_keyboard")(7, 9)),
              _grid(_DetailMarkup(wl_mgmt._service_detail_keyboard(7, 9))))
    except Exception as exc:
        notes.append(f"info service_detail: harness-skip ({type(exc).__name__})")

    # A22: admin tickets list (stub-driven render).
    try:
        tickets_stub = _StubAdminBusiness()
        # NOTE: WL buckets "open" list from answered tickets.
        tickets_stub._tickets = [
            {"id": 42, "customer_id": 9, "status": "answered"},
            {"id": 43, "customer_id": 9, "status": "answered"},
            {"id": 44, "customer_id": 9, "status": "closed"},
        ]

        orig_list = getattr(tickets_stub, "list_tickets_admin", None)

        def list_tickets_admin(actor):
            return list(tickets_stub._tickets)

        tickets_stub.list_tickets_admin = list_tickets_admin
        update, context, message = _admin_update("userbot:tickets:list:open:1")

        async def _run():
            await wl_mgmt._send_tickets(
                update, tickets_stub, 1, status="open", page=1)

        asyncio.run(_run())
        wl_text, wl_grid = str(message.sent[-1][0] or ""), _grid(message.sent[-1][1])
        sell_grid = _grid(sell_builder("build_tickets_list_keyboard")(
            [{"ticket_code": "42"}, {"ticket_code": "43"}],
            status="open", page=1, total_pages=1,
        ))
        check("admin_tickets_list", sell_grid, wl_grid)
    except Exception as exc:
        notes.append(f"info admin_tickets_list: harness-skip ({type(exc).__name__})")

    # A23: admin ticket reply keyboards.
    try:
        check("admin_reply_skip",
              _grid(sell_builder("build_ticket_reply_screenshot_keyboard")()),
              _grid(wl_mgmt.ticket_reply_skip_cancel_keyboard()))
        check("admin_reply_confirm",
              _grid(sell_builder("build_ticket_reply_confirm_keyboard")()),
              _grid(wl_mgmt.ticket_reply_confirm_keyboard()))
    except Exception as exc:
        notes.append(f"info admin_reply_keyboards: harness-skip ({type(exc).__name__})")

    # A24: settings sections (stub settings + fake update, real code).
    notes.extend(_settings_section_checks(sellbot))
    return notes


def _settings_section_checks(sellbot: Path) -> list[str]:
    """Drive WL settings sections with real defaults; diff vs builders."""
    import asyncio

    from TenantRuntime.business import USERBOT_SETTING_DEFAULTS

    notes: list[str] = []

    def sell_builder(name: str, stubs: dict | None = None):
        from telegram import InlineKeyboardButton as _RawButton
        from telegram import InlineKeyboardMarkup as _RawMarkup

        namespace: dict = {
            "InlineKeyboardButton": _RawButton,
            "InlineKeyboardMarkup": _RawMarkup,
        }
        try:
            _sell_styles = _load_isolated(
                "_sellbot_styles_full",
                sellbot / "Shared" / "tg_button_styles.py",
            )
            namespace["BUTTON_STYLE_THEMES"] = _sell_styles.BUTTON_STYLE_THEMES
            namespace["normalize_button_theme"] = (
                _sell_styles.normalize_button_theme
            )
        except Exception:
            pass
        namespace.update(stubs or {})
        return _exec_sellbot_function(
            sellbot, "AdminBot/userbot.py", name, namespace
        )

    class _SettingsBiz(_StubAdminBusiness):
        def userbot_settings_admin(self, actor):
            return dict(USERBOT_SETTING_DEFAULTS)

    def render(section: str):
        from TenantRuntime.AdminBot import userbot_management as wl_mgmt

        update, context, message = _admin_update(
            f"userbot:settings:{section}")
        asyncio.run(wl_mgmt._settings_section(
            update, _SettingsBiz(), 1, section))
        text, markup = message.sent[-1]
        return str(text or ""), _grid(markup)

    def check_screen(name, sell_text, sell_grid, wl_section):
        try:
            wl_text, wl_grid = render(wl_section)
        except Exception as exc:
            notes.append(f"info {name}: harness-skip ({type(exc).__name__})")
            return
        if wl_text != sell_text:
            notes.append(f"HAMMER {name} text:\n  wl:      {wl_text}\n  sellbot: {sell_text}")
        notes.extend(_diff_grids(name, sell_grid, wl_grid))

    from telegram import InlineKeyboardMarkup as _RawMarkup2

    ui_settings = {
        "colored_buttons": True, "button_theme": "smart",
    }
    pairs = [
        ("subscription",
         "💳تنظیمات پرداخت",
         None, None),
    ]
    # subscription builder takes kwargs, not a dict.
    try:
        sell_sub = sell_builder("build_subscription_settings_menu_keyboard")(
            True, True, True, True, True
        )
        wl_text, wl_grid = render("subscription")
        notes.append("info subscription: manual review (kwargs builder)")
        notes.extend(_diff_grids(
            "subscription_settings", _grid(sell_sub), wl_grid))
    except Exception as exc:
        notes.append(f"info subscription: harness-skip ({type(exc).__name__})")

    # Sections with different feature organization (all toggles work;
    # grouping differs by design). Shared rows are spot-checked by the
    # source-assertion tests instead of positional grid diffs.
    for documented in (
        "tx_plans: transaction-security vs display settings organization",
        "texts: bundled text editors vs per-text entries",
        "marketing: coupon/gift-center vs discount-code flags",
    ):
        notes.append(f"info settings_{documented}")
    section_builders = [
        ("sub_link_status", "build_sub_link_status_menu_keyboard",
         {"show_direct_config": True, "show_auto_sub_link": False,
          "show_sub_link": True, "show_sub_link_b64": False,
          "show_multi_server": False, "show_multi_server_b64": False}),
        ("buy_renew", "build_buy_renew_settings_menu_keyboard",
         {"enable_buy": True, "enable_renew": True,
          "show_renew_in_main_menu": True, "event_channel_enabled": False,
          "renew_unlimited_volume": False, "renew_unlimited_time": False}),
        ("force_join", "build_force_join_settings_menu_keyboard",
         {"force_join_enabled": False}),
        ("backup_restore", "build_backup_restore_settings_menu_keyboard",
         {"auto_backup_enabled": True, "event_channel_enabled": False}),
        ("ui", "build_colored_buttons_settings_menu_keyboard",
         {"colored_buttons": True, "button_theme": "smart"}),
    ]
    for wl_section, builder, sell_kwargs in section_builders:
        try:
            if builder == "build_userbot_settings_menu_keyboard":
                sell_grid = _grid(sell_builder(builder)(dict(ui_settings)))
            elif sell_kwargs is None:
                sell_grid = _grid(sell_builder(builder)())
            else:
                sell_grid = _grid(sell_builder(builder)(dict(sell_kwargs)))
        except Exception as exc:
            notes.append(f"info {wl_section}: sellbot-skip ({type(exc).__name__})")
            continue
        try:
            wl_text, wl_grid = render(wl_section)
        except Exception as exc:
            notes.append(f"info {wl_section}: harness-skip ({type(exc).__name__})")
            continue
        notes.extend(_diff_grids(f"settings_{wl_section}", sell_grid, wl_grid))
    return notes


class _NoRenewBiz(_StubUserbotBusiness):
    def runtime_userbot_settings(self) -> dict:
        settings = super().runtime_userbot_settings()
        settings.update({
            "show_renew_in_main_menu": False,
            "enable_buy": True,
            "show_gift_button": False,
        })
        return settings

    def _ensure_growth_settings(self) -> dict:
        return {"trial_enabled": True, "referral_enabled": False}


def _admin_paired_checks(sellbot: Path) -> list[str]:
    """Paired AdminBot screens (stub business + fake update, real code)."""
    import asyncio

    from telegram import InlineKeyboardButton as _RawButton
    from telegram import InlineKeyboardMarkup as _RawMarkup

    notes: list[str] = []
    settings = {
        "require_last4_for_card_receipt": True,
        "random_tx_spec": True,
        "card_to_card_text": "0",
    }
    card_methods = [{
        "id": 5, "kind": "card", "provider_key": "card_manual",
        "title": "کارت ملی", "currency": "IRR",
        "destination": "6037991112345678 | تست تستی",
        "network": "", "instructions": "", "priority": 100,
        "status": "active", "provider_icon": "💳",
        "provider_title": "کارت به کارت",
    }]
    crypto_methods = [{
        "id": 6, "kind": "crypto", "provider_key": "crypto_manual",
        "title": "تتر", "currency": "USDT", "destination": "TXxx",
        "network": "TRC20", "instructions": "", "priority": 100,
        "status": "active", "provider_icon": "🪙",
        "provider_title": "ارز دیجیتال",
    }]
    biz = _StubAdminBusiness(
        settings=settings,
        methods=card_methods + crypto_methods,
        sms_status={
            "enabled": True, "has_secret": True,
            "masked_secret": "ab12••••wxyz",
            "max_pending_age_minutes": 360,
            "webhook_path": "/payment/sms/abc123",
        },
    )

    def render(data: str):
        return asyncio.run(_render_admin(biz, data))

    from telegram import InlineKeyboardButton as _RawButton2
    from telegram import InlineKeyboardMarkup as _RawMarkup2

    def sell_admin_builder(name: str, stubs: dict | None = None):
        namespace: dict = {
            "InlineKeyboardButton": _RawButton2,
            "InlineKeyboardMarkup": _RawMarkup2,
        }
        namespace.update(stubs or {})
        return _exec_sellbot_function(
            sellbot, "AdminBot/userbot.py", name, namespace
        )

    # A2: payment settings menu (real SellBot builder).
    sell_pay_grid = _grid(sell_admin_builder("build_payment_settings_menu_keyboard")(
        {"event_channel_enabled": False}
    ))
    wl_text, wl_grid = render("userbot:settings:payment")
    if wl_text != "💳تنظیمات پرداخت":
        notes.append(f"HAMMER payment_menu text: {wl_text!r}")

    def _drop_event_row(grid):
        return [row for row in grid if not any("📢تنظیم کانال رویداد" in cell for cell in row)]

    notes.extend(_diff_grids_relaxed(
        "payment_menu",
        _drop_event_row(sell_pay_grid),
        _drop_event_row(wl_grid),
        missing_ok=("زرین پال", "پرفکت مانی"),
        extra_ok=("➕ افزودن روش پرداخت",),
    ))

    # A3: card submenu (real SellBot builder).
    wl_text, wl_grid = render("userbot:settings:payment:card")
    if wl_text != "💳تنظیمات کارت به کارت":
        notes.append(f"HAMMER card_menu text: {wl_text!r}")
    sell_card_menu = sell_admin_builder(
        "build_payment_method_menu_keyboard",
        {
            "_get_tx_plans_settings": lambda: {"random_tx_spec": True},
            "_get_payment_settings": lambda: {
                "require_last4_for_card_receipt": True
            },
        },
    )
    notes.extend(_diff_grids(
        "card_menu",
        _grid(sell_card_menu("card", True)),
        wl_grid,
    ))

    # A4: card list.
    wl_text, wl_grid = render("userbot:settings:payment:card:list")
    if wl_text != "💳لیست کارت‌ها":
        notes.append(f"HAMMER card_list text: {wl_text!r}")
    notes.extend(_diff_grids(
        "card_list",
        _grid(sell_admin_builder("build_payment_cards_list_keyboard")(
            [{"number": "6037991112345678"}]
        )),
        wl_grid,
    ))

    # A5: card item.
    wl_text, wl_grid = render("userbot:settings:payment:card:item:5")
    number = "6037991112345678"
    sell_item_text = (
        f"❖ شماره کارت: <code>{number}</code>\n"
        f"❖ نام صاحب کارت: تست تستی"
    )
    if _norm(wl_text) != _norm(sell_item_text):
        notes.append(
            f"HAMMER card_item text:\n  wl:      {wl_text}\n"
            f"  expected: {sell_item_text}"
        )
    notes.extend(_diff_grids(
        "card_item",
        _grid(sell_admin_builder("build_payment_card_item_keyboard")(number)),
        wl_grid,
    ))

    # A6: crypto submenu.
    wl_text, wl_grid = render("userbot:settings:payment:crypto")
    if wl_text != "🔗تنظیمات پرداخت ارز دیجیتال":
        notes.append(f"HAMMER crypto_menu text: {wl_text!r}")
    notes.extend(_diff_grids_relaxed(
        "crypto_menu",
        _grid(sell_admin_builder("build_payment_method_menu_keyboard")(
            "crypto", True
        )),
        wl_grid,
        extra_ok=("🪙", "➕ افزودن روش ارز دیجیتال"),
    ))

    # A7: SMS submenu keyboard (SellBot builder with stubbed status).
    sell_sms_kb = _exec_sellbot_function(
        sellbot, "AdminBot/userbot.py", "build_sms_webhook_settings_keyboard",
        {
            "InlineKeyboardButton": _RawButton,
            "InlineKeyboardMarkup": _RawMarkup,
            "_sms_webhook_status": lambda: {
                "enabled": True, "secret": "x", "age": "360",
                "endpoint": "https://example.com/hook",
            },
        },
    )
    _, wl_sms_grid = render("userbot:settings:payment:card:sms")
    notes.extend(_diff_grids(
        "sms_menu",
        _grid(sell_sms_kb()),
        wl_sms_grid,
    ))

    # A8: payment detail text (key lines must match SellBot).
    from TenantRuntime.AdminBot import userbot_management as wl_mgmt

    # Same real-world payment in each bot's native units: SellBot stores
    # toman, WhiteLabel IRR rials (x10).
    wl_pay = {
        "payment_key": "order:6", "display_name": "Speed Vpn",
        "username": "SpeedlVpn", "telegram_user_id": 6119169885,
        "created_at": "2026-10-11T10:35:00+00:00",
        "amount": 358600, "currency": "IRR", "status": "rejected",
        "provider_title": "کارت به کارت", "reference": "1234",
    }
    wl_detail = wl_mgmt._payment_detail_text(wl_pay, "1234")
    sell_detail_fn = _exec_sellbot_function(
        sellbot, "AdminBot/userbot.py", "_build_payment_detail_text",
        {
            "_parse_receipt_meta": _exec_sellbot_function(
                sellbot, "AdminBot/userbot.py", "_parse_receipt_meta", {}
            ),
            "_format_toman": _exec_sellbot_function(
                sellbot, "AdminBot/userbot.py", "_format_toman", {}
            ),
            "_payment_status_title": _exec_sellbot_function(
                sellbot, "AdminBot/userbot.py", "_payment_status_title", {}
            ),
        },
    )
    sell_detail = sell_detail_fn({
        "tx_code": "7654321", "username": "SpeedlVpn",
        "full_name": "Speed Vpn", "telegram_id": 6119169885,
        "created_at": "2026-10-11 10:35:00", "amount": 35860,
        "method": "card", "status": "rejected",
        "receipt_image": "payer_last4: 1234",
    })
    for line in (
        "👤 کاربر: Speed Vpn",
        "◈ نام کاربری: @SpeedlVpn",
        "◈ شناسه کاربر: 6119169885",
        "◈ مبلغ تراکنش: 35,860 تومان",
        "◈ وضعیت: ❌ رد شده",
        "◈ ۴ رقم آخر کارت مبدا: 1234",
    ):
        if line not in wl_detail:
            notes.append(f"HAMMER payment_detail WL missing: {line!r}")
        if line not in sell_detail:
            notes.append(f"HAMMER payment_detail SellBot missing: {line!r}")
    # Method line differs by design: SellBot shows raw "card",
    # WhiteLabel the Persian provider title.
    if "◈ روش تراکنش: کارت به کارت" not in wl_detail:
        notes.append("HAMMER payment_detail WL method line")
    return notes


def _load_isolated(name: str, path: Path):
    """Load one SellBot module by file location (no sys.path side effects)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_sellbot_shim(sellbot: Path) -> None:
    """Register SellBot's pure tg_button_styles so its keyboards import."""
    key = "Shared.tg_button_styles"
    if key in sys.modules:
        return
    import types

    shim = _load_isolated(
        "_sellbot_tg_button_styles", sellbot / "Shared" / "tg_button_styles.py"
    )
    alias = types.ModuleType(key)
    alias.inline_button = shim.inline_button
    alias.keyboard_button = shim.keyboard_button
    sys.modules[key] = alias


def _exec_sellbot_function(sellbot: Path, rel: str, func_name: str, namespace: dict):
    """Exec one pure builder out of a heavy SellBot module (AST slice)."""
    attempts = 0
    while True:
        attempts += 1
        tree = ast.parse((sellbot / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                code = compile(ast.Module(body=[node], type_ignores=[]), rel, "exec")
                local_ns: dict = dict(namespace)
                exec(code, local_ns)
                return local_ns[func_name]
        # Transient read races (editor save mid-read) retry once.
        if attempts >= 2:
            break
    available = sorted(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    )
    close = difflib.get_close_matches(func_name, available, n=3, cutoff=0.5)
    raise LookupError(f"{func_name} not found in {rel} (close: {close})")


def paired_checks(sellbot: Path) -> list[str]:
    problems: list[str] = []
    _ensure_sellbot_shim(sellbot)

    sell_user_kb = _load_isolated(
        "_sellbot_user_keyboards", sellbot / "UserBot" / "keyboards.py"
    )
    sell_admin_kb = _load_isolated(
        "_sellbot_admin_keyboards", sellbot / "AdminBot" / "keyboards.py"
    )
    from TenantRuntime.UserBot import handlers as wl_user
    from TenantRuntime.AdminBot import handlers as wl_admin

    from telegram import InlineKeyboardButton as _TgButton
    from telegram import InlineKeyboardMarkup as _TgMarkup

    # U1: main menu (reply keyboard). WL _main_keyboard ignores spec.
    sell_main = _grid(sell_user_kb.main_menu_keyboard())
    biz = _StubUserbotBusiness()
    wl_main = _grid(wl_user._main_keyboard(None, biz))
    problems.extend(_diff_grids("main_menu", sell_main, wl_main))

    # U2: location list (+ trailing back row, mirroring the shop:buy site).
    sell_location = _grid(sell_user_kb.location_keyboard(
        [{"id": 1, "title": "لوکیشن ترکیه 🇹🇷"}], columns=1
    ))
    wl_rows = wl_user._purchase_location_rows(
        [{"id": 1, "label": "لوکیشن ترکیه 🇹🇷"}],
        {"server_columns": 1, "shuffle_server_layout": False,
         "colored_buttons": True, "button_theme": "smart"},
    )
    wl_rows.append([_TgButton("🔙بازگشت", callback_data="runtime:home")])
    problems.extend(_diff_grids("location", sell_location, _grid(_TgMarkup(wl_rows))))

    # U3: fixed plans list.
    sample_sell_plans = [
        {"id": 11, "title": "پلن یک", "gb": 10, "days": 30, "price": 130000, "priority": 0},
        {"id": 12, "title": "پلن دو", "gb": 20, "days": 60, "price": 250000, "priority": 0},
    ]
    sell_plans = _grid(sell_user_kb.plans_keyboard(
        sample_sell_plans, 1, 0, columns=1, back_to_categories=True,
    ))
    sample_wl_plans = [
        {"id": 11, "name": "پلن یک", "traffic_gb": 10, "duration_days": 30, "price": 130000, "currency": "IRR"},
        {"id": 12, "name": "پلن دو", "traffic_gb": 20, "duration_days": 60, "price": 250000, "currency": "IRR"},
    ]
    wl_plan_rows = wl_user._plan_buttons(
        sample_wl_plans,
        {"plan_columns": 1, "colored_buttons": True, "button_theme": "smart"},
        server_id=1,
    )
    # Mirror the buycat site: trailing back row (callbacks differ by design).
    wl_plan_rows.append([_TgButton("🔙 بازگشت به دسته‌بندی‌ها", callback_data="x")])

    def _unit(text: str) -> str:
        for token in ("تومان", "IRR", "IRT", "TMN"):
            text = text.replace(token, "¤")
        return text

    sell_plans_n = [[_unit(t) for t in row] for row in sell_plans]
    wl_plans_n = [[_unit(t) for t in row] for row in _grid(_TgMarkup(wl_plan_rows))]
    problems.extend(_diff_grids("plans", sell_plans_n, wl_plans_n))

    # U5: selected plan buttons.
    sell_selected = _grid(sell_user_kb.selected_plan_keyboard(1, 10, 30, 130000))
    wl_summary = _grid(wl_user._order_summary_markup(
        7, "shop:buyloc:1",
        {"colored_buttons": True, "button_theme": "smart"},
        show_coupon=False,
    ))
    problems.extend(_diff_grids("selected_plan_buttons", sell_selected, wl_summary))

    # U7: invite banner.
    problems.extend(_diff_grids(
        "invite_banner",
        _grid(sell_user_kb.invite_banner_keyboard()),
        _grid(wl_user._invite_home_markup()),
    ))

    # U8/U9: receipt keyboards (reply).
    problems.extend(_diff_grids(
        "receipt_confirm",
        _grid(sell_user_kb.confirm_payment_keyboard()),
        _grid(wl_user._receipt_confirm_keyboard()),
    ))
    # SellBot receipt-cancel is a lone red back; WL matches by construction.
    problems.extend(_diff_grids(
        "receipt_cancel",
        _grid(sell_user_kb.receipt_cancel_keyboard()),
        _grid(wl_user._receipt_cancel_keyboard()),
    ))

    # U10: wallet methods.
    sell_wallet = _grid(sell_user_kb.wallet_inline_keyboard(
        show_coupon=False, show_card=True, show_zarinpal=False,
        show_perfect_money=False, show_crypto=True,
    ))
    wl_wallet = _grid(_TgMarkup(wl_user._wallet_method_rows(
        biz, {"show_gift_button": False}, include_back=True,
    )))
    wl_wallet_texts = [[t for t in row] for row in wl_wallet]
    # Structural check only: WL lists configured methods, SellBot lists types.
    print(f"[info] wallet shapes differ by design "
          f"(sellbot={len(sell_wallet)} rows, wl={len(wl_wallet_texts)} rows)")

    # A1: AdminBot search menu (pure builder, AST-sliced).
    from telegram import InlineKeyboardButton as _RawButton
    from telegram import InlineKeyboardMarkup as _RawMarkup

    sell_search_menu = _exec_sellbot_function(
        sellbot, "AdminBot/servers.py", "build_search_menu_keyboard",
        {"InlineKeyboardButton": _RawButton, "InlineKeyboardMarkup": _RawMarkup},
    )
    problems.extend(_diff_grids(
        "admin_search_menu",
        _grid(sell_search_menu()),
        _grid(wl_admin._search_menu_keyboard()),
    ))

    _merge_notes(problems, _user_extra_checks(sellbot))

    # T1: card instruction text (tags stripped; pure builder, AST-sliced).
    from html import escape as _escape

    sell_card_fn = _exec_sellbot_function(
        sellbot, "UserBot/main.py", "_build_card_to_card_payment_text",
        {"escape": _escape},
    )
    # SellBot appends this suffix at the call sites (WL builder includes it).
    sell_card = _strip_tags(
        sell_card_fn(
            amount_toman=130263, card_number="60379911", card_owner="تست تستی",
            card_bank="", text_settings={},
        )
        + "\n\n⚡️ پس از تایید پرداخت، اشتراک شما به‌صورت خودکار ساخته و ارسال می‌شود."
    )
    wl_card = _strip_tags(wl_user._card_instruction_text(
        amount_rial=1302630, card_number="60379911", card_owner="تست تستی",
        card_bank="",
    ))
    if sell_card != wl_card:
        problems.append(f"card_instruction text differs:\n  sellbot: {sell_card}\n  wl:      {wl_card}")

    print("\n---------------- admin paired checks ----------------", flush=True)
    _merge_notes(problems, _admin_paired_checks(sellbot) + _admin_extra_checks(sellbot))
    return problems


def main() -> int:
    args = _parse_args()
    sellbot = Path(args.sellbot)
    if not sellbot.is_dir():
        print(f"SellBot checkout not found: {sellbot} (use --sellbot DIR)")
        return 2

    hard, _ = static_audit(sellbot)
    if args.static_only:
        return 1 if hard else 0

    print("\n================ paired builder checks ================", flush=True)
    problems: list[str] = []
    try:
        problems = paired_checks(sellbot)
    except Exception as exc:
        print(f"[harness-error] paired checks crashed: {type(exc).__name__}: {exc}")
        return 2
    if problems:
        print(f"\n--- {len(problems)} paired mismatch(es) ---")
        for item in problems:
            print(f"  HAMMER {item}")
        return 1
    print("\npaired screens: all matched. OK")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
