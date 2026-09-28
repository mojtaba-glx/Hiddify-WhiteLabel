-- Master storefront settings.
-- Values are non-secret product/UI configuration. Secrets remain in .env or
-- their dedicated encrypted credential tables.

CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_platform_settings_updated
    ON platform_settings (updated_at);
