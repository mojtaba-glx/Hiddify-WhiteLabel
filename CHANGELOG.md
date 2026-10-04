# Changelog

## v1.2.4 — UserBot purchase flow SellBot parity

- مسیر «💳خرید اشتراک» ربات کاربران از کد واقعی SellBot به‌صورت location-first هماهنگ شد: ابتدا لوکیشن، سپس دسته‌بندی/پلن.
- پلن انتخاب‌شده از همان لوکیشن وارد Checkout می‌شود و دیگر مرحلهٔ تکراری انتخاب سرور نمایش داده نمی‌شود.
- پلن‌های ثابت بر اساس لوکیشن انتخاب‌شده فیلتر می‌شوند و callback شامل server_id می‌شود تا انتخاب کاربر حفظ شود.
- پلن‌های پویا نیز فقط برای لوکیشن انتخاب‌شده نمایش داده می‌شوند.
- مسیرهای قدیمی shop:buycat:* و shop:plan:* برای پیام‌های قدیمی backward-compatible باقی ماندند.
- تست‌های parity برای ترتیب مراحل و callbackهای خرید اضافه شد.

# Changelog

## v1.2.3 — UserBot free-trial provisioning fix

- اصلاح مسیر «🔥تست رایگان» ربات کاربران بر اساس رفتار واقعی SellBot.
- ایجاد موفق کاربر در Hiddify دیگر با خطای PATCH تثبیت وضعیت/Template State به‌اشتباه ناموفق اعلام نمی‌شود.
- اگر Hiddify بعد از POST موفق، کاربر را موقتاً در GET نشان ندهد، ساخت تست رایگان شکست کاذب نمی‌خورد.
- تست رگرسیون برای همین سناریوی Hiddify اضافه شد.
- rollback و کنترل مصرف تست رایگان قبلی همچنان حفظ می‌شود.

## v1.2.2 — SellBot plan-management parity

- بازطراحی مسیر «مدیریت سرورها → پلن‌ها» در AdminBot بر اساس کد واقعی Hiddify-SellBot.
- اصلاح ترتیب، متن و نمایش شرطی دکمه‌های پلن ثابت، پویا و ترکیبی.
- بازگردانی منوی دقیق «تنظیمات پلن‌ها» و انتخاب سه‌حالته ثابت/پویا/ترکیبی با وضعیت ✅/❌.
- هماهنگ‌سازی تنظیم پلن پویا با قیمت هر گیگ، قیمت هر ماه، بازه حجم و بازه ماه.
- انتقال رفتار مدیریت تخفیف حجمی/پلاکانی، روشن/خاموش، ویرایش و تایمر ساعتی از SellBot.
- حفظ سازگاری تنظیمات قدیمی روزمحور برای Tenantهای موجود و استفاده از مدل ماه‌محور برای تنظیمات جدید SellBot-style.
- افزودن تست‌های رگرسیون اختصاصی برای منوها، قیمت‌گذاری پویا و تخفیف‌ها.

## v1.1.0 — Complete Admin backup

- Completed the Tenant AdminBot `📫 دریافت بکاپ` flow instead of returning the previous placeholder.
- Kept the existing Tenant Backup/Restore v2 payload restore-compatible while extending manual full backups with per-panel artifacts under `PanelBackups/`.
- Added real Hiddify v11/v12/v13 backup downloads using the proven backup routes and binary-response validation.
- Added real X-UI Sanaei/Alireza database downloads through `server/getDb`.
- Added X-NET backup create/download/best-effort cleanup with persistent-token and JWT-fallback authentication.
- Kept manual backups strictly Tenant-scoped: other tenants, platform settings, live Tenant AdminBot/UserBot credentials and the platform master key are excluded.
- Preserved encrypted Tenant panel credentials inside Tenant business data without exposing their decryption key.
- Added a full-backup manifest with panel counts, checksums, errors and archive paths; partial panel failures no longer prevent delivery of the safe Tenant backup.
- Manual backup runs are recorded in `tenant_backup_runs`, and delivery success/failure is finalized only after Telegram send completes.
- Version promoted to `1.1.0`.

## v1.0.0 — Stable release

- Promoted the completed WhiteLabel implementation to the first Stable release after the Phase 16 final release gate.
- Added a cross-phase end-to-end release test covering multi-node purchase, paid renewal, expiry enforcement, AdminBot/UserBot support tickets, broadcast audit, full Tenant backup/restore and cross-Tenant isolation in one continuous flow.
- Added automatic inline-callback coverage across AdminBot, UserBot, management and broadcast handlers so directly rendered buttons cannot ship without a registered callback route.
- Revalidated the AdminBot ↔ UserBot two-role Tenant runtime contract while keeping future AgentBot/CustomerBot infrastructure dormant until its dedicated implementation phase.
- Verified the complete Tenant feature set: Hiddify/X-UI/X-NET server boundaries, multi-node smart subscriptions, customer status/config actions, renewal/expiry, payments, wallet/coupons/referrals/trials, tickets, broadcast/channel management, Force Join/event channels and Tenant Backup/Restore v2.
- Fixed Stable release metadata so README, VERSION and migration documentation agree on v1.0.0 and migration 0031.
- Final release gate: 688 tests passed on both Python 3.10 and Python 3.12, followed by successful Release Drill, install.sh syntax check and bootstrap.sh syntax check.

