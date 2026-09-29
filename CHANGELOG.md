# Changelog

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
