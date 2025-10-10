# src/ingestion/webhooks.py
from flask import Blueprint, request, jsonify, current_app
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import json

import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration

# Try to import Sync; if not available, we’ll fall back to /transactions/get
try:
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    HAS_SYNC = True
except Exception:
    HAS_SYNC = False

# Keep GET available as a fallback
try:
    from plaid.model.transactions_get_request import TransactionsGetRequest
    HAS_GET = True
except Exception:
    HAS_GET = False

webhooks_bp = Blueprint("webhooks", __name__)

DB_FILE = Path("data/plaid.db")
PLAID_ENV = (os.getenv("PLAID_ENV") or "sandbox").lower()
ENV_MAP = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

def get_db():
    return sqlite3.connect(DB_FILE)

def get_plaid_client():
    cfg = Configuration(
        host=ENV_MAP[PLAID_ENV],
        api_key={
            "clientId": os.getenv("PLAID_CLIENT_ID"),
            "secret": os.getenv("PLAID_SECRET"),
        },
    )
    return plaid_api.PlaidApi(ApiClient(cfg))

def get_access_token_by_item(conn, item_id: str):
    row = conn.execute(
        "SELECT access_token FROM items WHERE item_id = ? LIMIT 1;", (item_id,)
    ).fetchone()
    return row[0] if row else None

def get_cursor(conn, item_id: str):
    row = conn.execute(
        "SELECT cursor FROM transaction_cursors WHERE item_id = ?;",
        (item_id,),
    ).fetchone()
    return row[0] if row else None

def upsert_cursor(conn, item_id: str, cursor: str):
    conn.execute(
        """
        INSERT INTO transaction_cursors (item_id, cursor)
        VALUES (?, ?)
        ON CONFLICT(item_id) DO UPDATE
        SET cursor=excluded.cursor, updated_at=CURRENT_TIMESTAMP;
        """,
        (item_id, cursor),
    )
    conn.commit()

def insert_transactions(conn, item_id: str, txs):
    sql = """
        INSERT OR IGNORE INTO transactions (
            transaction_id, item_id, account_id, date, name, amount,
            merchant_name, category, pending,
            iso_currency_code, unofficial_currency_code
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = []
    for t in txs:
        rows.append((
            t.get("transaction_id"),
            item_id,
            t.get("account_id"),
            t.get("date"),
            t.get("name"),
            t.get("amount"),
            t.get("merchant_name"),
            json.dumps(t.get("category")) if t.get("category") is not None else None,
            int(bool(t.get("pending"))),
            t.get("iso_currency_code"),
            t.get("unofficial_currency_code"),
        ))
    cur = conn.cursor()
    if rows:
        cur.executemany(sql, rows)
        conn.commit()
    return cur.rowcount if rows else 0

def fetch_transactions_get(client, access_token: str, days: int = 30):
    if not HAS_GET:
        return []
    start_date = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    end_date = datetime.utcnow().date().isoformat()
    all_tx, count, offset = [], 100, 0
    while True:
        req = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options={"count": count, "offset": offset},
        )
        resp = client.transactions_get(req)
        txs = resp["transactions"]
        all_tx.extend(txs)
        total = resp["total_transactions"]
        offset += len(txs)
        if offset >= total or not txs:
            break
    return all_tx
WEBHOOKS_VERSION = "2025-10-06-sync-diag-1"
@webhooks_bp.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    wt = payload.get("webhook_type")
    wc = payload.get("webhook_code")
    item_id = payload.get("item_id")

    current_app.logger.info("Plaid webhook received: %s", payload)

    # Record webhook event
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO webhook_events (received_at, webhook_type, webhook_code, item_id, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), wt, wc, item_id, json.dumps(payload)),
        )
        conn.commit()
    except Exception as e:
        current_app.logger.error("Failed to insert webhook event: %s", e)

    # Diagnostics to return
    mode = None
    inserted = 0
    totals = {"added": 0, "modified": 0, "removed": 0}

    # Treat both classic and sync webhook codes as triggers.
    is_classic_tx = (wt == "TRANSACTIONS" and wc in ("DEFAULT_UPDATE", "INITIAL_UPDATE", "HISTORICAL_UPDATE"))
    is_sync_tx = (wt == "TRANSACTIONS" and wc == "SYNC_UPDATES_AVAILABLE")

    if item_id and (is_classic_tx or is_sync_tx):
        access_token = get_access_token_by_item(conn, item_id)
        if not access_token:
            current_app.logger.warning("No access_token for item_id=%s; skipping fetch.", item_id)
        else:
            client = get_plaid_client()
            try:
                if HAS_SYNC:
                    mode = "SYNC"
                    cursor = get_cursor(conn, item_id)  # None on first call
                    has_more = True
                    latest_cursor = cursor
                    while has_more:
                        req = TransactionsSyncRequest(access_token=access_token, cursor=latest_cursor)
                        resp = client.transactions_sync(req)
                        added = resp["added"]
                        modified = resp["modified"]
                        removed = resp["removed"]
                        has_more = resp["has_more"]
                        latest_cursor = resp["next_cursor"]

                        totals["added"] += len(added)
                        totals["modified"] += len(modified)
                        totals["removed"] += len(removed)

                        inserted += insert_transactions(conn, item_id, added)

                    upsert_cursor(conn, item_id, latest_cursor)
                    current_app.logger.info(
                        "SYNC - item %s: added=%s modified=%s removed=%s (inserted=%s)",
                        item_id, totals["added"], totals["modified"], totals["removed"], inserted
                    )
                else:
                    mode = "GET"
                    txs = fetch_transactions_get(client, access_token, days=30)
                    totals["added"] = len(txs)
                    inserted = insert_transactions(conn, item_id, txs)
                    current_app.logger.info(
                        "GET - fetched=%s; inserted=%s for item %s",
                        len(txs), inserted, item_id
                    )
            except Exception as e:
                current_app.logger.error("Fetch/insert failed for item %s: %s", item_id, e)

    conn.close()

    return jsonify({
        "status": "ok",
        "webhook_type": wt,
        "webhook_code": wc,
        "mode": mode,
        "added_total": totals["added"],
        "modified_total": totals["modified"],
        "removed_total": totals["removed"],
        "inserted": inserted,
        "version": WEBHOOKS_VERSION
    }), 200
