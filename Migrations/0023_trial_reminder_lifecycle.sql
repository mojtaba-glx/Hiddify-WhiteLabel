-- Phase 8: free-trial announcement parity with Hiddify-SellBot.

ALTER TABLE tenant_sales_growth_settings
ADD COLUMN trial_announce_enabled INTEGER NOT NULL DEFAULT 1
CHECK (trial_announce_enabled IN (0,1));
