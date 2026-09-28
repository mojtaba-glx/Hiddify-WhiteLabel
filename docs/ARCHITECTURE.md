# Architecture

Version: 0.9.0 | Status: Phase 0 through Phase 9 security boundary implemented

## 1. Context

`Hiddify-WhiteLabel` is a monthly-subscription SaaS that provisions an
independent VPN-sales stack per customer (tenant). The reference project
(`Hiddify-SellBot`, single-owner, 4 bots incl. `AgentBot`) was studied
**read-only** for healthy patterns: Hiddify/X-UI panel calls, plan/wizard
sales flow, card-to-card receipts, tickets, service enforcement. No code,
schema or secret was copied.

Key product differences:

- No `AgentBot`. Each tenant gets up to two bots: `TenantAdminBot`
  (full admin of that tenant) and `TenantUserBot` (end-user shop),
  at most one per role.
- `MasterBot` is role-based: the platform owner manages tenants and licenses;
  normal users use the customer storefront for plans, payment and setup.
- Expiry never deletes data; it only limits update processing for that
  tenant. In the shared runtime, expiry is **not** a process stop.

## 2. Logical components (one repo for now)

All four are logically independent but versioned together so migrations
and license semantics stay in sync (ADR-0001):

| Component | Responsibility | Phase |
|---|---|---|
| `MasterBot/` | owner-only CRUD for tenants/plans/licenses, staged full provisioning, secure token/secret rotation, confirm steps, pagination/search, statistics, warnings and audit | 2 + 4 (implemented) |
| `LicenseService/` | license state machine, scheduled transitions, durable 7/3/1-day warnings, lease recovery/retry, short cache and runtime gate | 1 + 3 (implemented) |
| `Gateway/` | validate encrypted credentials, assign bot ids to fixed shards, route to `tenant_id`, and enforce current route/owner/license policy | 5 (implemented) |
| `Provisioning/` | atomic tenant + namespace + two-bot enrollment, reversible lifecycle and private one-time webhook handoff | 4 (implemented) |
| `TenantRuntime/` | fixed-process supervisor, one in-process Telegram Application per active tenant bot, isolated state and tenant-specific Admin/User handlers | 5 (implemented) |
| `Shared/` | typed settings, redacted logging, token crypto, UTC time, access guard | 1 |
| `Database/` + `Migrations/` | connection, repositories, versioned migrations (`schema_migrations`) | 1 |
| `Ops/` + `install.sh` | process locks, systemd rendering, health, authenticated encrypted backup, validated restore and operational rollback | 6 (implemented) |

## 3. Data model (authoritative)

See `Migrations/0001_init.sql` and `docs/decisions/`. Summary:

- `tenants(id, public_id UNIQUE non-guessable, name, slug UNIQUE, owner_telegram_id, status, created_at, updated_at)` — `status ∈ {active, suspended, disabled}`.
- `tenant_bots(id, tenant_id FK RESTRICT, role ∈ {admin,user}, encrypted_token, token_fingerprint UNIQUE, token_tail, telegram_bot_id UNIQUE-when-set, telegram_username, status ∈ {active,disabled,revoked}, webhook_secret_hash, created_at, updated_at)` — `UNIQUE(tenant_id, role)` allows **at most one bot per role**; `readiness()` reports both roles active.
- `license_plans(id, name UNIQUE, duration_days>0, price>=0, max_servers, max_users, features JSON, status ∈ {active,archived,disabled})`.
- `licenses(id, tenant_id FK RESTRICT, plan_id FK RESTRICT, status ∈ {pending,active,grace,suspended,expired,cancelled}, starts_at, expires_at, grace_until NULL, suspended_at NULL, created_at, updated_at)` — at most **one current (active/grace)** license per tenant (partial unique index `idx_licenses_one_current`); history rows are unlimited.
- `audit_events(id, actor_id, tenant_id NULL FK SET NULL, action, entity_type, entity_id, safe_metadata JSON non-sensitive, created_at)`.
- `notification_events(id, tenant_id FK RESTRICT, event_type, event_key UNIQUE, scheduled_at, sent_at NULL, status ∈ {pending,processing,sent,failed,skipped}, locked_at NULL, next_attempt_at NULL, retry_count)`.
- `tenant_runtime_configs(tenant_id PK/FK RESTRICT, data_namespace UNIQUE non-guessable, status ∈ {provisioning,ready,disabled,error}, created_at, updated_at)` — one logical data namespace per provisioned tenant; future tenant-owned tables remain in the shared database and carry `tenant_id`.
- `tenant_user_state(tenant_id FK RESTRICT, bot_role, telegram_user_id, state_json, updated_at)` — composite primary key `(tenant_id, bot_role, telegram_user_id)` prevents state collision between tenants and bot roles.
- `tenant_servers`, `tenant_nodes`, `tenant_sale_plans`, `tenant_customers`,
  `tenant_payment_methods`, `tenant_orders`, `tenant_receipts`,
  `tenant_subscriptions`, `tenant_tickets`, and `tenant_smart_links` all have
  a required `tenant_id` FK. Their repository/service methods are permanently
  bound to a runtime tenant scope.
