-- Track the actual payment moment separately from later fulfillment updates.

ALTER TABLE customer_orders ADD COLUMN paid_at TEXT;

UPDATE customer_orders
SET paid_at = updated_at
WHERE paid_at IS NULL
  AND status IN ('paid', 'fulfilled')
  AND kind <> 'trial';

CREATE INDEX IF NOT EXISTS idx_orders_paid_at
    ON customer_orders (paid_at, currency);
