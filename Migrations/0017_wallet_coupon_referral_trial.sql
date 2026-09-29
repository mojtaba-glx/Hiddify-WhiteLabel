-- Tenant sales growth: wallet, coupons, referrals and one-time free trial.

ALTER TABLE tenant_orders ADD COLUMN order_kind TEXT NOT NULL DEFAULT 'purchase'
    CHECK (order_kind IN ('purchase','renewal','trial'));
ALTER TABLE tenant_orders ADD COLUMN original_amount INTEGER NOT NULL DEFAULT 0
    CHECK (original_amount >= 0);
ALTER TABLE tenant_orders ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0
    CHECK (discount_amount >= 0);
ALTER TABLE tenant_orders ADD COLUMN wallet_amount INTEGER NOT NULL DEFAULT 0
    CHECK (wallet_amount >= 0);
ALTER TABLE tenant_orders ADD COLUMN coupon_id INTEGER;

UPDATE tenant_orders
SET original_amount = amount
WHERE original_amount = 0 AND amount > 0;

UPDATE tenant_orders
SET order_kind = 'renewal'
WHERE id IN (SELECT order_id FROM tenant_renewal_orders);

ALTER TABLE tenant_customers ADD COLUMN referral_code TEXT;
ALTER TABLE tenant_customers ADD COLUMN invited_by_customer_id INTEGER;
ALTER TABLE tenant_customers ADD COLUMN trial_used_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_customer_referral_code
    ON tenant_customers (tenant_id, referral_code)
    WHERE referral_code IS NOT NULL AND referral_code <> '';

CREATE TABLE IF NOT EXISTS tenant_wallet_accounts (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, customer_id, currency)
);

CREATE TABLE IF NOT EXISTS tenant_wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    amount INTEGER NOT NULL CHECK (amount <> 0),
    kind TEXT NOT NULL CHECK (
        kind IN ('topup','admin_credit','admin_debit','referral_trial',
                 'referral_purchase','purchase','refund')
    ),
    order_id INTEGER REFERENCES tenant_orders(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    note TEXT,
    resulting_balance INTEGER NOT NULL CHECK (resulting_balance >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_wallet_history
    ON tenant_wallet_transactions (tenant_id, customer_id, id DESC);

CREATE TABLE IF NOT EXISTS tenant_coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    code TEXT NOT NULL,
    discount_kind TEXT NOT NULL CHECK (discount_kind IN ('percent','fixed')),
    value INTEGER NOT NULL CHECK (value > 0),
    currency TEXT,
    min_amount INTEGER NOT NULL DEFAULT 0 CHECK (min_amount >= 0),
    max_discount INTEGER NOT NULL DEFAULT 0 CHECK (max_discount >= 0),
    max_uses INTEGER NOT NULL DEFAULT 0 CHECK (max_uses >= 0),
    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
    per_customer_limit INTEGER NOT NULL DEFAULT 1 CHECK (per_customer_limit >= 0),
    starts_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS tenant_coupon_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    coupon_id INTEGER NOT NULL REFERENCES tenant_coupons(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES tenant_orders(id) ON DELETE RESTRICT,
    discount_amount INTEGER NOT NULL CHECK (discount_amount > 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_coupon_customer
    ON tenant_coupon_redemptions (tenant_id, coupon_id, customer_id);

CREATE TABLE IF NOT EXISTS tenant_sales_growth_settings (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
    referral_enabled INTEGER NOT NULL DEFAULT 0 CHECK (referral_enabled IN (0,1)),
    referral_trial_reward INTEGER NOT NULL DEFAULT 0 CHECK (referral_trial_reward >= 0),
    referral_purchase_reward INTEGER NOT NULL DEFAULT 0 CHECK (referral_purchase_reward >= 0),
    referral_min_purchase INTEGER NOT NULL DEFAULT 0 CHECK (referral_min_purchase >= 0),
    referral_max_rewards INTEGER NOT NULL DEFAULT 0 CHECK (referral_max_rewards >= 0),
    referral_currency TEXT NOT NULL DEFAULT 'IRR',
    trial_enabled INTEGER NOT NULL DEFAULT 0 CHECK (trial_enabled IN (0,1)),
    trial_traffic_gb INTEGER NOT NULL DEFAULT 1 CHECK (trial_traffic_gb > 0),
    trial_duration_days INTEGER NOT NULL DEFAULT 1 CHECK (trial_duration_days > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    inviter_customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    invitee_customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    invited_by_code TEXT NOT NULL,
    qualified INTEGER NOT NULL DEFAULT 1 CHECK (qualified IN (0,1)),
    fraud_flag INTEGER NOT NULL DEFAULT 0 CHECK (fraud_flag IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, invitee_customer_id)
);

CREATE TABLE IF NOT EXISTS tenant_referral_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    referral_id INTEGER NOT NULL REFERENCES tenant_referrals(id) ON DELETE RESTRICT,
    inviter_customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    invitee_customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    reward_type TEXT NOT NULL CHECK (reward_type IN ('trial','purchase')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL,
    order_id INTEGER REFERENCES tenant_orders(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'paid'
        CHECK (status IN ('paid','revoked')),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, referral_id, reward_type)
);

CREATE INDEX IF NOT EXISTS idx_tenant_referral_inviter
    ON tenant_referrals (tenant_id, inviter_customer_id);
CREATE INDEX IF NOT EXISTS idx_tenant_referral_rewards_inviter
    ON tenant_referral_rewards (tenant_id, inviter_customer_id, reward_type);

CREATE TABLE IF NOT EXISTS tenant_trial_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES tenant_orders(id) ON DELETE RESTRICT,
    subscription_id INTEGER REFERENCES tenant_subscriptions(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','issued','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, customer_id)
);

CREATE TABLE IF NOT EXISTS tenant_wallet_topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment','payment_review','paid','rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS tenant_wallet_topup_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    topup_id INTEGER NOT NULL REFERENCES tenant_wallet_topups(id) ON DELETE RESTRICT,
    payment_method_id INTEGER NOT NULL REFERENCES tenant_payment_methods(id) ON DELETE RESTRICT,
    reference TEXT,
    telegram_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    reviewed_by INTEGER,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK (reference IS NOT NULL OR telegram_file_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_wallet_topup_pending_receipt
    ON tenant_wallet_topup_receipts (topup_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_tenant_wallet_topups_admin
    ON tenant_wallet_topups (tenant_id, status, id DESC);

