"""
Personal Finance App
- Flask + Plaid integration
- Local + ngrok access
- Flask-Login authentication for /reports
"""

import os, sqlite3, json, time, logging, plaid
from datetime import date, timedelta, datetime
from flask import Flask, jsonify, request, make_response
from plaid.api import plaid_api
from plaid.exceptions import ApiException
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest

# ------------------------------------------------------------
#  Internal imports
# ------------------------------------------------------------
from src.common.config import load_env
from src.common.utils import convert, to_safe_json
from src.storage.db import (
    init_db,
    save_item,
    get_all_items,
    save_transactions,
    save_accounts,
    log_event_db,
    insert_plaid_raw   # <-- add this
)
from src.ingestion.debug_db import analyze_db, vacuum_db
from src.common.paths import LOG_DIR, DB_FILE

# ------------------------------------------------------------
#  Environment / Config
# ------------------------------------------------------------
ENV_TARGET = os.getenv("ENV_TARGET", "sandbox")
cfg = load_env(ENV_TARGET)

# ------------------------------------------------------------
#  Plaid Client Setup
# ------------------------------------------------------------
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = (os.getenv("PLAID_ENV") or "sandbox").lower()
ENV_MAP = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
plaid_host = ENV_MAP.get(PLAID_ENV, "https://sandbox.plaid.com")

print("DEBUG app.py: ENV_TARGET=", os.getenv("ENV_TARGET"),
      " PLAID_ENV=", PLAID_ENV,
      " plaid_host=", plaid_host)

configuration = plaid.Configuration(
    host=plaid_host,
    api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET}
)
api_client = plaid.ApiClient(configuration)
client = plaid_api.PlaidApi(api_client)

# ------------------------------------------------------------
#  Flask App + Blueprints
# ------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key")  # replace in prod

# Use only the reports blueprint (Flask-Login + bcrypt)
from src.presentation.reports import reports_bp, login_manager, ensure_summary_views_and_tables
ensure_summary_views_and_tables()

# One-time initializer flag
app.config["REPORTS_INIT_DONE"] = False

def _setup_reporting_views_once():
    if not app.config.get("REPORTS_INIT_DONE"):
        try:
            ensure_summary_views_and_tables()
            log_app("Reporting views/tables ensured.")
        except Exception as e:
            log_app(f"Warning during ensure_summary_views_and_tables(): {e}", "warning")
        finally:
            app.config["REPORTS_INIT_DONE"] = True

login_manager.init_app(app)
login_manager.login_view = "reports.login"
app.register_blueprint(reports_bp, url_prefix="/reports")
# Session timeout
app.permanent_session_lifetime = timedelta(
    minutes=int(os.getenv("REPORTS_SESSION_MINUTES", "30"))
 )   




#@app.before_first_request
#def setup_reporting_views():
#    """Initialize summary views/tables once Flask context is ready."""
#    try:
#        #ensure_summary_views_and_tables()
#        log_app("Reporting views/tables ensured.")
#    except Exception as e:
#        log_app(f"Warning during ensure_summary_views_and_tables(): {e}", "warning")
#
#login_manager.init_app(app)
#login_manager.login_view = "reports.login"
#app.register_blueprint(reports_bp, url_prefix="/reports")


#

# ------------------------------------------------------------
#  raw data helpers
# ------------------------------------------------------------
import os
import json
from datetime import datetime
from src.common.paths import DATA_DIR

RAW_DIR = DATA_DIR / "raw" / "plaid" / ENV_TARGET
RAW_DIR.mkdir(parents=True, exist_ok=True)

RAW_CAPTURE_ENABLED = os.getenv("RAW_PLAID_CAPTURE", "1") == "1"
RAW_CAPTURE_LIMIT = int(os.getenv("RAW_PLAID_CAPTURE_LIMIT", "25"))  # keep last N files per endpoint

# Note: "client_id" and "secret" will almost never appear in responses, but harmless to include.
SENSITIVE_KEYS = {"access_token", "public_token", "secret", "client_id"}

