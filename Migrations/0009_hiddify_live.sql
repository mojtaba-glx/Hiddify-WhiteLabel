-- Phase 10: non-secret Hiddify routing metadata.
-- The API key remains encrypted in tenant_panel_credentials.

ALTER TABLE tenant_servers ADD COLUMN admin_path TEXT;
ALTER TABLE tenant_servers ADD COLUMN user_path TEXT;
