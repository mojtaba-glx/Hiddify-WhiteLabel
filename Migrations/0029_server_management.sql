-- Panel inventory, server-owned catalog/domains, and admin-created node mappings.
ALTER TABLE tenant_sale_plans ADD COLUMN server_id INTEGER REFERENCES tenant_servers(id);
ALTER TABLE tenant_sale_plans ADD COLUMN is_dynamic INTEGER NOT NULL DEFAULT 0;
CREATE TABLE tenant_panel_users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
 server_id INTEGER NOT NULL REFERENCES tenant_servers(id),
 external_ref TEXT NOT NULL,
 name TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
 usage_bytes INTEGER NOT NULL DEFAULT 0, traffic_bytes INTEGER,
 expires_at TEXT, last_online TEXT,
 active INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','pending','deleted')),
 extra_json TEXT NOT NULL DEFAULT '{}', last_synced_at TEXT,
 UNIQUE(tenant_id,server_id,external_ref)
);
CREATE TABLE tenant_panel_user_nodes (
 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
 source_user_id INTEGER NOT NULL REFERENCES tenant_panel_users(id),
 server_id INTEGER NOT NULL REFERENCES tenant_servers(id),
 external_ref TEXT NOT NULL, last_error TEXT, fail_count INTEGER NOT NULL DEFAULT 0,
 frozen_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(tenant_id,source_user_id,server_id)
);
CREATE TABLE tenant_server_domains (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
 server_id INTEGER NOT NULL REFERENCES tenant_servers(id),
 title TEXT NOT NULL, origin TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
 UNIQUE(tenant_id,server_id,origin)
);
CREATE UNIQUE INDEX tenant_server_domain_primary ON tenant_server_domains(tenant_id,server_id) WHERE is_primary=1;
CREATE TABLE tenant_server_sales_settings (
 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
 server_id INTEGER NOT NULL REFERENCES tenant_servers(id),
 settings_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(tenant_id,server_id)
);
CREATE INDEX tenant_panel_user_list ON tenant_panel_users(tenant_id,server_id,state,id);
