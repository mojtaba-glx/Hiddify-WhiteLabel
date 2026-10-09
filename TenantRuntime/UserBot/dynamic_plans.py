"""Server-specific dynamic quotes using the normal, immutable checkout flow.

SellBot-parity enhancements (v2):
- shop:buyloc:{sid} now goes directly to the dynamic plan builder, skipping
  the intermediate plan-list step entirely (SellBot parity).
- shop:buy callback re-shows the server/location list as an inline edit.
- Price shown in Toman for IRR currencies.
- Confirm button text: 'تایید و خرید' (was 'تایید و پرداخت').
- SellBot-style plan header: 'بسته مورد نیاز خود را جهت خرید تنظیم کنید'
- A SellBot-style plan summary (حجم / زمان / قیمت + payment method buttons)
  is shown after confirm, instead of routing straight to the checkout form.
- Existing tenants on legacy day-based pricing continue to work unchanged.
"""

from telegram import InlineKeyboardMarkup
from TenantRuntime.button_styles import inline_button as Button
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.server_admin import ServerAdminService


def _month_mode(sales: dict) -> bool:
    return sales.get("pricing_model") == "sellbot_month"


def _display_price(price: int, currency: str) -> "tuple[int, str]":
    """تبدیل ریال به تومان برای نمایش SellBot-parity."""
    if str(currency).upper() == "IRR":
        return price // 10, "تومان"
    return price, currency


def rows(business, *, subscription=None, server_id=None):
    service = ServerAdminService(business)
    result = []
    servers = business.list_purchase_servers()
    for server in servers:
        sid = int(server["id"])
        if subscription and sid != int(subscription.get("server_id") or 0):
            continue
        if server_id is not None and sid != int(server_id):
            continue
        sales = service.sales(sid)
        time_price = sales["price_month"] if _month_mode(sales) else sales["price_day"]
        if (
            sales["mode"] in {"dynamic", "mixed"}
            and sales["price_gb"] + time_price > 0
        ):
            callback = f"shop:dynamic:{sid}:open" + (
                f':{subscription["id"]}' if subscription else ""
            )
            result.append(
                [Button(
                    ("f39b پلن پویا · " + server['label'])[:60],
                    callback_data=callback,
                )]
            )
    return result


async def _open_plan_builder(
    query,
    context,
    *,
    business,
    actor: int,
    sid: int,
    subid,
    settings: dict,
) -> None:
    """Initialise a fresh quote and render the plan-builder screen."""
    business._customer(actor)
    business._purchase_server(sid)
    service = ServerAdminService(business)
    sales = service.sales(sid)
    month_mode = _month_mode(sales)
    key = f"dynamic_quote:{sid}:{subid or 0}"
    context.user_data[key] = (
        {"gb": sales["min_gb"], "months": sales["min_month"]}
        if month_mode
        else {"gb": sales["min_gb"], "days": sales["min_days"]}
    )
    quote = context.user_data[key]
    await _render_builder(
        query,
        context,
        business=business,
        sid=sid,
        subid=subid,
        quote=quote,
        service=service,
        sales=sales,
        month_mode=month_mode,
    )


