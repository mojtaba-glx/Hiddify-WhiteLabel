-- Legacy Hiddify-SellBot -> WhiteLabel Tenant restore support.
-- Raw source members are preserved tenant-scoped so no information is lost
-- even when a legacy table has no native WhiteLabel equivalent yet.

CREATE TABLE IF NOT EXISTS tenant_legacy_restore_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    restore_id TEXT NOT NULL,
    source_format TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK(status IN ('completed','failed')),
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, restore_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_legacy_restore_runs
    ON tenant_legacy_restore_runs(tenant_id, id DESC);

CREATE TABLE IF NOT EXISTS tenant_legacy_restore_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    restore_id TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK(kind IN ('database','json','media','panel_backup','manifest','other')),
    size INTEGER NOT NULL DEFAULT 0 CHECK(size >= 0),
    sha256 TEXT NOT NULL CHECK(length(sha256)=64),
    encoding TEXT NOT NULL DEFAULT 'fernet-chunked-b64',
    content BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, restore_id, path)
);

CREATE INDEX IF NOT EXISTS idx_tenant_legacy_restore_assets
    ON tenant_legacy_restore_assets(tenant_id, restore_id, kind, id);
