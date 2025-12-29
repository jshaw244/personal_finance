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
import re

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
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest


# ------------------------------------------------------------
#  Internal imports
# ------------------------------------------------------------
from src.common.config import load_env
from src.classification.rules import apply_classification_rules
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
    upsert_liabilities_raw,
    #apply_classification_rules,
    get_top_merchants,
    get_credit_card_statement_summary,
    upsert_recurring_raw,
    get_recurring_raw,
    get_transactions_for_classification,
    upsert_transaction_classification,
    get_spend_canonical,
    get_transaction_basic,
    normalize_merchant,
    insert_classification_rule,
    apply_best_rule_to_transaction,
    apply_rules_bulk,
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
init_db()
from src.presentation.reports import reports_bp, login_manager, ensure_summary_views_and_tables  # noqa: E402

if os.getenv("ENABLE_SUMMARY_OBJECTS", "0") == "1":
    ensure_summary_views_and_tables()
else:
    print("Summary objects creation disabled (ENABLE_SUMMARY_OBJECTS!=1)")

app.config["REPORTS_INIT_DONE"] = False

# ------------------------------------------------------------
#  Logging Setup
# ------------------------------------------------------------

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

_TRANSFER_HINTS = (
    "transfer", "xfer", "webxfr", "etransfer", "online transfer",
    "ach", "payment", "pmt", "epayment", "autopay",
    "initiate", "initiated payment",
)

def classify_one(txn_row: dict) -> tuple[int, str | None, str | None]:
    """
    Returns: (exclude_from_spend, exclude_reason, merchant_normalized)

    Policy (loose v1):
      - Exclude definite credit card payments
      - Exclude transfers ONLY when text also looks like a transfer/payment
      - Do NOT exclude just because Plaid labeled it TRANSFER_* if merchant looks real
    """
    name = (txn_row.get("name") or "").lower()
    merchant = (txn_row.get("merchant_name") or "").lower()
    merch_norm = normalize_merchant(txn_row.get("merchant_name") or txn_row.get("name"))

    meta = {}
    try:
        if txn_row.get("meta_json"):
            meta = json.loads(txn_row["meta_json"])
    except Exception:
        meta = {}

    pfc = meta.get("personal_finance_category") or {}
    pfc_primary = (pfc.get("primary") or "").upper()
    pfc_detailed = (pfc.get("detailed") or "").upper()

    text = f"{name} {merchant}".strip()
    has_transfer_hints = any(h in text for h in _TRANSFER_HINTS)

    # 1) Strong exclude: Plaid explicitly says credit card payment
    if "CREDIT_CARD_PAYMENT" in pfc_detailed:
        return 1, "pfc_credit_card_payment", merch_norm

    # 2) Transfers: require BOTH (category suggests transfer) AND (text hints OR empty merchant)
    is_transfer_category = (
        "ACCOUNT_TRANSFER" in pfc_detailed
        or pfc_primary in ("TRANSFER_IN", "TRANSFER_OUT")
    )

    merchant_present = bool(merchant) or bool(txn_row.get("merchant_name"))
    if is_transfer_category and (has_transfer_hints or not merchant_present):
        return 1, "pfc_transfer_with_transfer_text", merch_norm

    # 3) Heuristic fallback (no/weak PFC):
    # if it screams payment/transfer, exclude
    if has_transfer_hints and (("card" in text) or ("bank" in text) or ("payment" in text) or ("pmt" in text)):
        return 1, "text_looks_like_payment_or_transfer", merch_norm

    return 0, None, merch_norm


# ------------------------------------------------------------
#  Plaid API Routes
# ------------------------------------------------------------
@app.route("/link_token/create", methods=["POST"])
def link_token_create():
    try:
        webhook = os.getenv("PLAID_WEBHOOK_URL")
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            additional_consented_products=[
                Products("liabilities"),
                Products("investments"),
                Products("recurring_transactions"),
                # Enrich + Transactions Refresh are “endpoint billed” features;
                # consent handling depends on your Plaid setup, but this is the right place to include if supported.
                Products("transactions_refresh"),
                Products("enrich"),
            ],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            client_user_id = os.getenv("APP_USER_ID", "local_user"),
            user=LinkTokenCreateRequestUser(client_user_id=os.getenv("APP_USER_ID", "local_user")),
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
            additional_consented_products=[
                Products("liabilities"),
                Products("investments"),
                Products("recurring_transactions"),
                # Enrich + Transactions Refresh are “endpoint billed” features;
                # consent handling depends on your Plaid setup, but this is the right place to include if supported.
                Products("transactions_refresh"),
                Products("enrich"),
            ],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            client_user_id = os.getenv("APP_USER_ID", "local_user"),
            user=LinkTokenCreateRequestUser(client_user_id=os.getenv("APP_USER_ID", "local_user")),
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



