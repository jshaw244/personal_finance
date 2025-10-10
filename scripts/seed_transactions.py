# scripts/seed_transactions.py
#!/usr/bin/env python3
"""
Seed fake transactions into the Plaid SQLite database for sandbox analytics.
Use this only for sandbox/testing environments — not production.

Usage:
    python scripts/seed_transactions.py
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import random
import json

DB_PATH = Path("data/plaid.db")
ITEM_ID = "wNVgrkGZomu9Kz8gJrJPHZy5dbbzg4urrAxNL"  # your sandbox item_id

CATEGORIES = [
    ("Food and Drink", "Restaurants", ["Chipotle", "Starbucks", "McDonald's"]),
    ("Shops", "Groceries", ["Target", "Whole Foods", "Kroger"]),
    ("Travel", "Airlines", ["Delta", "United Airlines", "American Airlines"]),
    ("Entertainment", "Streaming", ["Netflix", "Spotify", "Hulu"]),
    ("Income", "Payroll", ["Employer Inc.", "Freelance Client"]),
]

def seed_transactions(conn, count: int = 25):
    now = datetime.utcnow().date()
    sql = """
    INSERT OR IGNORE INTO transactions (
        transaction_id, item_id, account_id, date, name, amount,
        merchant_name, category, pending, iso_currency_code, unofficial_currency_code
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = []
    for i in range(count):
        cat_major, cat_minor, merchants = random.choice(CATEGORIES)
        merchant = random.choice(merchants)
        category = [cat_major, cat_minor]

        is_income = cat_major == "Income"
        amount = round(random.uniform(50, 5000), 2)
        if not is_income:
            amount = -abs(amount)

        date = (now - timedelta(days=random.randint(1, 180))).isoformat()

        rows.append((
            f"seed_{i}_{int(datetime.utcnow().timestamp())}",
            ITEM_ID,
            f"acc_seed_{random.randint(100, 999)}",  # ✅ fixed line
            date,
            f"{cat_minor} - {merchant}",
            amount,
            merchant,
            json.dumps(category),
            0,
            "USD",
            None
        ))

    cur = conn.cursor()
    cur.executemany(sql, rows)
    conn.commit()
    return len(rows)

def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}")
        return
    con = sqlite3.connect(DB_PATH)
    try:
        n = seed_transactions(con)
        print(f"✅ Seeded {n} fake transactions into {DB_PATH}")
    finally:
        con.close()

if __name__ == "__main__":
    main()
