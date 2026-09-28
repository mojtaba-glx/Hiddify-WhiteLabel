-- Phase 5: durable state scoped by tenant + bot role + Telegram user.

CREATE TABLE IF NOT EXISTS tenant_user_state (
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    bot_role TEXT NOT NULL CHECK (bot_role IN ('admin', 'user')),
    telegram_user_id INTEGER NOT NULL CHECK (telegram_user_id > 0),
    state_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(state_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, bot_role, telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_state_updated
    ON tenant_user_state (tenant_id, updated_at);
