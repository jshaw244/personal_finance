# scripts/trigger_webhook.py
import os
import sqlite3
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.configuration import Configuration
from plaid.api_client import ApiClient
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest

from src.common.paths import DB_FILE, ENV_FILE_SANDBOX
DB_PATH = str(DB_FILE)
ENV_PATH = str(ENV_FILE_SANDBOX)

def get_access_tokens(limit=5):
    """Fetch most recent access tokens from the items table."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT item_id, access_token FROM items ORDER BY created_at DESC LIMIT ?;", (limit,)
    ).fetchall()
    conn.close()
    return rows

def main():
    # 1. Load environment
    load_dotenv(ENV_PATH)
    PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
    PLAID_SECRET = os.getenv("PLAID_SECRET")

    if not PLAID_CLIENT_ID or not PLAID_SECRET:
        raise RuntimeError("Missing PLAID_CLIENT_ID or PLAID_SECRET in environment")

    # 2. Initialize Plaid client
    configuration = Configuration(
        host="https://sandbox.plaid.com",
        api_key={
            "clientId": PLAID_CLIENT_ID,
            "secret": PLAID_SECRET
        }
    )
    api_client = ApiClient(configuration)
    client = plaid_api.PlaidApi(api_client)

    # 3. Get recent access tokens
    tokens = get_access_tokens()
    if not tokens:
        print("❌ No items found in database. Link an account first.")
        return

    print("📌 Available access tokens:")
    for idx, (item_id, token) in enumerate(tokens, start=1):
        print(f"{idx}) {item_id}  →  {token}")

    choice = input("\nSelect item number to fire webhook (1-{}): ".format(len(tokens)))
    try:
        idx = int(choice) - 1
        item_id, access_token = tokens[idx]
    except (ValueError, IndexError):
        print("❌ Invalid selection.")
        return

    # 4. Fire webhook
    req = SandboxItemFireWebhookRequest(
        access_token=access_token,
        webhook_code="DEFAULT_UPDATE"
    )
    print(f"\n🚀 Firing webhook for item: {item_id} ...")
    resp = client.sandbox_item_fire_webhook(req)
    print("✅ Plaid response:", resp)

    # 5. Show last webhook events
    conn = sqlite3.connect(DB_PATH)
    events = conn.execute("""
        SELECT received_at, webhook_type, webhook_code, item_id
        FROM webhook_events
        ORDER BY received_at DESC
        LIMIT 5;
    """).fetchall()
    conn.close()

    print("\n📬 Recent webhook events:")
    for ev in events:
        print(ev)

if __name__ == "__main__":
    main()