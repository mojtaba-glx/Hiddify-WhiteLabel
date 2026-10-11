# Changelog

All notable changes to Hiddify-WhiteLabel are documented here.

---

## [v1.3.1] – 2026-10-12

### Added (MasterBot: full-backup settings section, owner-only)

**Files:** `MasterBot/views.py`, `MasterBot/handlers.py`, `MasterBot/customer_service.py`, `MasterBot/platform_settings.py`, `MasterBot/main.py`

- دکمه `🗄 بکاپ کامل` در `⚙️ تنظیمات` (فقط ادمین مستر؛ مشتری‌ها این بخش را نمی‌بینند)
- صفحه بکاپ با چیدمان ۳ردیفه: ردیف ۱ `📤 ارسال خودکار` تمام‌عرض، ردیف ۲ `📦 بکاپ فوری` + `📥 بازیابی بکاپ` کنار هم، ردیف ۳ `↩️ بازگشت` تمام‌عرض
- بکاپ فوری: ساخت `.wlbak` رمزنگاری‌شده (دیتابیس کامل شامل لایسنس‌ها و مشتری‌ها + `.env`) و ارسال به DM مالک؛ رمز از `BACKUP_PASSPHRASE` یا ورودی محرمانه (حذف فوری پیام)
- ارسال خودکار: تاگل ذخیره‌شده + جاب روزانه (نگهداری ۷ نسخه)؛ بدون `BACKUP_PASSPHRASE` جاب رد می‌شود
- بازیابی: آپلود `.wlbak` + رمز، فقط بررسی اعتبار و نمایش مشخصات + دستور ترمینال ریستور (بازیابی زنده از تلگرام انجام نمی‌شود)
- تنظیمات جدید `auto_backup_enabled` / `backup_last_at` + آدیت `platform_backup.create` / `platform_backup.auto_toggle`

---

## [v1.3.0] – 2026-10-11

### Changed (SellBot-parity: subscription detail text, panel note, vpn names, missing-service hiding)

**Files:** `TenantRuntime/AdminBot/handlers.py`, `TenantRuntime/AdminBot/userbot_management.py`, `TenantRuntime/AdminBot/server_actions.py`, `TenantRuntime/UserBot/handlers.py`, `TenantRuntime/business.py`, `TenantRuntime/panels.py`, `TenantRuntime/hiddify.py`, `TenantRuntime/xui.py`, `TenantRuntime/xnet.py`

| # | تغییر | قبل | جدید |
|---|---|---|---|
| 1 | متن جزئیات اشتراک ادمین | مصرف `0.00 از 20.00`، آخرین اتصال خام، یادداشت همیشه `—`، انقضای floor | مصرف `0.0 از 20.0`، آخرین اتصال نسبی (`22 ساعت پیش`) + `آنلاین`، یادداشت واقعی پنل، انقضای تقویمی مثل SellBot |
| 2 | یادداشت پنل هنگام ساخت | فقط `WhiteLabel tenant=.. subscription=..` | `HiddifyBot:{telegram_id} \| WhiteLabel tenant=.. subscription=..` (انگلیسی روی پنل، فارسی در نمایش) |
| 3 | نام پنل X-UI | همیشه `wl-t-s` (نادیده گرفتن اسم سرویس) | اسم سرویس `vpn-######` مثل Hiddify/X-NET؛ fallback قدیمی فقط وقتی اسم خالی است |
| 4 | اشتراک حذف‌شده در UserBot | دکمه مستقیم وضعیت، اشتراک حذف‌شده را نشان می‌داد | مخفی + streak مثل SellBot؛ پاک‌سازی از دیتابیس بعد از ۷ روز، بازگشت خودکار اگر روی پنل پیدا شود |

### Fixed

- `shop:substatus` مستقیم و لیست `shop:subs` حالا از همان فیلتر `missing/unreachable` استفاده می‌کنند
- تست `test_missing_service_sweep_phase19` آداپتر صحیح را به سرویس تزریق می‌کند (به‌جای monkeypatch بی‌اثر)

---

## [v1.2.7] – 2026-10-09

### Changed (SellBot-parity: dynamic plan builder + invite menu)

**Files:** `TenantRuntime/UserBot/dynamic_plans.py`, `TenantRuntime/UserBot/handlers.py`

| # | تغییر | قبل | جدید |
|---|---|---|---|
| 1 | کیبورد پنل پویا | `➖ حجم` / `➖ مدت` در یک ردیف، بدون label | ردیف جداگانه `📊 حجم` و `⏳ زمان` + ردیف تخفیف/قیمت |
| 2 | دکمه‌های کم/زیاد | `➖ حجم` / `➕ حجم` | `➖` / `➕` ساده |
| 3 | نمایش مدت | `X ماه` | `X ماهه` |
| 4 | دکمه تایید | `✅ تایید و خرید` | `💳 تایید و خرید` |
| 5 | متن هدر builder | عنوان + حجم/زمان/قیمت | فقط `📦بسته مورد نیاز خود را جهت خرید تنظیم کنید` |
| 6 | دکمه منوی اصلی | `🎁دریافت هدیه` | `💌دعوت دوستان` (مثل SellBot) |
| 7 | کیبورد دعوت دوستان | بنر دعوت + کیف پول | ۵ دکمه SellBot: لینک دعوت / جوایز / دعوت‌ها / آمار / تاریخچه |

### Added

- `_invite_home_markup()` — کیبورد ۵ دکمه‌ای دعوت دوستان (SellBot parity)
- `shop:inviterewards` / `shop:invitelist` / `shop:invitestats` / `shop:invitehistory` handlers
- سازگاری: `show_gift_button` قدیمی همچنان دکمه دعوت دوستان را نمایش می‌دهد

### Removed

- `BTN_GIFT` (`🎁دریافت هدیه`) از منوی اصلی و هندلر آن — `shop:gift` داخلی کیف پول دست‌نخورده ماند

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
