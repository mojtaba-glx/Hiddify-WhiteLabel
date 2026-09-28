-- Provider extensibility + live X-NET routing metadata.
-- Keep legacy panel_kind unchanged for backward-compatible CHECK constraints.
ALTER TABLE tenant_servers ADD COLUMN provider_kind TEXT
    CHECK (provider_kind IS NULL OR provider_kind IN ('manual', 'hiddify', 'xui', 'xnet'));

UPDATE tenant_servers
SET provider_kind = panel_kind
WHERE provider_kind IS NULL;

ALTER TABLE tenant_servers ADD COLUMN xnet_inbound_ids TEXT;
ALTER TABLE tenant_servers ADD COLUMN xnet_public_origin TEXT;
ALTER TABLE tenant_servers ADD COLUMN xnet_sub_port INTEGER
    CHECK (xnet_sub_port IS NULL OR (xnet_sub_port > 0 AND xnet_sub_port <= 65535));
ALTER TABLE tenant_servers ADD COLUMN xnet_sub_path TEXT;
