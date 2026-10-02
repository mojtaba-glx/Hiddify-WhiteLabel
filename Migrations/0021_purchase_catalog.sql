-- Phase 3: tenant purchase catalog and deterministic customer-selected server.
-- Categories are tenant-scoped because WhiteLabel sale plans are tenant-wide.
-- Existing plans remain uncategorized and continue to be purchasable.

CREATE TABLE IF NOT EXISTS tenant_plan_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, title)
);

CREATE INDEX IF NOT EXISTS idx_tenant_plan_categories
    ON tenant_plan_categories (tenant_id, status, priority, id);

ALTER TABLE tenant_sale_plans
    ADD COLUMN category_id INTEGER
        REFERENCES tenant_plan_categories(id) ON DELETE SET NULL;

ALTER TABLE tenant_sale_plans
    ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tenant_sale_plans_catalog
    ON tenant_sale_plans (tenant_id, status, category_id, priority, id);

ALTER TABLE tenant_orders
    ADD COLUMN selected_server_id INTEGER
        REFERENCES tenant_servers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_orders_selected_server
    ON tenant_orders (tenant_id, selected_server_id, id);
