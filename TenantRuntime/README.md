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

Run one process for each shard index:

```bash
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=0 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=1 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=2 python3 -m TenantRuntime
RUNTIME_SHARD_COUNT=4 RUNTIME_SHARD_INDEX=3 python3 -m TenantRuntime
```

The current handlers provide the licensed tenant-specific Admin/User runtime
shell and durable isolated state. VPN panel, sales, payment, and ticket feature
modules are separate product work and are not copied from the reference bot.
