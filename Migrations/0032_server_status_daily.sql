-- SellBot-compatible Tenant-scoped daily traffic baseline for server status.
CREATE TABLE IF NOT EXISTS tenant_server_traffic_daily (
    tenant_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    baseline_gb REAL NOT NULL DEFAULT 0,
    last_total_gb REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, server_id, day),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (server_id) REFERENCES tenant_servers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tenant_server_traffic_daily_server
ON tenant_server_traffic_daily(tenant_id, server_id, day);
