-- Durable subscription reminders + global enforcer runtime state.

ALTER TABLE tenant_subscriptions ADD COLUMN enforcement_pending INTEGER NOT NULL DEFAULT 0
    CHECK (enforcement_pending IN (0, 1));
ALTER TABLE tenant_subscriptions ADD COLUMN enforcement_error TEXT;
ALTER TABLE tenant_subscriptions ADD COLUMN enforced_at TEXT;

ALTER TABLE tenant_subscription_nodes ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0
    CHECK (fail_count >= 0);
ALTER TABLE tenant_subscription_nodes ADD COLUMN frozen_at TEXT;

CREATE TABLE IF NOT EXISTS tenant_subscription_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions(id) ON DELETE CASCADE,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('days', 'usage', 'expired')),
    stage_value INTEGER NOT NULL DEFAULT 0 CHECK (stage_value >= 0),
    period_key TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent', 'failed', 'skipped')),
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    locked_at TEXT,
    next_attempt_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_sub_notif_due
    ON tenant_subscription_notifications(status, scheduled_at, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_tenant_sub_notif_tenant
    ON tenant_subscription_notifications(tenant_id, status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_tenant_sub_enforcement
    ON tenant_subscriptions(tenant_id, enforcement_pending, status, expires_at);
