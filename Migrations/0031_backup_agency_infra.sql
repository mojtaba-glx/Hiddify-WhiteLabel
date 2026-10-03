-- Phase 15: durable Tenant backup bookkeeping and shared AgentBot/CustomerBot schema.
-- No AgentBot/CustomerBot runtime/menu is enabled by this migration.  These tables
-- only freeze ownership, wallet, access and attribution contracts for later phases.

CREATE TABLE IF NOT EXISTS tenant_backup_runs (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    slot_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed','success','failed')),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    file_size INTEGER NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    sha256 TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, slot_key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_backup_runs_status
    ON tenant_backup_runs (tenant_id, status, slot_key DESC);

CREATE TABLE IF NOT EXISTS tenant_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    telegram_user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    tier_code TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_agents_status
    ON tenant_agents (tenant_id, status, id DESC);

CREATE TABLE IF NOT EXISTS tenant_agent_wallet_accounts (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, currency)
);

CREATE TABLE IF NOT EXISTS tenant_agent_wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    amount INTEGER NOT NULL CHECK (amount <> 0),
    kind TEXT NOT NULL CHECK (
        kind IN ('topup','admin_credit','admin_debit','purchase',
                 'refund','adjustment','customer_sale')
    ),
    idempotency_key TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    resulting_balance INTEGER NOT NULL CHECK (resulting_balance >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_agent_wallet_history
    ON tenant_agent_wallet_transactions (tenant_id, agent_id, id DESC);

CREATE TABLE IF NOT EXISTS tenant_customer_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    encrypted_token TEXT NOT NULL,
    token_fingerprint TEXT NOT NULL UNIQUE,
    token_tail TEXT NOT NULL,
    telegram_bot_id INTEGER UNIQUE,
    telegram_username TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','active','disabled')),
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_customer_bots_agent
    ON tenant_customer_bots (tenant_id, agent_id, status, id DESC);

CREATE TABLE IF NOT EXISTS tenant_agent_server_access (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    server_id INTEGER NOT NULL REFERENCES tenant_servers(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, server_id)
);

CREATE TABLE IF NOT EXISTS tenant_agent_plan_access (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES tenant_sale_plans(id) ON DELETE RESTRICT,
    wholesale_price INTEGER NOT NULL DEFAULT 0 CHECK (wholesale_price >= 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, plan_id)
);

CREATE TABLE IF NOT EXISTS tenant_agent_customers (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK (source IN ('agent','customer_bot')),
    customer_bot_id INTEGER REFERENCES tenant_customer_bots(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, customer_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_agent_customers_customer
    ON tenant_agent_customers (tenant_id, customer_id, agent_id);

CREATE TABLE IF NOT EXISTS tenant_agent_subscriptions (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    agent_id INTEGER NOT NULL REFERENCES tenant_agents(id) ON DELETE RESTRICT,
    subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK (source IN ('agent','customer_bot')),
    customer_bot_id INTEGER REFERENCES tenant_customer_bots(id) ON DELETE RESTRICT,
    wholesale_amount INTEGER NOT NULL DEFAULT 0 CHECK (wholesale_amount >= 0),
    retail_amount INTEGER NOT NULL DEFAULT 0 CHECK (retail_amount >= 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_agent_subscriptions_agent
    ON tenant_agent_subscriptions (tenant_id, agent_id, subscription_id DESC);
