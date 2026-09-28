-- Live X-UI routing metadata. Secrets stay encrypted in tenant_panel_credentials.
ALTER TABLE tenant_servers ADD COLUMN xui_flavor TEXT
    CHECK (xui_flavor IS NULL OR xui_flavor IN ('sanaei', 'alireza'));
ALTER TABLE tenant_servers ADD COLUMN xui_inbound_ids TEXT;
ALTER TABLE tenant_servers ADD COLUMN xui_public_origin TEXT;
ALTER TABLE tenant_servers ADD COLUMN xui_sub_path TEXT;
