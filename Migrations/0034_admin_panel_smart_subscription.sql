-- Tenant-scoped smart links for AdminBot-created native panel users.
-- These users do not have tenant_subscriptions rows, so they need their own
-- idempotent target namespace for managed multi-node subscription delivery.

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_panel_user_smart_target
ON tenant_smart_links (tenant_id, target)
WHERE target LIKE 'paneluser:%';
