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
from datetime import timedelta

from flask import Flask, jsonify, request, make_response
from plaid.api import plaid_api
from plaid.exceptions import ApiException

from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.item_remove_request import ItemRemoveRequest

# ------------------------------------------------------------
#  Internal imports
# ------------------------------------------------------------
from src.common.config import load_env
from src.storage.db import (
    init_db,
    save_item,
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
    count_transactions_canonical,
    delete_item_local,
    get_item_by_id,
    count_items_by_institution,
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
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key")

# Reports blueprint
from src.presentation.reports import reports_bp, login_manager, ensure_summary_views_and_tables  # noqa: E402

if os.getenv("ENABLE_SUMMARY_OBJECTS", "0") == "1":
    ensure_summary_views_and_tables()
else:
    print("Summary objects creation disabled (ENABLE_SUMMARY_OBJECTS!=1)")

app.config["REPORTS_INIT_DONE"] = False

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


def log_maintenance(msg: str) -> None:
    maintenance_logger.info(msg)
    print(msg)
    log_event_db("maintenance", "INFO", msg)


def log_app(msg: str, level: str = "info") -> None:
    getattr(app_logger, level)(msg)
    print(msg)
    log_event_db("app", level.upper(), msg)


log_app(f"App starting: Target={cfg.get('TARGET', ENV_TARGET)}, PLAID_ENV={PLAID_ENV}")

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
#  Plaid API Routes
# ------------------------------------------------------------
@app.route("/link_token/create", methods=["POST"])
def link_token_create():
    try:
        webhook = os.getenv("PLAID_WEBHOOK_URL")
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


@app.route("/link_token/update", methods=["POST"])
def link_token_update():
    """
    Create a Link token in UPDATE MODE for an existing item.
    Expects: { "item_id": "..." }
    """
    try:
        payload = request.json or {}
        item_id = payload.get("item_id")
        if not item_id:
            return jsonify({"error": "Missing item_id"}), 400

        item = get_item_by_id(item_id)
        if not item:
            return jsonify({"error": f"Unknown item_id: {item_id}"}), 404

        webhook = os.getenv("PLAID_WEBHOOK_URL")
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=str(time.time())),
            access_token=item["access_token"],  # UPDATE MODE
            webhook=webhook,
        )
        res = client.link_token_create(req)
        return jsonify(res.to_dict())
    except Exception as e:
        log_app(f"Error in link_token_update: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/item/public_token/exchange", methods=["POST"])
