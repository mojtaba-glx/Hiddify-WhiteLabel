-- Customer-facing names and resumable credential rotation (no release bump).
ALTER TABLE tenant_subscriptions ADD COLUMN service_name TEXT NOT NULL DEFAULT '';
CREATE TABLE tenant_subscription_rotations (
 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
 subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions(id),
 new_ref TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id, subscription_id)
);
ALTER TABLE tenant_subscription_nodes ADD COLUMN usage_offset_bytes INTEGER NOT NULL DEFAULT 0 CHECK (usage_offset_bytes >= 0);
