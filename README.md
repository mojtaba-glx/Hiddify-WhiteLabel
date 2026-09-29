# Hiddify-WhiteLabel

**Current version: `v0.9.3`**

White-Label SaaS for selling VPN subscriptions: each customer (tenant) gets
two dedicated bots (`TenantAdminBot` + `TenantUserBot`). The platform owner
operates the same `MasterBot` as a role-based PlatformBot: the owner gets
the management panel and normal Telegram users get the customer portal.

> Scope status: **Phases 0–9 foundation done** (design, independent infrastructure,
> owner-only MasterBot, automated license jobs, atomic tenant provisioning,
> shared sharded TenantRuntime, and production installation/operations).

## Layout

```text
Hiddify-WhiteLabel/
├── MasterBot/            # owner UI + customer storefront
├── TenantRuntime/        # Phase 5 - shared sharded AdminBot/UserBot runtime
│   ├── AdminBot/
│   └── UserBot/
├── Gateway/              # Phase 5 - secure bot catalog and update policy
├── LicenseService/       # Phase 3 - evaluator, notifications and runtime gate
├── Provisioning/         # Phase 4 - atomic tenant/bot setup and handoff
├── Ops/                  # Phase 6 - health, encrypted backup, locks, systemd
├── Shared/               # settings, redacted logging, crypto, time, access
├── Database/             # connection, repositories, migration runner
├── Migrations/           # versioned SQL (currently 0001 through 0017)
├── Tests/                # offline pytest suite, fake tokens only
├── scripts/              # migrate helper
├── install.sh            # English operations menu and systemd installer
├── docs/                 # ARCHITECTURE, THREAT_MODEL, ROADMAP, decisions/
├── .env.example          # empty template, no secrets
├── requirements.txt      # pinned deps
└── VERSION               # 0.9.3
```

Reference project `Hiddify-SellBot` was used **read-only** to understand
Hiddify/X-UI, sales, payment, ticket and service logic. Nothing was copied;
this repo is independent. No `AgentBot` exists here by design.

## Security rules (enforced)

- Bot tokens are **never** stored/logged in plain text. Only Fernet
  ciphertext (`encrypted_token`) + SHA-256 `token_fingerprint` + a safe
  `token_tail` (last 4 chars) live in DB.
- Webhook secrets are stored only as a SHA-256 hash (`webhook_secret_hash`)
  and compared in constant time.
- All timestamps stored in **UTC**; display may use `Asia/Tehran`.
- All SQL is parameterized; renew/suspend run in explicit transactions
  (autocommit connections + `BEGIN IMMEDIATE`), with CAS for concurrent renew.
- Logs and exceptions are redacted at the formatter level (tokens, `gAAAA…`
  payloads, `token=/secret=` assignments and JSON/dict reprs, including
  tracebacks from `logger.exception`).
- The DB file and its WAL/SHM sidecars are `0600`; the data dir is `0700`.
- Test tokens are fake and offline; no real Telegram calls.

## Storage engine

The current implementation is **SQLite only**. The schema and repository
layer use broadly portable SQL, so a future PostgreSQL adapter is feasible,
but it is *not* included: a Postgres driver adapter and a separate
PostgreSQL migration set are required and tracked as future work
(see `docs/decisions/ADR-0002-sqlite-postgres.md`). Do not treat this
snapshot as production-PostgreSQL-ready.

## Run tests (offline, no token, no network)

