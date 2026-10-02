-- Tenant AdminBot server-management parity with Hiddify-SellBot.
-- Adds operational server capacity/priority and an explicit parent relation
-- for multi-node topology. Existing NULL parent rows remain legacy/global.

ALTER TABLE tenant_servers ADD COLUMN users_limit INTEGER NOT NULL DEFAULT 0
    CHECK (users_limit >= 0);
ALTER TABLE tenant_servers ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
    CHECK (priority >= 0);

ALTER TABLE tenant_nodes ADD COLUMN parent_server_id INTEGER
    REFERENCES tenant_servers (id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_tenant_nodes_parent
    ON tenant_nodes (tenant_id, parent_server_id, status);