def item_public_token_exchange():
    """
    Expects JSON:
      {
        "public_token": "...",
        "institution_id": "...",      (optional)
        "institution_name": "...",    (optional)
        "confirm_duplicate": false    (optional)
      }
    Soft-block: if already linked and confirm_duplicate is false, returns 409 with message.
    """
    try:
        payload = request.json or {}
        public_token = payload.get("public_token")
        institution_id = payload.get("institution_id")
        institution_name = payload.get("institution_name")
        confirm_duplicate = bool(payload.get("confirm_duplicate", False))

        if not public_token:
            return jsonify({"error": "Missing public_token"}), 400

        existing_count = 0
        if institution_id:
            existing_count = int(count_items_by_institution(institution_id))

        if existing_count > 0 and not confirm_duplicate:
            return jsonify({
                "error": "Institution already linked",
                "action": "confirm_duplicate",
                "institution_id": institution_id,
                "institution_name": institution_name,
                "already_linked_count": existing_count,
                "message": (
                    f"{institution_name or 'This institution'} already has "
                    f"{existing_count} linked connection(s). "
                    "If you are fixing login issues, use Reconnect selected. "
                    "Click OK to proceed and create another connection, or Cancel to stop."
                ),
            }), 409

        # Exchange public token -> access token + item_id
        req = ItemPublicTokenExchangeRequest(public_token=public_token)
        res = client.item_public_token_exchange(req)
        res_dict = res.to_dict()
        capture_plaid_raw("item/public_token/exchange", None, res_dict)

        access_token = res_dict["access_token"]
        item_id = res_dict["item_id"]

        save_item(
            item_id=item_id,
            access_token=access_token,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        log_app(f"Stored Item: {item_id} (institution_id={institution_id}, name={institution_name})")

        # Immediately fetch & save accounts for the new item
        try:
            acct_req = AccountsGetRequest(access_token=access_token)
            acct_res = client.accounts_get(acct_req).to_dict()
            capture_plaid_raw("accounts/get", item_id, acct_res)

            accounts = acct_res.get("accounts", []) or []
            save_accounts(item_id, accounts)
            log_app(f"Saved {len(accounts)} accounts for item {item_id}")
        except Exception as e:
            log_app(f"Warning: could not fetch/save accounts for new item {item_id}: {e}", "warning")

        return jsonify({
            "ok": True,
            "item_id": item_id,
            "institution_id": institution_id,
            "institution_name": institution_name,
            "already_linked_count_before": existing_count,
        })

    except Exception as e:
        log_app(f"Error during public_token exchange: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/items", methods=["GET"])
def list_items():
    """
    Returns linked items WITHOUT access tokens (safe for UI).
    """
    try:
        items = get_all_items()
        safe = [
            {
                "item_id": i["item_id"],
                "institution_id": i.get("institution_id"),
                "institution_name": i.get("institution_name"),
            }
            for i in items
        ]
        return jsonify({"count": len(safe), "items": safe})
    except Exception as e:
        log_app(f"Error in /items: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/item/remove", methods=["POST"])
def item_remove():
    """
    Removes an item in Plaid AND deletes local rows (cascade) so we avoid "already linked" issues.
    Expects: { "item_id": "..." }
    """
    try:
        payload = request.json or {}
        item_id = payload.get("item_id")
        if not item_id:
            return jsonify({"error": "Missing item_id"}), 400

        item = get_item_by_id(item_id)
        if not item:
            return jsonify({"error": f"Unknown item_id: {item_id}"}), 404

        access_token = item["access_token"]

        # 1) Unlink at Plaid
        req = ItemRemoveRequest(access_token=access_token)
        res = client.item_remove(req).to_dict()
        capture_plaid_raw("item/remove", item_id, res)

        # 2) Delete local (FK cascade handles children)
        delete_item_local(item_id)

        log_app(f"Removed item_id={item_id} ({item.get('institution_name')})")
        return jsonify({"ok": True, "item_id": item_id, "plaid": res})

    except ApiException as e:
        log_app(f"Plaid ApiException in /item/remove: {e}", "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_app(f"Error in /item/remove: {e}", "error")
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


@app.route("/transactions", methods=["GET"])
def get_transactions():
    """
    Canonical read ONLY (no Plaid calls).
    Use /sync_all or /transactions/sync to ingest first.
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


def _sync_accounts_for_items(items: list[dict]) -> dict:
    """
    Best-effort accounts sync:
      - continues even if one item fails
      - returns {items: [...], errors: [...]}
    """
    results = []
    errors = []

    for item in items:
        item_id = item["item_id"]
        access_token = item["access_token"]

        try:
            req = AccountsGetRequest(access_token=access_token)
            res = client.accounts_get(req).to_dict()
            capture_plaid_raw("accounts/get", item_id, res)

            accounts = res.get("accounts", []) or []
            save_accounts(item_id, accounts)

            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "accounts_saved": len(accounts),
                "ok": True,
            })

        except ApiException as e:
            msg = str(e)
            log_app(f"Plaid ApiException in accounts/get item_id={item_id}: {msg}", "error")
            errors.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "stage": "accounts/get",
                "error": msg,
            })
            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "accounts_saved": 0,
                "ok": False,
            })

        except Exception as e:
            msg = str(e)
            log_app(f"Error in accounts sync item_id={item_id}: {msg}", "error")
            errors.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "stage": "accounts/get",
                "error": msg,
            })
            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "accounts_saved": 0,
                "ok": False,
            })

    return {"items": results, "errors": errors}


def _sync_transactions_for_items(items: list[dict], count: int = 500) -> dict:
    """
    Best-effort transactions sync:
      - continues even if one item fails
      - returns {items: [...], errors: [...]}
    """
    results = []
    errors = []

    for item in items:
        item_id = item["item_id"]
        access_token = item["access_token"]

        cursor = None
        total_added = 0
        total_modified = 0
        total_removed = 0
        pages = 0

        try:
            cursor = get_transaction_cursor(item_id)

            has_more = True
            while has_more:
                req_kwargs = {"access_token": access_token, "count": count}
                if cursor not in (None, "", "None"):
                    req_kwargs["cursor"] = cursor

                req = TransactionsSyncRequest(**req_kwargs)
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

                if cursor not in (None, "", "None"):
                    set_transaction_cursor(item_id, cursor)

                pages += 1

            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "pages": pages,
                "added": total_added,
                "modified": total_modified,
                "removed": total_removed,
                "ok": True,
            })

        except ApiException as e:
            msg = str(e)
            log_app(f"Plaid ApiException in transactions/sync item_id={item_id}: {msg}", "error")
            errors.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "stage": "transactions/sync",
                "error": msg,
            })
            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "pages": pages,
                "added": total_added,
                "modified": total_modified,
                "removed": total_removed,
                "ok": False,
            })

        except Exception as e:
            msg = str(e)
            log_app(f"Error in transactions sync item_id={item_id}: {msg}", "error")
            errors.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "stage": "transactions/sync",
                "error": msg,
            })
            results.append({
                "item_id": item_id,
                "institution_name": item.get("institution_name"),
                "pages": pages,
                "added": total_added,
                "modified": total_modified,
                "removed": total_removed,
                "ok": False,
            })

    return {"items": results, "errors": errors}


@app.route("/sync_all", methods=["POST"])
def sync_all():
    """
    1) Sync accounts for ALL items
    2) Sync transactions for ALL items
    3) Return summary only
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        started = time.time()
        log_app(f"/sync_all START items={len(items)}")

        acct_out = _sync_accounts_for_items(items)
        txn_out = _sync_transactions_for_items(items, count=500)

        elapsed_ms = int((time.time() - started) * 1000)

        canonical_accounts_count = len(get_accounts_canonical())
        canonical_txn_count_30d = count_transactions_canonical(days=30)

        log_app(f"/sync_all END elapsed_ms={elapsed_ms}")

        return jsonify({
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "items_count": len(items),
            "accounts_sync": {
                "items": acct_out["items"],
                "errors": acct_out["errors"],
                "total_accounts_saved": sum(r.get("accounts_saved", 0) for r in acct_out["items"]),
            },
            "transactions_sync": {
                "items": txn_out["items"],
                "errors": txn_out["errors"],
                "total_added": sum(r.get("added", 0) for r in txn_out["items"]),
                "total_modified": sum(r.get("modified", 0) for r in txn_out["items"]),
                "total_removed": sum(r.get("removed", 0) for r in txn_out["items"]),
            },
            "canonical_counts": {
                "accounts_count": canonical_accounts_count,
                "transactions_last_30d_count": canonical_txn_count_30d,
            },
        })

    except ApiException as e:
        log_app(f"Plaid ApiException in /sync_all: {e}", "error")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_app(f"Error in /sync_all: {e}", "error")
        return jsonify({"error": str(e)}), 400


