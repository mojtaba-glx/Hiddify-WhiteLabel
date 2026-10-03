"""Server-specific dynamic quotes using the normal, immutable checkout flow."""

from telegram import InlineKeyboardMarkup
from TenantRuntime.button_styles import inline_button as Button
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.server_admin import ServerAdminService


def rows(business, *, subscription=None):
    service = ServerAdminService(business)
    result = []
    servers = business.list_purchase_servers()
    for server in servers:
        sid = int(server["id"])
        if subscription and sid != int(subscription.get("server_id") or 0):
            continue
        sales = service.sales(sid)
        if (
            sales["mode"] in {"dynamic", "mixed"}
            and sales["price_gb"] + sales["price_day"] > 0
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
    key = f"dynamic_quote:{sid}:{subid or 0}"
    if action == "open":
        context.user_data[key] = {"gb": sales["min_gb"], "days": sales["min_days"]}
    quote = context.user_data.get(key)
    if not isinstance(quote, dict):
        raise TenantBusinessError("quote expired")
    changes = {
        "gb_plus": ("gb", sales["step_gb"]),
        "gb_minus": ("gb", -sales["step_gb"]),
        "days_plus": ("days", sales["step_days"]),
        "days_minus": ("days", -sales["step_days"]),
    }
    if action in changes:
        field, change = changes[action]
        quote[field] = max(
            sales["min_" + field], min(sales["max_" + field], quote[field] + change)
        )
    elif action not in {"open", "confirm"}:
        raise ValueError("invalid quote action")
    price, currency = service.quote(sid, quote["gb"], quote["days"])
    query = update.callback_query
    if action == "confirm":
        from TenantRuntime.UserBot.handlers import _checkout_text, _checkout_markup

        plan = service.dynamic_plan(actor, sid, quote["gb"], quote["days"])
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

    def callback(action):
        return f"shop:dynamic:{sid}:{action}" + (f":{subid}" if subid else "")

    keyboard = [
        [
            Button("➖ حجم", callback_data=callback("gb_minus")),
            Button(f"{quote['gb']} گیگابایت", callback_data="noop"),
            Button("➕ حجم", callback_data=callback("gb_plus")),
        ],
        [
            Button("➖ مدت", callback_data=callback("days_minus")),
            Button(f"{quote['days']} روز", callback_data="noop"),
            Button("➕ مدت", callback_data=callback("days_plus")),
        ],
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
        f"🎛 پلن پویا · {business.server(sid)['label']}\n\n📊 حجم: {quote['gb']} گیگ\n📅 مدت: {quote['days']} روز\n💰 مبلغ: {price:,} {currency}\n🎟 تخفیف: {service.discount_percent(sid,quote['gb'])}٪",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True
