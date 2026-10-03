-- Keep the public subscription endpoint separate from optional management API.
ALTER TABLE tenant_servers ADD COLUMN xnet_api_url TEXT;