```bash
cd /home/mojte/Hiddify-WhiteLabel
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Configure and run MasterBot

```bash
cp .env.example .env
chmod 600 .env
# fill MASTER_BOT_TOKEN, MASTER_ADMIN_ID, TOKEN_ENCRYPTION_KEY
python3 scripts/migrate.py --db data/whitelabel.db
python3 -m MasterBot
```

MasterBot verifies tenant tokens with Telegram `getMe` before encrypting and
storing them. The platform-owner menu manages tenants, plans, licenses,
payments, statistics, warnings and audit history. Destructive state changes
use a confirmation step and data is disabled/archived rather than physically
deleted.

## Customer portal and payments

Any ordinary Telegram user sees a Persian customer menu with `خرید ربات`,
`سرویس‌های من`, `راه‌اندازی ربات`, `کیف پول`, `راهنمای استفاده`, `ویژگی‌ها`
and `لایسنس تست`. The owner sees only the administration menu. Customer
callbacks are namespaced and the global access gate rejects copied owner
callbacks before they reach a handler.

The owner configures card-to-card and cryptocurrency payment destinations
from `💳 پرداخت‌ها`. A customer chooses a public plan, receives the selected
payment destination, then submits a tracking code or a photo receipt. The
owner must make a live final approval; a receipt cannot be approved twice.
Card numbers and crypto addresses are public payment instructions, while no
payment-provider API credential is stored in this project.

Wallet deposits are credited only after a reviewed receipt. Wallet spending,
payment approval, renewal and order completion are explicit SQLite
transactions. The price, currency and duration are always read from the plan
in the database, never from a Telegram callback. A plan can be shown or hidden from the storefront, assigned a currency and
given a one-time trial duration. The owner can also select the exact active
public plan used for one-time trials, or leave trial-plan selection automatic.

After a purchase is paid, the customer submits only the shop name and two
BotFather tokens. The internal tenant slug is generated automatically. Each
token is verified and its Telegram message is deleted immediately; tenant,
two encrypted bot credentials, runtime namespace and active license are then
provisioned together. Internal webhook secrets are not exposed in the
customer flow.

## Tenant AdminBot and UserBot

Each provisioned tenant now has its own business records in the shared
database, always scoped by `tenant_id`: servers, nodes, sale plans, end users,
payment methods, orders, receipts, subscriptions, tickets and smart links.
The TenantAdminBot manages these records. The TenantUserBot lets its users
choose a plan, submit a text or photo receipt, view subscriptions, renew an
existing service and create a support ticket. Live provisioning supports
Hiddify Manager, X-UI Sanaei, X-UI Alireza and X-NET. Payment approval leaves an
order `paid` until the remote operation succeeds, then the order becomes
`fulfilled`. Failed purchase or renewal fulfillment is retryable. Panel
credentials remain encrypted at rest and tenant scoped.

X-UI/X-NET servers store only non-secret routing metadata in
`tenant_servers`: provider/flavor, target inbound ids and public subscription
routing. Sanaei Bearer tokens, Alireza username/password plus optional Xray
secret headers, and X-NET API-token/fallback-login material are encrypted in
`tenant_panel_credentials`. X-NET keeps its management API endpoint separate
from the public subscription listener (default port 2096 + `/sub`).

Subscription usage and last-online state are synchronized periodically. Time-
or quota-expired users are verified disabled on their provider before the local
service is marked `expired`; an expired service cannot be re-enabled without
renewal.

Multi-node subscriptions are now first-class. The default sales server remains
the required primary; every active `tenant_nodes` entry that points at a
configured Hiddify/X-UI/X-NET server is provisioned as a child mapping. Child
failures are recorded per node and can be retried without duplicating the
primary service. Renewal, enable/disable, expiry, deletion and usage sync follow
all current mappings; usage is summed across the active topology before global
quota enforcement.

Each activated subscription receives a high-entropy managed smart-link token.
Shard 0 serves the shared HTTP endpoint on `SMART_SUB_HOST:SMART_SUB_PORT`
(default `127.0.0.1:8091`). Set `SMART_SUB_PUBLIC_BASE_URL` to the TLS
reverse-proxy/domain exposed to customers. The endpoint fetches native
subscriptions from all active provider mappings, accepts plain or base64 input,
filters config lines, deduplicates them and serves `all.txt` or `all.b64`
with standard subscription metadata headers.

The runtime also includes a bounded Global Enforcer. By default it runs every
20 seconds, prioritizes subscriptions near expiry or quota, then round-robins
the rest. Multi-node usage is summed globally. If a provider is temporarily
unreachable its last known usage remains frozen in the total instead of being
dropped. Expiry is committed only after every current node has been verified
disabled; partial failures persist as `enforcement_pending` and are retried.

Tenant UserBots send durable renewal reminders when the remaining time or
traffic crosses configured thresholds (defaults: 3 days / 3 GiB), plus one
verified-expiry notice. Reminder keys are scoped to the subscription period, so
restarts and repeated scans cannot duplicate a notice and renewal automatically
starts a fresh reminder sequence. Failed Telegram sends use bounded exponential
retry and never require the MasterBot token.

Tenant AdminBot now has operational reporting and customer-management screens.
The dashboard combines confirmed revenue, purchase/renewal counts, current
service states, aggregate traffic, active infrastructure and attention queues.
Reports support today, 7/30/90-day and all-time views using the immutable
`tenant_orders.paid_at` timestamp, so later provisioning retries do not move
historical revenue between reporting periods. Customer search supports name,
username, Telegram ID and internal customer ID with strict tenant scope.

Support tickets now have a full lifecycle: users can list and reopen their own
ticket history to read replies; admins can inspect a ticket, reply, close it and
jump to the tenant-scoped customer profile. UserBot also exposes account and
order-history screens without allowing blocked customers to create new orders.
Tenant sales growth now includes wallet checkout/top-up, coupons, referrals and
one-time free trials. Wallet credits use an append-only transaction ledger with
idempotency keys. Users can top up by existing tenant payment methods; AdminBot
reviews the top-up receipt and a repeated approval cannot double-credit the
wallet. Full wallet payment supports purchases and renewals, while zero-value
orders such as a 100% coupon can finalize without an external receipt.

Coupons support percentage or fixed discounts, minimum order amounts, optional
maximum discounts, global use limits, per-customer limits and expiry. Rejected
payment receipts or explicit cancellation of an unpaid order release the
reserved coupon use.

Referral links use tenant-local opaque codes (`/start ref_<code>`), prevent
self/cross-tenant referrals and can reward the inviter for a valid free trial
and the invitee's first qualifying purchase. Rewards are credited to the same
wallet ledger exactly once per referral/reward type.

Free Trial is configured per tenant (enabled, traffic GB and duration days),
is available only once to a customer with no previous paid purchase/renewal,
uses the normal multi-node provisioning + smart-subscription engine, and keeps
the same claim/order for safe retry if the provider is temporarily unavailable.

The MasterBot JobQueue runs the Phase-3 evaluator at the configured interval.
It advances expiry/grace states, suspends expired licenses, persists 7/3/1-day
warnings, retries failed sends and recovers abandoned worker leases. Runtime
license checks do not depend on the MasterBot being online: they read the same
SQLite state through a fail-closed, short-lived cache. The gate also requires
the tenant runtime namespace to be `ready`.

## Provision a complete tenant

Use the `🚀 راه‌اندازی کامل` flow in MasterBot, or run the English-terminal
helper below. Both Telegram bot tokens are read as hidden input by the CLI and
are never accepted as command-line arguments:

```bash
./scripts/create-tenant.sh
```

Provisioning verifies both bots with Telegram first. Tenant, namespace, both
encrypted bot rows, two distinct webhook-secret hashes and the audit record are
then committed together. The CLI stores the two raw webhook secrets once in a
new `0600` handoff file under a `0700` ignored runtime directory and prints only
its path. Configure the webhooks and securely delete that file afterward.

Disabling a tenant preserves its namespace, bot rows, licenses and future
tenant-owned data. Re-enabling requires both bots to be active and never
bypasses license expiry.

## Run the shared tenant bots

Tenant bots use a fixed number of runtime processes. A shard loads only its
assigned bot ids, validates each encrypted token fingerprint, and runs those
Telegram Applications in one asyncio process. A periodic reconciler starts new
bots and restarts changed tokens without creating tenant folders or processes.

Set the same `RUNTIME_SHARD_COUNT` for every process and use one unique index
from zero through `count - 1`:

```bash
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=0 python3 -m TenantRuntime
```

Run the equivalent command for indexes 1, 2 and 3. The Phase-6 installer
creates and manages those instances automatically.

Before every update, the runtime revalidates the bot route and applies the
tenant, owner, runtime-readiness and license gates. State is durable and keyed
by `(tenant_id, bot_role, telegram_user_id)`. Database or credential errors
fail closed for the affected update, while failure to start one bot does not
stop the other tenant bots in that shard.

The tenant runtime includes tenant-scoped inventory, sale plans, payment
instructions, receipts, subscriptions, tickets and smart-link flows. Hiddify,
X-UI Sanaei, X-UI Alireza and X-NET now implement the live provider boundary
for provisioning, renewal, enable/disable, deletion, usage/last-online sync and
native subscription links. Staged production drills with real test panels are
kept separate from the offline CI suite.

## Easy install and operations

Fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/mojtaba-glx/Hiddify-WhiteLabel/main/bootstrap.sh | sudo bash
```

The bootstrap installs OS prerequisites, creates a dedicated `whitelabel`
system account, clones the application to `/opt/hiddify-whitelabel`, asks for
the MasterBot token and owner Telegram ID, creates the encrypted runtime
environment, applies migrations, installs systemd services and runs health
checks.

After installation, all routine operations are available from one command:

```bash
sudo whitelabel
```

The terminal manager includes GitHub update, install/repair, restart/start/stop,
service status, MasterBot and TenantRuntime logs, health checks, MasterBot
token/Admin-ID/shard settings, database migrations, encrypted backup/restore,
service-only removal and guarded full uninstall.

Updates fetch `origin/main`, refuse tracked local source changes, install
dependencies and run the complete offline test suite before stopping live
services. Before migrations, the updater creates a private consistent SQLite +
`.env` rollback snapshot; any migration/startup/health failure restores the
previous database, environment and source version automatically. See
`docs/OPERATIONS.md` for direct commands and recovery details.
