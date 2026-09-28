-- Phase 9: one encrypted panel credential per tenant server.
-- Endpoint/config are non-secret routing metadata. The credential is Fernet
-- ciphertext only; raw panel keys/passwords never enter runtime state or logs.

CREATE TABLE IF NOT EXISTS tenant_panel_credentials (
    server_id INTEGER PRIMARY KEY REFERENCES tenant_servers (id) ON DELETE RESTRICT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    encrypted_secret TEXT NOT NULL CHECK (length(encrypted_secret) >= 60),
    secret_fingerprint TEXT NOT NULL CHECK (length(secret_fingerprint) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, secret_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_panel_credentials_tenant ON tenant_panel_credentials (tenant_id);
