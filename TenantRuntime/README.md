# TenantRuntime

TenantRuntime runs all provisioned tenant `AdminBot` and `UserBot` instances
inside a fixed number of shard processes. Each shard owns the bots whose
database id matches `bot_id % RUNTIME_SHARD_COUNT == RUNTIME_SHARD_INDEX`.
Adding a tenant therefore does not create a new operating-system process or a
new source directory.

Every Telegram update passes through a policy gate before its handler. The
gate re-reads the current bot route and checks tenant status, runtime readiness,
the tenant owner's access to `AdminBot`, and the effective license. A changed
token, disabled tenant, invalid route, unavailable database, or unusable
license fails closed for that update. One tenant failure does not stop other
workers in the shard.

Conversation state is stored in `tenant_user_state` under the composite scope
`tenant_id + bot_role + telegram_user_id`. Each application receives a state
store permanently bound to its own tenant and role.

Hiddify, X-UI Sanaei and X-UI Alireza are live through the provider adapter.
Approved purchase orders are fulfilled only after remote provisioning succeeds.
Renewal orders reuse the same receipt-review flow and remain `paid` if the
panel call fails, so an admin can retry safely. Provider renewal requests carry
a remote retry marker so a retry cannot reset post-renewal usage a second time.
X-UI can target the first active inbound, every supported inbound, or an
explicit comma-separated inbound list while preserving one stable subscription
identity across copies.

A shard-safe lifecycle coordinator runs every
`RUNTIME_LIFECYCLE_SECONDS` (default 180). Tenant ownership for maintenance
uses `tenant_id % RUNTIME_SHARD_COUNT`, so only one shard synchronizes each
tenant. It refreshes usage and last-online state, disables time/quota-expired
accounts before marking them expired locally, and never reactivates an expired
subscription without renewal.

Run one process for each shard index:

```bash
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=0 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=1 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=2 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=3 python3 -m TenantRuntime
```

The current handlers provide tenant-scoped sales, payment review, Hiddify/X-UI
provisioning, paid renewal, subscription lifecycle controls and support tools.
Sanaei uses an encrypted Bearer API token; Alireza uses encrypted
username/password and an optional encrypted Xray application secret header.
The implementation is independent and does not import the reference SellBot.
