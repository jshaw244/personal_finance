"""
Personal Finance App
- Flask + Plaid integration
- Local + ngrok access
- Flask-Login authentication for /reports
"""

print("RUNNING FILE:", __file__)
import os
import sqlite3
import json
import time
import logging
import plaid
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
from plaid.model.transactions_sync_request import TransactionsSyncRequest

# ------------------------------------------------------------
#  Internal imports
# ------------------------------------------------------------
from src.common.config import load_env
from src.storage.db import (
    init_db,
    save_item,  # NOTE: update signature to accept institution_id + institution_name
    get_all_items,
    save_transactions,
    save_accounts,
    log_event_db,
    insert_plaid_raw,
    get_transaction_cursor,
    set_transaction_cursor,
    apply_removed_transactions,
    get_accounts_canonical,
    get_transactions_canonical,
)
from src.common.paths import LOG_DIR, DB_FILE, DATA_DIR

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

print(
    "DEBUG app.py: ENV_TARGET=",
    os.getenv("ENV_TARGET"),
    " PLAID_ENV=",
    PLAID_ENV,
    " plaid_host=",
    plaid_host,
)

configuration = plaid.Configuration(
    host=plaid_host,
    api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
)
api_client = plaid.ApiClient(configuration)
client = plaid_api.PlaidApi(api_client)

# ------------------------------------------------------------
#  Flask App + Blueprints
# ------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key")  # replace in prod

# Reports blueprint
from src.presentation.reports import reports_bp, login_manager, ensure_summary_views_and_tables

if os.getenv("ENABLE_SUMMARY_OBJECTS", "0") == "1":
    ensure_summary_views_and_tables()
else:
    print("Summary objects creation disabled (ENABLE_SUMMARY_OBJECTS!=1)")

app.config["REPORTS_INIT_DONE"] = False

def log_maintenance(msg: str) -> None:
    maintenance_logger.info(msg)
    print(msg)
    log_event_db("maintenance", "INFO", msg)

def log_app(msg: str, level: str = "info") -> None:
    getattr(app_logger, level)(msg)
    print(msg)
    log_event_db("app", level.upper(), msg)

login_manager.init_app(app)
login_manager.login_view = "reports.login"
app.register_blueprint(reports_bp, url_prefix="/reports")
app.permanent_session_lifetime = timedelta(
    minutes=int(os.getenv("REPORTS_SESSION_MINUTES", "30"))
)

# ------------------------------------------------------------
#  Raw data capture helpers
# ------------------------------------------------------------
RAW_DIR = DATA_DIR / "raw" / "plaid" / ENV_TARGET
RAW_DIR.mkdir(parents=True, exist_ok=True)

RAW_CAPTURE_ENABLED = os.getenv("RAW_PLAID_CAPTURE", "1") == "1"
RAW_CAPTURE_LIMIT = int(os.getenv("RAW_PLAID_CAPTURE_LIMIT", "25"))
SENSITIVE_KEYS = {"access_token", "public_token", "secret", "client_id"}

def _redact(obj):
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
        reverse=True,
    )
    for p in files[RAW_CAPTURE_LIMIT:]:
        try:
            p.unlink()
        except Exception as e:
            try:
                log_event_db("plaid_raw", "WARNING", f"Trim raw file failed: {p.name} ({e})")
            except Exception:
                pass

