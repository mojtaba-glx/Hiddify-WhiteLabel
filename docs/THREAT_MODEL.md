# Threat Model and Security Boundaries

**Scope:** current Hiddify-WhiteLabel repository, version recorded in `VERSION` (1.3.18 at the time of this review).

This document summarizes the principal assets, threats and controls described by the current design. It is not an independent penetration test or a certification that every control has been verified in production.

## Assets

1. Platform MasterBot token and token-encryption key.
2. Tenant AdminBot and UserBot tokens.
3. Tenant and platform database records, license state, customer/order data and audit history.
4. Provider credentials and subscription data handled by tenant business integrations.
5. Backup archives, environment files, webhook secrets and deployment configuration.

## Actors and trust boundaries

- External Telegram users submitting commands, messages and callback data.
- Tenant owners who must be isolated from other tenants.
- A compromised tenant bot or provider credential.
- A network attacker attempting replay, forgery or unauthorized access.
- An operator or process with access to logs, database files, backups or environment configuration.
- A compromised host, dependency or CI workflow.

The platform-owner boundary, tenant-owner boundary, tenant-to-tenant boundary, Telegram/provider API boundary and host/backup boundary must be treated separately.

## Threats and documented controls

| Threat | Design controls in the current codebase |
|---|---|
| Bot-token exposure through database or logs | Fernet-based token encryption, token fingerprints, redacted logging and restricted environment-file permissions |
| Unauthorized MasterBot operation | Platform-owner ID gate at update handling and service-operation boundaries |
| Forged or stale callback data | Re-read identifiers, state and ownership from the database; confirmation for sensitive operations |
| Cross-tenant access | Tenant-bound runtime routing, tenant-scoped repositories and state keys containing tenant, role and Telegram user |
| Duplicate license transitions or notifications | Expected-state updates, audit records, unique event keys, claim leases and retry handling |
| SQL and shell injection | Parameterized database access and constrained operational command construction |
| Missing auditability | Audit events for sensitive platform and tenant operations |
| Webhook forgery or secret reuse | Per-bot random webhook secrets, stored hashes, constant-time comparison and rotation |
| One tenant bot failure affecting others | Fixed-shard supervision and per-bot failure isolation |
| Unauthorized tenant AdminBot use | Current tenant-owner identity check before tenant-admin handlers |
| Backup disclosure or tampering | Authenticated encryption, manifest validation, private staging and integrity checks before restore |
| Duplicate polling processes | Process locks for MasterBot and runtime shard instances |
| Failed update leaving broken services | Guarded update flow with service/unit rollback and health checks |

The implementation details and operational procedures are described in the source, `docs/ARCHITECTURE.md` and `docs/OPERATIONS.md`.

## Known limitations and items requiring validation

- The current persistence implementation is SQLite. PostgreSQL adapter and PostgreSQL RLS controls are not implemented.
- Provider adapters and offline contract tests do not prove live compatibility with all Hiddify/X-UI/X-NET versions. Follow `docs/STAGED_RELEASE_DRILL.md`.
- The final live provider, backup/restore and failed-update rollback drill is still outstanding.
- Security of provider credential handling, webhook deployment, rate limiting and the host environment must be validated against the exact release candidate and deployment configuration.
- CI and repository checks do not protect against a compromised maintainer account, malicious dependency update or compromised GitHub Actions supply chain.

## Public repository review

The repository is public. The current tree and selected configuration, test, provider, provisioning, backup and documentation files were spot-checked. No obvious live Telegram bot token, private key, production database or backup artifact was identified in the inspected current files. Test credential-like strings inspected were placeholders.

This spot check is **not** a complete historical secret scan. Every branch, tag, commit object, workflow artifact and external deployment has not been forensically inspected. GitHub Secret Scanning alerts were not available through the current review connection. Keep this limitation explicit; if a real credential is discovered, revoke/rotate it immediately and then follow the incident response procedure.

## Incident response minimum

1. Revoke or rotate any exposed token, password, encryption key or webhook secret immediately.
2. Identify affected tenants, services, backups and time window.
3. Remove exposed material from current files and history where appropriate, but do not treat history rewriting as a substitute for credential rotation.
4. Inspect logs and audit events for unauthorized use.
5. Publish a sanitized incident note if users or contributors may be affected.

## License and code provenance

A source comment in `TenantRuntime/UserBot/qr_and_configs.py` says the module was ported from `Hiddify-SellBot`. Confirm the rights, ownership and attribution obligations for this and any other adapted code before choosing a repository license or describing the project as fully open source.