## v0.50.0 — Broadcast and channel management parity

- Finalized Tenant AdminBot broadcast delivery using Hiddify-SellBot targeting semantics for all users, no-order users and expired cohorts through 1/2/4/8-week windows.
- Added SellBot-style live audience statistics, tenant-safe target selection and detailed delivery results for successful, failed, recovered, unreachable, temporary and Telegram errors.
- Completed broadcast authoring for text, photo and video with up to eight URL buttons, preview, independent editing, full replacement, confirmation-by-publish and safe cancellation.
- Bridged AdminBot-owned media to the sibling UserBot token by downloading once and reusing UserBot-owned Telegram file IDs, with RetryAfter/network retry handling and safe delivery pacing.
- Added long-caption handling so media over Telegram's caption limit is delivered first and the formatted text/buttons are sent separately instead of failing.
- Completed channel management for text/photo/video posts, buttons, preview, edit text/media, replace while preserving buttons, channel target configuration and graceful publish failures.
- Added tenant-scoped broadcast audit details and migration `0030_broadcast_channel_phase13`, including video/button/error counters.
- Added regression coverage for SellBot segmentation semantics, tenant isolation, media token bridging, video/buttons, audit counts, channel target validation and edit preservation.

## v0.40.0 — Tenant tickets and support parity

- Rebuilt Tenant support tickets around a threaded message model while preserving and backfilling legacy ticket body/admin replies.
- Added SellBot-style UserBot ticket creation: subject, full message, optional screenshot, preview, edit, send and cancel.
- Added UserBot ticket list/detail, threaded replies, optional reply screenshots, attachment viewing and user-side close flow.
- Added AdminBot pending/open/closed ticket buckets, threaded detail view, text/photo reply preview, edit, send, close and reopen controls.
- Stored ticket images as Tenant-scoped media so separate AdminBot/UserBot tokens can review and deliver attachments reliably.
- Kept `ticket_panel_text` as the real support-panel text source and restored SellBot-style FAQ / my tickets / create ticket navigation.
- Added migration `0028_ticket_phase12` and regression coverage for message/media lifecycle, status transitions, tenant isolation and UI wiring.

## v0.32.0 — Complete customer subscription status

- Added a paginated subscription selector and separate live status details with service names, recorded purchase/renewal prices, remaining quota/time and last connection.
- Completed customer config, renewal, rename, confirmed link change, refresh, copyable ID and connection-help controls; subscription links include QR images and copyable text.
- Added resumable credential rotation across Hiddify, Sanaei/Alireza X-UI and X-NET, invalidating previous smart-link tokens while preserving subscription terms and consumption.
- Added customer/tenant ownership checks, duplicate-confirmation protection, cached-status notices during outages and preflight rejection of unsupported WireGuard rotation.
- Added migration `0027_subscription_status` and 17 dedicated regression tests; the complete suite passes 602 tests with CI on Python 3.10 and 3.12.

## v0.30.0 — Tenant payment architecture

- Unified Tenant card-to-card, Crypto, wallet checkout and wallet-topup payment status under one payment history/review model.
- Added a provider registry and generic payment-start contract so future gateways can return receipt, external-URL or completed actions without provider-specific UserBot branches.
- Added provider metadata, priority and safe method editing/removal while preserving old payment receipts and installed databases.
- Added unified AdminBot approve/reject handling for both order receipts and wallet-topup receipts, with review notes and payment audit events.
- Added Tenant-scoped receipt-media archival so photos submitted through UserBot remain reviewable from the separate AdminBot token.
- Added UserBot payment-status history for manual receipts and direct wallet payments without changing the persistent main-menu layout.
- Added migration `0026_payment_phase11` and regression coverage for provider extension, wallet review, payment status, receipt media and safe method lifecycle.

## v0.20.1 — Server connection parity

