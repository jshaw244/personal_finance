# scripts/list_webhook_events.py
import sqlite3
from pathlib import Path

DB_PATH = Path("data/plaid.db")

def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, received_at, webhook_type, webhook_code, item_id
        FROM webhook_events
        ORDER BY received_at DESC
        LIMIT 20;
    """).fetchall()
    conn.close()

    if not rows:
        print("⚠️  No webhook events found.")
        return

    print("\n📬 Recent Webhook Events:")
    for row in rows:
        wid, received_at, wtype, wcode, item_id = row
        print(f"#{wid} | {received_at} | type={wtype} | code={wcode} | item_id={item_id}")

if __name__ == "__main__":
    main()