# ------------------------------------------------------------
#  UI Route
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
      #status { padding:.5rem .75rem; background:#fff6d6; border:1px solid #f0d37a; border-radius:8px; margin: .75rem 0; display:none; }
    </style>
  </head>
  <body>
    <h1>Connect + Sync</h1>

    <div style="margin-bottom: 1rem;">
        <button id="refresh-btn">Refresh data</button>
        <button id="add-bank-btn">Add bank</button>
    </div>

    <div style="margin-bottom: 1rem;">
        <label for="item-select"><b>Existing bank:</b></label>
        <select id="item-select" style="min-width: 420px;"></select>
        <button id="reconnect-btn">Reconnect selected</button>
        <button id="remove-btn">Remove selected</button>
    </div>

    <div id="status"></div>
    <pre id="out"></pre>

    <p><a href="/reports">→ View Latest Analysis Reports</a></p>
    <p><a href="/reports/login">→ Reports Login</a></p>

<script>
const out = document.getElementById('out');
const statusEl = document.getElementById('status');

const refreshBtn = document.getElementById('refresh-btn');
const addBtn = document.getElementById('add-bank-btn');
const reconnectBtn = document.getElementById('reconnect-btn');
const removeBtn = document.getElementById('remove-btn');
const itemSelect = document.getElementById('item-select');

function log(obj) { out.textContent = JSON.stringify(obj, null, 2); }

function setStatus(msg) {
  if (!msg) {
    statusEl.style.display = 'none';
    statusEl.textContent = '';
    return;
  }
  statusEl.style.display = 'block';
  statusEl.textContent = msg;
}

function setBusy(busy, label) {
  refreshBtn.disabled = busy;
  addBtn.disabled = busy;
  reconnectBtn.disabled = busy;
  removeBtn.disabled = busy;
  if (busy) setStatus(label || 'Working...');
  else setStatus(null);
}

async function loadItems() {
  const r = await fetch('/items');
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'Failed to load items');

  itemSelect.innerHTML = '';
  (j.items || []).forEach(it => {
    const opt = document.createElement('option');
    opt.value = it.item_id;
    opt.textContent = `${it.institution_name || '(unknown)'} — ${it.item_id}`;
    itemSelect.appendChild(opt);
  });

  return j.items || [];
}

async function createLinkToken() {
  const r = await fetch('/link_token/create', { method: 'POST' });
  const j = await r.json();
  if (!r.ok || !j.link_token) throw new Error('Failed to create link_token');
  return j.link_token;
}

async function createUpdateLinkToken(item_id) {
  const r = await fetch('/link_token/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id })
  });
  const j = await r.json();
  if (!r.ok || !j.link_token) throw new Error(j.error || 'Failed to create update link_token');
  return j.link_token;
}

