# Roadmap

This roadmap describes the current repository state at version `1.3.18`. A checked implementation phase does not mean that every external provider or production deployment has passed live acceptance.

## Phase 0 — Design and boundaries

- [x] Define the multi-tenant product model and component boundaries.
- [x] Record architecture decisions and the security/threat model.
- [x] Establish the reference-project study and code-provenance review requirement.

## Phase 1 — Shared infrastructure

- [x] Typed configuration, UTC helpers, redacted logging and token-crypto helpers.
- [x] SQLite schema, migrations, repositories and integrity checks.
- [x] License state machine and audit foundations.
- [x] Offline regression coverage for core infrastructure.

## Phase 2 — MasterBot platform management

- [x] Platform-owner access gate.
- [x] Tenant, plan and license management workflows.
- [x] Confirmation, search/pagination, history/audit and platform statistics.
- [x] Customer-facing storefront and platform payment settings.

## Phase 3 — License lifecycle

- [x] Expiry/grace evaluation and runtime access policy.
- [x] Deduplicated 7/3/1-day notification scheduling.
- [x] Delivery retry/lease recovery and bounded runtime cache.

## Phase 4 — Tenant provisioning

- [x] Verify tenant bot identities before enrollment.
- [x] Create tenant runtime namespace and tenant bot records transactionally.
- [x] Store bot credentials encrypted and webhook secrets as hashes.
- [x] Support reversible enable/disable lifecycle without deleting tenant data.

## Phase 5 — Gateway and shared tenant runtime

- [x] Bot-to-tenant routing and fixed-count runtime shards.
- [x] Per-update tenant, owner and license policy checks.
- [x] Tenant/role/user-scoped conversation state.
- [x] Tenant-specific AdminBot and UserBot runtime applications.

## Phase 6 — Installation and operations

- [x] Ubuntu/Debian installer and systemd integration.
- [x] Duplicate-process locks and health checks.
- [x] Encrypted backup and validated restore.
- [x] Guarded update and operational rollback tooling.
- [ ] Complete a documented live backup/restore/update/rollback drill on a disposable staging host for the release candidate.

## Phase 7 — Customer commerce

- [x] Public plan catalog, purchase and renewal workflows.
- [x] Card-to-card and cryptocurrency payment instructions.
- [x] Receipt submission/review and paid-order handling.
- [x] Wallet top-up/spending, coupons, referrals and one-time trials.
- [x] Tenant-scoped customer and order access checks.

## Phase 8 — Tenant product and provider integrations

- [x] Tenant-scoped product modules for Hiddify Manager, X-UI Sanaei/Alireza and X-NET adapters.
- [x] Subscription lifecycle, usage synchronization and multi-node/smart-link code paths.
- [x] Reminder/global-enforcement, reports and AdminBot/UserBot management flows.
- [x] Offline release drill and regression checks.
- [ ] Run the live provider matrix with disposable test users for the exact release candidate.
- [ ] Record provider results, including explicit `not executed` entries where a provider is unavailable.
- [ ] Verify retries converge without duplicate remote users or unintended second quota resets.
- [ ] Complete the commerce and tenant-boundary staging checklist.
- [ ] Complete backup/restore and controlled failed-update rollback on staging.
- [ ] Keep Phase 8 **partial** until all advertised providers and the operational rollback drill pass for the same candidate.

## Open-source publication readiness

- [x] Publish current English README and Persian README companion.
- [x] Document actual implementation scope and known limitations.
- [x] Keep project status and live-acceptance requirements explicit.
- [ ] Confirm code provenance and rights for adapted/ported components.
- [ ] Select a license only after the rights holder confirms the intended permissions and third-party obligations.
- [ ] Add the approved license text and required dependency notices.
- [ ] Complete a full-history secret scan across all branches and tags.
- [ ] Review branch disposition without deleting potentially useful work prematurely.
- [ ] Re-check public examples, screenshots, logs and artifacts for customer/infrastructure data.

## Acceptance policy

A feature marked implemented means the code path and available automated tests exist. It does not automatically mean the feature has passed live operational acceptance. Report implementation, automated test status and live staging status separately.