def _redact(obj):
    """Recursively redact sensitive keys in dict/list structures."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj

def _trim_raw_files(endpoint: str):
    prefix = endpoint.replace("/", "_") + "_"
    files = sorted(
        [p for p in RAW_DIR.glob(f"{prefix}*.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    for p in files[RAW_CAPTURE_LIMIT:]:
        try:
            p.unlink()
        except Exception as e:
            # don't crash app for cleanup issues
            try:
                log_event_db("plaid_raw", "WARNING", f"Trim raw file failed: {p.name} ({e})")
            except Exception:
                pass

def capture_plaid_raw(endpoint: str, item_id: str | None, payload: dict):
    """
    Persist the full Plaid response payload (redacted) so you can inspect every raw field later.
    1) Writes redacted JSON to disk (data/raw/plaid/<env>/...)
    2) Inserts the same redacted JSON into DB table plaid_raw
    """
    if not RAW_CAPTURE_ENABLED:
        return

    safe = _redact(payload)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    iid = item_id or "no_item"
    fname = f"{endpoint.replace('/', '_')}_{iid}_{ts}.json"
    fpath = RAW_DIR / fname

    # --- Write to disk ---
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(safe, f, indent=2, ensure_ascii=False)
    except Exception as e:
        # disk write failing shouldn't hide the payload entirely
        log_event_db("plaid_raw", "ERROR", f"File write failed for {fname}: {e}")

    # --- Insert into DB (redacted) ---
    try:
        req_id = safe.get("request_id") if isinstance(safe, dict) else None
        insert_plaid_raw(
            env_target=ENV_TARGET,
            endpoint=endpoint,
            item_id=item_id,
            request_id=req_id,
            payload=safe,
        )
    except Exception as e:
        log_event_db("plaid_raw", "ERROR", f"DB insert failed for endpoint={endpoint} item_id={item_id}: {e}")

    # --- Index entry in log_events (optional but useful) ---
    try:
        log_event_db("plaid_raw", "INFO", f"Captured {endpoint} raw payload -> {fpath.name}")
    except Exception:
        pass

    _trim_raw_files(endpoint)

# ------------------------------------------------------------
#  Logging Setup
# ------------------------------------------------------------
init_db()
LOG_DIR.mkdir(exist_ok=True)

MAINTENANCE_LOG = LOG_DIR / "maintenance.log"
maintenance_logger = logging.getLogger("maintenance")
if not maintenance_logger.handlers:
    maintenance_handler = logging.FileHandler(MAINTENANCE_LOG)
    maintenance_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    maintenance_logger.addHandler(maintenance_handler)
    maintenance_logger.setLevel(logging.INFO)

APP_LOG = LOG_DIR / "app.log"
app_logger = logging.getLogger("app")
if not app_logger.handlers:
    app_handler = logging.FileHandler(APP_LOG)
    app_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    app_logger.addHandler(app_handler)
    app_logger.setLevel(logging.INFO)

def log_maintenance(msg):
    maintenance_logger.info(msg)
    print(msg)
    log_event_db("maintenance", "INFO", msg)

def log_app(msg, level="info"):
    getattr(app_logger, level)(msg)
    print(msg)
    log_event_db("app", level.upper(), msg)

log_app(f"App starting: Target={cfg.get('TARGET', ENV_TARGET)}, PLAID_ENV={PLAID_ENV}")

# ------------------------------------------------------------
#  Helper: Record Webhook Event
# ------------------------------------------------------------
def save_webhook_event(webhook_type, webhook_code, item_id, payload):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO webhook_events (received_at, webhook_type, webhook_code, item_id, payload)
        VALUES (datetime('now'), ?, ?, ?, ?)
        """,
        (webhook_type, webhook_code, item_id, str(payload))
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------
#  Plaid API Routes (Link Token, Exchange, Accounts, Transactions)
# ------------------------------------------------------------

@app.route("/link_token/create", methods=["POST"])
def link_token_create():
    """Create a link_token for Plaid Link."""
    try:
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=str(time.time())),
            link_customization_name = "data_transparency_messaging"
        )
        res = client.link_token_create(req)
        return jsonify(res.to_dict())
    except Exception as e:
        log_app(f"Error in link_token_create: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/item/public_token/exchange", methods=["POST"])
def item_public_token_exchange():
    """Exchange the public_token for an access_token."""
    try:
        public_token = request.json.get("public_token")
        req = ItemPublicTokenExchangeRequest(public_token=public_token)
        res = client.item_public_token_exchange(req)

        res_dict = res.to_dict()
        capture_plaid_raw("item/public_token/exchange", None, res_dict)
        access_token = res_dict["access_token"]
        item_id = res_dict["item_id"]

        # Save the item itself
        save_item(item_id, access_token)
        log_app(f"Stored Item: {item_id}")

        # Immediately fetch & save accounts for this item
        try:
            acct_req = AccountsGetRequest(access_token=access_token)
            acct_res = client.accounts_get(acct_req).to_dict()
            capture_plaid_raw("accounts/get", item_id, acct_res)
            accounts = acct_res.get("accounts", [])
            if accounts:
                save_accounts(item_id, accounts)
                log_app(f"Saved {len(accounts)} accounts for item {item_id}")
        except Exception as e:
            log_app(
                f"Warning: could not fetch/save accounts for new item {item_id}: {e}",
                "warning"
            )

        return jsonify({"item_id": item_id})

    except Exception as e:
        log_app(f"Error during public_token exchange: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/accounts", methods=["GET"])
