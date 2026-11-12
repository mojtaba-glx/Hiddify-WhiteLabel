# Architecture

**Repository version:** see the root `VERSION` file (currently `1.3.18` in this review).  
**Project state:** active development; live provider and operational acceptance is not complete.

## 1. Product overview

Hiddify-WhiteLabel is a Telegram-based, multi-tenant platform for operating branded VPN subscription services. The platform owner uses **MasterBot** to manage tenants, plans, licenses and platform operations. Each tenant is provisioned with a tenant-scoped **AdminBot** and **UserBot**. Tenant bots are hosted by a shared, fixed-shard runtime rather than one operating-system process per tenant.

The repository is a single versioned codebase. Its components are separated by responsibility, while sharing migrations, licensing rules and release procedures.

## 2. Main components

| Component | Responsibility |
|---|---|
| `MasterBot/` | Platform-owner administration, tenant and plan management, license operations, audit/history, platform settings and customer storefront workflows |
| `LicenseService/` | License state transitions, grace periods, scheduled expiry notifications, retry/lease handling and runtime access decisions |
| `Provisioning/` | Tenant and bot enrollment, credential verification, runtime namespace creation and reversible lifecycle operations |
| `Gateway/` | Bot identity catalog, tenant routing and current tenant/owner/license policy checks |
| `TenantRuntime/` | Fixed-shard supervisor and tenant-specific Telegram applications |
| `TenantRuntime/AdminBot/` | Tenant-owner workflows for servers, customers, subscriptions, payments and operations |
| `TenantRuntime/UserBot/` | End-user storefront, purchase/renewal, payment, wallet, support and subscription workflows |
| `Database/` + `Migrations/` | SQLite connection/repositories and versioned schema migrations |
| `Shared/` | Typed configuration, cryptography helpers, access controls, UTC utilities and redacted logging |
| `Ops/`, scripts and shell entry points | Installation, systemd integration, health checks, backup/restore, release checks and rollback tooling |
| `Tests/` | Offline automated regression and contract tests |

## 3. Runtime and tenant isolation

The runtime uses a configured, fixed number of operating-system shards. A bot is assigned to a shard from its stable bot identity. Each shard supervises multiple Telegram applications and reconciles provisioned, changed or disabled bot records.

Before a tenant update reaches its feature handlers, the runtime checks the current bot route and tenant state. AdminBot updates are also checked against the tenant owner identity. License policy is evaluated at update time, with a bounded cache that must not extend access beyond the license/grace cutoff. Runtime state is scoped by tenant, bot role and Telegram user so identical Telegram user IDs in different tenants do not share conversation state.

A tenant license expiry does not require terminating a shared worker process. Access is restricted at the update-policy boundary; renewal can restore access without restarting unrelated tenants.

## 4. Data model and persistence

The current implementation uses **SQLite**. PostgreSQL support, PostgreSQL migrations and PostgreSQL row-level security are not implemented and must not be advertised as available.

The schema includes platform tenants, tenant bot identities, license plans and licenses, audit and notification events, tenant runtime configuration, tenant user state, and tenant-scoped business records such as servers, nodes, plans, customers, payment methods, orders, receipts, subscriptions, tickets and smart links.

Tenant-owned repositories and service operations are expected to carry an explicit tenant scope. See the SQL migrations and architecture decision records for the authoritative schema and design decisions.

## 5. License and provisioning lifecycle

License records support pending, active, grace, expired, suspended and cancelled states. State changes are guarded by expected-state updates and audit records. Expiry notifications use deduplication keys and retry/lease handling so a transient Telegram delivery failure does not reverse a license transition.

Provisioning verifies the tenant's bot identities before committing the tenant, runtime namespace, encrypted bot credentials, webhook-secret hashes and audit event as one unit. Sensitive operations require explicit confirmation and are recorded in the audit trail.

## 6. VPN provider integrations

The source tree contains provider adapters and offline contract coverage for:

- Hiddify Manager
- X-UI Sanaei
- X-UI Alireza
- X-NET

The existence of an adapter or a passing offline test does **not** establish compatibility with every panel release or prove production readiness. Provider support must be reported as live-validated only after the provider-specific disposable staging procedure has passed for the same candidate commit. Otherwise record it as `not executed` or `not accepted`.

Multi-node mappings, managed smart-subscription links, usage aggregation and lifecycle enforcement are part of the implemented product scope, but their operational acceptance remains governed by the staged release record.

## 7. Security design

The implementation includes encrypted bot-token storage, token fingerprints, webhook-secret hashes, owner checks, tenant-scoped routing/state, callback revalidation, parameterized database access, redacted logging, audit events, restricted secret handoff files, encrypted backup archives and staged restore validation.

These are implementation design controls, not a claim of independent security certification. The current repository snapshot, selected source/configuration files and ignore rules have been reviewed, but a complete scan of every Git object, historical commit, branch, tag and workflow artifact is still required before publication can be considered fully cleared.

The codebase contains a source comment identifying a UserBot config/QR delivery module as ported from Hiddify-SellBot. The precise provenance, rights and required attribution for adapted material must be confirmed by the maintainers before selecting a license or claiming the repository is fully open source.

## 8. Operations and recovery

The repository provides Ubuntu/Debian installation/bootstrap tooling, systemd units, process locks, health checks, database migrations, encrypted backup/restore and guarded update rollback. The documented staging procedure requires a disposable host and test panel accounts. Never run destructive acceptance or rollback drills against production tenants or customer accounts.

## 9. Current limitations

- SQLite is the implemented database backend; PostgreSQL is not implemented.
- The Telegram bot interface is currently Persian. English developer documentation does not imply English bot localization.
- Provider adapters and offline tests do not replace live provider acceptance.
- The final live staging, backup/restore and rollback acceptance record remains outstanding.
- No repository license has been selected or added yet.

For current acceptance status, consult [Project Status](PROJECT_STATUS.md), [Roadmap](ROADMAP.md) and [Staged Release Drill](STAGED_RELEASE_DRILL.md).