- Completed tenant server connection controls using Hiddify-SellBot `AdminBot/servers.py` and its provider-specific authentication flows as the reference.
- Added read-only protected API probes for Hiddify v11/v12/v13, X-UI Sanaei/Alireza and X-NET; new setup and connection edits save only after successful verification.
- Added Sanaei cookie login and token rejection fallback, with per-inbound creation, renewal, state and deletion paths; state edits preserve usage and quotas.
- Added X-NET token/fallback login edits, optional internal management API, subscription port/path controls, inbound discovery and a working default-server action.
- Added full connection reconfiguration for incomplete existing server records, preserving server IDs and mapped subscriptions.
- Kept secret edits encrypted and atomic, preserved working settings after failures, scoped JWT caches to credential material, and restored cancellation/back navigation.
- Added migration `0025_server_connection_parity` and regression tests for all four setup flows, read-only probes, failures/retries, credential rotation, tenant isolation and cookie-auth lifecycle.

## v0.20.0 — Referral parity

- Completed referral management across Tenant AdminBot and UserBot using the proven Hiddify-SellBot behavior.
- Added independent enable/disable switches for trial and first-purchase rewards.
- Added editable referral invite text with live placeholders for invite link, reward values and referral count.
- Corrected the successful-referral cap so trial and purchase reward quotas are enforced independently.
- Added tenant-scoped manual referral rewards that credit the wallet and remain visible in referral reports and UserBot wallet history.
- Expanded referral dashboard, invitation list and reward list reporting without exposing dead controls.
- Added migration `0024_referral_phase10` and regression coverage for reward switches, caps, invite text and manual rewards.


## v0.10.4 — Search flow UI parity fix

- Fixed Smart User Search to match Hiddify-SellBot exactly: the old inline menu is removed, the prompt is sent as a new message, and the bottom reply keyboard becomes a single `❌ لغو` button.
- Restored the exact Hiddify-SellBot smart-search prompt: `نام کاربر، UUID یا لینک کانفیگ را ارسال کنید.`
- Smart-search cancellation now returns `❌ جستجو لغو شد.` and restores the main admin keyboard.
- Subscription-tracking text input now also uses the same bottom cancel keyboard.
- Added regression coverage so the incorrect inline-only prompt cannot return.

## v0.10.3 — Admin user search and daily report parity

- Rebuilt Tenant AdminBot user search around the Hiddify-SellBot search menu: smart search, subscription tracking, expired users, expired subscriptions and old/suspicious records.
- Smart search now resolves real tenant subscriptions by customer name, username, Telegram ID, Customer ID, subscription ID, order ID and panel external reference.
- Added SellBot-style search result paging and subscription detail actions for configs, editing, renewal, deletion and customer profile.
- Added working subscription edit controls for enable/disable, usage reset, traffic change, duration reset and duration change across all mapped nodes.
- Added tenant-scoped previous-day accounting matching SellBot daily-report semantics while avoiding double-counting wallet spend as new cash.
- Added regression coverage for search resolution, expired lists, subscription edit/link actions and daily accounting.

## v0.10.2 — Tenant AdminBot server management parity

- Rebuilt the Tenant AdminBot server list and server-detail screens to follow the Hiddify-SellBot AdminBot labels, layout and navigation.
- Added a step-by-step server wizard for Hiddify, X-UI Sanaei/Alireza and X-NET with bottom-keyboard cancellation and encrypted credential storage.
- Added working server edit, safe delete, capacity and priority controls.
- Added server-scoped user listing, user sync/enable/disable/delete-panel actions, search, plan view, subscription-domain view, frozen-node report and server sync.
- Added explicit parent-server relations for Multi-node so nodes can belong to the same main server model used by Hiddify-SellBot; legacy NULL-parent nodes remain compatible.
- Added working node add/remove flows from the selected server.
- Added regression tests for tenant-scoped server CRUD/topology and the exact SellBot server-menu labels.

## v0.10.1 — Tenant AdminBot menu parity, stage 1

- Rebuilt the Tenant AdminBot main menu as a persistent Telegram reply keyboard matching Hiddify-SellBot's proven AdminBot layout.
- Restored the same main labels and row order for server management, user search, daily report, UserBot management, server status, agency and backup.
- Main-menu button presses now cancel stale text flows before navigation, following SellBot's state-handling pattern.
- Wired existing WhiteLabel server management, customer search, daily report and status capabilities into the new main menu.
- Kept agency and tenant-scoped backup explicitly isolated for later staged ports instead of exposing incomplete or cross-tenant behavior.
- Added regression coverage for the exact AdminBot main-menu layout.

## v0.10.1 — License Assignment Picker

- Replaced manual Telegram-ID entry during license creation with explicit tenant selection buttons.
- Tenant buttons display the internal tenant ID and name, so multiple customer bots may safely share the same Telegram owner ID.
- After choosing a tenant, only active license plans are shown as buttons; the selected plan then proceeds to the grace-period step.
- Added regression coverage for two tenants sharing the same owner Telegram ID and issuing a license to the intended tenant.
- Promoted versioning from release-candidate suffixes to normal patch releases.

## v0.10.0-rc.3 — Guided Flow Input Fixes

