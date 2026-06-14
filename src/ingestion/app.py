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
from functools import wraps

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
    upsert_transaction_classifications_batch,
    get_connection,
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
PLAID_DISABLED = os.getenv("PLAID_DISABLED", "0") == "1"
client = None

def require_plaid(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if PLAID_DISABLED:
            return jsonify({"error": "Plaid is disabled (PLAID_DISABLED=1)"}), 503
        return fn(*args, **kwargs)
    return wrapper

if not PLAID_DISABLED:
    configuration = plaid.Configuration(
        host=plaid_host,
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    api_client = plaid.ApiClient(configuration)
    client = plaid_api.PlaidApi(api_client)
else:
    print("PLAID_DISABLED=1 -> Plaid client not initialized")

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

_PFC_CATEGORY_MAP = {
    "FOOD_AND_DRINK":            "Food & Drink",
    "GENERAL_MERCHANDISE":       "Shopping",
    "ENTERTAINMENT":             "Entertainment",
    "TRAVEL":                    "Travel",
    "INCOME":                    "Income",
    "LOAN_PAYMENTS":             "Loan Payments",
    "TRANSFER_IN":               "Transfer In",
    "TRANSFER_OUT":              "Transfer Out",
    "RENT_AND_UTILITIES":        "Utilities",
    "MEDICAL":                   "Medical",
    "GENERAL_SERVICES":          "Services",
    "TRANSPORTATION":            "Transportation",
    "PERSONAL_CARE":             "Personal Care",
    "HOME_IMPROVEMENT":          "Home Improvement",
    "GOVERNMENT_AND_NON_PROFIT": "Government",
    "BANK_FEES":                 "Bank Fees",
}

_INVESTMENT_NAMES = (
    "schwab", "fidelity", "vanguard", "merrill", "edward jones",
    "e*trade", "etrade", "robinhood", "acorns", "wealthfront", "betterment",
)

def classify_one(txn_row: dict) -> tuple[int, str | None, str | None, str | None]:
    """
    Returns: (exclude_from_spend, exclude_reason, merchant_normalized, user_category)

    Checking-account cashflow policy:
      - Zelle IN  (amount < 0)  → Income, not excluded
      - Zelle OUT (amount > 0)  → Expense, not excluded
      - Credit card payment     → excluded
      - Transfer to investment broker (Schwab etc.) → "Investment", excluded
      - Transfer OUT (non-investment) → "Savings", excluded
      - Transfer IN from savings  → "Transfer In", excluded (not income)
    """
    name = (txn_row.get("name") or "").lower()
    merchant = (txn_row.get("merchant_name") or "").lower()
    amount = float(txn_row.get("amount") or 0)
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

    user_category = _PFC_CATEGORY_MAP.get(pfc_primary)

    # 1) Zelle — person-to-person, treat as income or expense (never a transfer)
    if "zelle" in text:
        if amount < 0:   # money arriving in checking
            return 0, None, merch_norm, "Income"
        else:            # money leaving checking to a person
            return 0, None, merch_norm, "Expense"

    # 2) Credit card payment → exclude
    if "CREDIT_CARD_PAYMENT" in pfc_detailed:
        return 1, "pfc_credit_card_payment", merch_norm, user_category

    # 3) Investment transfers (Schwab, Fidelity, etc.) → "Investment", excluded
    if any(nm in text for nm in _INVESTMENT_NAMES):
        return 1, "investment_transfer", merch_norm, "Investment"

    has_transfer_hints = any(h in text for h in _TRANSFER_HINTS)
    merchant_present   = bool(merchant) or bool(txn_row.get("merchant_name"))
    is_out_category    = pfc_primary == "TRANSFER_OUT" or "TRANSFER_OUT" in pfc_detailed
    is_in_category     = pfc_primary == "TRANSFER_IN"  or "TRANSFER_IN"  in pfc_detailed
    is_transfer_cat    = is_out_category or is_in_category or "ACCOUNT_TRANSFER" in pfc_detailed

    # 4) Transfer OUT of checking → savings (exclude from spend, tag Savings)
    if is_out_category and amount > 0 and (has_transfer_hints or not merchant_present):
        return 1, "savings_transfer_out", merch_norm, "Savings"

    # 5) Transfer IN from savings → exclude but NOT income
    if is_in_category and amount < 0 and (has_transfer_hints or not merchant_present):
        return 1, "transfer_in_excluded", merch_norm, "Transfer In"

    # 6) Remaining generic transfer (unknown direction)
    if is_transfer_cat and (has_transfer_hints or not merchant_present):
        return 1, "pfc_transfer_with_transfer_text", merch_norm, user_category

    # 7) Heuristic: text strongly resembles a payment/transfer
    if has_transfer_hints and (("card" in text) or ("bank" in text) or ("payment" in text) or ("pmt" in text)):
        return 1, "text_looks_like_payment_or_transfer", merch_norm, user_category

    return 0, None, merch_norm, user_category


# ------------------------------------------------------------
#  Plaid API Routes
# ------------------------------------------------------------
@app.route("/link_token/create", methods=["POST"])
@require_plaid
def link_token_create():
    try:
        webhook = os.getenv("PLAID_WEBHOOK_URL")
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            additional_consented_products=[
                Products("liabilities"),
                Products("investments"),
            ],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
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
@require_plaid
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

        # Only request liabilities consent for items that actually hold a credit
        # account. Requesting a product the institution doesn't support (e.g.
        # 'investments' on the Amazon store card, or 'liabilities' on a
        # Schwab investment item) fails link_token_create with INVALID_FIELD
        # before the user can log in. Granting liabilities consent here is what
        # lets /liabilities later fetch statement balance + due dates (otherwise
        # it returns ADDITIONAL_CONSENT_REQUIRED).
        try:
            accts = get_accounts_canonical(item_id=item_id)
        except Exception:
            accts = []
        has_credit = any((a.get("type") or "").lower() == "credit" for a in accts)

        webhook = os.getenv("PLAID_WEBHOOK_URL")
        kwargs = dict(
            products=[Products("transactions")],
            client_name="Personal Finance App",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id=os.getenv("APP_USER_ID", "local_user")),
            access_token=item["access_token"],  # UPDATE MODE
            webhook=webhook,
        )
        if has_credit:
            kwargs["additional_consented_products"] = [Products("liabilities")]
        req = LinkTokenCreateRequest(**kwargs)
        res = client.link_token_create(req)
        return jsonify(res.to_dict())
    except Exception as e:
        log_app(f"Error in link_token_update: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/item/public_token/exchange", methods=["POST"])
@require_plaid
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
@require_plaid
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
@require_plaid
def liabilities_get():
    try:
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items yet"}), 400

        out = []
        for item in items:
            item_id = item["item_id"]
            access_token = item["access_token"]

            # Per-item: liabilities is only supported on some products (credit /
            # student loans / mortgage). Depository and investment items raise
            # PRODUCTS_NOT_SUPPORTED, and a stale item raises ITEM_LOGIN_REQUIRED.
            # Isolate failures so one bad item doesn't abort the whole refresh.
            try:
                req = LiabilitiesGetRequest(access_token=access_token)
                res = client.liabilities_get(req).to_dict()
                capture_plaid_raw("liabilities/get", item_id, res)
                upsert_liabilities_raw(item_id, res)
                out.append({"item_id": item_id, "ok": True})
            except Exception as item_err:
                msg = str(item_err)
                code = "ERROR"
                m = re.search(r'"error_code":\s*"([^"]+)"', msg)
                if m:
                    code = m.group(1)
                log_app(f"liabilities skip item_id={item_id}: {code}", "info")
                out.append({"item_id": item_id, "ok": False, "error_code": code})

        ok_n = sum(1 for o in out if o.get("ok"))
        return jsonify({"ok": True, "updated": ok_n, "items": out})
    except Exception as e:
        log_app(f"Error in /liabilities: {e}", "error")
        return jsonify({"error": str(e)}), 400


@app.route("/recurring", methods=["GET"])
@require_plaid
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

def _run_classify(days: int = 500, item_id: str | None = None) -> dict:
    """
    Shared classify logic: reads transactions+meta, writes to transaction_classifications.
    Batches all writes into a single transaction to avoid repeated write-lock acquisition.
    Called by both the /classify route and sync_all.
    """
    rows = get_transactions_for_classification(days=days, item_id=item_id)
    batch = []
    excluded = 0
    for r in rows:
        tid = r.get("transaction_id")
        if not tid:
            continue
        ex, reason, merch_norm, user_category = classify_one(r)
        batch.append({
            "transaction_id": tid,
            "exclude_from_spend": ex,
            "exclude_reason": reason,
            "merchant_normalized": merch_norm,
            "user_category": user_category,
            "user_subcategory": None,
        })
        excluded += int(ex)
    written = upsert_transaction_classifications_batch(batch)
    return {"classified": written, "excluded_from_spend": excluded}


@app.route("/classify", methods=["POST"])
def classify_transactions():
    """
    Classifies transactions and upserts transaction_classifications.
    Body (optional):
      { "days": 500, "item_id": "..." }
    """
    try:
        payload = request.json or {}
        days = int(payload.get("days", 500))
        item_id = payload.get("item_id")
        result = _run_classify(days=days, item_id=item_id)
        return jsonify({"ok": True, "days": days, "item_id": item_id, **result})
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
@require_plaid
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
        classify_out = _run_classify(days=500)

        elapsed_ms = int((time.time() - started) * 1000)

        canonical_accounts_count = len(get_accounts_canonical())
        canonical_txn_count_30d = count_transactions_canonical(days=30)

        log_app(f"/sync_all END elapsed_ms={elapsed_ms} classified={classify_out['classified']}")

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
            "classify": classify_out,
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


@app.route("/resync_full", methods=["POST"])
@require_plaid
def resync_full():
    """
    Reset transaction sync cursors so Plaid re-sends every transaction
    from scratch, repopulating transaction_meta for all historical records.

    Body (optional):
      { "item_id": "..." }   — only reset that item; omit to reset all items

    WARNING: This re-downloads all transactions. Safe because save_transactions()
    uses ON CONFLICT DO UPDATE, so no duplicates are created.
    """
    _step = "init"
    try:
        payload = request.json or {}
        target_item_id = payload.get("item_id")

        _step = "get_items"
        items = get_all_items()
        if not items:
            return jsonify({"error": "No linked items"}), 400

        if target_item_id:
            items = [i for i in items if i["item_id"] == target_item_id]
            if not items:
                return jsonify({"error": f"item_id not found: {target_item_id}"}), 404

        # Clear cursors — next sync call to Plaid will start from the beginning
        _step = "clear_cursors"
        with get_connection() as _conn:
            if target_item_id:
                _conn.execute(
                    "DELETE FROM transaction_cursors WHERE item_id = ?",
                    (target_item_id,),
                )
            else:
                _conn.execute("DELETE FROM transaction_cursors")
            _conn.commit()
        # log AFTER commit so log_event_db doesn't contend for the write lock
        if target_item_id:
            log_app(f"/resync_full: cleared cursor for item_id={target_item_id}")
        else:
            log_app(f"/resync_full: cleared all {len(items)} cursors")

        started = time.time()
        _step = "sync_transactions"
        txn_out = _sync_transactions_for_items(items, count=500)
        _step = "classify"
        classify_out = _run_classify(days=500)
        elapsed_ms = int((time.time() - started) * 1000)

        log_app(f"/resync_full END elapsed_ms={elapsed_ms} classified={classify_out['classified']}")

        return jsonify({
            "ok": True,
            "items_reset": len(items),
            "elapsed_ms": elapsed_ms,
            "transactions_sync": {
                "total_added": sum(r.get("added", 0) for r in txn_out["items"]),
                "total_modified": sum(r.get("modified", 0) for r in txn_out["items"]),
                "errors": txn_out["errors"],
            },
            "classify": classify_out,
        })

    except ApiException as e:
        log_app(f"Plaid ApiException in /resync_full [{_step}]: {e}", "error")
        return jsonify({"error": str(e), "step": _step}), 400
    except Exception as e:
        log_app(f"Error in /resync_full [{_step}]: {e}", "error")
        return jsonify({"error": str(e), "step": _step}), 400


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
    <title>Connect + Sync</title>
    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
    <style>
      *, *::before, *::after { box-sizing: border-box; }
      body { font-family: system-ui, sans-serif; padding: 2rem; background: #f8f9fa; max-width: 860px; }
      h1 { margin: 0 0 1.25rem; color: #1a1a2e; }
      button {
        padding: .5rem .95rem; font-size: .92rem; margin-right: .4rem;
        background: #0078d7; color: #fff; border: none; border-radius: 6px; cursor: pointer;
      }
      button:hover { background: #005fa3; }
      button:disabled { background: #aaa; cursor: default; }
      button.btn-danger { background: #c0392b; }
      button.btn-danger:hover { background: #992d22; }
      button.btn-secondary { background: #6c757d; }
      button.btn-secondary:hover { background: #545b62; }
      select { padding: .4rem .5rem; border: 1px solid #ccc; border-radius: 6px; font-size: .92rem; }

      .btn-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; align-items: center; }
      .bank-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.25rem; align-items: center; }

      /* Spinning busy indicator */
      #busy-bar {
        display: none; padding: .5rem .85rem; border-radius: 6px;
        background: #e8f0fe; color: #1a56b0; font-size: .9rem;
        border: 1px solid #b3cdf5; margin-bottom: .75rem;
      }

      /* Result panel */
      #result-panel { margin-bottom: 1rem; }
      .result-card {
        border-radius: 8px; padding: .75rem 1rem; margin-bottom: .5rem;
        font-size: .9rem; line-height: 1.5;
      }
      .card-ok      { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
      .card-warn    { background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; }
      .card-error   { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
      .card-info    { background: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }
      .card-title   { font-weight: 700; margin-bottom: .2rem; }
      .card-detail  { font-size: .82rem; opacity: .85; }
      .card-action  { margin-top: .4rem; font-weight: 600; font-size: .85rem; }

      a { color: #0078d7; text-decoration: none; font-size: .95rem; }
      a:hover { text-decoration: underline; }
      .nav-link { display: inline-block; margin-top: 1rem; }
    </style>
  </head>
  <body>
    <h1>Connect + Sync</h1>

    <div class="btn-row">
      <button id="refresh-btn">Refresh data</button>
      <button id="add-bank-btn">Add bank</button>
      <button id="resync-full-btn" class="btn-secondary"
              title="Resets sync cursors and re-downloads all transactions">Full Re-sync</button>
    </div>

    <div class="bank-row">
      <label for="item-select"><b>Bank:</b></label>
      <select id="item-select" style="min-width: 360px;"></select>
      <button id="reconnect-btn">Reconnect</button>
      <button id="remove-btn" class="btn-danger">Remove</button>
    </div>

    <div id="busy-bar"></div>
    <div id="result-panel"></div>

    <a class="nav-link" href="/reports/login">→ Reports Login</a>

<script>
const busyBar    = document.getElementById('busy-bar');
const resultPanel = document.getElementById('result-panel');
const refreshBtn  = document.getElementById('refresh-btn');
const addBtn      = document.getElementById('add-bank-btn');
const reconnectBtn = document.getElementById('reconnect-btn');
const removeBtn   = document.getElementById('remove-btn');
const resyncFullBtn = document.getElementById('resync-full-btn');
const itemSelect  = document.getElementById('item-select');

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setBusy(busy, label) {
  [refreshBtn, addBtn, reconnectBtn, removeBtn, resyncFullBtn].forEach(b => b.disabled = busy);
  if (busy) {
    busyBar.style.display = 'block';
    busyBar.textContent = label || 'Working...';
  } else {
    busyBar.style.display = 'none';
  }
}

function clearResult() { resultPanel.innerHTML = ''; }

function addCard(type, title, detail, action) {
  const d = document.createElement('div');
  d.className = 'result-card card-' + type;
  d.innerHTML = `<div class="card-title">${escHtml(title)}</div>` +
    (detail ? `<div class="card-detail">${escHtml(detail)}</div>` : '') +
    (action ? `<div class="card-action">${action}</div>` : '');
  resultPanel.appendChild(d);
}

function showSyncResult(j, label) {
  clearResult();
  if (!j || j.error) {
    addCard('error', (label || 'Sync') + ' failed', j ? j.error : 'Unknown error');
    return;
  }
  const txn  = j.transactions_sync || {};
  const added = txn.total_added || 0;
  const mod   = txn.total_modified || 0;
  const rem   = txn.total_removed || 0;
  const cls   = (j.classify || {}).classified || 0;
  const accts = (j.accounts_sync || {}).total_accounts_saved || 0;
  const ms    = j.elapsed_ms ? ` (${(j.elapsed_ms/1000).toFixed(1)}s)` : '';

  if (added === 0 && mod === 0 && rem === 0) {
    addCard('ok', (label || 'Refresh') + ' complete — no new transactions' + ms,
      `${accts} account(s) updated  •  ${cls} classified`);
  } else {
    const parts = [];
    if (added) parts.push(`${added} new`);
    if (mod)   parts.push(`${mod} updated`);
    if (rem)   parts.push(`${rem} removed`);
    addCard('ok', (label || 'Refresh') + ' complete — ' + parts.join(', ') + ms,
      `${accts} account(s) synced  •  ${cls} classified`);
  }

  // Per-item errors
  const itemErrors = (txn.errors || []).concat((j.accounts_sync || {}).errors || []);
  itemErrors.forEach(err => showItemError(err));
}

function showItemError(err) {
  const code    = err.error_code || err.code || '';
  const msg     = err.error_message || err.display_message || err.message || String(err);
  const inst    = err.institution_name || err.item_id || 'A bank';

  if (code === 'ITEM_LOGIN_REQUIRED') {
    addCard('warn', inst + ' — credentials expired',
      'Plaid can no longer access this account.',
      'Select it in the dropdown and click <b>Reconnect</b> to re-authenticate.');
  } else if (code === 'INSTITUTION_REGISTRATION_REQUIRED') {
    addCard('warn', inst + ' — registration required',
      msg,
      'Visit <b>dashboard.plaid.com → Activity → Status → OAuth Institutions</b> to register.');
  } else if (code && code.startsWith('INSTITUTION_')) {
    addCard('warn', inst + ' — institution issue (' + code + ')',
      msg, 'This is usually temporary. Try again later or check Plaid status.');
  } else if (code === 'INVALID_ACCESS_TOKEN' || code === 'ITEM_NOT_FOUND') {
    addCard('error', inst + ' — invalid connection',
      msg, 'Remove and re-add this bank to restore access.');
  } else {
    addCard('error', inst + ' — sync error' + (code ? ' (' + code + ')' : ''), msg);
  }
}

function showLinkError(err, metadata) {
  if (!err) return;
  const code = err.error_code || '';
  const msg  = err.display_message || err.error_message || '';
  const inst = (metadata && metadata.institution) ? metadata.institution.name : 'Bank';

  if (code === 'ITEM_LOGIN_REQUIRED') {
    addCard('warn', inst + ' — session expired', msg,
      'Select it below and click <b>Reconnect</b>.');
  } else if (code === 'INSTITUTION_REGISTRATION_REQUIRED') {
    addCard('warn', inst + ' — not registered with Plaid', msg,
      'Register at <b>dashboard.plaid.com → Activity → Status → OAuth Institutions</b>.');
  } else if (code === 'INSTITUTION_NOT_AVAILABLE') {
    addCard('warn', inst + ' — temporarily unavailable', msg || 'Try again later.');
  } else {
    addCard('error', inst + ' — Link error (' + code + ')', msg || 'The bank connection could not be completed.');
  }
}

resyncFullBtn.addEventListener('click', async () => {
  if (!confirm('This resets all sync cursors and re-downloads every transaction from Plaid. Continue?')) return;
  clearResult();
  setBusy(true, 'Full re-sync in progress — this may take a minute...');
  try {
    const r = await fetch('/resync_full', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'resync_full failed');
    showSyncResult(j, 'Full re-sync');
  } catch (e) {
    clearResult();
    addCard('error', 'Full re-sync failed', e.message);
  } finally {
    setBusy(false);
  }
});

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
  clearResult();
  setBusy(true, 'Refreshing — syncing accounts and transactions...');
  try {
    const res = await syncAllWithTimeout();
    showSyncResult(res, 'Refresh');
    await loadItems();
  } catch (e) {
    clearResult();
    addCard('error', 'Refresh failed', e.message || String(e));
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
        clearResult();
        setBusy(true, 'Link success — exchanging token...');
        try {
          const first = await exchangePublicToken(public_token, metadata, false);

          if (!first.ok && first.status === 409 && first.body && first.body.action === "confirm_duplicate") {
            const msg = first.body.message || "This institution is already linked. Proceed anyway?";
            if (!confirm(msg)) {
              addCard('info', 'Duplicate link cancelled', 'No changes were made.');
              return;
            }
            setBusy(true, 'Exchanging token (duplicate allowed)...');
            const second = await exchangePublicToken(public_token, metadata, true);
            if (!second.ok) throw new Error("Failed to exchange token after duplicate confirmation");
          } else {
            if (!first.ok) throw new Error("Failed to exchange public token");
          }

          setBusy(true, 'Syncing — this may take a moment on first link...');
          const synced = await syncAllWithTimeout();
          showSyncResult(synced, 'Bank added');
          await loadItems();

        } catch (e) {
          clearResult();
          addCard('error', 'Add bank failed', e.message || String(e));
        } finally {
          setBusy(false);
        }
      },

      onExit: (err, metadata) => {
        if (err) { clearResult(); showLinkError(err, metadata); }
        setBusy(false);
      }
    });

    handler.open();

  } catch (e) {
    clearResult();
    addCard('error', 'Add bank failed', e.message || String(e));
    setBusy(false);
  }
};

reconnectBtn.onclick = async () => {
  const item_id = itemSelect.value;
  if (!item_id) { addCard('warn', 'No bank selected', 'Choose a bank from the dropdown first.'); return; }

  clearResult();
  setBusy(true, 'Opening Plaid reconnect...');
  try {
    const linkToken = await createUpdateLinkToken(item_id);

    const handler = Plaid.create({
      token: linkToken,

      onSuccess: async () => {
        clearResult();
        setBusy(true, 'Reconnect success — syncing...');
        try {
          const synced = await syncAllWithTimeout();
          showSyncResult(synced, 'Reconnect');
          await loadItems();
        } catch (e) {
          clearResult();
          addCard('error', 'Reconnect sync failed', e.message || String(e));
        } finally {
          setBusy(false);
        }
      },

      onExit: (err, metadata) => {
        if (err) { clearResult(); showLinkError(err, metadata); }
        setBusy(false);
      }
    });

    handler.open();

  } catch (e) {
    clearResult();
    addCard('error', 'Reconnect failed', e.message || String(e));
    setBusy(false);
  }
};

removeBtn.onclick = async () => {
  const item_id = itemSelect.value;
  if (!item_id) { addCard('warn', 'No bank selected', 'Choose a bank from the dropdown first.'); return; }

  if (!confirm('Remove this bank? This revokes Plaid access and deletes local data.')) return;

  clearResult();
  setBusy(true, 'Removing bank...');
  try {
    await removeItem(item_id);
    addCard('ok', 'Bank removed', 'Plaid access revoked and local data deleted.');
    await loadItems();
  } catch (e) {
    clearResult();
    addCard('error', 'Remove failed', e.message || String(e));
  } finally {
    setBusy(false);
  }
};

// Initial load
loadItems().catch(e => addCard('error', 'Could not load banks', e.message || String(e)));
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
