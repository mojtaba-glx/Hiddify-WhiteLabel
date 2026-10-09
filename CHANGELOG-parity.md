# SellBot Parity Changelog

All parity work lives on branch `feat/sellbot-parity-phase1`.

---

## v1.2.7 — Dynamic builder + invite menu parity

- Plan-builder keyboard now matches SellBot: separate `📊 حجم` / `⏳ زمان` label rows, plain `➖`/`➕` steppers, discount+price row, `💳 تایید و خرید` confirm, `X ماهه` format, short header text.
- Main-menu `🎁دریافت هدیه` replaced with SellBot-style `💌دعوت دوستان` (visible when referral is enabled or legacy `show_gift_button` is on).
- Invite home keyboard is now the SellBot 5-button layout with `inviterewards` / `invitelist` / `invitestats` / `invitehistory` handlers backed by `referral_summary`.
- Wallet gift-code redeem (`shop:gift`) unchanged. Full offline suite: 744 passed.

## v1.2.6 — Purchase-flow parity guard

- Kept the SellBot-style location → dynamic plan builder flow for dynamic and mixed sales.
- Restored the existing fixed-plan/category flow for locations without dynamic pricing.
- Verified the post-confirm summary with direct-payment methods and wallet-payment actions.

## v0.3.0-parity-phase345 — Delivery Loop, QR/Configs, Force-join & Events

### Phase 3: `TenantRuntime/UserBot/direct_buy_delivery.py` (323 lines)

Direct-buy payment delivery loop ported from `Hiddify-SellBot UserBot/main.py`.

| Function | SellBot origin | Description |
|---|---|---|
| `direct_buy_delivery_loop` | `_direct_buy_delivery_loop` | asyncio background task (6s poll) |
| `_process_approved_direct_buy_payments` | same | scan pending orders & provision Hiddify service |
| `_deliver_direct_buy_after_sms_notice` | same | immediate delivery on auto-approve |
| `_warn_admin_direct_delivery_exhausted` | same | DM admin when retries exhausted |
| `_warn_admin_pending_node_sync` | same | DM admin when node sync needed |

### Phase 4: `TenantRuntime/UserBot/qr_and_configs.py` (402 lines)

QR code and config delivery ported from `Hiddify-SellBot UserBot/main.py`.

| Function | SellBot origin | Description |
|---|---|---|
| `send_qr_code` | QR action block | Generate QR from sub link, send as photo |
| `send_direct_configs` | configs delivery block | Fetch raw sub, split URIs, send individually |
| `send_auto_sub_link` | auto-sub delivery block | Send automatic subscription link |
| `send_smart_sub_link` | smart sub block | Send multi-server smart link (plain/b64) |
| `send_sub_link` | sub link block | Send standard subscription link (plain/b64) |
| `start_tenant_sub_server` | `sub_http_server.start_sub_server()` | Sub HTTP server stub (runner-managed) |

### Phase 5: `TenantRuntime/UserBot/force_join_and_events.py` (275 lines)

Force-join and event channel ported from `Hiddify-SellBot UserBot/main.py`.

| Function | SellBot origin | Description |
|---|---|---|
| `send_event_channel_message` | event_channel block | Post purchase/renewal event to channel via AdminBot |
| `enforce_force_join` | `_enforce_force_join` | Gate: check & prompt force-join |
| `check_force_join_membership` | `_user_joined_force_channel` | Direct membership API check |
| `register_background_tasks` | `_post_init_set_menu` task block | Register all background tasks in post_init |
| `shutdown_background_tasks` | `_post_shutdown_userbot` | Graceful shutdown in post_shutdown |
| `register_ticket_autoclose_job` | USERBOT_TICKET_AUTOCLOSE block | Register ticket auto-close job_queue job |

---

## v0.2.0-parity-phase2 — Card Payment Flow
**Commit:** a87ab9fc14e68e71fe0532906a3af16b35101f25

### Added: `TenantRuntime/UserBot/card_payment.py`

| Function | SellBot origin | Description |
|---|---|---|
| `_notify_admin_card_payment` | `_notify_admin_new_card_receipt` | Notify AdminBot when card receipt submitted |
| `finalize_pending_card_payment` | `_finalize_pending_card_payment` | Download receipt + submit + archive media + notify |
| `finalize_pending_wallet_topup` | wallet topup variant | Same flow for wallet top-up |
| `pending_card_admin_notify_job` | `_pending_card_admin_notify_job` | PTB job_queue periodic reminder (interval=25s, first=8s) |
| `card_payment_result_text` | `_card_payment_result_user_text` | User-facing status message (approved/pending/rejected) |
| `build_card_payment_instruction` | buy flow display | Card details panel before bank transfer |
| `require_last4_enabled` | settings check | Per-tenant 4-digit card sender verification flag |

---

## v0.1.0-parity-phase1 — Utility Functions
**Commit:** 053c7ada

### Added: `TenantRuntime/UserBot/sellbot_utils.py`

25 utility functions from SellBot adapted for WhiteLabel multi-tenant architecture.

---

## Roadmap

| Phase | File | Status |
|---|---|---|
| 1 | `sellbot_utils.py` | ✅ Done |
| 2 | `card_payment.py` | ✅ Done |
| 3 | `direct_buy_delivery.py` | ✅ Done |
| 4 | `qr_and_configs.py` | ✅ Done |
| 5 | `force_join_and_events.py` | ✅ Done |
