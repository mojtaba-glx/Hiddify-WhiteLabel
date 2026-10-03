-- Phase 12: threaded Tenant tickets with cross-bot media storage.
-- Existing ticket rows remain intact; their body/admin_reply are backfilled into
-- the message stream so old conversations are not lost.

CREATE TABLE IF NOT EXISTS tenant_ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    ticket_id INTEGER NOT NULL REFERENCES tenant_tickets(id) ON DELETE RESTRICT,
    sender_type TEXT NOT NULL CHECK (sender_type IN ('user','admin')),
    sender_name TEXT NOT NULL DEFAULT '',
    message_text TEXT NOT NULL DEFAULT '',
    media_mime TEXT NOT NULL DEFAULT '',
    media BLOB,
    created_at TEXT NOT NULL,
    CHECK (length(message_text) > 0 OR media IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_tenant_ticket_messages_ticket
    ON tenant_ticket_messages (tenant_id, ticket_id, id);

INSERT INTO tenant_ticket_messages
    (tenant_id, ticket_id, sender_type, sender_name, message_text, created_at)
SELECT
    t.tenant_id,
    t.id,
    'user',
    COALESCE(c.display_name, c.username, CAST(c.telegram_user_id AS TEXT)),
    t.body,
    t.created_at
FROM tenant_tickets t
JOIN tenant_customers c
    ON c.id=t.customer_id AND c.tenant_id=t.tenant_id
WHERE trim(COALESCE(t.body,'')) <> '';

INSERT INTO tenant_ticket_messages
    (tenant_id, ticket_id, sender_type, sender_name, message_text, created_at)
SELECT
    t.tenant_id,
    t.id,
    'admin',
    'پشتیبانی',
    t.admin_reply,
    t.updated_at
FROM tenant_tickets t
WHERE trim(COALESCE(t.admin_reply,'')) <> '';