async function exchangePublicToken(public_token, metadata, confirmDuplicate=false) {
  const inst = (metadata && metadata.institution) ? metadata.institution : null;

  const body = {
    public_token: public_token,
    institution_id: inst ? inst.institution_id : null,
    institution_name: inst ? inst.name : null,
    confirm_duplicate: confirmDuplicate
  };

  const r = await fetch('/item/public_token/exchange', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  const j = await r.json();
  return { ok: r.ok, status: r.status, body: j, sent: body };
}

async function syncAllWithTimeout(ms=180000) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), ms);
  try {
    const r = await fetch('/sync_all', { method: 'POST', signal: controller.signal });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'sync_all failed');
    return j;
  } finally {
    clearTimeout(t);
  }
}

async function removeItem(item_id) {
  const r = await fetch('/item/remove', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id })
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'item/remove failed');
  return j;
}

// --- Button actions ---
refreshBtn.onclick = async () => {
  setBusy(true, 'Refreshing: syncing accounts + transactions...');
  try {
    log({ step: 'refresh starting' });
    const res = await syncAllWithTimeout();
    log({ step: 'refresh complete', res });
    await loadItems();
  } catch (e) {
    log({ step: 'refresh error', error: e.message || String(e) });
  } finally {
    setBusy(false);
  }
};

addBtn.onclick = async () => {
  setBusy(true, 'Add bank: opening Plaid Link...');
  try {
    const linkToken = await createLinkToken();

    const handler = Plaid.create({
      token: linkToken,

      onSuccess: async (public_token, metadata) => {
        setBusy(true, 'Link success: exchanging token...');
        try {
          const first = await exchangePublicToken(public_token, metadata, false);

          if (!first.ok && first.status === 409 && first.body && first.body.action === "confirm_duplicate") {
            const msg = first.body.message || "This institution is already linked. Proceed anyway?";
            const proceed = confirm(msg);
            if (!proceed) {
              log({ step: "duplicate link canceled by user", details: first.body });
              return;
            }

            setBusy(true, 'Confirmed: exchanging token (duplicate allowed)...');
            const second = await exchangePublicToken(public_token, metadata, true);
            if (!second.ok) {
              log({ step: "exchange failed after confirmation", status: second.status, response: second.body });
              throw new Error("Failed to exchange public_token after confirmation");
            }
            log({ step: "exchanged after confirmation", response: second.body });

          } else {
            if (!first.ok) {
              log({ step: "exchange failed", status: first.status, response: first.body });
              throw new Error("Failed to exchange public_token");
            }
            log({ step: "exchanged", response: first.body });
          }

          setBusy(true, 'Syncing all items (this can take a bit on first link)...');
          log({ step: 'sync starting', at: new Date().toISOString() });

          const synced = await syncAllWithTimeout();
          log({ step: "synced", synced });

          await loadItems();

        } catch (e) {
          log({ step: "error after onSuccess", error: e.message || String(e) });
        } finally {
          setBusy(false);
        }
      },

      onExit: (err, metadata) => {
        if (err) log({ step: 'plaid_link exit error', err, metadata });
        setBusy(false);
      }
    });

    handler.open();

  } catch (e) {
    log({ step: 'add bank error', error: e.message || String(e) });
    setBusy(false);
  }
};

reconnectBtn.onclick = async () => {
  const item_id = itemSelect.value;
  if (!item_id) { log({ step: 'reconnect', error: 'No item selected' }); return; }

  setBusy(true, 'Reconnect: opening Plaid Link (update mode)...');
  try {
    const linkToken = await createUpdateLinkToken(item_id);

    const handler = Plaid.create({
      token: linkToken,

      onSuccess: async () => {
        setBusy(true, 'Reconnect success: syncing all items...');
        try {
          const synced = await syncAllWithTimeout();
          log({ step: 'reconnect sync complete', synced });
          await loadItems();
        } catch (e) {
          log({ step: 'reconnect sync error', error: e.message || String(e) });
        } finally {
          setBusy(false);
        }
      },

      onExit: (err, metadata) => {
        if (err) log({ step: 'reconnect exit error', err, metadata });
        setBusy(false);
      }
    });

    handler.open();

  } catch (e) {
    log({ step: 'reconnect error', error: e.message || String(e) });
    setBusy(false);
  }
};

removeBtn.onclick = async () => {
  const item_id = itemSelect.value;
  if (!item_id) { log({ step: 'remove', error: 'No item selected' }); return; }

  if (!confirm('Remove this bank? This revokes Plaid access and deletes local data.')) return;

  setBusy(true, 'Removing bank (Plaid unlink + local delete)...');
  try {
    const res = await removeItem(item_id);
    log({ step: 'removed', res });
    await loadItems();
  } catch (e) {
    log({ step: 'remove error', error: e.message || String(e) });
  } finally {
    setBusy(false);
  }
};

// Initial load
loadItems().catch(e => log({ step: 'load items error', error: e.message || String(e) }));
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
