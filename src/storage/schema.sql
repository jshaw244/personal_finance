-- ============================================================
-- personal_finance canonical schema (Option 2: Plaid IDs)
-- Production-safe: CREATE IF NOT EXISTS only (no ALTER / DROP)
-- ============================================================

PRAGMA foreign_keys = ON;

---------------------------------------------------------------
-- Raw Plaid payload capture (for transparency/debugging)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plaid_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
  env_target TEXT,
  endpoint TEXT,
  item_id TEXT,
  request_id TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_plaid_raw_item_id ON plaid_raw(item_id);
CREATE INDEX IF NOT EXISTS idx_plaid_raw_endpoint ON plaid_raw(endpoint);
CREATE INDEX IF NOT EXISTS idx_plaid_raw_captured_at ON plaid_raw(captured_at);

---------------------------------------------------------------
-- Items table
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    item_id       TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    institution_id TEXT,
    institution_name TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_items_institution_id ON items(institution_id);

---------------------------------------------------------------
-- Accounts table (Option 2 - ACTIVE)
-- Use Plaid account_id as PK (stable join target for transactions)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    account_id               TEXT PRIMARY KEY,   -- Plaid account_id
    item_id                  TEXT NOT NULL,

    name                     TEXT,
    official_name            TEXT,
    mask                     TEXT,
    type                     TEXT,
    subtype                  TEXT,

    -- balances
    current                  REAL,
    available                REAL,
    balance_limit            REAL,

    -- currency
    iso_currency_code        TEXT,
    unofficial_currency_code TEXT,

    created_at               TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at               TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_accounts_item_id ON accounts(item_id);
CREATE INDEX IF NOT EXISTS idx_accounts_type_subtype ON accounts(type, subtype);

---------------------------------------------------------------
-- Transactions table
-- transaction_id is Plaid transaction_id (PK)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id           TEXT PRIMARY KEY,
    item_id                  TEXT NOT NULL,
    account_id               TEXT NOT NULL,

    date                     TEXT,
    authorized_date          TEXT,

    name                     TEXT,
    merchant_name            TEXT,
    amount                   REAL,

    iso_currency_code        TEXT,
    unofficial_currency_code TEXT,

    pending                  INTEGER,
    pending_transaction_id   TEXT,

    category                 TEXT,   -- stored as JSON string
    payment_channel          TEXT,

    created_at               TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at               TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transactions_item_id ON transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);

CREATE TABLE IF NOT EXISTS transactions_removed (
  transaction_id TEXT PRIMARY KEY,
  item_id        TEXT NOT NULL,
  removed_at     TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transactions_removed_item_id
ON transactions_removed(item_id);

---------------------------------------------------------------
-- Webhook events table (audit/debug)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    webhook_type TEXT,
    webhook_code TEXT,
    item_id      TEXT,
    payload      TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_item_id ON webhook_events(item_id);

---------------------------------------------------------------
-- Log events table (app + maintenance logs + plaid_raw index entries)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    source    TEXT,
    level     TEXT,
    message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_events_timestamp ON log_events(timestamp);

---------------------------------------------------------------
-- Maintenance log table
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_log (
    key      TEXT PRIMARY KEY,
    last_run TEXT
);

---------------------------------------------------------------
-- Cursor table for /transactions/sync (future)
---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_cursors (
    item_id    TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

-- ============================================================
-- End of schema.sql
-- ============================================================
