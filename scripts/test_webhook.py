import os
import sqlite3
import json
import logging
import requests
from pathlib import Path
from plaid.api import plaid_api
import plaid
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest
from plaid.model.webhook_type import WebhookType
import argparse

# ----------------------------
# Setup basic logger (no emojis)
# ----------------------------
logger = logging.getLogger("test_webhook")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)

def log(msg):
    logger.info(msg)
parser = argparse.ArgumentParser()
parser.add_argument("--classic", action="store_true", help="Send DEFAULT_UPDATE instead of SYNC_UPDATES_AVAILABLE")
args = parser.parse_args()

# ----------------------------
# Config
# ----------------------------
DB_FILE = Path("data/plaid.db").resolve()
WEBHOOK_URL = os.getenv("PLAID_WEBHOOK_URL", "http://127.0.0.1:5000/plaid/webhook")

log(f"Target webhook URL: {WEBHOOK_URL}")
log(f"Using database: {DB_FILE}")

# ----------------------------
# Connect to DB and get latest item_id
# ----------------------------
conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()
cur.execute("SELECT item_id, access_token FROM items ORDER BY ROWID DESC LIMIT 1;")
row = cur.fetchone()

if row is None:
    log("No items found in the database. Cannot fire webhook.")
    exit(1)

item_id, access_token = row
log(f"Using most recent item_id: {item_id}")

# ----------------------------
# Sandbox: Fire webhook to simulate new transactions
# ----------------------------
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = (os.getenv("PLAID_ENV") or "sandbox").lower()

env_map = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

configuration = plaid.Configuration(
    host=env_map[PLAID_ENV],
    api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET}
)
api_client = plaid.ApiClient(configuration)
client = plaid_api.PlaidApi(api_client)

log("Triggering sandbox to generate new transactions...")
try:
    log(f"DEBUG → access_token: {access_token}")
    fire_req = SandboxItemFireWebhookRequest(
        access_token=str(access_token),
        webhook_type=WebhookType(value="TRANSACTIONS"),
        webhook_code="DEFAULT_UPDATE"
    )
    client.sandbox_item_fire_webhook(fire_req)
    log("Sandbox transactions webhook fired successfully.")
except Exception as e:
    log(f"Failed to fire sandbox webhook: {e}")

# --- Force Plaid to check for new transactions (Sandbox) ---
log("Requesting on-demand transactions refresh from Plaid...")
refresh_ok = False
try:
    # Modern SDKs (OpenAPI): request-model style
    from plaid.model.transactions_refresh_request import TransactionsRefreshRequest
    refresh_req = TransactionsRefreshRequest(access_token=access_token)
    client.transactions_refresh(refresh_req)
    refresh_ok = True
    log("transactions_refresh request sent.")
except Exception as e1:
    # Older SDKs sometimes expose a different interface; try legacy-style call
    try:
        # Some versions expose refresh via a subclient; harmless no-op if missing
        client.Transactions.refresh(access_token)  # noqa: attribute-defined-outside-init
        refresh_ok = True
        log("transactions_refresh (legacy) request sent.")
    except Exception as e2:
        log(f"transactions_refresh not available: {e1} / {e2}")

# Give Plaid a moment to synthesize new sandbox transactions
import time
if refresh_ok:
    time.sleep(2)

# ----------------------------
# Count transactions before webhook
# ----------------------------
cur.execute("SELECT COUNT(*) FROM transactions WHERE item_id = ?;", (item_id,))
before_count = cur.fetchone()[0]
log(f"Transactions before webhook: {before_count}")

# ----------------------------
# Send webhook
# ----------------------------
webhook_code = "DEFAULT_UPDATE" if args.classic else "SYNC_UPDATES_AVAILABLE"
payload = {
    "webhook_type": "TRANSACTIONS",
    "webhook_code": webhook_code,
    "item_id": item_id
}

log(f"Sending webhook payload: {payload}")
resp = requests.post(WEBHOOK_URL, json=payload)
log(f"Webhook POST status: {resp.status_code}")
log("Response body:\n" + resp.text)

# ----------------------------
# Show last 5 webhook events
# ----------------------------
log("\nLast 5 webhook events:")
for row in conn.execute("""
    SELECT received_at, webhook_type, webhook_code, item_id
    FROM webhook_events
    ORDER BY received_at DESC
    LIMIT 5;
"""):
    print(f" - {row[0]} | type={row[1]} | code={row[2]} | item_id={row[3]}")

# ----------------------------
# Count transactions after webhook
# ----------------------------
cur.execute("SELECT COUNT(*) FROM transactions WHERE item_id = ?;", (item_id,))
after_count = cur.fetchone()[0]

log("\nTransaction validation:")
log(f"   Before webhook: {before_count}")
log(f"   After webhook:  {after_count}")
log(f"   New transactions added: {after_count - before_count}")

if after_count == before_count:
    log("WARNING: No new transactions added. Check webhook handler logic.")

log("Webhook test complete.\n" + "-" * 60)
conn.close()
