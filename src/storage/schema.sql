-- ============================================================
-- personal_finance canonical schema
-- Option 1 (active): integer PK accounts, no FK on account_id
-- Option 2 (future): Plaid account_id as PK with FK (commented)
-- ============================================================

---------------------------------------------------------------
-- Items table
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    institution  TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

---------------------------------------------------------------
-- Accounts table (Option 1 - ACTIVE)
-- Matches current db.py::init_db + save_accounts
--  - No account_id column here (we only store balances/metadata)
--  - "id" is an auto-increment surrogate key
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT,
    name            TEXT,
    official_name   TEXT,
    mask            TEXT,
    type            TEXT,
    subtype         TEXT,
    current         REAL,
    available       REAL,
    iso_currency_code TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Incremental migrations for legacy accounts schemas
-- These are safe to run against old DBs / duplicates will be logged
-- and skipped by migrate_schema.py.
ALTER TABLE accounts ADD COLUMN current REAL;
ALTER TABLE accounts ADD COLUMN available REAL;
ALTER TABLE accounts ADD COLUMN iso_currency_code TEXT;

CREATE INDEX IF NOT EXISTS idx_accounts_item_id ON accounts(item_id);

---------------------------------------------------------------
-- Transactions table
-- Matches db.py::save_transactions
--  - transaction_id is the Plaid transaction_id (PK)
--  - account_id stores Plaid's account_id as a plain TEXT field
--  - No FK enforced on account_id in Option 1 (can be added later)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id          TEXT PRIMARY KEY,
    item_id                 TEXT,
    account_id              TEXT,
    date                    TEXT,
    name                    TEXT,
    amount                  REAL,
    merchant_name           TEXT,
    category                TEXT,
    pending                 INTEGER,
    iso_currency_code       TEXT,
    unofficial_currency_code TEXT,
    created_at              TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Incremental migrations for legacy transactions schemas
-- (If older DBs were missing these currency columns)
ALTER TABLE transactions ADD COLUMN iso_currency_code TEXT;
ALTER TABLE transactions ADD COLUMN unofficial_currency_code TEXT;

CREATE INDEX IF NOT EXISTS idx_transactions_item_id ON transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);

---------------------------------------------------------------
-- Webhook events table
-- Note: also created defensively in db.py::save_webhook_event
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    webhook_type TEXT,
    webhook_code TEXT,
    item_id      TEXT,
    payload      TEXT
);

---------------------------------------------------------------
-- Log events table
-- Matches db.py::log_event_db
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source    TEXT,
    level     TEXT,
    message   TEXT
);

---------------------------------------------------------------
-- Maintenance log table
-- Matches db.py::init_db maintenance_log
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_log (
    key      TEXT PRIMARY KEY,
    last_run TIMESTAMP
);

---------------------------------------------------------------
-- Cursor table for /transactions/sync
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_cursors (
    item_id    TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- OPTION 2 (FUTURE): Plaid account_id as PK + strict FK
-- ============================================================
-- When/if you want to move to a schema where:
--   - accounts.account_id is the primary key (Plaid account_id)
--   - transactions.account_id has a real FK to accounts.account_id
-- you can:
--   1) Migrate data into this shape,
--   2) Replace the Option 1 accounts + transactions definitions
--      above with the ones below (uncomment + adjust as needed).
--
-- *** DO NOT UNCOMMENT UNTIL YOU'VE RUN A DEDICATED MIGRATION ***
-- *** AND UPDATED db.py::init_db + save_accounts ACCORDINGLY. ***
--
-- ------------------------------------------------------------
-- -- Accounts table (Option 2 - FUTURE, COMMENTED OUT)
-- ------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS accounts (
--     account_id            TEXT PRIMARY KEY,   -- Plaid account_id
--     item_id               TEXT NOT NULL,
--     name                  TEXT,
--     official_name         TEXT,
--     type                  TEXT,
--     subtype               TEXT,
--     mask                  TEXT,
--     current_balance       REAL,
--     available_balance     REAL,
--     iso_currency_code     TEXT,
--     unofficial_currency_code TEXT,
--     last_updated          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--     FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
-- );
--
-- CREATE INDEX IF NOT EXISTS idx_accounts_item_id ON accounts(item_id);
--
-- ------------------------------------------------------------
-- -- Transactions table (Option 2 - FUTURE, COMMENTED OUT)
-- ------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS transactions (
--     transaction_id          TEXT PRIMARY KEY,
--     item_id                 TEXT NOT NULL,
--     account_id              TEXT,  -- FK to accounts.account_id
--     name                    TEXT,
--     amount                  REAL,
--     iso_currency_code       TEXT,
--     unofficial_currency_code TEXT,
--     date                    TEXT,
--     category                TEXT,
--     pending                 INTEGER,
--     merchant_name           TEXT,
--     created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--     updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--     FOREIGN KEY (item_id)   REFERENCES items(item_id) ON DELETE CASCADE,
--     FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
-- );
--
-- CREATE INDEX IF NOT EXISTS idx_transactions_item_id   ON transactions(item_id);
-- CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
-- CREATE INDEX IF NOT EXISTS idx_transactions_date      ON transactions(date);
--
-- ============================================================
-- End of schema.sql
-- ============================================================