- License creation now resolves the customer/shop using the owner's numeric Telegram ID instead of incorrectly treating that value as the internal tenant database ID.
- Guided tenant, provisioning, plan and license creation now show `❌ لغو` in Telegram's bottom reply keyboard during every step.
- Cancelling a guided flow clears its state, hides the bottom keyboard and returns to the correct management menu.
- Successful guided creation removes the temporary reply keyboard before rendering the resulting detail view.
- Added regression coverage for Telegram-owner lookup, ambiguous owner IDs, bottom-keyboard cancellation and license creation by owner Telegram ID.

## v0.10.0-rc.2 — Update Path Hardening

- Changed tenant, plan and license creation in MasterBot to guided step-by-step flows with cancellation.
- Fixed fresh-install Git dirtiness by tracking `bootstrap.sh` as executable.
- Replaced the full pytest run during production updates with a fast syntax/compile/release-plan preflight; the complete suite remains in GitHub Actions.
- Added requirements hashing so unchanged dependencies skip redundant pip installation.
- Fixed update snapshot tests to use the active virtual-environment interpreter instead of the system `python3`.
- No-op updates now run health validation instead of performing a full repair/restart cycle.

## v0.10.0-rc.1 — Live Staging Release Candidate

- Promoted the completed offline implementation to a traceable release candidate for real-panel staging.
- Added live provider support for Hiddify Manager, X-UI Sanaei/Alireza and X-NET.
- Added renewal, expiry, usage/last-online synchronization, multi-node provisioning and managed smart subscriptions.
- Added reminder/global enforcement, tenant reports and AdminBot/UserBot operational flows.
- Added wallet, coupons, referrals and one-time free trials with tenant-scoped transactional safeguards.
- Added the deterministic offline release drill and the real-panel staged acceptance/rollback runbook.
- Final `v0.10.0` remains gated on the same candidate passing Hiddify, X-UI, X-NET, multi-node and rollback staging checks.

## v0.9.3 — Installer Rollback Hardening

- Added private pre-update SQLite + `.env` snapshots using SQLite's backup API.
- Failed migrations, service startup or health validation now restore database, environment and previous source version together.
- SQLite WAL/SHM sidecars from a failed candidate are removed before rollback restore.
- Master admin ID changes now use automatic `.env` rollback on service failure.
- Added display-timezone management and a non-secret settings summary to the terminal manager.
- Added dedicated regression tests for real update snapshot create/restore behavior.

## v0.9.2 — Easy Installer and Operations Manager

- Added one-line Ubuntu/Debian bootstrap installation from GitHub.
- Added a dedicated unprivileged `whitelabel` system account and default install path under `/opt/hiddify-whitelabel`.
- Added the permanent `sudo whitelabel` terminal manager command.
- Added GitHub update flow with dependency refresh, full offline test run before downtime, migrations, systemd refresh, restart and health validation.
- Added Install/Repair, Start/Stop/Restart, status, logs, health, backup, restore and migration actions.
- Added Settings submenu for MasterBot token, Master admin Telegram ID and Runtime shard count.
- MasterBot tokens are verified with Telegram `getMe` before saving.
- Sensitive token/config values are passed to helper scripts through stdin, not command-line arguments.
- Added guarded full uninstall with exact `DELETE ALL` confirmation plus service-only removal that preserves data.
- Restored full systemd enabled/active state rollback and added shard-change rollback.
- Added safe bootstrap and installer dry-run modes.
- Added direct Bash syntax checks for both installer entry points.
- Expanded installer regression coverage to 341 tests on Python 3.10 and 3.12.

## v0.9.1 — MasterBot Completion

- Completed the owner-only MasterBot control panel and separated it from the customer storefront.
- Added persistent storefront settings, sales/trial switches, support info and deterministic trial-plan selection.
- Added customer administration, blocking/unblocking, wallet adjustment with audit logging and customer financial history.
- Added full order administration, payment receipt history, search/filter flows and accurate financial reporting.
- Fixed wallet-paid renewal fulfillment and added immutable `paid_at` timestamps.
- Improved card/crypto payment method management and receipt approval/rejection flows with rejection notes.
- Added immediate Master notifications for new receipts and completed customer provisioning.
- Simplified customer onboarding to shop name + two BotFather tokens; internal slug generation is automatic.
- Hid internal webhook secrets from customer-facing flows.
- Added safe unpaid-order cancellation and localized customer/master status displays.
- Added migrations `0007_platform_settings` and `0008_order_paid_at`.
- Added end-to-end purchase-to-provisioning tests and expanded CI coverage on Python 3.10 and 3.12.

### Not included yet

- Live Hiddify/X-UI panel adapters.
- Live VPN subscription provisioning and usage synchronization.
- Import of the production SellBot feature set into tenant runtimes.
