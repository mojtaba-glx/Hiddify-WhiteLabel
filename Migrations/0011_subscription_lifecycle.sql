-- Subscription lifecycle metadata and renewal-order linkage.
ALTER TABLE tenant_subscriptions ADD COLUMN last_online TEXT;
ALTER TABLE tenant_subscriptions ADD COLUMN last_synced_at TEXT;
ALTER TABLE tenant_subscriptions ADD COLUMN expired_at TEXT;

CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_lifecycle
ON tenant_subscriptions(tenant_id, status, expires_at);

CREATE TABLE IF NOT EXISTS tenant_renewal_orders (
    order_id INTEGER PRIMARY KEY REFERENCES tenant_orders(id) ON DELETE RESTRICT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_renewal_orders_subscription
ON tenant_renewal_orders(tenant_id, subscription_id, order_id);
