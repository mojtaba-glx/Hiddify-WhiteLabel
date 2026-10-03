-- Phase 10: Hiddify-SellBot referral parity.

ALTER TABLE tenant_sales_growth_settings
ADD COLUMN referral_trial_reward_enabled INTEGER NOT NULL DEFAULT 1
CHECK (referral_trial_reward_enabled IN (0,1));

ALTER TABLE tenant_sales_growth_settings
ADD COLUMN referral_purchase_reward_enabled INTEGER NOT NULL DEFAULT 1
CHECK (referral_purchase_reward_enabled IN (0,1));

ALTER TABLE tenant_sales_growth_settings
ADD COLUMN referral_invite_text TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS tenant_referral_manual_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    wallet_transaction_id INTEGER NOT NULL UNIQUE
        REFERENCES tenant_wallet_transactions(id) ON DELETE RESTRICT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_referral_manual_rewards_customer
ON tenant_referral_manual_rewards (tenant_id, customer_id, id DESC);
