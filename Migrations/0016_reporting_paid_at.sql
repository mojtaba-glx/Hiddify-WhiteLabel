-- Stable tenant reporting dimensions and payment timestamps.

ALTER TABLE tenant_orders ADD COLUMN paid_at TEXT;

UPDATE tenant_orders
SET paid_at = COALESCE(
    (
        SELECT MAX(r.reviewed_at)
        FROM tenant_receipts r
        WHERE r.order_id = tenant_orders.id
          AND r.tenant_id = tenant_orders.tenant_id
          AND r.status = 'approved'
    ),
    updated_at
)
WHERE paid_at IS NULL
  AND status IN ('paid', 'fulfilled');

CREATE INDEX IF NOT EXISTS idx_tenant_orders_paid_at
    ON tenant_orders (tenant_id, paid_at, currency);

CREATE INDEX IF NOT EXISTS idx_tenant_orders_reporting
    ON tenant_orders (tenant_id, status, paid_at, id);

CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_reporting
    ON tenant_subscriptions (tenant_id, status, expires_at, id);

CREATE INDEX IF NOT EXISTS idx_tenant_customers_search
    ON tenant_customers (tenant_id, display_name, username, telegram_user_id);
