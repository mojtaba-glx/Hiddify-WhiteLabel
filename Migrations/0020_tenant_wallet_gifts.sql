-- Tenant-scoped wallet gift vouchers used by the SellBot-compatible
-- "مدیریت هدایا" flow. Gift redemption is independently auditable and updates
-- the tenant wallet atomically without overloading discount coupons.

CREATE TABLE IF NOT EXISTS tenant_gift_vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    code TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL DEFAULT 'IRR' CHECK (length(currency) BETWEEN 3 AND 8),
    max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses > 0),
    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS tenant_gift_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    voucher_id INTEGER NOT NULL REFERENCES tenant_gift_vouchers(id) ON DELETE RESTRICT,
    customer_id INTEGER NOT NULL REFERENCES tenant_customers(id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL CHECK (length(currency) BETWEEN 3 AND 8),
    resulting_balance INTEGER NOT NULL CHECK (resulting_balance >= 0),
    redeemed_at TEXT NOT NULL,
    UNIQUE (tenant_id, voucher_id, customer_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_gifts_status
    ON tenant_gift_vouchers (tenant_id, status, expires_at, id DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_gift_redemptions
    ON tenant_gift_redemptions (tenant_id, voucher_id, id DESC);
