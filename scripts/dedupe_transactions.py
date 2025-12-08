# scripts/dedupe_transactions.py

import os
import sqlite3
from src.common.config import load_env
from src.common.paths import DB_FILE

def main():
    # Use ENV_TARGET (sandbox/development/production)
    env_target = os.getenv("ENV_TARGET", "sandbox")
    cfg = load_env(env_target)

    db_file = str(DB_FILE)
    print(f"ENV_TARGET = {env_target}")
    print(f"DB_FILE    = {db_file}")

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    # Before counts
    cur.execute("SELECT COUNT(*) FROM transactions")
    before = cur.fetchone()[0]
    print(f"Rows before dedupe: {before}")

    # --- DEDUPE BY transaction_id (safe, Plaid-native) ---
    # Keep the *latest* row per transaction_id (max(rowid)) and delete the rest.
    cur.execute("""
        DELETE FROM transactions
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM transactions
            GROUP BY transaction_id
        )
    """)
    deleted = conn.total_changes
    print(f"Deleted duplicate rows: {deleted}")

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM transactions")
    after = cur.fetchone()[0]
    print(f"Rows after dedupe: {after}")

    conn.close()

if __name__ == "__main__":
    main()
