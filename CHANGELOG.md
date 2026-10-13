# Changelog

All notable changes to Hiddify-WhiteLabel are documented here.

---

## [v1.3.5] – 2026-10-13

### Changed (TenantRuntime marketing: exact SellBot parity — buttons, texts, behavior)

**Files:** `TenantRuntime/AdminBot/userbot_management.py`, `TenantRuntime/business.py`, `TenantRuntime/UserBot/handlers.py`

| # | تغییر | قبل | جدید (مثل SellBot) |
|---|---|---|---|
| 1 | ردیف‌های منو | `کد تخفیف`/`نمایش دکمه هدیه`/`نمایش وضعیت` + مدیریتی‌ها | بلوک دقیق SellBot اول با همان ترتیب: `اعمال کد تخفیف`، `اعمال کد افزایشی`، `دکمه هدیه`، `نمایش وضعیت کاربر`، `استفاده آنی هدیه کوپن`، `تنظیم متن هدایای اتوماتیک`، `حداقل شارژ هدیه اتوماتیک`؛ ردیف‌های مدیریتی WL بعدش می‌مانند |
| 2 | کلیدهای جدید | وجود نداشت | `enable_increase_code`، `instant_gift_coupon`، `auto_gift_text`، `min_auto_gift_charge` با همان دیفالت‌های SellBot |
| 3 | گیت کوپن در خرید | فقط `enable_discount_code` | `discount OR increase` مثل SellBot (دکمه و هندلر `shop:coupon`) |
| 4 | ویزاردها | — | پرامپت/خطا/تأیید دقیق SellBot؛ حداقل شارژ صفر را هم قبول می‌کند |
| 5 | رفتار instant/auto | — | مثل SellBot فقط ذخیره/تنظیم است (در SellBot هم رفتار زنده ندارد) |
| 6 | صفحه ترکیبی خرید | لوکیشن مستقیم به بیلدر می‌رفت و لیست ثابت دیده نمی‌شد | بیلدر + ردیف `📋 پلن های آماده` زیر دکمه خرید + `🔙 بازگشت`؛ صفحه پلن‌های آماده تخت با گارد حالت و هشدارهای دقیق SellBot |
| 7 | تیتر بیلدر ترکیبی | `📦بسته مورد نیاز...` | دقیق SellBot: `🛒 خرید اشتراک` + `🎛 بسته دلخواه خود را بسازید یا از پلن‌های آماده استفاده کنید` |

---

## [v1.3.4] – 2026-10-13

### Changed (TenantRuntime buy_renew + plans: exact SellBot parity)

**Files:** `TenantRuntime/AdminBot/userbot_management.py`, `TenantRuntime/AdminBot/server_actions.py`, `TenantRuntime/business.py`, `TenantRuntime/UserBot/handlers.py`