@app.route("/liabilities", methods=["GET"])
def liabilities_get():
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        out = []
        for item in items:
            item_id = item["item_id"]
            access_token = item["access_token"]

            req = LiabilitiesGetRequest(access_token=access_token)
            res = client.liabilities_get(req).to_dict()
            capture_plaid_raw("liabilities/get", item_id, res)

            upsert_liabilities_raw(item_id, res)
            out.append({"item_id": item_id, "ok": True})

        return jsonify({"ok": True, "items": out})
    except Exception as e:
        log_app(f"Error in /liabilities: {e}", "error")
        return jsonify({"error": str(e)}), 400   


@app.route("/recurring", methods=["GET"])
def recurring_get():
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        out = []
        for item in items:
            item_id = item["item_id"]
            access_token = item["access_token"]

            req = TransactionsRecurringGetRequest(access_token=access_token)
            res = client.transactions_recurring_get(req).to_dict()
            capture_plaid_raw("transactions/recurring/get", item_id, res)

            upsert_recurring_raw(item_id, res)
            out.append({"item_id": item_id, "ok": True})

        return jsonify({"ok": True, "items": out})
    except Exception as e:
        log_app(f"Error in /recurring: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/recurring/raw", methods=["GET"])
def recurring_raw_get():
    try:
        item_id = request.args.get("item_id")  # optional
        rows = get_recurring_raw(item_id=item_id)
        return jsonify({"count": len(rows), "rows": rows})
    except Exception as e:
        log_app(f"Error in /recurring/raw: {e}", "error")
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


#@app.route("/classify", methods=["POST"])
#def classify():
#    payload = request.json or {}
#    days = int(payload.get("days", 365))
#    item_id = payload.get("item_id")
#    res = apply_classification_rules(days=days, item_id=item_id)
#    return jsonify({"ok": True, **res})

@app.route("/spend/top_merchants", methods=["GET"])
def top_merchants():
    days = int(request.args.get("days", "30"))
    limit = int(request.args.get("limit", "50"))
    return jsonify({"ok": True, "days": days, "rows": get_top_merchants(days=days, limit=limit)})


@app.route("/liabilities/credit_summary", methods=["GET"])
def credit_summary():
    item_id = request.args.get("item_id")
    rows = get_credit_card_statement_summary(item_id=item_id)
    return jsonify({"ok": True, "count": len(rows), "rows": rows})

@app.route("/classify", methods=["POST"])
def classify_transactions():
    """
    Classifies transactions and upserts transaction_classifications.
    Body (optional):
      { "days": 365, "item_id": "..." }
    """
    try:
        payload = request.json or {}
        days = int(payload.get("days", 365))
        item_id = payload.get("item_id")

        rows = get_transactions_for_classification(days=days, item_id=item_id)

        updated = 0
        excluded = 0

        for r in rows:
            tid = r.get("transaction_id")
            if not tid:
                continue

            ex, reason, merch_norm = classify_one(r)
            upsert_transaction_classification(
                transaction_id=tid,
                exclude_from_spend=ex,
                exclude_reason=reason,
                merchant_normalized=merch_norm,
            )
            updated += 1
            excluded += int(ex)

        return jsonify({
            "ok": True,
            "days": days,
            "item_id": item_id,
            "classified": updated,
            "excluded_from_spend": excluded,
        })

    except Exception as e:
        log_app(f"Error in /classify: {e}", "error")
        return jsonify({"error": str(e)}), 400

@app.route("/spend", methods=["GET"])
def spend_get():
    """
    Returns spend transactions (amount > 0).
    Query params:
      days=30
      item_id=...
      account_id=...
      include_excluded=0|1
      limit=500
      offset=0
    """
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        days = int(request.args.get("days", "30"))
        item_id = request.args.get("item_id")
        account_id = request.args.get("account_id")
        include_excluded = request.args.get("include_excluded", "0") == "1"
        limit = int(request.args.get("limit", "500"))
        offset = int(request.args.get("offset", "0"))

        rows = get_spend_canonical(
            days=days,
            item_id=item_id,
            account_id=account_id,
            include_excluded=include_excluded,
            limit=limit,
            offset=offset,
        )
        return jsonify({"count": len(rows), "spend": rows})

    except Exception as e:
        log_app(f"Error in /spend: {e}", "error")
        return jsonify({"error": str(e)}), 400

