from src.common.config import load_env   # ✅ fixed
from src.common.utils import convert, to_safe_json
from src.storage.db import init_db, save_item, get_all_items, save_transactions, log_event_db
import os
import sqlite3

# Choose environment via ENV_TARGET or default to 'sandbox'
ENV_TARGET = os.getenv("ENV_TARGET", "sandbox")
cfg = load_env(ENV_TARGET)  # loads env/.env.<target> into os.environ

import json
import time
from datetime import date, timedelta
from flask import Flask, jsonify, request, make_response
import plaid
from plaid.api import plaid_api
from plaid.exceptions import ApiException

# Plaid models
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest

# ---- App Config ----
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = (os.getenv("PLAID_ENV") or "sandbox").lower()

print(f"[Plaid Flask] Target={cfg.get('TARGET', ENV_TARGET)}  PLAID_ENV={PLAID_ENV}")

# ---- Plaid Environment Mapping (new SDK) ----
ENV_MAP = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

plaid_host = ENV_MAP.get(PLAID_ENV, "https://sandbox.plaid.com")

configuration = plaid.Configuration(
    host=plaid_host,
    api_key={
        "clientId": PLAID_CLIENT_ID,
        "secret": PLAID_SECRET
    }
)
api_client = plaid.ApiClient(configuration)
client = plaid_api.PlaidApi(api_client)

app = Flask(__name__)

# ---- Initialize DB ----
init_db()

# ---- Import maintenance helpers ----
from src.ingestion.debug_db import analyze_db, vacuum_db   # ✅ fixed

# ---- Logging setup ----
import logging
from src.common.paths import LOG_DIR, DB_FILE  # ✅ import your new shared paths

LOG_DIR.mkdir(exist_ok=True)

# Maintenance log (shared with debug_db.py)
MAINTENANCE_LOG = LOG_DIR / "maintenance.log"
maintenance_logger = logging.getLogger("maintenance")
maintenance_handler = logging.FileHandler(MAINTENANCE_LOG)
maintenance_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
maintenance_logger.addHandler(maintenance_handler)
maintenance_logger.setLevel(logging.INFO)

def log_maintenance(msg):
    maintenance_logger.info(msg)
    print(msg)
    log_event_db("maintenance", "INFO", msg)


# App log
APP_LOG = LOG_DIR / "app.log"
app_logger = logging.getLogger("app")
app_handler = logging.FileHandler(APP_LOG)
app_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
app_logger.addHandler(app_handler)
app_logger.setLevel(logging.INFO)

def log_app(msg, level="info"):
    getattr(app_logger, level)(msg)
    print(msg)
    log_event_db("app", level.upper(), msg)

log_app(f"App starting: Target={cfg.get('TARGET', ENV_TARGET)}, PLAID_ENV={PLAID_ENV}")

# save_webhook_function
def save_webhook_event(webhook_type, webhook_code, item_id, payload):
    """Save webhook event details to the database."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO webhook_events (received_at, webhook_type, webhook_code, item_id, payload)
        VALUES (datetime('now'), ?, ?, ?, ?)
    """, (webhook_type, webhook_code, item_id, str(payload)))
    conn.commit()
    conn.close()

# ---- 1) Serve Plaid Link ----
@app.route("/")
def index():
    log_app("Serving / (Plaid Link HTML)")
    html = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Plaid Flask Quick Flow</title>
    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
    <style>
      body { font-family: system-ui, sans-serif; padding: 2rem; }
      button { padding: .6rem 1rem; font-size: 1rem; margin-right: .5rem; }
      pre { background:#f6f8fa; padding:1rem; border-radius:8px; max-width: 1000px; overflow:auto;}
    </style>
  </head>
  <body>
    <h1>Plaid Link → Exchange → Fetch</h1>
    <p>Connect a (sandbox) bank, then fetch accounts & transactions.</p>
    <button id="link-btn">Connect a bank</button>
    <button id="accounts-btn" disabled>Fetch Accounts</button>
    <button id="txns-btn" disabled>Fetch Transactions (last 30 days)</button>
    <pre id="out"></pre>

<script>
const out = document.getElementById('out');
const accountsBtn = document.getElementById('accounts-btn');
const txnsBtn = document.getElementById('txns-btn');

async function log(obj){ out.textContent = JSON.stringify(obj, null, 2); }

async function createLinkToken() {
  const r = await fetch('/link_token/create', { method: 'POST' });
  const j = await r.json();
  if (!j.link_token) { await log(j); throw new Error('No link_token'); }
  return j.link_token;
}

async function openLink() {
  try {
    const linkToken = await createLinkToken();
    const handler = Plaid.create({
      token: linkToken,
      onSuccess: async function(public_token, metadata) {
        const r = await fetch('/item/public_token/exchange', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ public_token })
        });
        const j = await r.json();
        await log({ step: "exchanged", response: j });
        accountsBtn.disabled = false;
        txnsBtn.disabled = false;
      },
      onExit: function(err, metadata) { if (err) log({ exit: err }); },
    });
    handler.open();
  } catch(e) {
    await log({ error: e.message || String(e) });
  }
}

