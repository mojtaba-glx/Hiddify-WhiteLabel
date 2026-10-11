-- Tenant-scoped bank-SMS auto-approval (Hiddify-SellBot parity).
--
-- Each tenant gets its own webhook secret and enable flag so one tenant's
-- bank SMS can never approve another tenant's payments. Secrets are stored
-- as Fernet ciphertext (same cipher as panel credentials) and compared only
-- after decryption; the Android app posts to
-- /payment/sms/{tenant_public_id} with the secret in the
-- X-WhiteLabel-Sms-Secret header (X-SellBot-Sms-Secret accepted too).
-- Events are idempotent per (tenant_id, event_id) and record which receipt
-- they approved, mirroring SellBot's userbot_sms_webhook_events statuses.

CREATE TABLE IF NOT EXISTS tenant_sms_settings (
    tenant_id INTEGER NOT NULL PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    encrypted_secret TEXT,
    max_pending_age_minutes INTEGER NOT NULL DEFAULT 360,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_sms_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    amount_raw INTEGER NOT NULL DEFAULT 0,
    currency_raw TEXT NOT NULL DEFAULT '',
    amount_toman INTEGER NOT NULL DEFAULT 0,
    reference TEXT NOT NULL DEFAULT '',
    card_last4 TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN (
            'received', 'test_received', 'invalid_amount', 'no_pending_match',
            'ambiguous', 'approved', 'approved_duplicate', 'attached_manual',
            'approve_failed'
        )),
    message TEXT NOT NULL DEFAULT '',
    matched_receipt_id INTEGER,
    received_at INTEGER NOT NULL DEFAULT 0,
    device_time INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_sms_events_match
    ON tenant_sms_events (tenant_id, status, amount_toman, created_at);
