"""
Incremental schema migration tool for personal_finance
Safely applies CREATE or ALTER TABLE statements from schema.sql.
"""

import sqlite3
import os
import datetime

# NEW: reuse the same paths/env logic as the app
from src.common.config import load_env
from src.common.paths import PROJECT_ROOT, DB_FILE as DB_PATH, LOG_DIR

# Ensure ENV_TARGET is set (sandbox|development|production)
ENV_TARGET = os.getenv("ENV_TARGET", "sandbox")
load_env(ENV_TARGET)  # makes sure env + DATABASE_URL/DB_FILE are aligned

PROJECT_ROOT = str(PROJECT_ROOT)
DB_FILE = str(DB_PATH)  # this will now be ...plaid.db, plaid_dev.db, or plaid_prod.db
SCHEMA_FILE = os.path.join(PROJECT_ROOT, "src", "storage", "schema.sql")
LOG_FILE = os.path.join(LOG_DIR, "schema_watcher.log")

def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] MIGRATE_SCHEMA - {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def run_migrations():
    if not os.path.exists(DB_FILE):
        log(f"Database not found: {DB_FILE}")
        return

    if not os.path.exists(SCHEMA_FILE):
        log(f"Schema file not found: {SCHEMA_FILE}")
        return

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    log("Starting schema migration check...")

    with open(SCHEMA_FILE, encoding="utf-8") as f:
        sql_text = f.read()

    # Split statements by semicolon, ignoring empty ones
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]

    for stmt in statements:
        stmt_upper = stmt.upper()
        try:
            if stmt_upper.startswith("CREATE TABLE"):
                tbl_name = stmt.split()[2]
                # Skip if already exists
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl_name,))
                if cur.fetchone():
                    log(f"Table '{tbl_name}' already exists — skipping CREATE.")
                else:
                    cur.execute(stmt)
                    log(f"Created table '{tbl_name}'.")
            elif stmt_upper.startswith("ALTER TABLE"):
                cur.execute(stmt)
                log(f"Executed ALTER TABLE statement: {stmt[:60]}...")
            else:
                # Optional: support for CREATE INDEX, etc.
                cur.execute(stmt)
                log(f"Executed SQL statement: {stmt[:60]}...")
        except Exception as e:
            log(f"⚠️ Skipped statement '{stmt[:60]}...': {e}")

    conn.commit()
    conn.close()
    log("Migration completed successfully.\n")

if __name__ == "__main__":
    run_migrations()