@app.route("/transactions/<transaction_id>/classify", methods=["POST"])
def classify_transaction(transaction_id: str):
    """
    Manual classification override for a single transaction.
    Optionally saves a merchant rule for future auto-classification.

    Body JSON example:
    {
      "exclude_from_spend": 0,
      "exclude_reason": "user: subscription",
      "user_category": "Subscriptions",
      "user_subcategory": "AI",
      "merchant_normalized": "openai chatgpt",
      "save_rule": true,
      "rule_scope": "account",          // 'account' or 'global'
      "match_field": "either",          // 'merchant_name' | 'name' | 'either'
      "match_op": "contains",           // 'equals' | 'contains'
      "priority": 50
    }
    """
    try:
        payload = request.json or {}

        tx = get_transaction_basic(transaction_id)
        if not tx:
            return jsonify({"error": f"Unknown transaction_id: {transaction_id}"}), 404

        exclude_from_spend = int(bool(payload.get("exclude_from_spend", 0)))
        exclude_reason = payload.get("exclude_reason")
        user_category = payload.get("user_category")
        user_subcategory = payload.get("user_subcategory")

        # If merchant_normalized not provided, derive from merchant_name/name
        merchant_norm = payload.get("merchant_normalized")
        if not merchant_norm:
            merchant_norm = normalize_merchant(tx.get("merchant_name") or tx.get("name"))

        # 1) Apply manual override
        upsert_transaction_classification(
            transaction_id=transaction_id,
            exclude_from_spend=exclude_from_spend,
            exclude_reason=exclude_reason,
            user_category=user_category,
            user_subcategory=user_subcategory,
            merchant_normalized=merchant_norm,
        )

        rule_id = None

        # 2) Optionally save as rule
        if bool(payload.get("save_rule", False)):
            rule_scope = (payload.get("rule_scope") or "account").lower()  # account/global
            match_field = (payload.get("match_field") or "either").lower()
            match_op = (payload.get("match_op") or "contains").lower()
            priority = int(payload.get("priority", 100))

            scoped_account_id = tx["account_id"] if rule_scope == "account" else None

            # The rule "match_value" is normalized; for contains match,
            # you can pass shorter tokens too. We'll default to merchant_norm.
            match_value = normalize_merchant(payload.get("match_value") or merchant_norm)

            rule_id = insert_classification_rule(
                match_field=match_field,
                match_op=match_op,
                match_value=match_value,
                account_id=scoped_account_id,
                exclude_from_spend=exclude_from_spend,
                exclude_reason=exclude_reason,
                user_category=user_category,
                user_subcategory=user_subcategory,
                merchant_normalized=merchant_norm,
                priority=priority,
            )

        return jsonify({
            "ok": True,
            "transaction_id": transaction_id,
            "applied_override": {
                "exclude_from_spend": exclude_from_spend,
                "exclude_reason": exclude_reason,
                "user_category": user_category,
                "user_subcategory": user_subcategory,
                "merchant_normalized": merchant_norm,
            },
            "saved_rule_id": rule_id,
            "tx_context": {
                "account_id": tx["account_id"],
                "merchant_name": tx.get("merchant_name"),
                "name": tx.get("name"),
            }
        })

    except Exception as e:
        log_app(f"Error in classify_transaction: {e}", "error")
        return jsonify({"error": str(e)}), 400

@app.route("/rules/apply", methods=["POST"])
def rules_apply():
    """
    Apply saved rules to past transactions (does NOT overwrite manual overrides).
    Body JSON:
      { "days": 365, "item_id": null }
    """
    try:
        payload = request.json or {}
        days = int(payload.get("days", 365))
        item_id = payload.get("item_id")

        res = apply_rules_bulk(days=days, item_id=item_id)
        return jsonify({"ok": True, **res})

    except Exception as e:
        log_app(f"Error in /rules/apply: {e}", "error")
        return jsonify({"error": str(e)}), 400


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
