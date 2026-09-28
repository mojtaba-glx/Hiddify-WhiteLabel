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

Hiddify, X-UI Sanaei, X-UI Alireza and X-NET are live through the provider adapter.
Approved purchase orders are fulfilled only after remote provisioning succeeds.
Renewal orders reuse the same receipt-review flow and remain `paid` if the
panel call fails, so an admin can retry safely. Provider renewal requests carry
a remote retry marker so a retry cannot reset post-renewal usage a second time.
X-UI can target the first active inbound, every supported inbound, or an
explicit comma-separated inbound list while preserving one stable subscription
identity across copies. X-NET accepts its native string inbound ids, supports
the same first/all/explicit selection model, and creates one UUID across the
primary and extra inbound set in one management request. Its public
subscription listener is configured independently from the management API.

A shard-safe Global Enforcer is owned by
`tenant_id % RUNTIME_SHARD_COUNT`, so only one shard maintains each tenant.
The dedicated cadence is `RUNTIME_ENFORCER_SECONDS` (default 20 seconds).
Each pass processes a bounded hot-first/round-robin batch, refreshes aggregate
usage and last-online state, and never reactivates an expired subscription
without renewal. A node read failure preserves that node's last usage snapshot;
after repeated failures it is marked frozen for diagnostics. If a subscription
is due, every mapped node must be successfully disabled before the local service
is committed as `expired`; otherwise `enforcement_pending=1` is persisted
and the next pass retries the incomplete enforcement.

Active `tenant_nodes` define the Multi-node topology for new and repaired
subscriptions. A primary mapping is mandatory; child mappings are best-effort
and persist independent status/error metadata in
`tenant_subscription_nodes`. Aggregate usage is the sum of the current
topology, so a user cannot bypass the plan quota by switching locations.

Shard index 0 also owns the managed subscription HTTP listener. Configure:

```bash
SMART_SUB_HOST=127.0.0.1
SMART_SUB_PORT=8091
SMART_SUB_PUBLIC_BASE_URL=https://sub.example.com
```

The public URL delivered to UserBot is
`/sub/<opaque-code>/all.b64`. `/all.txt` is available for plain text. The
opaque code resolves only to the tenant-scoped subscription recorded in
`tenant_smart_links`; panel credentials are decrypted only inside the server
while fetching each provider's native subscription.

Renewal reminders are durable and tenant-scoped. The default thresholds are
`RUNTIME_REMINDER_DAYS=3` and `RUNTIME_REMINDER_REMAINING_GB=3`. The
current UserBot sends one message for each newly crossed day/GB bucket and one
expiry message only after enforcement is verified. The reminder queue has
lease recovery, bounded exponential retry and period fingerprints, so stale
warnings from a previous renewal are skipped rather than delivered.

Run one process for each shard index:

```bash
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=0 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=1 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=2 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=3 python3 -m TenantRuntime
```

The current handlers provide tenant-scoped sales, payment review,
Hiddify/X-UI/X-NET provisioning, paid renewal, subscription lifecycle controls
and support tools.
Sanaei uses an encrypted Bearer API token; Alireza uses encrypted
username/password and an optional encrypted Xray application secret header.
The implementation is independent and does not import the reference SellBot.
