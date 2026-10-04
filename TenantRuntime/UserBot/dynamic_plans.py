"""Server-specific dynamic quotes using the normal, immutable checkout flow.

The AdminBot SellBot-parity editor uses month-based duration settings. Existing
tenants that still have legacy day-based pricing continue to use their old
fields until an admin edits the dynamic-plan settings.
"""

from telegram import InlineKeyboardMarkup
from TenantRuntime.button_styles import inline_button as Button
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.server_admin import ServerAdminService


def _month_mode(sales: dict) -> bool:
    return sales.get("pricing_model") == "sellbot_month"


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
                [Button(f"🎛 پلن پویا · {server['label']}"[:60], callback_data=callback)]
            )
    return result


async def handle_callback(update, context, *, business, actor, settings):
    data = str(update.callback_query.data or "")
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
    query = update.callback_query
    if action == "confirm":
        from TenantRuntime.UserBot.handlers import _checkout_text, _checkout_markup

        plan = service.dynamic_plan(actor, sid, quote["gb"], quote_days)
        order = (
            business.create_renewal_order(
                actor, subscription_id=subid, plan_id=plan["id"]
            )
            if subid
            else business.create_order(actor, plan["id"], server_id=sid)
        )
        context.user_data.pop(key, None)
        await query.answer()
        await query.edit_message_text(
            _checkout_text(order, business.wallet_summary(actor)),
            reply_markup=_checkout_markup(int(order["id"]), settings),
        )
        return True

    def callback(name):
        return f"shop:dynamic:{sid}:{name}" + (f":{subid}" if subid else "")

    if month_mode:
        duration_row = [
            Button("➖ مدت", callback_data=callback("months_minus")),
            Button(f"{quote['months']} ماه", callback_data="noop"),
            Button("➕ مدت", callback_data=callback("months_plus")),
        ]
        duration_text = f"{quote['months']} ماه"
    else:
        duration_row = [
            Button("➖ مدت", callback_data=callback("days_minus")),
            Button(f"{quote['days']} روز", callback_data="noop"),
            Button("➕ مدت", callback_data=callback("days_plus")),
        ]
        duration_text = f"{quote['days']} روز"

    keyboard = [
        [
            Button("➖ حجم", callback_data=callback("gb_minus")),
            Button(f"{quote['gb']} گیگابایت", callback_data="noop"),
            Button("➕ حجم", callback_data=callback("gb_plus")),
        ],
        duration_row,
        [Button("✅ تایید و پرداخت", callback_data=callback("confirm"))],
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
        f"🎛 پلن پویا · {business.server(sid)['label']}\n\n"
        f"📊 حجم: {quote['gb']} گیگ\n"
        f"📅 مدت: {duration_text}\n"
        f"💰 مبلغ: {price:,} {currency}\n"
        f"🎟 تخفیف: {service.discount_percent(sid,quote['gb'])}٪",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True
