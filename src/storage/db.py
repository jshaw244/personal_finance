import json
import sqlite3
from pathlib import Path
from datetime import datetime

from src.common.paths import DB_FILE as DB_PATH

def get_connection():
    """Return a sqlite3 connection to the Plaid DB (auto row dicts)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn 

# -----------------------------
# Core DB Setup
# -----------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                access_token TEXT,
                institution TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT,
                name TEXT,
                official_name TEXT,
                mask TEXT,
                type TEXT,
                subtype TEXT,
                current REAL,
                available REAL,
                iso_currency_code TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                item_id TEXT,
                account_id TEXT,
                date TEXT,
                name TEXT,
                amount REAL,
                merchant_name TEXT,
                category TEXT,
                pending INTEGER,
                iso_currency_code TEXT,
                unofficial_currency_code TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS log_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                level TEXT,
                message TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_log (
                key TEXT PRIMARY KEY,
                last_run TEXT
            )
        """)

# -----------------------------
# Item helpers
# -----------------------------
def save_item(item_id, access_token, institution=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO items (item_id, access_token, institution, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE
            SET access_token=excluded.access_token,
                institution=excluded.institution,
                updated_at=CURRENT_TIMESTAMP
        """, (item_id, access_token, institution))

def get_all_items():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT item_id, access_token, institution FROM items")
        return cur.fetchall()

# -----------------------------
# Account helpers
# -----------------------------
def save_accounts(item_id, accounts):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("""
            INSERT INTO accounts (
                item_id, name, official_name, mask, type, subtype,
                current, available, iso_currency_code
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                item_id,
                a.get("name"),
                a.get("official_name"),
                a.get("mask"),
                a.get("type"),
                a.get("subtype"),
                a.get("current"),
                a.get("available"),
                a.get("iso_currency_code"),
            )
            for a in accounts
        ])

# -----------------------------
# Transaction helpers
# -----------------------------
def save_transactions(item_id, transactions):
    with sqlite3.connect(DB_PATH) as conn:
        for t in transactions:
            conn.execute("""
                INSERT OR REPLACE INTO transactions (
                    transaction_id, item_id, account_id, date, name, amount,
                    merchant_name, category, pending,
                    iso_currency_code, unofficial_currency_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t.get("transaction_id"),
                item_id,
                t.get("account_id"),
                t.get("date"),
                t.get("name"),
                t.get("amount"),
                t.get("merchant_name"),
                str(t.get("category")),
                int(t.get("pending") or 0),
                t.get("iso_currency_code"),
                t.get("unofficial_currency_code"),
            ))

# -----------------------------
# Webhook helpers
# -----------------------------
def save_webhook_event(webhook_type, webhook_code, item_id, payload):
    """Save a raw Plaid webhook event to the database for auditing/debugging."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                webhook_type TEXT,
                webhook_code TEXT,
                item_id TEXT,
                payload TEXT
            )
        """)  # ensures table exists even if schema.sql not rerun

        conn.execute("""
            INSERT INTO webhook_events (
                webhook_type, webhook_code, item_id, payload
            ) VALUES (?, ?, ?, ?)
        """, (
            webhook_type,
            webhook_code,
            item_id,
            json.dumps(payload)
        ))

# -----------------------------
# Logging helpers
# -----------------------------
def log_event_db(source, level, message):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO log_events (timestamp, source, level, message)
            VALUES (?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(sep=" ", timespec="seconds"), source, level, message))