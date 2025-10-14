"""
Personal Finance Sandbox App
- Flask + Plaid integration
- Local + ngrok access
- Flask-Login authentication for /reports
"""

import os, sqlite3, json, time, logging, plaid
from datetime import date, timedelta
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
from src.storage.db import init_db, save_item, get_all_items, save_transactions, log_event_db
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

login_manager.init_app(app)
login_manager.login_view = "reports.login"

app.register_blueprint(reports_bp, url_prefix="/reports")

# Session timeout
app.permanent_session_lifetime = timedelta(
    minutes=int(os.getenv("REPORTS_SESSION_MINUTES", "30"))
)

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
    <p>Connect a (sandbox) bank, then fetch accounts & transactions.</p>
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
