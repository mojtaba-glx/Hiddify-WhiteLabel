# Changelog

All notable changes to Hiddify-WhiteLabel are documented here.

---

## [v1.2.5] – 2026-10-05

### Changed (SellBot-parity purchase flow)

**File:** `TenantRuntime/UserBot/dynamic_plans.py`  
**Commit:** `9e9868fe54585a33953d09848049f4f3d871bd21`

| # | تغییر | قبل | جدید |
|---|---|---|---|
| 1 | ردیرکت لوکیشن | لیست پلن (مرحله اضافه) | مستقیم به plan builder |
| 2 | نمایش قیمت | `IRR 30,000` | `130,000 تومان` |
| 3 | دکمه confirm | تایید و پرداخت | تایید و خرید |
| 4 | بعد از confirm | مستقیم checkout | خلاصه پلن + انتخاب روش پرداخت |
| 5 | دکمه بازگشت | — | برمی‌گردده به لیست لوکیشن |

### Added

- `_display_price()` — تبدیل IRR ÷ 10 → تومان
- `_open_plan_builder()` — اینیشیالایز و رندر plan builder
- `_render_builder()` — نمایش SellBot-style keyboard
- `shop:buy` handler — نمایش دوباره لیست سرورها (برای دکمه بازگشت)
- `shop:buyloc:{sid}` intercept — بدون مردودیر به plan list

---

## [v1.2.4] – base branch

`feature/userbot-buy-sellbot-parity-v1.2.4`

### Added (previous phases on this branch)

| فایل | commit | توضیح |
|---|---|---|
| `sellbot_utils.py` | `053c7ada` | 25 utility function (SellBot parity) |
| `card_payment.py` | `a87ab9fc` | Card + wallet payment flow |

---

## Upcoming

- `handlers.py` — تغییر هدر BTN_BUY + دکمه‌های «یک اشتراک همه لوکیشن‌ها» / «مولتی‌سرور هوشمند»
- QR دلیوری و config delivery
- Force join و event channel
