# scripts/trigger_webhook.py
import os
import sys
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.configuration import Configuration
from plaid.api_client import ApiClient
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest
from plaid.model.webhook_type import WebhookType

from src.common.paths import DB_FILE, ENV_FILE_SANDBOX

DB_PATH = str(DB_FILE)
ENV_PATH = str(ENV_FILE_SANDBOX)
LOG_FILE = os.path.join("logs", "webhook.log")


# -----------------------------
# Logging Setup
# -----------------------------
def setup_logger():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("webhook_logger")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


logger = setup_logger()


# -----------------------------
# Database Helpers
# -----------------------------
def get_access_tokens(limit=None):
    """Fetch most recent access tokens from the items table."""
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT item_id, access_token FROM items ORDER BY created_at DESC"
    if limit:
        query += f" LIMIT {limit}"
    rows = conn.execute(query).fetchall()
    conn.close()
    return rows


# -----------------------------
# Webhook Firing Logic
# -----------------------------
def fire_webhook(client, item_id, access_token):
    """Fire a DEFAULT_UPDATE webhook for a given item."""
    logger.info(
        f"Attempting to fire webhook for item_id={item_id}, access_token={access_token}, webhook_type=TRANSACTIONS, webhook_code=DEFAULT_UPDATE"
    )
    print(f"\nFiring webhook for item: {item_id} ...")

    req = SandboxItemFireWebhookRequest(
        access_token=access_token,
        webhook_type=WebhookType("TRANSACTIONS"),
        webhook_code="DEFAULT_UPDATE",
    )

    try:
        resp = client.sandbox_item_fire_webhook(req)
        print("Plaid response:", resp)
        logger.info(
            f"Webhook fired successfully for item_id={item_id}, request_id={resp['request_id']}, webhook_fired={resp['webhook_fired']}"
        )
    except Exception as e:
        logger.error(f"Webhook failed for item_id={item_id}: {e}")
        raise


# -----------------------------
# Webhook Events Retrieval
# -----------------------------
def show_recent_webhook_events(limit=5):
    """Display recent webhook events from the database."""
    conn = sqlite3.connect(DB_PATH)
    events = conn.execute(
        """
        SELECT received_at, webhook_type, webhook_code, item_id
        FROM webhook_events
        ORDER BY received_at DESC
        LIMIT ?;
    """,
        (limit,),
    ).fetchall()
    conn.close()

    print("\nRecent webhook events:")
    for ev in events:
        print(ev)
        received_at, webhook_type, webhook_code, item_id = ev
        logger.info(
            f"Webhook event recorded: received_at={received_at}, webhook_type={webhook_type}, webhook_code={webhook_code}, item_id={item_id}"
        )


# -----------------------------
# Main Execution
# -----------------------------
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
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    api_client = ApiClient(configuration)
    client = plaid_api.PlaidApi(api_client)

    # 3. Retrieve items
    tokens = get_access_tokens()
    if not tokens:
        print("No items found in database. Link an account first.")
        return

    success_count = 0
    skipped_count = 0

    # 4. Fire webhooks
    if "--all" in sys.argv:
        print("Running in batch mode: firing webhook for all access tokens...")
        logger.info("Batch mode started: firing webhook for all access tokens.")
        for item_id, access_token in tokens:
            try:
                fire_webhook(client, item_id, access_token)
                success_count += 1
            except Exception as e:
                print(f"Skipping item {item_id} due to error: {e}")
                logger.error(
                    f"Skipping item {item_id} due to error: {e.__class__.__name__}: {e}"
                )
                skipped_count += 1
        logger.info(
            f"Batch complete. Success: {success_count}, Skipped: {skipped_count}"
        )
        print(f"\nWebhook firing complete. Success: {success_count}, Skipped: {skipped_count}")

    else:
        print("Available access tokens:")
        for idx, (item_id, token) in enumerate(tokens, start=1):
            print(f"{idx}) {item_id}  →  {token}")

        choice = input("\nSelect item number to fire webhook (1-{}): ".format(len(tokens)))
        try:
            idx = int(choice) - 1
            item_id, access_token = tokens[idx]
            fire_webhook(client, item_id, access_token)
            success_count += 1
        except (ValueError, IndexError):
            print("Invalid selection.")
            return
        except Exception as e:
            print(f"Error firing webhook: {e}")
            logger.error(f"Error firing webhook for item {item_id}: {e}")
            skipped_count += 1

    # 5. Show and log webhook events
    show_recent_webhook_events()


if __name__ == "__main__":
    main()
