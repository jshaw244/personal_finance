# scripts/check_webhook_events.py
import sqlite3
from pathlib import Path

DB_PATH = Path("data/plaid.db")

def show_webhooks(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, received_at, webhook_type, webhook_code, item_id
        FROM webhook_events
        ORDER BY received_at DESC
        LIMIT ?;
    """, (limit,)).fetchall()
    conn.close()

    if not rows:
        print("⚠️ No webhook events found.")
    else:
        print("\n📬 Recent Webhook Events:")
        for r in rows:
            print(f"#{r[0]} | {r[1]} | type={r[2]} | code={r[3]} | item={r[4]}")

if __name__ == "__main__":
    show_webhooks()
