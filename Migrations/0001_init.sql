-- Migration 0001: initial (and, for now, only) White-Label schema.
-- Created in pre-release; the project has no production data, so there is
-- exactly one final baseline migration.
-- All timestamps are ISO-8601 UTC text. Monetary values are integer minor units.

-- This schema is the sole baseline. token_tail / webhook_secret_hash /
-- partial indexes are present from the very first version: the raw
-- webhook_secret column never exists.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    owner_telegram_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    -- Fernet ciphertext (gAAAA...) only; plain tokens are rejected by the repo.
    encrypted_token TEXT NOT NULL
        CHECK (length(encrypted_token) >= 50),
    token_fingerprint TEXT NOT NULL UNIQUE
        CHECK (length(token_fingerprint) = 64),
    token_tail TEXT NOT NULL
        CHECK (length(token_tail) = 4),
    telegram_bot_id INTEGER,
    telegram_username TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'revoked')),
    webhook_secret_hash TEXT
        CHECK (webhook_secret_hash IS NULL OR length(webhook_secret_hash) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, role)
);

CREATE TABLE IF NOT EXISTS license_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    price INTEGER NOT NULL CHECK (price >= 0),
    max_servers INTEGER NOT NULL CHECK (max_servers > 0),
    max_users INTEGER NOT NULL CHECK (max_users > 0),
    features TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived', 'disabled'))
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES license_plans (id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'grace', 'suspended', 'expired', 'cancelled')),
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    grace_until TEXT,
    suspended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    safe_metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    scheduled_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent', 'failed', 'skipped')),
    locked_at TEXT,
    next_attempt_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_tenants_owner ON tenants (owner_telegram_id);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants (status);
CREATE INDEX IF NOT EXISTS idx_bots_tenant ON tenant_bots (tenant_id);
CREATE INDEX IF NOT EXISTS idx_bots_fingerprint ON tenant_bots (token_fingerprint);
-- telegram_bot_id is UNIQUE only where it is set (SQLite partial index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_telegram_id_unique
    ON tenant_bots (telegram_bot_id) WHERE telegram_bot_id IS NOT NULL;
-- At most ONE current (active/grace) license per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS idx_licenses_one_current
    ON licenses (tenant_id) WHERE status IN ('active', 'grace');
CREATE INDEX IF NOT EXISTS idx_licenses_tenant ON licenses (tenant_id);
CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses (status);
CREATE INDEX IF NOT EXISTS idx_licenses_expires ON licenses (expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON audit_events (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_notif_status_scheduled ON notification_events (status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_notif_tenant ON notification_events (tenant_id);
