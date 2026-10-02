-- Tenant-scoped UserBot management settings and broadcast audit.
-- Every row is isolated by tenant_id. Values are non-secret JSON.

CREATE TABLE IF NOT EXISTS tenant_userbot_settings (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_userbot_settings_updated
    ON tenant_userbot_settings (tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS tenant_broadcast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    segment TEXT NOT NULL,
    message_kind TEXT NOT NULL DEFAULT 'text'
        CHECK (message_kind IN ('text','photo','document')),
    target_count INTEGER NOT NULL DEFAULT 0 CHECK (target_count >= 0),
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenant_broadcast_runs
    ON tenant_broadcast_runs (tenant_id, id DESC);