def capture_plaid_raw(endpoint: str, item_id: str | None, payload: dict):
    if not RAW_CAPTURE_ENABLED:
        return

    safe = _redact(payload)

    ts = time.strftime("%Y%m%d_%H%M%S")
    iid = item_id or "no_item"
    fname = f"{endpoint.replace('/', '_')}_{iid}_{ts}.json"
    fpath = RAW_DIR / fname

    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(safe, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        log_event_db("plaid_raw", "ERROR", f"File write failed for {fname}: {e}")

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

log_app(f"App starting: Target={cfg.get('TARGET', ENV_TARGET)}, PLAID_ENV={PLAID_ENV}")

# ------------------------------------------------------------
#  Webhook events
# ------------------------------------------------------------
def save_webhook_event(webhook_type, webhook_code, item_id, payload):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO webhook_events (received_at, webhook_type, webhook_code, item_id, payload)
        VALUES (datetime('now'), ?, ?, ?, ?)
        """,
        (webhook_type, webhook_code, item_id, str(payload)),
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------
#  Plaid API Routes
# ------------------------------------------------------------
@app.route("/link_token/create", methods=["POST"])
def link_token_create():
    try:
        webhook = os.getenv("PLAID_WEBHOOK_URL")
        print("DEBUG PLAID_WEBHOOK_URL:", os.getenv("PLAID_WEBHOOK_URL"))
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=str(time.time())),
            link_customization_name="data_transparency_messaging",
            webhook=webhook,
        )
        res = client.link_token_create(req)
        return jsonify(res.to_dict())
    except Exception as e:
        log_app(f"Error in link_token_create: {e}", "error")
        return jsonify({"error": str(e)}), 400

@app.route("/item/public_token/exchange", methods=["POST"])
def item_public_token_exchange():
    """
    Expects JSON from browser like:
      {
        "public_token": "...",
        "institution_id": "...",      (optional)
        "institution_name": "..."     (optional)
      }
    """
    try:
        payload = request.json or {}

        public_token = payload.get("public_token")
        institution_id = payload.get("institution_id")
        institution_name = payload.get("institution_name")

        if not public_token:
            return jsonify({"error": "Missing public_token"}), 400

        req = ItemPublicTokenExchangeRequest(public_token=public_token)
        res = client.item_public_token_exchange(req)

        res_dict = res.to_dict()
        capture_plaid_raw("item/public_token/exchange", None, res_dict)

        access_token = res_dict["access_token"]
        item_id = res_dict["item_id"]

        # Save item + institution metadata
        # NOTE: update save_item() accordingly in db.py
        save_item(
            item_id=item_id,
            access_token=access_token,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        log_app(f"Stored Item: {item_id} (institution_id={institution_id}, name={institution_name})")

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
            log_app(f"Warning: could not fetch/save accounts for new item {item_id}: {e}", "warning")

        return jsonify({"item_id": item_id})

    except Exception as e:
        log_app(f"Error during public_token exchange: {e}", "error")
        return jsonify({"error": str(e)}), 400
    
@app.route("/accounts", methods=["GET"])
def get_accounts():
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        item_id = request.args.get("item_id")  # optional filter
        rows = get_accounts_canonical(item_id=item_id)
        return jsonify({"count": len(rows), "accounts": rows})

    except Exception as e:
        log_app(f"Error fetching canonical accounts: {e}", "error")
        return jsonify({"error": str(e)}), 400
    

@app.route("/accounts/sync", methods=["POST"])
def accounts_sync():
    """
    Pull accounts from Plaid and write to canonical accounts table.
    Loops ALL items (full financial picture).
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        results = _sync_accounts_for_items(items)
        return jsonify({"ok": True, "results": results})

    except ApiException as e:
        log_app(f"Plaid ApiException in /accounts/sync: {e}", "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_app(f"Error in /accounts/sync: {e}", "error")
        return jsonify({"error": str(e)}), 400



@app.route("/transactions", methods=["GET"])
def get_transactions():
    """
    Canonical read ONLY (no Plaid calls).
    Use /transactions/sync to ingest first.
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        days = int(request.args.get("days", "30"))
        item_id = request.args.get("item_id")
        account_id = request.args.get("account_id")
        limit = int(request.args.get("limit", "500"))
        offset = int(request.args.get("offset", "0"))

        rows = get_transactions_canonical(
            days=days,
            item_id=item_id,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )

        return jsonify({"count": len(rows), "transactions": rows})

    except Exception as e:
        log_app(f"Error fetching canonical transactions: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/transactions/sync", methods=["POST"])
def transactions_sync():
    """
    Cursor-based ingestion for ALL items (full financial picture).
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        txn_results = _sync_transactions_for_items(items, count=500)
        return jsonify({"ok": True, "results": txn_results})

    except ApiException as e:
        log_app(f"Plaid ApiException in /transactions/sync: {e}", "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_app(f"Error in /transactions/sync: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/sync_all", methods=["POST"])
def sync_all():
    """
    One-button workflow:
    1) Sync accounts for ALL items
    2) Sync transactions for ALL items
    3) Return canonical reads (summary)
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        # ✅ INSERT THESE TWO LINES RIGHT HERE
        acct_results = _sync_accounts_for_items(items)
        txn_results  = _sync_transactions_for_items(items, count=500)

        # 3) canonical reads (just for preview; ingestion is not limited to 30 days)
        canonical_accounts = get_accounts_canonical()
        canonical_transactions = get_transactions_canonical(days=30, limit=200, offset=0)

        return jsonify({
            "ok": True,
            "accounts_sync": acct_results,
            "transactions_sync": txn_results,
            "canonical": {
                "accounts_count": len(canonical_accounts),
                "transactions_count": len(canonical_transactions),
                "accounts": canonical_accounts,
                "transactions": canonical_transactions,
            },
        })

    except ApiException as e:
        log_app(f"Plaid ApiException in /sync_all: {e}", "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_app(f"Error in /sync_all: {e}", "error")
        return jsonify({"error": str(e)}), 400



def _sync_accounts_for_items(items: list[dict]) -> list[dict]:
    results = []
    for item in items:
        item_id = item["item_id"]
        access_token = item["access_token"]

        req = AccountsGetRequest(access_token=access_token)
        res = client.accounts_get(req).to_dict()
        capture_plaid_raw("accounts/get", item_id, res)

        accounts = res.get("accounts", []) or []
        save_accounts(item_id, accounts)

        results.append({"item_id": item_id, "accounts_saved": len(accounts)})
    return results

def _sync_transactions_for_items(items: list[dict], count: int = 500) -> list[dict]:
    results = []

    for item in items:
        item_id = item["item_id"]
        access_token = item["access_token"]

        cursor = get_transaction_cursor(item_id)

        total_added = 0
        total_modified = 0
        total_removed = 0
        pages = 0

        has_more = True
        while has_more:
            req = TransactionsSyncRequest(
                access_token=access_token,
                cursor=cursor,
                count=count,
            )
            resp = client.transactions_sync(req).to_dict()
            capture_plaid_raw("transactions/sync", item_id, resp)

            added = resp.get("added", []) or []
            modified = resp.get("modified", []) or []
            removed = resp.get("removed", []) or []

            if added or modified:
                save_transactions(item_id, added + modified)
            if removed:
                apply_removed_transactions(item_id, removed)

            total_added += len(added)
            total_modified += len(modified)
            total_removed += len(removed)

            prev_cursor = cursor
            cursor = resp.get("next_cursor")
            has_more = bool(resp.get("has_more"))

            if has_more and cursor == prev_cursor:
                raise RuntimeError(f"transactions/sync cursor did not advance for item_id={item_id}")

            if cursor is not None:
                set_transaction_cursor(item_id, cursor)

            pages += 1

        results.append({
            "item_id": item_id,
            "pages": pages,
            "added": total_added,
            "modified": total_modified,
            "removed": total_removed,
        })

    return results


# ------------------------------------------------------------
#  Routes
# ------------------------------------------------------------
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
      pre { background:#f6f8fa; padding:1rem; border-radius:8px; max-width:1100px; overflow:auto; }
      a { display:inline-block; margin-top:1rem; }
    </style>
  </head>
  <body>
    <h1>Connect + Sync</h1>
    <p>One button: connect a bank, then sync accounts + transactions for all linked items.</p>

    <button id="connect-sync-btn">Connect bank + Sync all</button>
    <pre id="out"></pre>

    <p><a href="/reports">→ View Latest Analysis Reports</a></p>
    <p><a href="/reports/login">→ Reports Login</a></p>

<script>
const out = document.getElementById('out');
const btn = document.getElementById('connect-sync-btn');

function log(obj) {
  out.textContent = JSON.stringify(obj, null, 2);
}

async function createLinkToken() {
  const r = await fetch('/link_token/create', { method: 'POST' });
  const j = await r.json();
  if (!r.ok || !j.link_token) {
    log({ step: 'link_token/create failed', status: r.status, response: j });
    throw new Error('Failed to create link_token');
  }
  return j.link_token;
}

async function exchangePublicToken(public_token, metadata) {
  const inst = (metadata && metadata.institution) ? metadata.institution : null;

  const body = {
    public_token: public_token,
    institution_id: inst ? inst.institution_id : null,
    institution_name: inst ? inst.name : null
  };

  const r = await fetch('/item/public_token/exchange', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  const j = await r.json();
  if (!r.ok) {
    log({ step: 'item/public_token/exchange failed', status: r.status, sent: body, response: j });
    throw new Error('Failed to exchange public_token');
  }

  return { sent: body, response: j };
}

async function syncAll() {
  const r = await fetch('/sync_all', { method: 'POST' });
  const j = await r.json();

  if (!r.ok) {
    log({ step: 'sync_all failed', status: r.status, response: j });
    throw new Error('Failed to sync_all');
  }

  return j;
}

async function connectAndSync() {
  btn.disabled = true;
  btn.textContent = 'Working...';

  try {
    log({ step: 'starting' });

    const linkToken = await createLinkToken();

    const handler = Plaid.create({
      token: linkToken,

      onSuccess: async (public_token, metadata) => {
        try {
          const exchanged = await exchangePublicToken(public_token, metadata);
          log({ step: 'exchanged', ...exchanged });

          const synced = await syncAll();
          log({ step: 'synced', synced });

          btn.disabled = false;
          btn.textContent = 'Connect bank + Sync all';
        } catch (e) {
          log({ step: 'error after onSuccess', error: e.message || String(e) });
          btn.disabled = false;
          btn.textContent = 'Connect bank + Sync all';
        }
      },

      onExit: (err, metadata) => {
        if (err) log({ step: 'plaid_link exit error', err, metadata });
        btn.disabled = false;
        btn.textContent = 'Connect bank + Sync all';
      }
    });

    handler.open();

  } catch (e) {
    log({ step: 'error before link open', error: e.message || String(e) });
    btn.disabled = false;
    btn.textContent = 'Connect bank + Sync all';
  }
}

btn.onclick = connectAndSync;
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
