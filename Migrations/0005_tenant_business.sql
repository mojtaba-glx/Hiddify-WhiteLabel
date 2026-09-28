-- Phase 8: tenant-owned business data for the dedicated AdminBot/UserBot.
-- Every business table carries tenant_id; there are no per-tenant databases.

CREATE TABLE IF NOT EXISTS tenant_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    label TEXT NOT NULL,
    panel_kind TEXT NOT NULL DEFAULT 'manual'
        CHECK (panel_kind IN ('manual', 'hiddify', 'xui')),
    endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'offline', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, label)
);

CREATE TABLE IF NOT EXISTS tenant_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    server_id INTEGER REFERENCES tenant_servers (id) ON DELETE RESTRICT,
    label TEXT NOT NULL,
    location TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'offline', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, label)
);

CREATE TABLE IF NOT EXISTS tenant_sale_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    traffic_gb INTEGER NOT NULL CHECK (traffic_gb > 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    price INTEGER NOT NULL CHECK (price >= 0),
    currency TEXT NOT NULL DEFAULT 'IRR' CHECK (length(currency) BETWEEN 3 AND 8),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS tenant_customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    telegram_user_id INTEGER NOT NULL CHECK (telegram_user_id > 0),
    display_name TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'blocked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, telegram_user_id)
);

CREATE TABLE IF NOT EXISTS tenant_payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('card', 'crypto')),
    title TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    destination TEXT NOT NULL,
    network TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, title)
);

CREATE TABLE IF NOT EXISTS tenant_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers (id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES tenant_sale_plans (id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment', 'payment_review', 'paid', 'fulfilled', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    order_id INTEGER NOT NULL REFERENCES tenant_orders (id) ON DELETE RESTRICT,
    payment_method_id INTEGER NOT NULL REFERENCES tenant_payment_methods (id) ON DELETE RESTRICT,
    reference TEXT,
    telegram_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by INTEGER,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK (reference IS NOT NULL OR telegram_file_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_receipt_pending
    ON tenant_receipts (order_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers (id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES tenant_sale_plans (id) ON DELETE RESTRICT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES tenant_orders (id) ON DELETE RESTRICT,
    server_id INTEGER REFERENCES tenant_servers (id) ON DELETE RESTRICT,
    external_ref TEXT,
    status TEXT NOT NULL DEFAULT 'pending_provisioning'
        CHECK (status IN ('pending_provisioning', 'active', 'disabled', 'expired')),
    usage_bytes INTEGER NOT NULL DEFAULT 0 CHECK (usage_bytes >= 0),
    traffic_bytes INTEGER NOT NULL CHECK (traffic_bytes > 0),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers (id) ON DELETE RESTRICT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    admin_reply TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'answered', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_smart_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    code TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_servers ON tenant_servers (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_nodes ON tenant_nodes (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_sale_plans ON tenant_sale_plans (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_customers ON tenant_customers (tenant_id, telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_tenant_orders ON tenant_orders (tenant_id, customer_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_receipts ON tenant_receipts (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions ON tenant_subscriptions (tenant_id, customer_id, status);
CREATE INDEX IF NOT EXISTS idx_tenant_tickets ON tenant_tickets (tenant_id, status);
