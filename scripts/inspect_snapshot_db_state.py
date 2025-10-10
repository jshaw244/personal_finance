#!/usr/bin/env python3
"""
Inspect DB record counts vs. environment snapshot history.
Correlates database state with snapshot timestamps and Git tags.
"""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/plaid.db")

def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("=" * 80)
    print(f"Database snapshot analysis as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    query = """
    WITH summary AS (
        SELECT
            date(received_at) AS snapshot_date,
            COUNT(*) AS webhook_events,
            (SELECT COUNT(*) FROM transactions WHERE date >= date(received_at)) AS transactions_after
        FROM webhook_events
        GROUP BY snapshot_date
    )
    SELECT
        s.snapshot_date AS "DB Snapshot Date",
        s.webhook_events AS "Webhook Events (up to date)",
        s.transactions_after AS "Transactions (after date)",
        (SELECT COUNT(*) FROM transactions WHERE date <= s.snapshot_date) AS "Transactions (before date)",
        (SELECT COUNT(*) FROM items) AS "Total Items",
        (SELECT COUNT(*) FROM accounts) AS "Total Accounts"
    FROM summary s
    ORDER BY s.snapshot_date DESC;
    """

    for row in cur.execute(query):
        print(row)

    con.close()

if __name__ == "__main__":
    main()