| # | تغییر | قبل | جدید (مثل SellBot) |
|---|---|---|---|
| 1 | هدر خرید و تمدید | عنوان + خط سیاست تمدید | فقط `🛒تنظیمات خرید و تمدید` |
| 2 | منوی ستون پلن‌ها | عمودی (`1 ستون`/`2 ستون`/`3 ستون`) | افقی تک‌ردیف (`✅ ｜ یک`/`دو`)؛ فقط ۱/۲ مثل SellBot |
| 3 | منوی ستون سرورها | عمودی + هدر جدا | افقی تک‌ردیف (`یک`/`دو`/`سه`) + همان هدر بخش |
| 4 | کال‌بک ستون‌ها | `:plan_columns:1` | `:plan_columns:set:1` (مسیر قدیمی سازگار ماند)؛ بعد از ذخیره همان منو تازه می‌شود |
| 5 | دکمه‌های حد تمدید | با مقدار (`｜ N روز`) | بدون مقدار؛ خط آخر `مصرف` به‌جای `حجم` |
| 6 | پرامپت‌های حد/نامحدود | لحن اختصاصی | متن دقیق SellBot (`📝 تعیین کنید...`، `♾ لطفاً حداقل مقدار...`) |
| 7 | پیام ذخیره عددی | عمومی (`تنظیم تمدید ذخیره شد`) | جداگانه برای هر کلید مثل SellBot + اعتبارسنجی داخل ویزارد |
| 8 | تغییر حالت ثابت/ترکیبی/پویا | باگ گزارش‌شده: بدون تغییر ظاهری | سوئیچ سالم است (تست شد)؛ مشکل واقعی خالی بودن کاتالوگ ثابت بود |
| 9 | پلن نمونه | وجود نداشت (فروشگاه در حالت ثابت خالی) | دکمه `✨ ساخت پلن‌های نمونه` (یک‌ماهه/سه‌ماهه + ۳ پلن با اعداد SellBot) فقط وقتی کاتالوگ خالی است؛ idempotent |
| 10 | متن خالی فروشگاه | `پلنی موجود نیست` | دقیق SellBot: `❌ پلن ثابت برای این سرور یافت نشد.` / `❌ دسته‌بندی برای این سرور یافت نشد.` |
| 11 | لیست دسته‌ها (خالی) | فقط عنوان | راهنمای SellBot وقتی دسته‌ای ثبت نشده |
| 12 | حالت ترکیبی در فروشگاه | لوکیشن مستقیم به بیلدر پویا می‌رفت؛ لیست ثابت هیچ‌وقت دیده نمی‌شد | صفحه ترکیبی SellBot: گزینه پویا اول + لیست ثابت؛ هدر `بسته دلخواه خود را بسازید یا از پلن‌های آماده استفاده کنید`؛ در تمدید هم همین ترتیب |

---

## [v1.3.3] – 2026-10-13

### Changed (TenantRuntime tx_plans: exact SellBot parity — buttons, texts, behavior)

**Files:** `TenantRuntime/AdminBot/userbot_management.py`, `TenantRuntime/business.py`, `TenantRuntime/UserBot/handlers.py`

| # | تغییر | قبل | جدید (مثل SellBot) |
|---|---|---|---|
| 1 | منوی بخش | ۷ ردیف اختصاصی (تاگل دسته‌بندی، مدیریت دسته‌ها، اولویت دستی، ستون‌ها...) | دقیقاً ۵ ردیف SellBot: مشخصه تصادفی، حداقل تراکنش، دسته‌بندی، ترتیب، بازگشت |
| 2 | مشخصه تصادفی تراکنش | فقط در منوی کارت | تاگل در همین بخش (`tx_plans:random_tx_spec`) + همان رفتار در پرداخت کارتی |
| 3 | حداقل تراکنش | وجود نداشت | کلید `min_transaction_toman` (پیش‌فرض ۱۰٬۰۰۰) + ویزارد با متن‌های دقیق + اعمال در شارژ کیف پول |
| 4 | دسته‌بندی پلن‌ها | تاگل مستقیم + مدیر داخلی | منوی حالت SellBot (`ساده` / `دسته‌بندی`) روی همان کلید؛ مدیریت دسته‌ها در بخش پلن‌ها (`srv:categories`) می‌ماند |
| 5 | ترتیب پلن‌ها | مدل `id/price_asc/...` + جهت چسبیده | مدل SellBot (`price/gb/days` + `نزولی/صعودی`)؛ مقادیر قدیمی خودکار مایگریت می‌شوند |
| 6 | چیدمان نزولی | بدون mirror | سطرهای چندستونه در حالت نزولی mirror می‌شوند (RTL) مثل SellBot |

---

## [v1.3.2] – 2026-10-13

### Fixed (TenantRuntime admin user list: SellBot parity for expired/online/last-seen)

**Files:** `TenantRuntime/hiddify.py`, `TenantRuntime/server_admin.py`, `TenantRuntime/AdminBot/server_actions.py`, `TenantRuntime/AdminBot/handlers.py`, `TenantRuntime/business.py`

