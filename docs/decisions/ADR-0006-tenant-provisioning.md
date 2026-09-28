# ADR-0006 — Atomic tenant provisioning and one-time webhook handoff

## Status

Accepted in Phase 4.

## Decision

A tenant owns one row in `tenant_runtime_configs` and at most one AdminBot and
one UserBot. Both tokens are verified with Telegram before database writes.
The tenant, namespace, bot credentials, webhook-secret hashes and audit entry
are inserted in one `BEGIN IMMEDIATE` transaction.

The shared database remains the data boundary: future tenant-owned tables must
contain `tenant_id`; no source-code tree or database is cloned per customer.

Webhook secrets are independent per bot. SQLite stores only SHA-256 hashes.
The raw values are returned once to the owner. CLI provisioning writes them to
a new mode-`0600` file in a mode-`0700` ignored directory and never prints the
values. Telegram provisioning uses protected-content messages. Rotation
requires confirmation and invalidates the previous value.

Tenant disable/enable changes state only. It never deletes bot credentials,
license history, namespace or tenant-owned records. The license gate remains
an independent requirement for update processing.

## Consequences

- A duplicate token, duplicate Telegram identity or second-bot insert failure
  leaves no half-created tenant.
- A failed external delivery of the one-time secret requires secret rotation;
  no plaintext recovery is possible from the database by design.
- Actual webhook registration and TenantAdmin/User handler execution belong to
  Phase 5.
