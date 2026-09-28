-- Phase 4: one durable runtime namespace per tenant.
-- Tenant business data stays in the shared database and every future table
-- must be scoped by tenant_id. No per-customer source tree or database is
-- created.

CREATE TABLE IF NOT EXISTS tenant_runtime_configs (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants (id) ON DELETE RESTRICT,
    data_namespace TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'provisioning'
        CHECK (status IN ('provisioning', 'ready', 'disabled', 'error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_status
    ON tenant_runtime_configs (status);
