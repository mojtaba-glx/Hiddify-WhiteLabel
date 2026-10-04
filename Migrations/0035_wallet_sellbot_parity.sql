-- Repair malformed wallet currencies created by the old AdminBot wallet wizard.
-- The previous flow asked for "currency" first and accepted any 3-8 character
-- string, so entering the intended amount (e.g. 100000) could create an account
-- whose currency was literally "100000".  Preserve the balance and move it to
-- the customer's real commerce currency.

CREATE TABLE IF NOT EXISTS _wallet_currency_repair_0035 (
    tenant_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    bad_currency TEXT NOT NULL,
    target_currency TEXT NOT NULL,
    balance INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, customer_id, bad_currency)
);

DELETE FROM _wallet_currency_repair_0035;

INSERT INTO _wallet_currency_repair_0035
    (tenant_id, customer_id, bad_currency, target_currency, balance, updated_at)
SELECT
    a.tenant_id,
    a.customer_id,
    a.currency,
    COALESCE(
        (
            SELECT o.currency
            FROM tenant_orders o
            WHERE o.tenant_id=a.tenant_id
              AND o.customer_id=a.customer_id
              AND o.currency GLOB '[A-Za-z]*'
            ORDER BY o.id DESC
            LIMIT 1
        ),
        (
            SELECT p.currency
            FROM tenant_sale_plans p
            WHERE p.tenant_id=a.tenant_id
              AND p.status='active'
              AND p.currency GLOB '[A-Za-z]*'
            ORDER BY p.id DESC
            LIMIT 1
        ),
        (
            SELECT m.currency
            FROM tenant_payment_methods m
            WHERE m.tenant_id=a.tenant_id
              AND m.status='active'
              AND m.currency GLOB '[A-Za-z]*'
            ORDER BY m.priority ASC, m.id ASC
            LIMIT 1
        ),
        'IRR'
    ),
    a.balance,
    a.updated_at
FROM tenant_wallet_accounts a
WHERE a.currency GLOB '[0-9]*';

INSERT OR IGNORE INTO tenant_wallet_accounts
    (tenant_id, customer_id, currency, balance, updated_at)
SELECT tenant_id, customer_id, target_currency, 0, MAX(updated_at)
FROM _wallet_currency_repair_0035
GROUP BY tenant_id, customer_id, target_currency;

UPDATE tenant_wallet_accounts
SET balance = balance + COALESCE(
        (
            SELECT SUM(s.balance)
            FROM _wallet_currency_repair_0035 s
            WHERE s.tenant_id=tenant_wallet_accounts.tenant_id
              AND s.customer_id=tenant_wallet_accounts.customer_id
              AND s.target_currency=tenant_wallet_accounts.currency
        ),
        0
    ),
    updated_at = COALESCE(
        (
            SELECT MAX(s.updated_at)
            FROM _wallet_currency_repair_0035 s
            WHERE s.tenant_id=tenant_wallet_accounts.tenant_id
              AND s.customer_id=tenant_wallet_accounts.customer_id
              AND s.target_currency=tenant_wallet_accounts.currency
        ),
        updated_at
    )
WHERE EXISTS (
    SELECT 1
    FROM _wallet_currency_repair_0035 s
    WHERE s.tenant_id=tenant_wallet_accounts.tenant_id
      AND s.customer_id=tenant_wallet_accounts.customer_id
      AND s.target_currency=tenant_wallet_accounts.currency
);

UPDATE tenant_wallet_transactions
SET currency = (
    SELECT s.target_currency
    FROM _wallet_currency_repair_0035 s
    WHERE s.tenant_id=tenant_wallet_transactions.tenant_id
      AND s.customer_id=tenant_wallet_transactions.customer_id
      AND s.bad_currency=tenant_wallet_transactions.currency
    LIMIT 1
)
WHERE EXISTS (
    SELECT 1
    FROM _wallet_currency_repair_0035 s
    WHERE s.tenant_id=tenant_wallet_transactions.tenant_id
      AND s.customer_id=tenant_wallet_transactions.customer_id
      AND s.bad_currency=tenant_wallet_transactions.currency
);

DELETE FROM tenant_wallet_accounts
WHERE EXISTS (
    SELECT 1
    FROM _wallet_currency_repair_0035 s
    WHERE s.tenant_id=tenant_wallet_accounts.tenant_id
      AND s.customer_id=tenant_wallet_accounts.customer_id
      AND s.bad_currency=tenant_wallet_accounts.currency
);

DROP TABLE _wallet_currency_repair_0035;
