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


-- Store the full Plaid transaction payload (or selected subset) for rules/classification
CREATE TABLE IF NOT EXISTS transaction_meta (
  transaction_id TEXT PRIMARY KEY,
  item_id        TEXT NOT NULL,
  payload_json   TEXT NOT NULL,
  updated_at     TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
  FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transaction_meta_item_id ON transaction_meta(item_id);

-- Your app-side classification + flags
CREATE TABLE IF NOT EXISTS transaction_classifications (
  transaction_id        TEXT PRIMARY KEY,
  exclude_from_spend    INTEGER DEFAULT 0,
  exclude_reason        TEXT,
  user_category         TEXT,
  user_subcategory      TEXT,
  merchant_normalized   TEXT,
  source                TEXT DEFAULT 'auto',   -- 'auto' (classifier) | 'user' (manual override, authoritative)
  updated_at            TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_txn_classifications_exclude ON transaction_classifications(exclude_from_spend);


CREATE TABLE IF NOT EXISTS liabilities_raw (
  item_id      TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  captured_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recurring_raw (
  item_id      TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  captured_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

-- ------------------------------------------------------------
-- Classification rules (merchant-based)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS classification_rules (
  rule_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  enabled             INTEGER DEFAULT 1,
  priority            INTEGER DEFAULT 100,              -- lower runs first

  -- Match inputs
  match_field         TEXT NOT NULL,                    -- 'merchant_name' | 'name' | 'either'
  match_op            TEXT NOT NULL,                    -- 'equals' | 'contains'
  match_value         TEXT NOT NULL,                    -- normalized value

  -- Optional scope (null = all accounts)
  account_id          TEXT,

  -- Optional amount match (null = any amount; set to match only this exact amount ±$0.02)
  amount_exact        REAL,

  -- Outputs
  exclude_from_spend  INTEGER DEFAULT 0,
  exclude_reason      TEXT,
  user_category       TEXT,
  user_subcategory    TEXT,
  merchant_normalized TEXT,

  created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_class_rules_enabled_priority
  ON classification_rules(enabled, priority);

CREATE INDEX IF NOT EXISTS idx_class_rules_match
  ON classification_rules(match_value, match_op, match_field);

CREATE INDEX IF NOT EXISTS idx_class_rules_account
  ON classification_rules(account_id);

CREATE INDEX IF NOT EXISTS idx_transactions_date_amount
ON transactions(date, amount);

-- ------------------------------------------------------------
-- Manual account balances (from statement uploads)
-- One row per upload; latest row per (institution, account_name) is current balance.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_account_balances (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  institution    TEXT NOT NULL,
  account_name   TEXT NOT NULL,
  account_type   TEXT NOT NULL,   -- 'credit', 'brokerage', 'checking', 'savings', 'mortgage'
  balance        REAL NOT NULL,
  statement_date TEXT,            -- YYYY-MM-DD closing date from statement
  source_file    TEXT,            -- original filename
  uploaded_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_manual_balances_account
  ON manual_account_balances(institution, account_name, uploaded_at DESC);

-- ------------------------------------------------------------
-- Savings goals
-- One row per goal. progress is computed live from savings-account
-- balance growth since start_balance (snapshot taken at creation).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS savings_goals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  target_amount  REAL NOT NULL,        -- dollars to save over the goal window
  start_date     TEXT NOT NULL,        -- YYYY-MM-DD, goal opened
  end_date       TEXT NOT NULL,        -- YYYY-MM-DD, target deadline (default Dec 31)
  start_balance  REAL NOT NULL,        -- total savings balance snapshot at creation
  status         TEXT DEFAULT 'active',-- 'active' | 'achieved' | 'archived'
  achieved_at    TEXT,                 -- timestamp when marked achieved
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_savings_goals_status ON savings_goals(status);

-- ------------------------------------------------------------
-- Budget framework
-- budget_plan: per-category tier + manual monthly target ("need").
--   tier: 'need' | 'want' | 'savings'. Monthless template.
-- budget_settings: small key/value store (e.g. expected_income).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget_plan (
  category      TEXT PRIMARY KEY,
  tier          TEXT NOT NULL DEFAULT 'need',   -- 'need' | 'want' | 'savings'
  target_amount REAL NOT NULL DEFAULT 0,        -- monthly target dollars
  group_name    TEXT,                           -- optional budget group, e.g. 'Housing'
  sort_order    INTEGER DEFAULT 100,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budget_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- Investment holdings (Plaid Investments product)
-- investment_holdings_raw: full /investments/holdings/get payload per item.
-- securities: reference data per security_id (latest seen).
-- holdings: current positions, one row per (account_id, security_id).
-- holdings_snapshots: append-only per-account total value per fetch (trend).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS investment_holdings_raw (
  item_id      TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  captured_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS securities (
  security_id      TEXT PRIMARY KEY,
  ticker_symbol    TEXT,
  name             TEXT,
  type             TEXT,            -- equity | etf | mutual fund | cash | fixed income | ...
  close_price      REAL,
  close_price_date TEXT,
  iso_currency     TEXT,
  is_cash_equiv    INTEGER DEFAULT 0,  -- cash / money-market sweep
  updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS holdings (
  account_id        TEXT NOT NULL,
  security_id       TEXT NOT NULL,
  quantity          REAL,
  institution_price REAL,
  institution_value REAL,
  cost_basis        REAL,
  iso_currency      TEXT,
  updated_at        TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, security_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE,
  FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE INDEX IF NOT EXISTS idx_holdings_account ON holdings(account_id);

CREATE TABLE IF NOT EXISTS holdings_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  total_value REAL NOT NULL,
  captured_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_holdings_snap_account
  ON holdings_snapshots(account_id, captured_at);

-- ============================================================
-- End of schema.sql
-- ============================================================
