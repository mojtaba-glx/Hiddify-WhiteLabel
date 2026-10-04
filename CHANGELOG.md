# Changelog – SellBot Parity

All parity changes are tracked here. Branch: `feat/sellbot-parity-phase1`

---

## v0.4.0-purchase-flow – SellBot-parity Purchase Flow

**Commit:** 9e9868fe54585a33953d09848049f4f3d871bd21  
**File:** `TenantRuntime/UserBot/dynamic_plans.py`

### Changed (SellBot UX parity)

| # | تغییر | WhiteLabel قدیم | SellBot / جدید |
|---|---|---|---|
| 1 | **ردیرکت مستقیم از لکیشن** | لیست پلن (“پلن پویا · ترکیه”) | مستقیم به plan builder |
| 2 | **نمایش قیمت** | `IRR 30,000` | `130,000 تومان` (IRR ÷ 10) |
| 3 | **هدر plan builder** | — | “بسته مورد نیاز خود را جهت خرید تنظیم کنید” |
| 4 | **دکمه confirm** | “✅ تایید و پرداخت” | “✅ تایید و خرید” |
| 5 | **مرحله بعد از confirm** | مستقیم checkout | خلاصه پلن (حجم/زمان/قیمت) + انتخاب روش پرداخت |
| 6 | **دکمه‌های پرداخت** | دکمه تک (جریان کارت و کیف پول) | «💳 پرداخت مستقیم» + «💰 پرداخت از کیف پول» |
| 7 | **دکمه بازگشت** | — | بازگشت به لیست لکیشن (بدون بارگذاری صفحه) |

### Added

- `_display_price(price, currency)` — IRR ÷ 10 → تومان
- `_open_plan_builder()` — helper برای اینیشیالایز quote و رندر builder
- `_render_builder()` — رندر SellBot-style plan builder keyboard
- handler برای `shop:buy` — نمایش دوباره لیست سرورها (inline edit) برای دکمه بازگشت
- intercept برای `shop:buyloc:{sid}` — بدون مرحله میانی plan list

### Technical notes

- `handle_callback` now intercepts `shop:buy` and `shop:buyloc:{sid}` **before**
  the existing handlers in `handlers.py` see them.
- Callbacks `shop:buyloc:all` and `shop:buyloc:multi` (future multi-server features)
  return `False` and fall through to the original handler.
- Plan summary step uses `shop:paymethods:{oid}` and `shop:walletpay:{oid}` — the
  same callbacks that the existing checkout flow already handles.
- No changes to `handlers.py` required for the core flow; BTN_BUY header text
  update is optional and deferred.

---

## v0.2.0-parity-phase2 – Card Payment Flow

**Commit:** a87ab9fc14e68e71fe0532906a3af16b35101f25  
**File:** `TenantRuntime/UserBot/card_payment.py`

### Added

| Function | SellBot origin | Description |
|---|---|---|
| `finalize_pending_card_payment` | `_finalize_pending_card_payment` | Download receipt + submit + archive media + notify admin |
| `finalize_pending_wallet_topup` | *(wallet variant)* | Same flow for wallet top-up |
| `_notify_admin_card_payment` | `_notify_admin_new_ticket` pattern | Best-effort AdminBot DM on receipt submit |
| `pending_card_admin_notify_job` | `_pending_card_admin_notify_job` | PTB job_queue periodic reminder (25s / first 8s) |
| `card_payment_result_text` | `_card_payment_result_user_text` | User-facing approved/pending/rejected message |
| `build_card_payment_instruction` | buy flow display | Card-to-card panel with TX marker support |
| `require_last4_enabled` | `_get_payment_settings()` | Per-tenant 4-digit card verification flag |

### Architecture notes

- All global singletons (`userbot_db`, `ADMIN_ID`, `ADMIN_BOT_TOKEN`) replaced with per-tenant `business` object
- AdminBot token resolved via `_sibling_admin_bot_token(business)` from `handlers.py`
- Admin chat ID via `business.owner_telegram_id`
- Media archived via `business.attach_payment_receipt_media()`
- Receipt submitted via `business.submit_receipt()` / `business.submit_wallet_topup_receipt()`

---

## v0.1.0-parity-phase1 – Core Utilities

**Commit:** 053c7ada  
**File:** `TenantRuntime/UserBot/sellbot_utils.py`

### Added (25 utility functions)

`_toman_to_irt`, `_irt_to_toman`, `_humanize_bytes`, `_fmt_toman`,
`_format_subscription_period`, `_get_plan_emoji`, `_get_status_emoji`,
`_get_payment_method_emoji`, `_get_delivery_emoji`,
`_apply_random_tx_marker`, `_validate_tx_marker`,
`_parse_exact_card_last4`, `_build_card_to_card_payment_text`,
`_card_payment_result_user_text`, `_get_payment_settings`,
`_parse_plan_traffic_gb`, `_plan_display_name`, `_plan_short_description`,
`_order_status_label`, `_payment_status_label`,
`_subscription_status_label`, `_safe_int`, `_safe_float`,
`_chunk_list`, `_flatten`

---

## Upcoming

- **v0.5.0** – `_direct_buy_delivery_loop` + `_process_approved_direct_buy_payments`
- **v0.6.0** – QR delivery, config delivery, sub HTTP server
- **v0.7.0** – Force join, event channel
- **handlers.py** – BTN_BUY header text update + optional «یک اشتراک همه لوکیشن‌ها» / «مولتی‌سرور هوشمند» buttons
