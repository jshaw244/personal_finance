#scripts/migrate_items_table.py
import sqlite3

DB_PATH = "data/plaid.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Try adding each column individually
columns = [
    "ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'active'",
    "ALTER TABLE items ADD COLUMN last_webhook_received_at TIMESTAMP",
    "ALTER TABLE items ADD COLUMN last_error_code TEXT",
    "ALTER TABLE items ADD COLUMN last_error_message TEXT",
    "ALTER TABLE items ADD COLUMN institution_name TEXT",
]

for sql in columns:
    try:
        cur.execute(sql)
        print(f"✅ Executed: {sql}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"⚠️ Skipped (already exists): {sql}")
        else:
            print(f"❌ Error: {sql}\n   {e}")

conn.commit()
conn.close()
print("🏁 Migration complete.")
