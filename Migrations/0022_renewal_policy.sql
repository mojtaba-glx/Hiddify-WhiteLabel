-- Phase 4: snapshot renewal behavior on each renewal order.
-- This prevents an admin policy change after checkout from silently changing
-- how an already-created renewal will be fulfilled.

ALTER TABLE tenant_renewal_orders
    ADD COLUMN renew_volume_mode TEXT NOT NULL DEFAULT 'reset'
        CHECK (renew_volume_mode IN ('add','reset'));

ALTER TABLE tenant_renewal_orders
    ADD COLUMN renew_time_mode TEXT NOT NULL DEFAULT 'reset'
        CHECK (renew_time_mode IN ('add','reset'));
