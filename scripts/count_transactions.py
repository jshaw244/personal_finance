# scripts/count_transactions.py
import sqlite3
from pathlib import Path

DB_FILE = Path("data/plaid.db")

def main():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    print("📊 Transaction Counts by Item:\n")

    cur.execute("""
        SELECT item_id, COUNT(*) 
        FROM transactions 
        GROUP BY item_id 
        ORDER BY COUNT(*) DESC
    """)
    rows = cur.fetchall()

    if not rows:
        print("⚠️  No transactions found.")
    else:
        total = 0
        for idx, (item_id, count) in enumerate(rows, 1):
            print(f"{idx}. item_id={item_id}\n   transactions={count}")
            total += count

        print("\n📈 TOTAL TRANSACTIONS:", total)

    conn.close()

if __name__ == "__main__":
    main()