| # | مشکل | علت | رفتار جدید |
|---|---|---|---|
| 1 | `🔵 آنلاین` همیشه ۰ | پنل Hiddify آخرین اتصال را به ساعت محلی تهران بدون آفست می‌دهد ولی کد آن را UTC فرض می‌کرد؛ پنجره آنلاین هیچ‌وقت نمی‌خورد | نرمال‌سازی به UTC در مرز آداپتر؛ کاربر فعال در ~۹۰ ثانیه آنلاین نشان داده می‌شود |
| 2 | `📶آخرین اتصال` غلط (مثلاً «۲۰ ساعت پیش» برای فعالیت چند دقیقه قبل) | همان شیفت ‎+۳:۳۰ در محاسبه نسبی | نمایش نسبی درست («۹ دقیقه پیش» و...) |
| 3 | `🔴 منقضی شده` همیشه ۰ ولی کاربران غیرفعال پنل جدا شمرده می‌شدند | SellBot کاربر غیرفعال پنل Hiddify را منقضی حساب می‌کند | `user_status` با provider: غیرفعال Hiddify/X-UI → منقضی؛ X-NET مثل SellBot غیرفعال می‌ماند؛ فعال‌سازی کاربر منقضی نیاز به تمدید دارد |
| 4 | کرش ریفرش با `OverflowError` برای کاربران هیچ‌وقت‌متصل‌نشده | پنل برای آن‌ها `0001-01-01` برمی‌گرداند و تبدیل منطقه زمانی سرریز می‌کرد | مقادیر نگهبان/نامعتبر به «نامشخص» تبدیل می‌شوند و ریفرش کامل می‌شود |
| 5 | تاپ روی دکمه گاهی هیچ‌چیز نشان نمی‌داد (مرگ خاموش) | خطای ویرایش پیام (حذف‌شده/اختلال شبکه) گرفته نمی‌شد | در صورت شکست ویرایش، همان محتوا به‌صورت پیام جدید ارسال می‌شود |

---

## [v1.3.1] – 2026-10-12

### Added (MasterBot: full-backup settings section, owner-only)

**Files:** `MasterBot/views.py`, `MasterBot/handlers.py`, `MasterBot/customer_service.py`, `MasterBot/platform_settings.py`, `MasterBot/main.py`

- دکمه `🗄 بکاپ کامل` در `⚙️ تنظیمات` (فقط ادمین مستر؛ مشتری‌ها این بخش را نمی‌بینند)
- صفحه بکاپ با چیدمان ۳ردیفه: ردیف ۱ `📤 ارسال خودکار` تمام‌عرض، ردیف ۲ `📦 بکاپ فوری` + `📥 بازیابی بکاپ` کنار هم، ردیف ۳ `↩️ بازگشت` تمام‌عرض
- بکاپ فوری: ساخت `.wlbak` رمزنگاری‌شده (دیتابیس کامل شامل لایسنس‌ها و مشتری‌ها + `.env`) و ارسال به DM مالک؛ رمز از `BACKUP_PASSPHRASE` یا ورودی محرمانه (حذف فوری پیام)
- ارسال خودکار: تاگل ذخیره‌شده + جاب روزانه (نگهداری ۷ نسخه)؛ بدون `BACKUP_PASSPHRASE` جاب رد می‌شود
- بازیابی: آپلود `.wlbak` + رمز، فقط بررسی اعتبار و نمایش مشخصات + دستور ترمینال ریستور (بازیابی زنده از تلگرام انجام نمی‌شود)
- دکمه `🔐 تنظیم رمز بکاپ`: تعیین/تغییر رمز از داخل ربات (ذخیره اتمیک در `.env` سرور + آدیت بدون رمز)؛ بکاپ فوری، ارسال خودکار و بازیابی با همان رمز کار می‌کنند؛ آپلود بکاپ با رمز ذخیره‌شده خودکار بررسی می‌شود
- تنظیمات جدید `auto_backup_enabled` / `backup_last_at` + آدیت `platform_backup.create` / `platform_backup.auto_toggle`
- صفحه بکاپ وضعیت رمز سرور (`BACKUP_PASSPHRASE`) را نشان می‌دهد و اگر ارسال خودکار بدون رمز فعال باشد هشدار می‌دهد
- دوره ارسال خودکار قابل‌تنظیم (هر ۶ / ۱۲ / ۲۴ ساعت، پیش‌فرض ۱۲) با دکمه‌های انتخاب در همان صفحه؛ جاب ساعتی سررسید را چک می‌کند و ۱۴ نسخه آخر را نگه می‌دارد

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
