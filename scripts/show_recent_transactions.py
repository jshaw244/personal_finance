# scripts/show_recent_transactions.py
import sqlite3
from pathlib import Path

DB_FILE = Path("data/plaid.db")

def main():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    print("📄 Most Recent 10 Transactions:\n")

    cur.execute("""
        SELECT transaction_id, item_id, date, name, amount, merchant_name, category
        FROM transactions
        ORDER BY date DESC, transaction_id DESC
        LIMIT 10
    """)

    rows = cur.fetchall()

    if not rows:
        print("⚠️  No transactions found.")
    else:
        for idx, row in enumerate(rows, 1):
            txn_id, item_id, date, name, amount, merchant, category = row
            print(f"{idx}. {date} | ${amount:.2f} | {name} | {merchant or '—'}")
            print(f"   item_id: {item_id}")
            print(f"   category: {category}\n")

    conn.close()

if __name__ == "__main__":
    main()