- `schema_migrations(version PK, applied_at, checksum)`.

All timestamps are ISO-8601 UTC (`+00:00`). Display converts to
`Asia/Tehran` only at presentation (ADR-0004).

## 4. License lifecycle

States: `pending → active ⇄ grace → expired → suspended`, plus
`cancelled` as terminal side-state. Every change is atomic
(`UPDATE ... WHERE status = expected` + audit insert in one locked
transaction, tenant re-verified from the row read), audited, and tested.
At most one current license per tenant. Detail: `LicenseService/states.py`.

Expiry behavior:

1. Warnings at 7 / 3 / 1 days before `expires_at` use an expiry-specific
   `event_key` (`license:<id>:expiring:<n>d:<period-hash>`). Each period/stage
   is sent at most once, renewal gets a fresh sequence, and queued warnings
   from an older period are skipped before delivery.
2. After `expires_at`: `active → grace` if `grace_until` is in the future,
   else `active → expired`.
3. After `grace_until`: `grace → expired`, then operator/job moves
   `expired → suspended` (tenant limited, data kept).
4. Renew (`expired|suspended|grace|active → active` with new
   `expires_at`/`grace_until`) re-enables the tenant. Nothing is deleted
   on suspend/expire.
5. `TenantRuntimeGate` answers per update from the database with a bounded
   cache. Positive cache entries never outlive expiry/grace. Database errors
   fail closed for that tenant while healthy tenants remain independent.
6. Notification delivery uses a claim lease, stale-lease recovery,
   exponential retry and a maximum attempt count. A Telegram outage does not
   roll back license transitions or lose the queued warning.

## 5. Security outline

Full model: `docs/THREAT_MODEL.md`. Essentials:

- Tokens encrypted with Fernet (`TOKEN_ENCRYPTION_KEY` outside DB,
  file mode `0600`); DB holds ciphertext + SHA-256 fingerprint only.
- `MASTER_ADMIN_ID` is enforced by an application-wide handler before every
  command/message/callback and repeated inside every Master service operation.
- Callback data is untrusted: ids, license status and tenant ownership
  are re-checked from DB.
- Logs/exceptions redacted; DB queries parameterized; sensitive
  renew/suspend wrapped in transactions with confirm + audit.
- Full provisioning verifies both tokens before writing, then creates the
  tenant, runtime namespace, two encrypted bot credentials, webhook hashes
  and audit event in one transaction. A failure rolls the whole unit back.
- Raw webhook secrets are returned once. The CLI writes them to a new `0600`
  handoff inside a `0700` directory; MasterBot marks the Telegram message as
  protected. Secret rotation requires explicit confirmation.
- TenantRuntime decrypts a bot token only while constructing its Telegram
  Application and verifies its SHA-256 fingerprint before use. Public runtime
  specs hide ciphertext and fingerprints from `repr`.
- Every update is checked against the current database route before a handler
  runs. Admin updates additionally require the tenant owner's Telegram id.
  Missing identities, changed tokens, disabled rows and database errors fail
  closed.

## 6. Shared runtime topology

`RUNTIME_SHARD_COUNT` fixes the number of operating-system processes. Each bot
belongs to exactly one shard by `bot_id % shard_count`; both tenant roles may
live in different shards. Inside a shard, `RuntimeSupervisor` reconciles many
independent Telegram Applications. It starts newly provisioned bots, restarts
changed routes/tokens and stops disabled bots. A bad credential or one failed
Telegram startup is isolated to that bot.

License expiry deliberately does not kill the worker. The application-wide
access handler invokes `RuntimePolicy` for each update, so expired or suspended
tenants are blocked while renewals take effect without a process restart. A
short license cache reduces database reads and never outlives the stored
expiry/grace cutoff.

The runtime handlers include tenant-scoped inventory, sales plans,
card/crypto payment instructions, receipt review, pending subscriptions,
tickets and smart links. Full Hiddify/X-UI API credentials, provisioning and
usage synchronization remain independent integration work; no panel
credential is currently accepted or stored.

## 7. Operations and recovery

One systemd unit runs MasterBot and its embedded license jobs. A templated unit
runs indexes `0..RUNTIME_SHARD_COUNT-1`; command-local environment assignment
prevents `.env` from overriding `%i`. File locks reject a duplicate MasterBot
or duplicate `(shard_count, shard_index)` process even outside systemd.

Health checks validate private permissions, typed environment settings,
SQLite integrity, foreign keys, migration presence/checksums and candidate bot
credential integrity. They return names/counts only and never secret values.

Backups use SQLite's online backup API, package the database and `.env` with a
SHA-256 manifest, then authenticate and encrypt the complete archive with a
Fernet key derived from the operator passphrase using PBKDF2-HMAC-SHA256. A
restore validates everything in a private staging directory, migrates the
staged database, snapshots current files for rollback and then uses atomic
replacement. Full procedure: `docs/OPERATIONS.md`.

## 8. Non-goals of this snapshot

Full tenant business modules and final live multi-tenant drills remain deferred
per `docs/ROADMAP.md`.
