# Changelog

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
