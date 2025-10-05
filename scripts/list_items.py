# scripts/list_items.py
import sqlite3
from pathlib import Path

DB_PATH = Path("data/plaid.db")

def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_id, access_token, webhook_url, status FROM items;").fetchall()
    conn.close()

    if not rows:
        print("⚠️  No items found in the database.")
        return

    print("\n📦 Items table contents:")
    for idx, row in enumerate(rows, 1):
        item_id, token, webhook_url, status = row
        print(f"{idx}. item_id={item_id}\n   access_token={token}\n   webhook_url={webhook_url}\n   status={status}\n")

if __name__ == "__main__":
    main()
