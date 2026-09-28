-- Deterministic per-tenant provisioning target for automatic order fulfillment.
ALTER TABLE tenant_servers ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_servers_default
ON tenant_servers(tenant_id)
WHERE is_default = 1;
