"""SellBot-parity contracts for the tenant UserBot purchase flow."""

from __future__ import annotations

from TenantRuntime.UserBot.handlers import _plan_buttons, _purchase_location_rows


def test_purchase_location_is_the_first_catalog_step() -> None:
    rows = _purchase_location_rows(
        [
            {"id": 7, "label": "🇩🇪 آلمان"},
            {"id": 9, "label": "🇹🇷 ترکیه"},
        ],
        {"server_columns": 1, "shuffle_server_layout": False},
    )

    assert rows[0][0].text == "🇩🇪 آلمان"
    assert rows[0][0].callback_data == "shop:buyloc:7"
    assert rows[1][0].callback_data == "shop:buyloc:9"


def test_plan_callback_keeps_the_selected_server() -> None:
    rows = _plan_buttons(
        [
            {
                "id": 31,
                "name": "Pro",
                "traffic_gb": 10,
                "duration_days": 30,
                "price": 120000,
                "currency": "IRT",
            }
        ],
        {"plan_columns": 1},
        server_id=7,
    )

    assert rows[0][0].callback_data == "shop:plan:7:31"


def test_purchase_flow_source_orders_location_before_plan() -> None:
    from pathlib import Path

    source = Path("TenantRuntime/UserBot/handlers.py").read_text(encoding="utf-8")
    buy_entry = source.index('if text == BTN_BUY:')
    buy_callback = source.index('if data == "shop:buy":')
    location_callback = source.index('if data.startswith("shop:buyloc:"):')
    category_callback = source.index('if data.startswith("shop:buycat:"):')
    plan_callback = source.index('if data.startswith("shop:plan:"):')
    assert buy_entry < buy_callback < location_callback < category_callback < plan_callback
