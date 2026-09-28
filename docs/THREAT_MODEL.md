# Threat model (Phases 0–6)

## Assets

1. Tenant bot tokens (`TenantAdminBot`, `TenantUserBot`) — full control of tenant bots.
2. `MASTER_BOT_TOKEN` + `TOKEN_ENCRYPTION_KEY` — platform crown jewels.
3. Platform DB (tenants, licenses, fingerprints, audit trail).
4. Future tenant panel credentials (Hiddify/X-UI) — not stored in Phase 1.

## Actors

- Network attacker (replay/forgery of callback data, cross-tenant id guessing).
- Malicious or compromised tenant owner (tries to read/act on other tenants).
- Curious operator reading logs/dumps/backups.
- Buggy client sending stale/duplicate updates.

## Threats → mitigations (mapped to build rules)

| # | Threat | Mitigation (implemented Phase 1 unless noted) |
|---|---|---|
| T1 | Token leak via DB dump/log/exception/terminal | Fernet encrypt with key outside DB; `__repr__` hides secrets; redacting logger + `safe_format_exception`; `.env` mode `0600`, never in git |
| T2 | Non-owner calls MasterBot handlers | `MASTER_ADMIN_ID` gate runs in handler group `-1` for all updates and is repeated in every `MasterService` public operation |
| T3 | Forged callback (`tenant_id`, `license_id`, fake `active`) | Re-read ids/status/ownership from DB; atomic `UPDATE … WHERE status=expected`; server-side confirmation state for sensitive operations |
| T4 | Cross-tenant read/write | Each application is bound to an immutable tenant/role spec; current route is re-read before every update; durable state uses the composite key `tenant_id+role+user`; repository and state isolation tests cover equal Telegram user ids across tenants |
| T5 | Double-spend of renew / duplicate or stale warnings | CAS renewal; `notification_events.event_key UNIQUE`; expiry-specific keys; claim leases; stale-event validation before delivery |
| T6 | Time confusion (local vs UTC, DST Asia/Tehran) | Store UTC ISO-8601; convert to Tehran only for display; UTC tests |
| T7 | SQL injection / shell injection | Parameterized `?` queries only; no `eval`/`shell=True`/user-built commands (grep-verified) |
| T8 | Silent tampering (who changed what) | `audit_events` with non-sensitive `safe_metadata` only; sensitive keys rejected |
| T9 | Backup/secret sprawl | `.gitignore` covers `.env`, `*.db`, logs, backups, runtime; `scripts/` never prints secrets |
| T10 | Half-created tenant or reused bot identity | Both Telegram identities are verified before the single provisioning transaction; global token fingerprint and Telegram bot-id constraints roll back tenant, namespace and first bot if the second insert fails |
| T11 | Webhook forgery or one shared secret across tenants | A cryptographically random secret is generated per bot; only its SHA-256 hash is stored; comparison is constant-time; rotation invalidates the prior secret |
| T12 | One bad tenant bot crashes all tenants | Fixed shard supervisors isolate start/stop errors per bot; malformed credentials are rejected per catalog row; a catalog outage leaves workers alive while the per-update policy fails closed |
| T13 | Tenant AdminBot used by another Telegram user | Every AdminBot update compares the current tenant `owner_telegram_id` before any feature handler runs |
| T14 | Plain backup leaks encrypted bot DB plus decryption key from `.env` | Database and environment are packaged with hashes, authenticated and encrypted under a PBKDF2-derived backup key; archive and rollback permissions are `0600`/`0700` |
| T15 | Modified or wrong backup replaces production | Restore authenticates encryption, validates the manifest hashes, environment, SQLite quick check, foreign keys and migration checksums in staging before atomic replacement |
| T16 | Duplicate shard or MasterBot consumes the same Telegram updates | Non-blocking per-instance file locks reject duplicate Master and `(shard_count, shard_index)` processes; systemd uses one template instance per index |
| T17 | Failed unit update leaves broken services installed | Installer records previous unit files and enable/active state, then restores them if installation, startup or health validation fails |

## Out of scope (later phases)

- A live production deployment and external monitoring/alert delivery; the
  systemd and local health mechanisms are implemented but were tested offline.
- Gateway webhook header enforcement, rate limiting, panel-credential vaulting.
- PostgreSQL RLS policies (the current implementation is SQLite-only; PostgreSQL adapter + separate migration set are future work, **not yet implemented**).
