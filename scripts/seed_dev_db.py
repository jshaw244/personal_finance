#!/usr/bin/env python3
"""
seed_dev_db.py
---------------------------------------------
Clone the sandbox database (plaid.db) into the
development database (plaid_dev.db).

Features:
  • Copies all core tables (items, accounts, transactions, budgets, financial_health_history)
  • Preserves schema, data types, and integrity
  • Skips triggers/views; focuses on user data tables
  • Optional --truncate flag wipes target tables first
  • Logs actions to logs/maintenance.log
---------------------------------------------
Usage:
  python scripts/seed_dev_db.py
  python scripts/seed_dev_db.py --truncate
  python scripts/seed_dev_db.py --source data/plaid.db --target data/plaid_dev.db
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

# -----------------------------------------------------
# Configuration
# -----------------------------------------------------
PROJECT_ROOT = Path("C:/DATA/personal_finance")
SOURCE_DB = PROJECT_ROOT / "data/plaid.db"         # sandbox
TARGET_DB = PROJECT_ROOT / "data/plaid_dev.db"     # development
LOG_FILE   = PROJECT_ROOT / "logs/maintenance.log"

TABLES_TO_COPY = [
    "items",
    "accounts",
    "transactions",
    "budgets",
    "financial_health_history",
    "summary_monthly",
    "summary_merchant",
    "transaction_cursors",
]

# -----------------------------------------------------
# Helper Functions
# -----------------------------------------------------
def log(message: str):
    """Append message with timestamp to maintenance.log"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def copy_table(conn_src, conn_tgt, table: str, truncate: bool = False):
    """Copy data safely, matching only columns that exist in both DBs."""
    cur_src = conn_src.cursor()
    cur_tgt = conn_tgt.cursor()

    cur_src.execute(f"PRAGMA table_info({table});")
    src_cols = [r[1] for r in cur_src.fetchall()]
    cur_tgt.execute(f"PRAGMA table_info({table});")
    tgt_cols = [r[1] for r in cur_tgt.fetchall()]
    shared = [c for c in src_cols if c in tgt_cols]

    if not shared:
        log(f"[WARN] Skipping {table}: no shared columns.")
        return

    cols = ", ".join(shared)
    qmarks = ", ".join(["?"] * len(shared))
    if truncate:
        cur_tgt.execute(f"DELETE FROM {table};")

    cur_src.execute(f"SELECT {cols} FROM {table};")
    rows = cur_src.fetchall()
    if rows:
        cur_tgt.executemany(f"INSERT INTO {table} ({cols}) VALUES ({qmarks})", rows)
        conn_tgt.commit()
        log(f"[OK] Copied {len(rows)} rows → {table}")
    else:
        log(f"[INFO] {table}: no rows to copy.")


def seed_database(source: Path, target: Path, truncate: bool):
    """Perform the cloning operation"""
    if not source.exists():
        log(f"[ERROR] Source DB not found: {source}")
        return
    if not target.exists():
        log(f"[ERROR] Target DB not found: {target}")
        return

    log(f"--- SEED START: {source.name} → {target.name} ---")
    with sqlite3.connect(source) as conn_src, sqlite3.connect(target) as conn_tgt:
        for tbl in TABLES_TO_COPY:
            try:
                copy_table(conn_src, conn_tgt, tbl, truncate=truncate)
            except Exception as e:
                log(f"[ERROR] {tbl}: {e}")
    log(f"--- SEED COMPLETE: {target.name} ---\n")


# -----------------------------------------------------
# Main
# -----------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed development DB from sandbox.")
    parser.add_argument("--source", type=Path, default=SOURCE_DB)
    parser.add_argument("--target", type=Path, default=TARGET_DB)
    parser.add_argument(
        "--truncate", action="store_true", help="Clear target tables before inserting"
    )
    args = parser.parse_args()

    seed_database(args.source, args.target, args.truncate)
