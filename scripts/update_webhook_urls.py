# scripts/update_webhook_urls.py
import os
import sqlite3
from dotenv import load_dotenv
from src.common.paths import DB_FILE

# Load env so we can read PLAID_WEBHOOK_URL
env_path = os.path.join("config", "env", ".env.sandbox")
load_dotenv(env_path)

webhook_url = os.getenv("PLAID_WEBHOOK_URL")
if not webhook_url:
    print("❌ PLAID_WEBHOOK_URL is not set. Please check your .env.sandbox file.")
    exit(1)

print(f"✅ Using webhook URL: {webhook_url}")

# Connect to database
conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

# Update all items with webhook_url = None
cur.execute("""
    UPDATE items
    SET webhook_url = ?
    WHERE webhook_url IS NULL OR webhook_url = ''
""", (webhook_url,))

conn.commit()

# Verify update
rows = cur.execute("SELECT item_id, webhook_url FROM items").fetchall()
print("\n📦 Items and their webhook URLs:")
for item_id, url in rows:
    print(f" - {item_id} → {url}")

conn.close()
print("\n✅ Webhook URLs updated successfully for all items.")
