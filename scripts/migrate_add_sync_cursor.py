# scripts/migrate_add_sync_cursor.py
import sqlite3
from pathlib import Path

DB = Path("data/plaid.db")
DDL = """
CREATE TABLE IF NOT EXISTS transaction_cursors (
  item_id TEXT PRIMARY KEY,
  cursor  TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def main():
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}")
    con = sqlite3.connect(DB)
    try:
        con.executescript(DDL)
        con.commit()
        print("transaction_cursors table is present (created if missing).")
    finally:
        con.close()

if __name__ == "__main__":
    main()
