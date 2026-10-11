-- Per-subscription panel-presence probe (Hiddify-SellBot parity).
--
-- Mirrors SellBot's userbot_service_probe: when a subscription vanishes
-- from its panel(s), the row is hidden from the customer immediately while
-- a bounded grace timer runs. Only after the configured TTL (default 7
-- days, see missing_service_delete_days) is the row purged, so a temporary
-- panel outage can never delete a real subscription. A later sighting
-- resets the streak via mark_service_seen.
--
-- Only subscriptions missing on EVERY panel target accumulate a streak;
-- partially unreachable topologies stay visible without any streak.

CREATE TABLE IF NOT EXISTS tenant_service_probe (
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES tenant_subscriptions (id)
        ON DELETE CASCADE,
    missing_streak INTEGER NOT NULL DEFAULT 0,
    first_missing_at TEXT,
    last_seen_at TEXT,
    last_missing_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subscription_id)
);
