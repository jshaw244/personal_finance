# check_indexes.py
import sqlite3
from pathlib import Path

# Define all three database paths
db_paths = [
    Path("C:/DATA/personal_finance/data/plaid.db"),        # sandbox
    Path("C:/DATA/personal_finance/data/plaid_dev.db"),    # development
    Path("C:/DATA/personal_finance/data/plaid_prod.db"),   # production
]

for db_path in db_paths:
    print(f"\n=== Indexes in: {db_path} ===")
    if not db_path.exists():
        print("  ⚠️  File not found.")
        continue

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT tbl_name, name FROM sqlite_master WHERE type='index';")
        indexes = cur.fetchall()

        if not indexes:
            print("  (No indexes found)")
        else:
            for tbl, idx in indexes:
                print(f"  {tbl:30} {idx}")