async def _render_builder(
    query,
    context,
    *,
    business,
    sid: int,
    subid,
    quote: dict,
    service,
    sales: dict,
    month_mode: bool,
) -> None:
    """Render the SellBot-parity plan-builder keyboard."""
    quote_days = int(quote["months"]) * 30 if month_mode else int(quote["days"])
    price, currency = service.quote(sid, quote["gb"], quote_days)
    price_disp, currency_disp = _display_price(price, currency)

    def cb(name: str) -> str:
        return f"shop:dynamic:{sid}:{name}" + (f":{subid}" if subid else "")

    if month_mode:
        duration_row = [
            Button("➖", callback_data=cb("months_minus")),
            Button(f"{quote['months']} ماهه", callback_data="noop"),
            Button("➕", callback_data=cb("months_plus")),
        ]
        duration_text = f"{quote['months']} ماهه"
    else:
        duration_row = [
            Button("➖", callback_data=cb("days_minus")),
            Button(f"{quote['days']} روز", callback_data="noop"),
            Button("➕", callback_data=cb("days_plus")),
        ]
        duration_text = f"{quote['days']} روزه"

    keyboard = [
        [Button("📊 حجم", callback_data="noop")],
        [
            Button("➖", callback_data=cb("gb_minus")),
            Button(f"{quote['gb']} گیگابایت", callback_data="noop"),
            Button("➕", callback_data=cb("gb_plus")),
        ],
        [Button("⏳ زمان", callback_data="noop")],
        duration_row,
        [
            Button(f"🏷 تخفیف: {service.discount_percent(sid, quote['gb'])}%", callback_data="noop"),
            Button(f"💰 قیمت: {price_disp:,} {currency_disp}", callback_data="noop"),
        ],
        [Button("💳 تایید و خرید", callback_data=cb("confirm"))],
        [
            Button(
                "🔙 بازگشت",
                callback_data=f"shop:subrefresh:{subid}" if subid else "shop:buy",
            )
        ],
    ]
    await query.answer()
    from TenantRuntime.UserBot.handlers import _edit_subscription

    await _edit_subscription(
        query,
        "📦بسته مورد نیاز خود را جهت خرید تنظیم کنید",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_callback(update, context, *, business, actor, settings):
    data = str(update.callback_query.data or "")

    # SellBot-parity: re-show location list when user presses Back from builder
    if data == "shop:buy":
        if not settings.get("enable_buy", True):
            raise TenantBusinessError("purchase is disabled")
        from TenantRuntime.UserBot.handlers import _purchase_location_rows
        servers = business.list_purchase_servers()
        if not servers:
            await update.callback_query.answer(
                "سروری در دسترس نیست.",
                show_alert=True,
            )
            return True
        lrows = _purchase_location_rows(servers, settings)
        lrows.append([Button("🔙 منو", callback_data="runtime:home")])
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            str(settings.get("servers_list_text") or "").strip()
            or (
                "📡 لیست سرورها\n"
                "لطفاً لوکیشن مورد نظر خود را انتخاب کنید:"
            ),
            reply_markup=InlineKeyboardMarkup(lrows),
        )
        return True

    # SellBot-parity: shop:buyloc:{sid} -> skip plan list for dynamic sales.
    # Fixed-only servers must fall back to handlers.py, which renders their
    # normal plan/category list. Without this guard every location callback
    # was treated as a dynamic quote and could break fixed-plan purchases.
    if data.startswith("shop:buyloc:"):
        sid_str = data.rsplit(":", 1)[-1]
        if not sid_str.isdigit():
            return False  # 'all' / 'multi' variants handled elsewhere in on_callback
        sid = int(sid_str)
        if not settings.get("enable_buy", True):
            raise TenantBusinessError("purchase is disabled")
        sales = ServerAdminService(business).sales(sid)
        time_price = (
            sales["price_month"]
            if _month_mode(sales)
            else sales["price_day"]
        )
        if (
            sales["mode"] not in {"dynamic", "mixed"}
            or sales["price_gb"] + time_price <= 0
        ):
            return False
        await _open_plan_builder(
            update.callback_query,
            context,
            business=business,
            actor=actor,
            sid=sid,
            subid=None,
            settings=settings,
        )
        return True

    if not data.startswith("shop:dynamic:"):
        return False

    parts = data.split(":")
    sid = int(parts[2])
    action = parts[3] if len(parts) > 3 else "open"
    subid = int(parts[4]) if len(parts) > 4 else None

    if not subid and not settings.get("enable_buy", True):
        raise TenantBusinessError("purchase is disabled")
    if subid:
        if not settings.get("enable_renew", True):
            raise TenantBusinessError("renewal is disabled")
        owned = business.customer_subscription_status(
            actor, subscription_id=subid, refresh=False
        )
        if int(owned.get("server_id") or 0) != sid:
            raise TenantBusinessError("wrong renewal server")

    business._customer(actor)
    business._purchase_server(sid)
    service = ServerAdminService(business)
    sales = service.sales(sid)
    month_mode = _month_mode(sales)
    key = f"dynamic_quote:{sid}:{subid or 0}"

    if action == "open":
        context.user_data[key] = (
            {"gb": sales["min_gb"], "months": sales["min_month"]}
            if month_mode
            else {"gb": sales["min_gb"], "days": sales["min_days"]}
        )

    quote = context.user_data.get(key)
    if not isinstance(quote, dict):
        raise TenantBusinessError("quote expired")

    if month_mode:
        changes = {
            "gb_plus": ("gb", sales["step_gb"]),
            "gb_minus": ("gb", -sales["step_gb"]),
            "months_plus": ("months", sales["step_month"]),
            "months_minus": ("months", -sales["step_month"]),
            # Keep callbacks from already-rendered keyboards valid across an
            # AdminBot settings update.
            "days_plus": ("months", sales["step_month"]),
            "days_minus": ("months", -sales["step_month"]),
        }
        bounds = {
            "gb": (sales["min_gb"], sales["max_gb"]),
            "months": (sales["min_month"], sales["max_month"]),
        }
    else:
        changes = {
            "gb_plus": ("gb", sales["step_gb"]),
            "gb_minus": ("gb", -sales["step_gb"]),
            "days_plus": ("days", sales["step_days"]),
            "days_minus": ("days", -sales["step_days"]),
        }
        bounds = {
            "gb": (sales["min_gb"], sales["max_gb"]),
            "days": (sales["min_days"], sales["max_days"]),
        }

    if action in changes:
        field, change = changes[action]
        low, high = bounds[field]
        quote[field] = max(low, min(high, quote[field] + change))
    elif action not in {"open", "confirm"}:
        raise ValueError("invalid quote action")

    quote_days = int(quote["months"]) * 30 if month_mode else int(quote["days"])
    price, currency = service.quote(sid, quote["gb"], quote_days)
    price_disp, currency_disp = _display_price(price, currency)
    query = update.callback_query

    if action == "confirm":
        plan = service.dynamic_plan(actor, sid, quote["gb"], quote_days)
        order = (
            business.create_renewal_order(
                actor, subscription_id=subid, plan_id=plan["id"]
            )
            if subid
            else business.create_order(actor, plan["id"], server_id=sid)
        )
        oid = int(order["id"])
        context.user_data.pop(key, None)
        await query.answer()
        # SellBot-parity: plan summary + payment method selection
        await query.edit_message_text(
            (
                "📄 اطلاعات پلن انتخاب شده\n\n"
                f"📊 حجم: {quote['gb']} گیگ\n"
                f"📅 زمان: {quote_days} روز\n"
                f"💰 قیمت: {price_disp:,} {currency_disp}"
            ),
            reply_markup=InlineKeyboardMarkup([
                [Button(
                    "💳 پرداخت مستقیم",
                    callback_data=f"shop:paymethods:{oid}",
                )],
                [Button(
                    "💰 پرداخت از کیف پول",
                    callback_data=f"shop:walletpay:{oid}",
                )],
                [Button(
                    "🔙 بازگشت",
                    callback_data="runtime:home",
                )],
            ]),
        )
        return True

    # Default: render builder with updated quote (gb/time +/- actions)
    await _render_builder(
        query,
        context,
        business=business,
        sid=sid,
        subid=subid,
        quote=quote,
        service=service,
        sales=sales,
        month_mode=month_mode,
    )
    return True
