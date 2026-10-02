# Changelog

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
