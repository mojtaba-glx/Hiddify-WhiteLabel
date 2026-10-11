-- Delivery-loop retry bookkeeping for paid orders.
--
-- The direct-buy delivery loop retries panel fulfillment for approved
-- orders. Attempts and the last error are tracked here so repeated
-- failures can warn the admin instead of retrying silently forever.

ALTER TABLE tenant_orders
    ADD COLUMN fulfillment_attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE tenant_orders
    ADD COLUMN fulfillment_error TEXT;
