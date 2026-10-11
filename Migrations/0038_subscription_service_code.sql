-- SellBot-parity 7-digit service codes for subscriptions.
--
-- Hiddify-SellBot shows users a random 7-digit service code (stored in the
-- service comment) instead of the internal database id. WhiteLabel stores
-- it in a dedicated column so panels, renewals and support flows keep using
-- the internal id while every user-facing screen shows the code.
-- Backfill is deterministic and collision-free for realistic row counts
-- (7919 is coprime with 10^7, so (id * 7919) % 10^7 is injective).

ALTER TABLE tenant_subscriptions
    ADD COLUMN service_code TEXT;

UPDATE tenant_subscriptions
SET service_code = printf('%07d', (id * 7919) % 10000000)
WHERE service_code IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_subscriptions_service_code
    ON tenant_subscriptions (tenant_id, service_code);
