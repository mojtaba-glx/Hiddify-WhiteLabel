# Tenant provisioning — Phase 4

`TenantProvisioner` verifies both Telegram bot tokens, encrypts each token,
creates the tenant namespace and both bot rows in one transaction, and returns
two newly generated webhook secrets exactly once. Only SHA-256 hashes of those
webhook secrets are stored in SQLite.

Disabling a provisioned tenant changes `tenants.status` and
`tenant_runtime_configs.status`; bot rows, license history and tenant data are
kept. Re-enabling requires both bot roles to be active. The runtime license
gate still decides whether updates may run, so enabling a tenant does not
bypass an expired or suspended license.
