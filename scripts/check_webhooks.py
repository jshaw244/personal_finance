# scripts/check_webhooks.py
import os
import sqlite3
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.configuration import Configuration
from plaid.api_client import ApiClient
from plaid.model.item_get_request import ItemGetRequest

from src.common.paths import DB_FILE, ENV_FILE_SANDBOX

DB_PATH = str(DB_FILE)
ENV_PATH = str(ENV_FILE_SANDBOX)

def get_access_tokens():
    """Return all (item_id, access_token) pairs from the items table."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_id, access_token FROM items ORDER BY created_at DESC;").fetchall()
    conn.close()
    return rows

def main():
    # Load environment
    load_dotenv(ENV_PATH)
    PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
    PLAID_SECRET = os.getenv("PLAID_SECRET")

    configuration = Configuration(
        host="https://sandbox.plaid.com",
        api_key={
            "clientId": PLAID_CLIENT_ID,
            "secret": PLAID_SECRET
        }
    )
    api_client = ApiClient(configuration)
    client = plaid_api.PlaidApi(api_client)

    tokens = get_access_tokens()
    if not tokens:
        print("❌ No items found in database.")
        return

    print("\n🔎 Checking webhook URLs for all items:\n")
    for item_id, access_token in tokens:
        try:
            req = ItemGetRequest(access_token=access_token)
            resp = client.item_get(req)
            item_info = resp.to_dict()["item"]
            webhook_url = item_info.get("webhook")

            if not webhook_url:
                print(f"⚠️  {item_id} — MISSING webhook URL")
            else:
                print(f"✅ {item_id} — Webhook URL: {webhook_url}")

        except Exception as e:
            print(f"❌ {item_id} — Failed to retrieve info: {e}")

if __name__ == "__main__":
    main()
