-- Phase 11: provider-ready Tenant payment metadata and review audit fields.
-- Existing card/crypto rows remain valid; provider_key is the canonical extension
-- point so UserBot does not need provider-specific branches.

ALTER TABLE tenant_payment_methods ADD COLUMN provider_key TEXT NOT NULL DEFAULT '';
ALTER TABLE tenant_payment_methods ADD COLUMN priority INTEGER NOT NULL DEFAULT 100
    CHECK (priority >= 0);
ALTER TABLE tenant_payment_methods ADD COLUMN provider_options_json TEXT NOT NULL DEFAULT '{}';

UPDATE tenant_payment_methods
SET provider_key = CASE kind
    WHEN 'card' THEN 'card_manual'
    WHEN 'crypto' THEN 'crypto_manual'
    ELSE kind
END
WHERE provider_key = '';

ALTER TABLE tenant_receipts ADD COLUMN review_note TEXT NOT NULL DEFAULT '';
ALTER TABLE tenant_receipts ADD COLUMN provider_event_id TEXT;
ALTER TABLE tenant_wallet_topup_receipts ADD COLUMN review_note TEXT NOT NULL DEFAULT '';
ALTER TABLE tenant_wallet_topup_receipts ADD COLUMN provider_event_id TEXT;

CREATE TABLE IF NOT EXISTS tenant_payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    payment_source TEXT NOT NULL
        CHECK (payment_source IN ('order','wallet_topup','wallet_order')),
    payment_ref_id INTEGER NOT NULL CHECK (payment_ref_id > 0),
    provider_key TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('submitted','approved','rejected','provider_event')),
    actor_id INTEGER,
    external_event_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_payment_events_lookup
    ON tenant_payment_events (tenant_id, payment_source, payment_ref_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tenant_payment_methods_priority
    ON tenant_payment_methods (tenant_id, status, priority, id);
