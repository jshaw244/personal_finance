-- Items table
CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    institution TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Accounts table
CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL,
    name            TEXT,
    official_name   TEXT,
    type            TEXT,
    subtype         TEXT,
    mask            TEXT,
    current_balance REAL,
    available_balance REAL,
    iso_currency_code TEXT,
    unofficial_currency_code TEXT,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_accounts_item_id ON accounts(item_id);

-- Transactions table
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL,
    account_id    TEXT,
    name          TEXT,
    amount        REAL,
    iso_currency_code TEXT,
    unofficial_currency_code TEXT,
    date          TEXT,
    category      TEXT,
    pending       INTEGER,
    merchant_name TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_transactions_item_id ON transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);

-- Webhooks table
CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    webhook_type TEXT,
    webhook_code TEXT,
    item_id TEXT,
    payload TEXT
);

-- Log events table
CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source TEXT,
    level TEXT,
    message TEXT
);

-- Maintenance log table
CREATE TABLE IF NOT EXISTS maintenance_log (
    key TEXT PRIMARY KEY,
    last_run TIMESTAMP
);


-- Cursor for /transactions/sync
CREATE TABLE IF NOT EXISTS transaction_cursors (
  item_id TEXT PRIMARY KEY,
  cursor  TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

--test
--ALTER TABLE webhook_events ADD COLUMN test_col_python TEXT;