def get_accounts():
    """Fetch accounts for the most recent Item and persist them."""
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        # Most recently linked item
        item = items[-1]
        item_id = item["item_id"]
        access_token = item["access_token"]

        req = AccountsGetRequest(access_token=access_token)
        res = client.accounts_get(req).to_dict()
        capture_plaid_raw("accounts/get", item_id, res)
        accounts = res.get("accounts", [])

        if accounts:
            save_accounts(item_id, accounts)
            log_app(f"Refreshed {len(accounts)} accounts for item {item_id}")

        return jsonify(res)

    except Exception as e:
        log_app(f"Error fetching accounts: {e}", "error")
        return jsonify({"error": str(e)}), 400



@app.route("/transactions", methods=["GET"])
def get_transactions():
    """Fetch recent transactions and store them."""
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        item = items[-1]  # most recently linked institution
        access_token = item["access_token"]
        item_id = item["item_id"]

        end_date = date.today()
        start_date = end_date - timedelta(days=30)

        req = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
        )
        res = client.transactions_get(req).to_dict()
        capture_plaid_raw("transactions/get", item_id, res)

        transactions = res.get("transactions", [])
        if transactions:
            save_transactions(item_id, transactions)  # correct signature

        return jsonify({
            "item_id": item_id,
            "count": len(transactions),
            "transactions": transactions
        })

    except Exception as e:
        log_app(f"Error fetching transactions: {e}", "error")
        return jsonify({"error": str(e)}), 400


# ------------------------------------------------------------
#  Routes
# ------------------------------------------------------------
@app.route("/")
def index():
    """Root page — Plaid Link demo + navigation to Reports."""
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
      pre { background:#f6f8fa; padding:1rem; border-radius:8px; max-width:1000px; overflow:auto;}
      a { display:inline-block; margin-top:1rem; }
    </style>
  </head>
  <body>
    <h1>Plaid Link → Exchange → Fetch</h1>
    <p>Connect a bank, then fetch accounts & transactions.</p>
    <button id="link-btn">Connect a bank</button>
    <button id="accounts-btn" disabled>Fetch Accounts</button>
    <button id="txns-btn" disabled>Fetch Transactions (last 30 days)</button>
    <pre id="out"></pre>

    <p><a href="/reports">→ View Latest Analysis Reports</a></p>
    <p><a href="/reports/login">→ Reports Login</a></p>

<script>
const out=document.getElementById('out');
const accountsBtn=document.getElementById('accounts-btn');
const txnsBtn=document.getElementById('txns-btn');
async function log(obj){out.textContent=JSON.stringify(obj,null,2);}
async function createLinkToken(){
  const r=await fetch('/link_token/create',{method:'POST'});
  const j=await r.json(); if(!j.link_token){await log(j); throw new Error('No link_token');}
  return j.link_token;
}
async function openLink(){
  try{
    const linkToken=await createLinkToken();
    const handler=Plaid.create({
      token:linkToken,
      onSuccess:async function(public_token,metadata){
        const r=await fetch('/item/public_token/exchange',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({public_token})
        });
        const j=await r.json(); await log({step:'exchanged',response:j});
        accountsBtn.disabled=false; txnsBtn.disabled=false;
      },
      onExit:function(err,metadata){if(err)log({exit:err});},
    }); handler.open();
  }catch(e){await log({error:e.message||String(e)});}
}
document.getElementById('link-btn').onclick=openLink;
accountsBtn.onclick=async()=>{const r=await fetch('/accounts');await log(await r.json());};
txnsBtn.onclick=async()=>{const r=await fetch('/transactions');await log(await r.json());};
</script>
  </body>
</html>"""
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html"
    return resp

# ------------------------------------------------------------
#  Entrypoint
# ------------------------------------------------------------
if __name__ == "__main__":
    target = ENV_TARGET.lower()
    port = 5002 if target == "sandbox" else (5001 if target == "development" else 5000)
    host = "0.0.0.0" if target == "sandbox" else "127.0.0.1"
    app.run(host=host, port=port, debug=(target != "production"))
