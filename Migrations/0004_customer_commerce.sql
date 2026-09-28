-- Phase 8: customer storefront, wallet, payments and one-time trials.
-- Money is stored as integer minor units.  Payment destinations are public
-- presentation data; credentials/API secrets must never be stored here.

ALTER TABLE license_plans ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'
    CHECK (length(currency) BETWEEN 3 AND 8);
ALTER TABLE license_plans ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1
    CHECK (is_public IN (0, 1));
ALTER TABLE license_plans ADD COLUMN trial_days INTEGER NOT NULL DEFAULT 0
    CHECK (trial_days >= 0);

CREATE TABLE IF NOT EXISTS platform_customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE CHECK (telegram_user_id > 0),
    display_name TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'blocked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('card', 'crypto')),
    title TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    destination TEXT NOT NULL,
    recipient TEXT,
    network TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES platform_customers (id) ON DELETE RESTRICT,
    plan_id INTEGER REFERENCES license_plans (id) ON DELETE RESTRICT,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('purchase', 'renewal', 'wallet_topup', 'trial')),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment', 'payment_review', 'paid', 'fulfilled', 'cancelled', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'wallet_topup' AND plan_id IS NULL) OR
           (kind IN ('purchase', 'renewal', 'trial') AND plan_id IS NOT NULL)),
    CHECK (kind <> 'renewal' OR tenant_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS payment_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES customer_orders (id) ON DELETE RESTRICT,
    payment_method_id INTEGER NOT NULL REFERENCES payment_methods (id) ON DELETE RESTRICT,
    reference TEXT,
    telegram_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by INTEGER,
    review_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK (reference IS NOT NULL OR telegram_file_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_one_pending_per_order
    ON payment_receipts (order_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS wallet_accounts (
    customer_id INTEGER NOT NULL REFERENCES platform_customers (id) ON DELETE RESTRICT,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, currency)
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES platform_customers (id) ON DELETE RESTRICT,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount <> 0),
    kind TEXT NOT NULL CHECK (kind IN ('deposit', 'purchase', 'refund', 'admin')),
    order_id INTEGER REFERENCES customer_orders (id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    resulting_balance INTEGER NOT NULL CHECK (resulting_balance >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL UNIQUE REFERENCES platform_customers (id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES license_plans (id) ON DELETE RESTRICT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES customer_orders (id) ON DELETE RESTRICT,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'eligible'
        CHECK (status IN ('eligible', 'issued', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_status ON platform_customers (status);
CREATE INDEX IF NOT EXISTS idx_payment_methods_active ON payment_methods (status, currency);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON customer_orders (customer_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON customer_orders (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_status ON payment_receipts (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_customer ON wallet_transactions (customer_id, id DESC);
