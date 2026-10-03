-- Phase 13: richer broadcast audit supporting video, buttons and delivery details.
-- Rebuild the existing audit table so the message_kind CHECK can safely accept
-- video while preserving all historical rows.

DROP INDEX IF EXISTS idx_tenant_broadcast_runs;

ALTER TABLE tenant_broadcast_runs RENAME TO tenant_broadcast_runs_phase13_old;

CREATE TABLE tenant_broadcast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    segment TEXT NOT NULL,
    message_kind TEXT NOT NULL DEFAULT 'text'
        CHECK (message_kind IN ('text','photo','video','document')),
    target_count INTEGER NOT NULL DEFAULT 0 CHECK (target_count >= 0),
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    buttons_count INTEGER NOT NULL DEFAULT 0 CHECK (buttons_count >= 0),
    recovered_count INTEGER NOT NULL DEFAULT 0 CHECK (recovered_count >= 0),
    unreachable_count INTEGER NOT NULL DEFAULT 0 CHECK (unreachable_count >= 0),
    temporary_count INTEGER NOT NULL DEFAULT 0 CHECK (temporary_count >= 0),
    telegram_error_count INTEGER NOT NULL DEFAULT 0 CHECK (telegram_error_count >= 0),
    other_error_count INTEGER NOT NULL DEFAULT 0 CHECK (other_error_count >= 0),
    created_at TEXT NOT NULL,
    finished_at TEXT
);

INSERT INTO tenant_broadcast_runs
    (id, tenant_id, segment, message_kind, target_count, sent_count,
     failed_count, created_at, finished_at)
SELECT
    id, tenant_id, segment, message_kind, target_count, sent_count,
    failed_count, created_at, finished_at
FROM tenant_broadcast_runs_phase13_old;

DROP TABLE tenant_broadcast_runs_phase13_old;

CREATE INDEX idx_tenant_broadcast_runs
    ON tenant_broadcast_runs (tenant_id, id DESC);