document.getElementById('link-btn').onclick = openLink;

accountsBtn.onclick = async () => {
  const r = await fetch('/accounts', { method: 'GET' });
  const j = await r.json();
  await log(j);
};

txnsBtn.onclick = async () => {
  const r = await fetch('/transactions', { method: 'GET' });
  const j = await r.json();
  await log(j);
};
</script>
  </body>
</html>"""
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html"
    return resp

# ---- 2) Create a Link token ----
@app.route("/link_token/create", methods=["POST"])
def link_token_create():
    log_app("Endpoint hit: /link_token/create")

    # 👇 Read your webhook URL from the environment (set in .env.sandbox)
    webhook_url = os.getenv("PLAID_WEBHOOK_URL")
    if not webhook_url:
        log_app("⚠️ PLAID_WEBHOOK_URL not set — webhooks won't fire", level="warning")

    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="demo-user-123"),
        client_name="Flask Finance Tracker",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
        webhook=webhook_url,   # ✅ Tell Plaid where to POST webhooks
    )

    res = client.link_token_create(req)
    return jsonify(convert({"link_token": res.link_token}))

# ---- 3) Exchange public_token for access_token ----
@app.route("/item/public_token/exchange", methods=["POST"])
def item_public_token_exchange():
    log_app("Endpoint hit: /item/public_token/exchange")
    data = request.get_json(silent=True) or {}
    public_token = data.get("public_token")
    if not public_token:
        return jsonify(convert({"error": "missing public_token"})), 400

    res = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )

    item_id = res.item_id
    access_token = res.access_token

    save_item(item_id, access_token, institution="demo-bank")
    log_app(f"Item exchanged: item_id={item_id}")

    return jsonify(convert({
        "item_id": item_id,
        "ok": True,
        "debug_note": "Full response printed to server console"
    }))

# ---- 4) Fetch accounts ----
@app.route("/accounts", methods=["GET"])
def accounts():
    log_app("Endpoint hit: /accounts")
    items = get_all_items()
    if not items:
        return jsonify(convert({"error": "No stored items yet."})), 400

    item_id, access_token, institution = items[0]

    req = AccountsGetRequest(access_token=access_token)
    res = client.accounts_get(req)

    accts = [
        {
            "name": a.get("name"),
            "official_name": a.get("official_name"),
            "mask": a.get("mask"),
            "type": str(a.get("type")),
            "subtype": str(a.get("subtype")),
            "current": a.balances.current,
            "available": a.balances.available,
            "iso_currency_code": a.balances.iso_currency_code,
        }
        for a in res.get("accounts", [])
    ]
    log_app(f"Accounts fetched: {len(accts)} accounts returned")
    return jsonify(convert({"accounts": accts}))

# ---- 5) Fetch transactions ----
@app.route("/transactions", methods=["GET"])
def transactions():
    log_app("Endpoint hit: /transactions")
    items = get_all_items()
    if not items:
        return jsonify(convert({"error": "No stored items yet."})), 400

    start_date = date.today() - timedelta(days=30)
    end_date = date.today()
    all_txns = []

    for item_id, access_token, institution in items:
        try:
            req = TransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
            )
            res = client.transactions_get(req)

            txns = [
                {
                    "transaction_id": t.get("transaction_id"),
                    "account_id": t.get("account_id"),
                    "date": t.get("date"),
                    "name": t.get("name"),
                    "amount": float(t.get("amount", 0.0)),
                    "merchant_name": t.get("merchant_name"),
                    "category": t.get("category"),
                    "pending": t.get("pending"),
                    "iso_currency_code": t.get("iso_currency_code"),
                    "unofficial_currency_code": t.get("unofficial_currency_code"),
                }
                for t in res.get("transactions", [])
            ]

            save_transactions(item_id, txns)
            log_app(f"Transactions fetched: {len(txns)} saved for item {item_id}")

            all_txns.extend(txns)

        except ApiException as e:
            log_app(f"Plaid error during /transactions for item {item_id}", level="error")

    # Maintenance after all items processed
    analyze_db()
    log_maintenance("ANALYZE run after /transactions fetch")
    vacuum_db()
    log_maintenance("VACUUM check run after /transactions fetch")

    return jsonify(convert({"count": len(all_txns), "transactions": all_txns}))

# ---- 6) Webhook endpoint ----
@app.route("/plaid/webhook", methods=["POST"])
def plaid_webhook():
    print("📬 DEBUG: /plaid/webhook endpoint hit")  # ✅ Confirm the route is reached

    # Parse payload
    data = request.get_json(silent=True) or {}
    print(f"📦 DEBUG: Raw webhook payload:\n{json.dumps(data, indent=2)}")

    webhook_type = data.get("webhook_type")
    webhook_code = data.get("webhook_code")
    item_id = data.get("item_id")

    print(f"🔎 DEBUG: webhook_type={webhook_type}, webhook_code={webhook_code}, item_id={item_id}")

    # Save webhook to DB
    save_webhook_event(webhook_type, webhook_code, item_id, data)
    log_app(f"Webhook received: type={webhook_type}, code={webhook_code}, item_id={item_id}")

    # Handle TRANSACTIONS updates
    log_app(f"🔎 DEBUG: webhook_type={webhook_type}, webhook_code={webhook_code}, item_id={item_id}")
    log_app(f"🔎 DEBUG: Known item_ids in DB: {[it[0] for it in get_all_items()]}")
    if webhook_type == "TRANSACTIONS" and item_id:
        print("✅ DEBUG: TRANSACTIONS webhook detected, searching for matching item...")
        items = get_all_items()
        print(f"📊 DEBUG: Found {len(items)} items in DB")

        for it_item_id, access_token, institution in items:
            print(f"🔁 DEBUG: Checking item {it_item_id}")
            if it_item_id == item_id:
                print("🎯 DEBUG: Matching item found! Attempting transaction fetch...")
                try:
                    start_date = date.today() - timedelta(days=30)
                    end_date = date.today()

                    req = TransactionsGetRequest(
                        access_token=access_token,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    res = client.transactions_get(req)
                    txns = [
                        {
                            "transaction_id": t.get("transaction_id"),
                            "account_id": t.get("account_id"),
                            "date": t.get("date"),
                            "name": t.get("name"),
                            "amount": float(t.get("amount", 0.0)),
                            "merchant_name": t.get("merchant_name"),
                            "category": t.get("category"),
                            "pending": t.get("pending"),
                            "iso_currency_code": t.get("iso_currency_code"),
                            "unofficial_currency_code": t.get("unofficial_currency_code"),
                        }
                        for t in res.get("transactions", [])
                    ]
                    print(f"💾 DEBUG: {len(txns)} transactions fetched and ready to save")
                    save_transactions(item_id, txns)
                    log_app(f"Webhook transactions fetched: {len(txns)} saved for item {item_id}")

                    # ---- Auto-maintenance ----
                    analyze_db()
                    log_maintenance(f"ANALYZE run after webhook for item {item_id}")
                    vacuum_db()
                    log_maintenance(f"VACUUM check run after webhook for item {item_id}")

                    return jsonify(convert({
                        "status": "ok",
                        "webhook_type": webhook_type,
                        "webhook_code": webhook_code,
                        "fetched_transactions": len(txns)
                    }))
                except ApiException as e:
                    print(f"❌ DEBUG: Plaid API error during webhook: {e}")
                    log_app(f"Plaid API error during webhook for item {item_id}", level="error")
                    return jsonify(convert({
                        "status": "error",
                        "detail": getattr(e, "body", str(e))
                    })), 500
        print("⚠️ DEBUG: No matching item_id found in DB for this webhook")
    else:
        print("ℹ️ DEBUG: Webhook type is not TRANSACTIONS or missing item_id")

    return jsonify(convert({"status": "ok", "note": "Unhandled webhook"}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
