#!/usr/bin/env python3
"""
Verify presence of tables and indexes in the SQLite database.

Usage:
  python scripts/verify_db_objects.py
  python scripts/verify_db_objects.py --db data/plaid.db --table transaction_cursors --index idx_transaction_cursors_item
  python scripts/verify_db_objects.py --table items transactions webhook_events --index idx_transactions_item_id

Exit codes:
  0 = all requested objects exist
  1 = one or more requested objects are missing
"""

import sys
import sqlite3
from pathlib import Path
import argparse
from typing import List, Tuple

DEFAULT_DB = Path("data/plaid.db")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify tables and indexes in SQLite DB.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite DB (default: data/plaid.db)")
    p.add_argument("--table", nargs="*", default=["transaction_cursors"], help="Table names to verify")
    p.add_argument("--index", nargs="*", default=["idx_transaction_cursors_item"], help="Index names to verify")
    return p.parse_args()

def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;",
        (name,),
    ).fetchone()
    return row is not None

def index_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?;",
        (name,),
    ).fetchone()
    return row is not None

def table_info(con: sqlite3.Connection, name: str) -> List[Tuple]:
    return con.execute(f"PRAGMA table_info({name});").fetchall()

def main() -> int:
    args = parse_args()

    if not args.db.exists():
        print(f"ERROR: Database not found: {args.db}")
        return 1

    con = sqlite3.connect(args.db)
    try:
        missing = False

        if args.table:
            print("== Tables ==")
            for t in args.table:
                exists = table_exists(con, t)
                print(f"- {t}: {'present' if exists else 'MISSING'}")
                if exists:
                    cols = table_info(con, t)
                    # Pretty-print columns: cid, name, type, notnull, dflt_value, pk
                    for cid, name, typ, notnull, dflt, pk in cols:
                        print(f"    [{cid}] {name} {typ} "
                              f"{'NOT NULL' if notnull else ''} "
                              f"{'PRIMARY KEY' if pk else ''} "
                              f"{'(default=' + str(dflt) + ')' if dflt is not None else ''}")
                else:
                    missing = True

        if args.index:
            print("== Indexes ==")
            for idx in args.index:
                exists = index_exists(con, idx)
                print(f"- {idx}: {'present' if exists else 'MISSING'}")
                if not exists:
                    missing = True

        return 1 if missing else 0
    finally:
        con.close()

if __name__ == "__main__":
    sys.exit(main())
