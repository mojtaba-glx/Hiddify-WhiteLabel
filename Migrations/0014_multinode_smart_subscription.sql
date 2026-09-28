-- Multi-node subscription mappings and automatic managed smart links.

CREATE TABLE IF NOT EXISTS tenant_subscription_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES tenant_servers (id) ON DELETE RESTRICT,
    external_ref TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'disabled', 'expired', 'error')),
    usage_bytes INTEGER NOT NULL DEFAULT 0 CHECK (usage_bytes >= 0),
    last_online TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, subscription_id, server_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_subscription_nodes_sub
    ON tenant_subscription_nodes (tenant_id, subscription_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_subscription_primary
    ON tenant_subscription_nodes (tenant_id, subscription_id)
    WHERE is_primary = 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_subscription_smart_target
    ON tenant_smart_links (tenant_id, target)
    WHERE target LIKE 'subscription:%';
