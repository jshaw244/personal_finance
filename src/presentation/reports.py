"""
Flask blueprint: /reports
Secure, session-based dashboard with Flask-Login + bcrypt authentication.

Includes:
  A. Session timeout + redirect
  B. Login/logout access logging
  C. “Stay signed in” checkbox support
  D. Role placeholder for future expansion
  E. Extended analytics endpoints + category normalization, spending type, cached summaries
  F. Cashflow summary, recurring merchants, top transactions
  M4-M5. Budget engine, forecasts, daily allowance, paycheck/tax, insights, password generator
"""

from flask import (
    Blueprint, render_template, send_from_directory, abort,
    jsonify, request, redirect, url_for, flash, current_app,
    Response, stream_with_context
)
import requests

from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)

from flask import session
#session.permanent = True
from pathlib import Path
from datetime import datetime, timedelta, date
import pandas as pd
import sqlite3, os, logging, bcrypt, re, json, math, random, string
import subprocess, sys
from src.common.paths import DB_FILE


# -------------------------------------------------------------------
# Blueprint / paths / logging setup
# -------------------------------------------------------------------
reports_bp = Blueprint("reports", __name__, template_folder="templates")

login_manager = LoginManager()
login_manager.login_view = "reports.login"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
DB_PATH = DB_FILE
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)
MAINT_LOG = LOG_DIR / "maintenance.log"
CACHE_PATH = RESULTS_DIR / "sandbox_dashboard_cache.json"

maint_logger = logging.getLogger("reports_access")
if not maint_logger.handlers:
    h = logging.FileHandler(MAINT_LOG)
    h.setFormatter(logging.Formatter("%(asctime)s [ACCESS] %(message)s"))
    maint_logger.addHandler(h)
    maint_logger.setLevel(logging.INFO)

def log_access(msg: str):
    maint_logger.info(msg)
    print(f"[ACCESS] {msg}")

def write_cache(data):
    """
    Write small JSON payloads to CACHE_PATH for quick reuse.
    In production this is optional; failures are silently ignored.
    """
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception:
        # Never let cache writes break the API
        pass

# -------------------------------------------------------------------
# Environment credentials
# -------------------------------------------------------------------
ENV_TARGET = os.getenv("ENV_TARGET", "sandbox").lower()
REPORTS_USER = os.getenv(f"REPORTS_USER_{ENV_TARGET.upper()}", os.getenv("REPORTS_USER"))
REPORTS_PASS_HASH = os.getenv(f"REPORTS_PASS_HASH_{ENV_TARGET.upper()}", os.getenv("REPORTS_PASS_HASH"))
SESSION_MINUTES = int(os.getenv("REPORTS_SESSION_MINUTES", "30"))

# -------------------------------------------------------------------
# User class with role support
# -------------------------------------------------------------------
class ReportsUser(UserMixin):
    def __init__(self, id, role="admin"):
        self.id = id
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    if user_id == REPORTS_USER:
        return ReportsUser(user_id, role="admin")
    return None

# -------------------------------------------------------------------
# Session helpers / ping
# -------------------------------------------------------------------
@reports_bp.before_request
def refresh_session_timer():
    if current_user.is_authenticated:
        from flask import session
        session.modified = True

@reports_bp.route("/api/ping")
@login_required
def api_ping():
    return jsonify({"status": "ok"})

# -------------------------------------------------------------------
# Login / logout
# -------------------------------------------------------------------
@reports_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        remember = bool(request.form.get("remember"))
        if username == REPORTS_USER:
            try:
                if REPORTS_PASS_HASH and bcrypt.checkpw(password.encode(), REPORTS_PASS_HASH.encode()):
                    user = ReportsUser(username)
                    login_user(user, remember=remember, duration=timedelta(minutes=SESSION_MINUTES))
                    log_access(f"User {username} logged in from {request.remote_addr} (remember={remember})")
                    return redirect(url_for("reports.index"))
            except Exception as e:
                print("bcrypt error:", e)
        flash("Invalid credentials. Please try again.", "error")
        log_access(f"Failed login attempt from {request.remote_addr} (user={username})")
        return render_template("login.html"), 401
    return render_template("login.html")

@reports_bp.route("/logout")
@login_required
def logout():
    user = current_user.id if current_user.is_authenticated else "unknown"
    logout_user()
    log_access(f"User {user} logged out from {request.remote_addr}")
    flash("You have been logged out.", "info")
    return redirect(url_for("reports.login"))

# -------------------------------------------------------------------
# Data helpers
# -------------------------------------------------------------------
def _latest_result_file():
    files = sorted(
        [p for p in RESULTS_DIR.glob("*.xlsx")] + [p for p in RESULTS_DIR.glob("*.csv")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None

def _load_tx_detail_from_excel(xlsx: Path) -> pd.DataFrame:
    try:
        xl = pd.ExcelFile(xlsx)
        if "Transactions_Detail" in xl.sheet_names:
            return xl.parse("Transactions_Detail")
    except Exception:
        pass
    return pd.DataFrame()

def _load_tx_detail_from_db() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        return pd.read_sql_query(
            """
            SELECT t.*,
                   tc.user_category,
                   tc.user_subcategory,
                   COALESCE(tc.exclude_from_spend, 0) AS exclude_from_spend,
                   tc.exclude_reason,
                   tc.merchant_normalized,
                   a.subtype AS account_subtype,
                   a.type    AS account_type
            FROM transactions t
            LEFT JOIN transaction_classifications tc
              ON tc.transaction_id = t.transaction_id
            LEFT JOIN accounts a
              ON a.account_id = t.account_id
            """,
            conn,
        )
    finally:
        conn.close()

# -------------------------------------------------------------------
# Category derivation + normalization
# -------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "Groceries": ["walmart", "kroger", "aldi", "costco", "whole foods", "target", "jewel", "caputo"],
    "Dining": ["restaurant", "grill", "bar", "cafe", "pizza", "coffee", "burger", "7-11"],
    "Gas": ["shell", "bp", "exxon", "mobil", "marathon", "trip"],
    "Utilities": ["comcast", "att", "verizon", "village", "village", "nicor", "comed", "duke energy", "electric", "gas bill"],
    "Entertainment": ["netflix", "spotify", "amc", "theater", "cinema", "disney"],
    "Travel": ["airlines", "hotel", "uber", "lyft", "delta", "southwest"],
    "Shopping": ["amazon", "best buy", "ebay", "target"],
    "Income": ["payroll", "deposit", "refund", "credit"],
    "Pet": ["pet", "dog", "cat", "groom"],
    "Other": []
}

_FIXED_PATTERNS = [
    r"\bauto\s*pay\b", r"\bpymt\b", r"\bpayment\b",
    r"\bmortgage\b", r"\brent\b", r"\binsurance\b",
    r"\bcomcast\b", r"\bxfinity\b", r"\batt\b", r"\bverizon\b",
    r"\bnicor\b", r"\bcomed\b", r"\belectric\b", r"\bgas\s+bill\b",
    r"\bnetflix\b", r"\bspotify\b", r"\byoutube\b", r"\bgoogle\s+one\b",
    r"\broth\b", r"\bira\b", r"\bedward\s+jones\b", r"\bschwab\b",
    r"\bsavings\b", r"\btax\s+savings\b",
    r"\bchase\b.*\bcc\b", r"\bamazon\b.*\bcc\b", r"\bbest\s*buy\b.*\bcc\b",
]

def _derive_category(name: str, merchant: str) -> str:
    text = f"{name or ''} {merchant or ''}".lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return cat
    return "Other"

def _load_tx_detail() -> pd.DataFrame:
    df = _load_tx_detail_from_db()

    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # Resolve category: prefer PFC-based classification, fall back to keyword derivation
    df["category"] = df.apply(
        lambda r: r.get("user_category") or _derive_category(
            r.get("name", ""),
            r.get("merchant_name", "")
        ),
        axis=1,
    )

    # Income flag: use PFC-derived category (set by classify_one) or Plaid amount sign on depository
    df["is_income"] = df["category"].str.lower().eq("income")

    # Ensure exclude_from_spend is numeric
    df["exclude_from_spend"] = pd.to_numeric(df.get("exclude_from_spend", 0), errors="coerce").fillna(0).astype(int)

    if "amount" in df.columns:
        cat_lower = df["category"].str.lower().fillna("")
        df["spending_type"] = "expense"
        df.loc[df["is_income"], "spending_type"] = "income"
        df.loc[cat_lower == "savings",    "spending_type"] = "savings"
        df.loc[cat_lower == "investment", "spending_type"] = "investment"
        # anything else that is excluded but not savings/investment is a generic transfer
        df.loc[
            (df["exclude_from_spend"] == 1) &
            ~df["spending_type"].isin(["income", "savings", "investment"]),
            "spending_type"
        ] = "transfer"
    else:
        df["spending_type"] = "unknown"
    return df

def _load_tx_detail_db_only() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        return pd.read_sql_query("SELECT * FROM transactions;", conn)
    finally:
        conn.close()


def _filter_by_range(df: pd.DataFrame, range_key: str,
                     start: "str | None" = None,
                     end:   "str | None" = None) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    today = datetime.today().date()

    # Custom date range takes priority
    if start:
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end) if end else today
            return df[(df["date"].dt.date >= s) & (df["date"].dt.date <= e)]
        except ValueError:
            pass

    if range_key == "30d":
        s = today - timedelta(days=30)
    elif range_key == "ytd":
        s = date(today.year, 1, 1)
    elif range_key == "12m":
        s = today.replace(day=1) - timedelta(days=365)
    else:
        return df
    return df[df["date"].dt.date >= s]


def _filter_by_account(df: pd.DataFrame, account_id: "str | None" = None) -> pd.DataFrame:
    """Restrict to one account when account_id is provided (else unchanged).
    account_id defaults to the request's ?account_id= when not passed explicitly."""
    if account_id is None:
        account_id = (request.args.get("account_id") or "").strip()
    if account_id and not df.empty and "account_id" in df.columns:
        return df[df["account_id"] == account_id]
    return df

# -------------------------------------------------------------------
# E3: SQLite views + summary table enforcement (with full logging)
# -------------------------------------------------------------------
def ensure_summary_views_and_tables():
    """Guarantee that all summary tables and views exist at startup."""
    from src.storage.db import log_event_db  # safe import; avoids circulars

    def log(msg, level="info"):
        """Write message to both console and maintenance log."""
        print(msg)
        try:
            # File logger
            with open(LOG_DIR / "maintenance.log", "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] [SUMMARY_INIT] {msg}\n")
            # DB logger
            log_event_db("summary_init", level.upper(), msg)
        except Exception:
            pass

    if not DB_PATH.exists():
        log(f"[WARN] Database not found at {DB_PATH}", "warning")
        return

    # 1. Create/refresh built-in SQLite views
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        cur = conn.cursor()
        sql_view = """
CREATE VIEW IF NOT EXISTS v_monthly_summary AS
SELECT 
    strftime('%Y-%m', date) AS month,
    category,
    SUM(amount) AS total_amount,
    COUNT(*) AS tx_count
FROM transactions
GROUP BY 1, 2;
"""
        cur.execute(sql_view)
        conn.commit()
        conn.close()
        log("[OK] v_monthly_summary view ensured.")
    except Exception as e:
        log(f"[ERROR] Failed to create monthly summary view: {e}", "error")
        return

    # 2. Run the external summary table generator
    script = PROJECT_ROOT / "scripts" / "generate_summary_tables.py"
    if script.exists():
        try:
            subprocess.run([sys.executable, str(script)], check=True)
            log(f"[OK] Summary tables refreshed via {script.name}")
        except subprocess.CalledProcessError as e:
            log(f"[ERROR] generate_summary_tables.py exited with {e.returncode}: {e}", "error")
        except Exception as e:
            log(f"[ERROR] generate_summary_tables.py failed: {e}", "error")
    else:
        log(f"[WARN] {script} not found; skipping summary refresh.", "warning")


# -------------------------------------------------------------------
# Protected routes (page)
# -------------------------------------------------------------------
@reports_bp.route("/")
@login_required
def index():
    if not RESULTS_DIR.exists():
        RESULTS_DIR.mkdir(exist_ok=True, parents=True)
    files = sorted(RESULTS_DIR.glob("*.*"), key=lambda f: f.stat().st_mtime, reverse=True)
    file_info = [{
        "name": f.name,
        "suffix": f.suffix,
        "size": round(f.stat().st_size / 1024, 1),
        "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    } for f in files]
    images = [f for f in RESULTS_DIR.glob("*.png")]
    return render_template("reports_index.html", files=file_info, images=images)

@reports_bp.route("/file/<path:filename>")
@login_required
def serve_file(filename):
    path = RESULTS_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(RESULTS_DIR, filename, as_attachment=False)

@reports_bp.route("/api/list")
@login_required
def list_reports_json():
    if not RESULTS_DIR.exists():
        return {"error": "results folder not found"}, 404
    data = []
    for f in RESULTS_DIR.glob("*.*"):
        st = f.stat()
        data.append({
            "name": f.name,
            "suffix": f.suffix,
            "size_kb": round(st.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    data.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(data)

# -------------------------------------------------------------------
# Analytics Endpoints (existing)
# -------------------------------------------------------------------
@reports_bp.route("/api/spending_by_category")
@login_required
def api_spending_by_category():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "category" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404

    df = df.copy()

    # Expenses only: exclude income and transfers/CC payments
    spend_df = df.loc[~df["is_income"] & (df["exclude_from_spend"] == 0)].copy()
    spend_df["spend"] = spend_df["amount"].abs()

    by_cat = (
        spend_df
        .groupby("category", dropna=False)["spend"]
        .sum()
        .sort_values(ascending=False)
        .round(2)
    )

    return jsonify(by_cat.to_dict())

@reports_bp.route("/api/monthly_trend")
@login_required
def api_monthly_trend():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "date" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # Expenses only: exclude income and transfers/CC payments
    spend_df = df.loc[~df["is_income"] & (df["exclude_from_spend"] == 0)].copy()
    spend_df["spend"] = spend_df["amount"].abs()
    spend_df["month"] = spend_df["date"].dt.to_period("M").astype(str)

    trend = (
        spend_df
        .groupby("month")["spend"]
        .sum()
        .reset_index()
        .sort_values("month")
        .round(2)
    )
    trend.columns = ["month", "total"]

    return jsonify(trend.to_dict(orient="records"))

@reports_bp.route("/api/top_merchants")
@login_required
def api_top_merchants():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404
    merchant_col = "merchant_name" if "merchant_name" in df.columns else ("name" if "name" in df.columns else None)
    if not merchant_col:
        return jsonify({"error": "missing merchant column"}), 400
    spend_df = df.loc[~df["is_income"] & (df["exclude_from_spend"] == 0)].copy()
    agg = spend_df.groupby(merchant_col).agg(
        amount=("amount", "sum"),
        count=("amount", "count"),
        category=("category", lambda x: x.dropna().mode().iloc[0] if not x.dropna().mode().empty else ""),
    ).sort_values("amount", ascending=False).head(20).round({"amount": 2}).reset_index()
    agg.columns = ["merchant", "amount", "count", "category"]
    return jsonify(agg.to_dict(orient="records"))


@reports_bp.route("/api/paycycle_53")
@login_required
def api_paycycle_53():
    today = date.today()

    start_s = (request.args.get("start") or "").strip()
    if start_s:
        try:
            start = datetime.strptime(start_s, "%Y-%m-%d").date()
        except Exception:
            start = _paycycle_start(today)
    else:
        start = _paycycle_start(today)

    days = int(request.args.get("days", "14"))
    days = max(7, min(31, days))
    end = start + timedelta(days=days - 1)

    acct_id = _resolve_53_checking_account_id()
    if not acct_id:
        return jsonify({"error": "FIFTH_THIRD_CHECKING_ACCOUNT_ID not set"}), 400

    df = _load_tx_detail_db_only()
    if df.empty:
        return jsonify({
            "start": start.isoformat(), "end": end.isoformat(),
            "account_id": acct_id,
            "income": [], "fixed": [], "variable": [],
            "totals": {"income": 0.0, "fixed": 0.0, "variable": 0.0, "net": 0.0}
        })

    df.columns = [c.lower() for c in df.columns]
    if "date" not in df.columns or "amount" not in df.columns or "account_id" not in df.columns:
        return jsonify({"error": "transactions table missing date/amount/account_id"}), 400

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df[(df["date"].dt.date >= start) & (df["date"].dt.date <= end)]
    df = df[df["account_id"] == acct_id]

    merch = df.get("merchant_name", pd.Series([""] * len(df))).fillna("").astype(str)
    name = df.get("name", pd.Series([""] * len(df))).fillna("").astype(str)
    text = (merch + " " + name).str.lower()

    # Income: keywords OR Plaid credit (amount < 0)
    is_income = text.str.contains(r"payroll|deposit|refund|reversal", regex=True, na=False) | (df["amount"].astype(float) < 0)
    df["is_income"] = is_income

    fixed_mask = pd.Series([False] * len(df), index=df.index)
    for pat in _FIXED_PATTERNS:
        fixed_mask = fixed_mask | text.str.contains(pat, regex=True, na=False)

    def row_out(r):
        amt = float(r.get("amount") or 0.0)
        label = (r.get("merchant_name") or "").strip() or (r.get("name") or "").strip()
        return {
            "date": pd.to_datetime(r["date"]).strftime("%Y-%m-%d"),
            "transaction": label,
            "amount": round(abs(amt), 2),
            "transaction_id": r.get("transaction_id"),
        }

    income_df = df[df["is_income"]].sort_values("date")
    exp_df = df[~df["is_income"]].copy()
    fixed_df = exp_df[fixed_mask.reindex(exp_df.index, fill_value=False)].sort_values("date")
    var_df = exp_df[~fixed_mask.reindex(exp_df.index, fill_value=False)].sort_values("date")

    income_total = float(income_df["amount"].abs().sum()) if len(income_df) else 0.0
    fixed_total  = float(fixed_df["amount"].abs().sum()) if len(fixed_df) else 0.0
    var_total    = float(var_df["amount"].abs().sum()) if len(var_df) else 0.0

    return jsonify({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "account_id": acct_id,
        "income": [row_out(r) for _, r in income_df.iterrows()],
        "fixed": [row_out(r) for _, r in fixed_df.iterrows()],
        "variable": [row_out(r) for _, r in var_df.iterrows()],
        "totals": {
            "income": round(income_total, 2),
            "fixed": round(fixed_total, 2),
            "variable": round(var_total, 2),
            "net": round(income_total - (fixed_total + var_total), 2),
        }
    })


# -------------------- KPI / Cashflow / Recurring / Top Tx --------------------
@reports_bp.route("/api/kpi_summary")
@login_required
def api_kpi_summary():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "amount" not in df.columns or "date" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    # Parse dates safely and drop rows with invalid dates
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].notna()]

    df = df[df["amount"].notna()]

    # Treat spending as positive; exclude income and transfers/CC payments
    spend_df = df.loc[~df["is_income"] & (df["exclude_from_spend"] == 0)].copy()
    spend_df["spend"] = spend_df["amount"].abs()

    total_spent = spend_df["spend"].sum()
    avg_txn = spend_df["spend"].mean()

    spend_df["month"] = spend_df["date"].dt.to_period("M").astype(str)
    monthly = spend_df.groupby("month")["spend"].sum().sort_index()

    mom_pct = 0.0
    if len(monthly) >= 2:
        last, prev = monthly.iloc[-1], monthly.iloc[-2]
        mom_pct = ((last - prev) / prev * 100) if prev else 0.0

    spend_df["year"] = spend_df["date"].dt.year
    current_year = spend_df["year"].max()
    ytd_sum = spend_df.loc[spend_df["year"] == current_year, "spend"].sum()
    prior_sum = spend_df.loc[spend_df["year"] == current_year - 1, "spend"].sum()
    ytd_pct = ((ytd_sum - prior_sum) / prior_sum * 100) if prior_sum else 0.0

    result = {
        "total_spent": round(total_spent, 2),
        "avg_txn": round(avg_txn, 2),
        "mom_pct": round(mom_pct, 1),
        "ytd_pct": round(ytd_pct, 1),
        "records": len(spend_df)
    }
    write_cache(result)
    return jsonify(result)


@reports_bp.route("/api/income_expense_split")
@login_required
def api_income_expense_split():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "amount" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    df = df.copy()

    income = df.loc[df["is_income"], "amount"].abs().sum()
    expenses = df.loc[~df["is_income"] & (df["exclude_from_spend"] == 0), "amount"].abs().sum()

    net = income - expenses  # can ignore this on the frontend if you don't care about net right now

    result = {
        "income": round(float(income), 2),
        "expenses": round(float(expenses), 2),
        "net_balance": round(float(net), 2),
    }
    write_cache(result)
    return jsonify(result)

@reports_bp.route("/api/category_trends")
@login_required
def api_category_trends():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "category" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    df["month"] = df["date"].dt.to_period("M").astype(str)
    grouped = df.groupby(["month", "category"])["amount"].sum().reset_index()
    pivot = grouped.pivot(index="month", columns="category", values="amount").fillna(0)
    return jsonify(pivot.round(2).to_dict(orient="index"))


@reports_bp.route("/api/spending_trend")
@login_required
def api_spending_trend():
    """Top-N category spending by month — used for the trend line chart."""
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", "ytd"),
                          request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty:
        return jsonify({"categories": [], "months": [], "series": {}})

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    spend = df[~df["is_income"] & (df["exclude_from_spend"] == 0)].dropna(subset=["date"]).copy()
    spend["amount"] = spend["amount"].abs()
    spend["month"]    = spend["date"].dt.to_period("M").astype(str)
    spend["category"] = spend["category"].fillna("Uncategorized")

    n_top = int(request.args.get("n", 6))
    top_cats = (spend.groupby("category")["amount"].sum()
                .sort_values(ascending=False).head(n_top).index.tolist())

    spend = spend[spend["category"].isin(top_cats)]
    pivot = spend.groupby(["month", "category"])["amount"].sum().unstack(fill_value=0)
    months = sorted(pivot.index.tolist())

    return jsonify({
        "categories": top_cats,
        "months":     months,
        "series":     {cat: [round(float(pivot.loc[m, cat]) if m in pivot.index and cat in pivot.columns else 0, 2)
                             for m in months]
                       for cat in top_cats},
    })


@reports_bp.route("/api/account_history")
@login_required
def api_account_history():
    """Reconstruct daily account balances from current balance + transaction history."""
    range_key = request.args.get("range", "ytd")
    start_str = request.args.get("start")
    end_str   = request.args.get("end")
    today     = date.today()

    if start_str and end_str:
        try:
            range_start = date.fromisoformat(start_str)
            range_end   = date.fromisoformat(end_str)
        except ValueError:
            range_start, range_end = date(today.year, 1, 1), today
    elif range_key == "30d":
        range_start, range_end = today - timedelta(days=30), today
    elif range_key == "12m":
        range_start = today.replace(day=1) - timedelta(days=365)
        range_end   = today
    else:
        range_start, range_end = date(today.year, 1, 1), today

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    result = []
    try:
        accts = conn.execute("""
            SELECT a.account_id, a.name, a.type, a.subtype, a.current, i.institution_name
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            WHERE a.type IN ('depository', 'credit') AND a.current IS NOT NULL
            ORDER BY a.type DESC, a.subtype, a.name
        """).fetchall()

        for acct in accts:
            rows = conn.execute("""
                SELECT date, SUM(amount) AS day_amt
                FROM transactions
                WHERE account_id = ? AND IFNULL(pending,0)=0
                GROUP BY date ORDER BY date ASC
            """, (acct["account_id"],)).fetchall()

            if not rows:
                continue

            daily = {r["date"]: float(r["day_amt"]) for r in rows}
            all_dates_sorted = sorted(daily.keys())
            total_all = sum(daily.values())

            # cumulative sum at each date
            cum = 0.0
            cumsum = {}
            for d_str in all_dates_sorted:
                cum += daily[d_str]
                cumsum[d_str] = cum

            current_bal = float(acct["current"])

            # cumsum just before range_start
            pre_cum = 0.0
            for d_str in all_dates_sorted:
                if date.fromisoformat(d_str) < range_start:
                    pre_cum = cumsum[d_str]
                else:
                    break

            running = pre_cum
            dates_out, bals_out = [], []
            d = range_start
            while d <= range_end:
                d_str = d.isoformat()
                if d_str in daily:
                    running += daily[d_str]
                # balance_on_d = current_balance - transactions that happened AFTER d
                bal = current_bal - (total_all - running)
                dates_out.append(d_str)
                bals_out.append(round(bal, 2))
                d += timedelta(days=1)

            result.append({
                "name":        acct["name"],
                "institution": acct["institution_name"],
                "type":        acct["type"],
                "subtype":     acct["subtype"],
                "current":     current_bal,
                "dates":       dates_out,
                "balances":    bals_out,
            })
    finally:
        conn.close()

    return jsonify(result)


@reports_bp.route("/api/cashflow_summary")
@login_required
def api_cashflow_summary():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty or "amount" not in df.columns or "date" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["month"] = df["date"].dt.to_period("M").astype(str)

    # Default (All) tracks cash movement through checking only. When a specific
    # account is selected (_filter_by_account already applied it), show that account.
    if not (request.args.get("account_id") or "").strip() and "account_subtype" in df.columns:
        df = df[df["account_subtype"].str.lower().fillna("").eq("checking")]

    if "exclude_from_spend" not in df.columns:
        df["exclude_from_spend"] = 0

    # Assign cashflow bucket using exclude_reason + user_category + amount.
    # This works for both newly-classified (new rules) and old classifications
    # without requiring a full re-classify of the database.
    _inv_names_cf = (
        "schwab", "fidelity", "vanguard", "merrill", "edward jones",
        "e*trade", "etrade", "robinhood", "acorns", "wealthfront", "betterment",
    )

    def _cashflow_bucket(row) -> str:
        if row.get("is_income"):
            return "income"
        amt    = float(row.get("amount") or 0)
        cat    = str(row.get("category")       or "").lower()
        reason = str(row.get("exclude_reason") or "").lower()
        name   = str(row.get("name")           or "").lower()

        # Zelle: override any old classification — incoming = income, outgoing = expense
        if "zelle" in name:
            return "income" if amt < 0 else "expense"

        # Investment — new rule, explicit category, or transfer to investment broker
        if cat == "investment" or reason == "investment_transfer":
            return "investment"
        if any(n in name for n in _inv_names_cf):
            return "investment"

        # Transfer IN from savings → skip (not income, not an outflow)
        if reason == "transfer_in_excluded":
            return "skip"
        if reason == "pfc_transfer_with_transfer_text" and amt < 0:
            return "skip"

        # Savings — new rule or old outbound account transfer (not ATM, not Zelle)
        if cat == "savings" or reason == "savings_transfer_out":
            return "savings"
        if reason == "pfc_transfer_with_transfer_text" and amt > 0 and "atm" not in name:
            return "savings"

        # Everything else positive → expense (CC payments, bills, etc.)
        if amt > 0:
            return "expense"

        return "skip"

    df = df.copy()
    df["cf_bucket"] = df.apply(_cashflow_bucket, axis=1)

    def _monthly_abs(mask):
        sub = df.loc[mask].copy()
        return sub.groupby("month")["amount"].apply(lambda s: s.abs().sum())

    inflow     = _monthly_abs(df["cf_bucket"] == "income").rename("inflow")
    expense    = _monthly_abs(df["cf_bucket"] == "expense").rename("expense")
    savings    = _monthly_abs(df["cf_bucket"] == "savings").rename("savings")
    investment = _monthly_abs(df["cf_bucket"] == "investment").rename("investment")

    cash = pd.concat([inflow, expense, savings, investment], axis=1).fillna(0.0).sort_index()
    cash["outflow"] = cash["expense"] + cash["savings"] + cash["investment"]
    cash["net"]     = cash["inflow"] - cash["outflow"]
    cash["running_balance"] = cash["net"].cumsum()

    out = []
    for idx, row in cash.round(2).iterrows():
        out.append({
            "month":           idx,
            "inflow":          float(row["inflow"]),
            "outflow":         float(row["outflow"]),
            "expense":         float(row["expense"]),
            "savings":         float(row["savings"]),
            "investment":      float(row["investment"]),
            "net":             float(row["net"]),
            "running_balance": float(row["running_balance"]),
        })
    return jsonify(out)

@reports_bp.route("/api/recurring_merchants")
@login_required
def api_recurring_merchants():
    """
    Identify recurring merchants based on transaction frequency and month coverage.
    Handles empty datasets gracefully and adds safe defaults for parameters.
    """
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)

    # ✅ Defensive guard for missing or empty data
    if df.empty or "amount" not in df.columns:
        return jsonify([])

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # Merchant column detection
    merchant_col = "merchant_name" if "merchant_name" in df.columns else (
        "name" if "name" in df.columns else None
    )
    if not merchant_col:
        return jsonify([])

    # Clean + filter merchants
    df[merchant_col] = df[merchant_col].fillna("").str.strip()
    df = df[df[merchant_col] != ""]
    if df.empty:
        return jsonify([])

    #logging
    current_app.logger.info(
        "Recurring merchant scan: %d txns across %d merchants",
        len(df),
        df[merchant_col].nunique()
    )
    # Parameterized thresholds
    try:
        min_occ = int(request.args.get("min_occurrences", "3"))
        min_months = int(request.args.get("min_months", "3"))
        top_n = int(request.args.get("top", "20"))
    except ValueError:
        min_occ, min_months, top_n = 3, 3, 20

    # Derive month key
    df["month"] = df["date"].dt.to_period("M").astype(str)

    # Aggregate merchant stats
    grp = df.groupby(merchant_col)
    stats = grp.agg(
        total_spend=("amount", "sum"),
        txns=("amount", "count"),
        months=("month", pd.Series.nunique),
        first_seen=("date", "min"),
        last_seen=("date", "max"),
    ).reset_index()

    # 🔧 Ensure a consistent column name for merge
    merchant_col_final = "merchant"
    if merchant_col in stats.columns:
        stats = stats.rename(columns={merchant_col: merchant_col_final})
    elif "index" in stats.columns:
        stats = stats.rename(columns={"index": merchant_col_final})
    else:
        stats.insert(0, merchant_col_final, grp.groups.keys())

    # Filter for recurring merchants
    cand = stats.loc[(stats["txns"] >= min_occ) & (stats["months"] >= min_months)].copy()
    if cand.empty:
        return jsonify([])

    # Optional cadence calculation (avg days between txns)
    def cadence_days(g):
        d = g.sort_values("date")["date"].diff().dropna().dt.days
        return float(d.mean()) if len(d) else None

    cad = grp.apply(cadence_days).rename("avg_cadence_days").reset_index()

    # 🔧 Ensure cad has a consistent 'merchant' column
    if "merchant" not in cad.columns:
        if "index" in cad.columns:
            cad = cad.rename(columns={"index": "merchant"})
        elif "merchant_name" in cad.columns:
            cad = cad.rename(columns={"merchant_name": "merchant"})
        elif "name" in cad.columns:
            cad = cad.rename(columns={"name": "merchant"})
        else:
            cad.insert(0, "merchant", list(grp.groups.keys()))

    cand = cand.merge(cad, how="left", on="merchant")


    # Round + limit output
    cand["total_spend"] = cand["total_spend"].round(2)
    cand = cand.sort_values(["months", "txns", "total_spend"], ascending=[False, False, False])
    cand = cand.head(top_n)

    return jsonify(cand.to_dict(orient="records"))


@reports_bp.route("/api/top_transactions")
@login_required
def api_top_transactions():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""),
                         request.args.get("start"), request.args.get("end"))
    df = _filter_by_account(df)
    if df.empty:
        return jsonify({"error": "no transaction data"}), 404
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    merchant_col = "merchant_name" if "merchant_name" in df.columns else ("name" if "name" in df.columns else "name")
    if "amount" not in df.columns:
        return jsonify({"error": "missing required columns"}), 400
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    direction = request.args.get("direction", "out").lower()
    if direction == "out":
        df = df[~df["is_income"]]
    elif direction == "in":
        df = df[df["is_income"]]
    limit = int(request.args.get("limit", "25"))
    cols = [c for c in ["date", merchant_col, "name", "amount", "category", "transaction_id", "account_id"] if c in df.columns]
    top = df.sort_values("amount", ascending=False).head(limit)[cols].copy()
    top["date"] = top["date"].dt.strftime("%Y-%m-%d")
    top = top.rename(columns={merchant_col: "merchant"})
    return jsonify(top.to_dict(orient="records"))

@reports_bp.route("/api/healthcheck")
def api_healthcheck():
    latest = _latest_result_file()
    return jsonify({
        "status": "ok",
        "env": ENV_TARGET,
        "latest_file": latest.name if latest else None,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })



# -------------------------------------------------------------------
# Calendar / 2-week Spend Endpoints
# -------------------------------------------------------------------

def _paycycle_start(today: date) -> date:
    """
    Compute the most recent payday <= today using an anchor + cycle length.
    Falls back to "most recent Friday" if no anchor is set.
    """
    anchor_s = (os.getenv("PAYDAY_ANCHOR_DATE") or "").strip()
    cycle_days = int(os.getenv("PAYDAY_CYCLE_DAYS", "14"))
    payday_weekday = int(os.getenv("PAYDAY_WEEKDAY", "4"))  # Friday

    if anchor_s:
        try:
            anchor = datetime.strptime(anchor_s, "%Y-%m-%d").date()
            if today < anchor:
                # walk back by whole cycles until <= today
                while today < anchor:
                    anchor = anchor - timedelta(days=cycle_days)
                return anchor
            delta = (today - anchor).days
            return anchor + timedelta(days=(delta // cycle_days) * cycle_days)
        except Exception:
            pass

    # fallback: last payday_weekday (default Friday)
    offset = (today.weekday() - payday_weekday) % 7
    return today - timedelta(days=offset)


def _spend_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Spend rows = outflows only.
    Uses your existing "is_income" heuristic and requires amount > 0.
    """
    if df.empty:
        return df

    df = df.copy()
    if "date" not in df.columns or "amount" not in df.columns:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # your existing income-like heuristic (keep consistent with KPIs)
    merch = df.get("merchant_name", pd.Series([""] * len(df))).fillna("").astype(str)
    name = df.get("name", pd.Series([""] * len(df))).fillna("").astype(str)
    text = (merch + " " + name)

    df["is_income"] = text.str.contains(
        r"payroll|deposit|refund|credit|reversal",
        case=False,
        na=False
    )

    # IMPORTANT: spend = outflow transactions, amount > 0
    df = df[df["amount"].notna()]
    df = df[(~df["is_income"]) & (df["amount"] > 0)]

    # normalized spend column
    df["spend"] = df["amount"].astype(float)
    return df


@reports_bp.route("/api/calendar_events")
@login_required
def api_calendar_events():
    """
    Return scheduled events (payday, transfers, bills) for a given month.
    Query: ?month=YYYY-MM  (defaults to current month)
    Returns: [{date, type, name, auto_pay?}, ...]
    """
    import calendar as _cal
    from src.common.paths import PROJECT_ROOT

    month_str = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        year, mon = map(int, month_str.split("-"))
    except ValueError:
        return jsonify({"error": "invalid month format"}), 400

    _, days_in_month = _cal.monthrange(year, mon)
    month_start = date(year, mon, 1)
    month_end   = date(year, mon, days_in_month)

    sched_path = PROJECT_ROOT / "config" / "schedules.json"
    with open(sched_path, encoding="utf-8") as f:
        sched = json.load(f)

    events = []

    # ── Payday + transfers ────────────────────────────────────────────────────
    anchor  = date.fromisoformat(sched["payday"]["anchor_date"])
    cadence = sched["payday"]["cadence_days"]

    # Walk anchor to the first payday we need to consider.
    # We look back up to 6 days before month_start so a late-month payday
    # whose transfer Monday falls inside this month is included.
    scan_start = month_start - timedelta(days=6)
    d = anchor
    # Walk to the last payday on or before month_end
    while d > month_end:
        d -= timedelta(days=cadence)
    # Back up to the earliest payday still within the scan window
    while d - timedelta(days=cadence) >= scan_start:
        d -= timedelta(days=cadence)

    # Payday-linked transfers (no "day" field) vs fixed-date transfers (have "day" field)
    payday_transfers = [t for t in sched["transfers"] if "day" not in t]
    fixed_transfers  = [t for t in sched["transfers"] if "day" in t]

    while d <= month_end:
        if month_start <= d <= month_end:
            events.append({"date": d.isoformat(), "type": "payday", "name": "Payday"})

        # Monday after payday for payday-linked transfers
        days_to_mon = (7 - d.weekday()) % 7 or 7
        transfer_dt = d + timedelta(days=days_to_mon)
        if month_start <= transfer_dt <= month_end:
            for t in payday_transfers:
                events.append({"date": transfer_dt.isoformat(), "type": "transfer", "name": t["name"]})

        d += timedelta(days=cadence)

    # ── Fixed-date transfers (e.g. House Holdings on the 27th) ─────────────────
    for t in fixed_transfers:
        actual_day = min(t["day"], days_in_month)
        events.append({"date": date(year, mon, actual_day).isoformat(), "type": "transfer", "name": t["name"]})

    # ── Monthly income (e.g. House Holdings income on the 15th) ───────────────
    for inc in sched.get("income", []):
        actual_day = min(inc["day"], days_in_month)
        events.append({"date": date(year, mon, actual_day).isoformat(), "type": "income", "name": inc["name"]})

    # ── Bills ─────────────────────────────────────────────────────────────────
    for bill in sched["bills"]:
        # Skip if this bill has a multi-month cadence and this isn't a due month
        if "cadence_months" in bill:
            anchor_bill = date.fromisoformat(bill["anchor_date"])
            months_offset = (year - anchor_bill.year) * 12 + (mon - anchor_bill.month)
            if months_offset < 0 or months_offset % bill["cadence_months"] != 0:
                continue
        actual_day = min(bill["day"], days_in_month)
        events.append({
            "date":     date(year, mon, actual_day).isoformat(),
            "type":     "bill",
            "name":     bill["name"],
            "auto_pay": bill.get("auto_pay", False),
        })

    events.sort(key=lambda e: e["date"])

    # ── Enrich amounts ────────────────────────────────────────────────────────
    from src.storage.db import get_credit_card_statement_summary

    # 1. Credit card statement balances (Plaid liabilities)
    cc_by_name: dict[str, float] = {}
    try:
        for s in get_credit_card_statement_summary():
            acct = (s.get("account_name") or "").lower()
            bal  = s.get("last_statement_balance")
            if acct and bal is not None:
                cc_by_name[acct] = float(bal)
    except Exception:
        pass

    # 2. Recent income transactions (payday amounts)
    # 3. tx_match lookups — this month's actual + most recent for estimates
    income_list: list[tuple[str, float]] = []
    recent_income_list: list[tuple[str, float]] = []
    tx_match_this_month: dict[str, float] = {}              # first (scheduled) payment this month
    cc_extra_payments: dict[str, list[tuple[str, float]]] = {}  # additional CC payments this month
    tx_match_latest: dict[str, float] = {}                  # most recent ever (estimate fallback)
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.row_factory = sqlite3.Row
        try:
            # Income for the viewed month — used for actual payday detection (any month with data)
            rows = conn.execute("""
                SELECT t.date, ABS(t.amount) AS amount
                FROM transactions t
                JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
                WHERE tc.user_category = 'Income' AND IFNULL(t.pending, 0) = 0
                  AND ABS(t.amount) > 0
                  AND t.date >= ? AND t.date <= ?
                ORDER BY t.date DESC
            """, (month_start.isoformat(), month_end.isoformat())).fetchall()
            income_list = [(r["date"], float(r["amount"])) for r in rows]

            # Most recent income globally — estimate baseline for future months with no data yet
            recent_rows = conn.execute("""
                SELECT t.date, ABS(t.amount) AS amount
                FROM transactions t
                JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
                WHERE tc.user_category = 'Income' AND IFNULL(t.pending, 0) = 0
                  AND ABS(t.amount) > 0
                ORDER BY t.date DESC LIMIT 10
            """).fetchall()
            recent_income_list = [(r["date"], float(r["amount"])) for r in recent_rows]

            for item in sched["bills"] + sched.get("income", []):
                kw = (item.get("tx_match") or "").lower().strip()
                if not kw:
                    continue
                is_cc = bool(item.get("plaid_match"))
                acct_subtype = (item.get("tx_account_subtype") or "").strip()
                acct_filter_sql = (
                    "AND account_id IN (SELECT account_id FROM accounts WHERE subtype = ?) "
                    if acct_subtype else ""
                )
                acct_params = (acct_subtype,) if acct_subtype else ()
                if is_cc:
                    # Credit cards: fetch ALL transactions in month (ASC = scheduled first)
                    cc_rows = conn.execute(f"""
                        SELECT date, ABS(amount) AS amount FROM transactions
                        WHERE (LOWER(name) LIKE ? OR LOWER(merchant_name) LIKE ?)
                          AND date >= ? AND date <= ?
                          AND IFNULL(pending, 0) = 0
                          {acct_filter_sql}
                        ORDER BY date ASC
                    """, (f"%{kw}%", f"%{kw}%", month_start.isoformat(), month_end.isoformat()) + acct_params).fetchall()
                    if cc_rows:
                        tx_match_this_month[item["name"]] = float(cc_rows[0]["amount"])
                        if len(cc_rows) > 1:
                            cc_extra_payments[item["name"]] = [
                                (r["date"], float(r["amount"])) for r in cc_rows[1:]
                            ]
                else:
                    # Non-CC: earliest match only
                    row = conn.execute(f"""
                        SELECT ABS(amount) AS amount FROM transactions
                        WHERE (LOWER(name) LIKE ? OR LOWER(merchant_name) LIKE ?)
                          AND date >= ? AND date <= ?
                          AND IFNULL(pending, 0) = 0
                          {acct_filter_sql}
                        ORDER BY date ASC LIMIT 1
                    """, (f"%{kw}%", f"%{kw}%", month_start.isoformat(), month_end.isoformat()) + acct_params).fetchone()
                    if row:
                        tx_match_this_month[item["name"]] = float(row["amount"])
                # self_payment: check for payments (negative amounts) on the CC account itself
                if item.get("self_payment") and item.get("plaid_match") and item["name"] not in tx_match_this_month:
                    pm_lower = item["plaid_match"].lower()
                    sp_rows = conn.execute("""
                        SELECT t.date, ABS(t.amount) AS amount
                        FROM transactions t
                        JOIN accounts a ON a.account_id = t.account_id
                        WHERE LOWER(a.name) LIKE ? AND t.amount < 0
                          AND t.date >= ? AND t.date <= ?
                          AND IFNULL(t.pending, 0) = 0
                        ORDER BY t.date ASC
                    """, (f"%{pm_lower}%", month_start.isoformat(), month_end.isoformat())).fetchall()
                    if sp_rows:
                        tx_match_this_month[item["name"]] = float(sp_rows[0]["amount"])
                        if len(sp_rows) > 1:
                            existing = cc_extra_payments.get(item["name"], [])
                            cc_extra_payments[item["name"]] = existing + [
                                (r["date"], float(r["amount"])) for r in sp_rows[1:]
                            ]
                # Latest ever: fallback estimate for months with no actual yet
                row = conn.execute(f"""
                    SELECT ABS(amount) AS amount FROM transactions
                    WHERE (LOWER(name) LIKE ? OR LOWER(merchant_name) LIKE ?)
                      AND IFNULL(pending, 0) = 0
                      {acct_filter_sql}
                    ORDER BY date DESC LIMIT 1
                """, (f"%{kw}%", f"%{kw}%") + acct_params).fetchone()
                if row:
                    tx_match_latest[item["name"]] = float(row["amount"])
        finally:
            conn.close()
    except Exception:
        pass

    # Payday estimate: use viewed month's income if available, else fall back to recent history
    estimate_source = income_list if income_list else recent_income_list
    payday_estimate = estimate_source[1][1] if len(estimate_source) >= 2 else (
                      estimate_source[0][1] if estimate_source else None)

    # Build a combined config lookup for bills + transfers + income (for amount_override support)
    bill_cfg_map = {b["name"]: b for b in sched["bills"]}
    bill_cfg_map.update({t["name"]: t for t in sched["transfers"]})
    bill_cfg_map.update({i["name"]: i for i in sched.get("income", [])})

    # Month-specific overrides take priority over global amount_override
    month_str = f"{year}-{mon:02d}"
    monthly_overrides: dict = sched.get("monthly_overrides", {}).get(month_str, {})

    # Payday pass 1: expand actual payday events — one badge per deposit
    expanded = []
    latest_actual_total = None
    for ev in events:
        if ev["type"] != "payday":
            expanded.append(ev)
            continue
        ev_date = date.fromisoformat(ev["date"])
        deposits = [
            (d, amt) for d, amt in income_list
            if amt > 0 and abs((date.fromisoformat(d) - ev_date).days) <= 2
        ]
        if deposits:
            latest_actual_total = sum(amt for _, amt in deposits)
            for dep_date, dep_amt in deposits:
                expanded.append({**ev, "date": dep_date, "amount": dep_amt, "amount_type": "actual"})
        else:
            expanded.append(ev)
    events = sorted(expanded, key=lambda e: e["date"])

    # Payday pass 2: estimate future paydays using most recent actual total
    estimate_total = latest_actual_total if latest_actual_total is not None else payday_estimate
    for ev in events:
        if ev["type"] == "payday" and "amount" not in ev and estimate_total is not None:
            ev["amount"] = estimate_total
            ev["amount_type"] = "estimate"

    for ev in events:
        cfg = bill_cfg_map.get(ev["name"], {})

        if ev["type"] == "bill":
            # Priority 1: actual transaction from this month
            if ev["name"] in tx_match_this_month:
                ev["amount"] = tx_match_this_month[ev["name"]]
                ev["amount_type"] = "actual"
            else:
                # Priority 2: Plaid statement balance
                pm = (cfg.get("plaid_match") or "").lower().strip()
                if pm:
                    for acct_name, bal in cc_by_name.items():
                        if pm in acct_name:
                            ev["amount"] = bal
                            ev["amount_type"] = "statement"
                            break
                # Priority 3a: month-specific manual override
                if "amount" not in ev and ev["name"] in monthly_overrides:
                    ev["amount"] = float(monthly_overrides[ev["name"]])
                    ev["amount_type"] = "static"
                # Priority 3b: global static override (plan/budget default)
                if "amount" not in ev and "amount_override" in cfg and cfg["amount_override"] is not None:
                    ev["amount"] = float(cfg["amount_override"])
                    ev["amount_type"] = "static"
                # Priority 4: most recent transaction ever (estimate)
                if "amount" not in ev and ev["name"] in tx_match_latest:
                    ev["amount"] = tx_match_latest[ev["name"]]
                    ev["amount_type"] = "estimate"
        else:
            # transfers and income: month-specific override → global override
            if ev["name"] in monthly_overrides:
                ev["amount"] = float(monthly_overrides[ev["name"]])
                ev["amount_type"] = "static"
            elif "amount_override" in cfg and cfg["amount_override"] is not None:
                ev["amount"] = float(cfg["amount_override"])
                ev["amount_type"] = "static"

        # Stamp occurred flag
        if ev["type"] == "payday":
            ev["occurred"] = ev.get("amount_type") == "actual"
        elif ev["type"] in ("bill", "income"):
            ev["occurred"] = ev["name"] in tx_match_this_month
        else:
            ev["occurred"] = False

    # Append extra CC payment events on their actual dates
    bill_auto_map = {b["name"]: b.get("auto_pay", False) for b in sched["bills"]}
    for bill_name, extras in cc_extra_payments.items():
        for pay_date, pay_amt in extras:
            events.append({
                "date":       pay_date,
                "type":       "bill",
                "name":       bill_name,
                "auto_pay":   bill_auto_map.get(bill_name, False),
                "amount":     pay_amt,
                "amount_type": "actual",
                "occurred":   True,
            })

    # Misc checking transactions: all actual money movement not covered by scheduled events
    def _clean_misc_name(raw):
        n = raw.upper()
        if "ATM WITHDRAWAL" in n:
            return "ATM"
        if "SENT ZELLE PMT" in n and " TO " in n:
            return "Zelle \u2192 " + raw.split(" TO ")[-1].strip().title()
        if "RECEIVED ZELLE PMT" in n and " FROM " in n:
            return "Zelle \u2190 " + raw.split(" FROM ")[-1].strip().title()
        if "5/3 ONLINE TRANSFER FROM" in n:
            return "53 Savings \u2190"
        if "SCHWAB BROKERAGE MONEYLINK" in n:
            return "Schwab \u2190"
        if "WEB INITIATED PAYMENT AT " in n:
            after = n.split("WEB INITIATED PAYMENT AT ")[1]
            _stop = {"CK", "SV", "WEBXFR", "PURCHASE", "TRANSFER", "PAYMENT",
                     "BILLPAY", "VILLAGE", "GAS", "BANK"}
            parts = []
            for word in after.split():
                if word in _stop or re.match(r'^[A-Z0-9]{8,}$', word):
                    break
                parts.append(word)
            return " ".join(parts).title() if parts else after.split()[0].title()
        cleaned = re.sub(r"\s+\w{8,}\s*\d{6}\s*$", "", raw).strip()
        return cleaned[:35]

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.row_factory = sqlite3.Row
        try:
            # Build outgoing-only exclusions from scheduled tx_match patterns
            sched_out_kws = [
                item["tx_match"].lower()
                for item in sched.get("bills", []) + sched.get("income", [])
                if item.get("tx_match")
            ]
            # Scheduled transfers without tx_match — exclude only when positive (outgoing)
            extra_out_kws = [
                "schwab bank transfer",
                "schwab brokerage moneylink",
                "synchrony bank transfer",
                "edward jones investment",
                "american airlines",
                "5/3 online transfer",
                "capital one transfer",
            ]
            all_out_kws = sched_out_kws + extra_out_kws
            out_excl = " ".join(
                "AND NOT (t.amount > 0 AND LOWER(t.name) LIKE ?)" for _ in all_out_kws
            )
            out_params = tuple(f"%{kw}%" for kw in all_out_kws)

            misc_rows = conn.execute(f"""
                SELECT t.date, t.name, t.amount
                FROM transactions t
                JOIN accounts a ON a.account_id = t.account_id
                WHERE a.subtype = 'checking'
                  AND t.amount != 0
                  AND t.date >= ? AND t.date <= ?
                  AND IFNULL(t.pending, 0) = 0
                  AND LOWER(t.name) NOT LIKE '%employer payroll%'
                  AND LOWER(t.name) NOT LIKE '%rent payment%'
                  {out_excl}
                ORDER BY t.date ASC
            """, (month_start.isoformat(), month_end.isoformat()) + out_params).fetchall()
        finally:
            conn.close()

        for row in misc_rows:
            direction = "in" if row["amount"] < 0 else "out"
            events.append({
                "date":        row["date"],
                "type":        "cash",
                "direction":   direction,
                "name":        _clean_misc_name(row["name"]),
                "amount":      abs(float(row["amount"])),
                "amount_type": "actual",
                "occurred":    True,
            })
    except Exception:
        pass

    events.sort(key=lambda e: e["date"])

    return jsonify(events)


# ---------------------------------------------------------------------------
# Pay-period calendar helpers + routes
# ---------------------------------------------------------------------------

def _calc_pay_periods(year, month, anchor_str, cadence_days):
    """Return list of pay-period column dicts covering the given month.

    Each dict includes:
      label, start, end (ISO strings), is_tail,
      income_payday  – the payday whose check FUNDS this column (may be before month_start),
      proration_factor – fraction of the full pay period that falls in this month.
    """
    import calendar as _cal, math as _math
    if not anchor_str:
        return []
    anchor = date.fromisoformat(anchor_str)
    _, days_in_month = _cal.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end   = date(year, month, days_in_month)

    # prev_payday: last payday on or before month_start
    diff = (month_start - anchor).days
    prev_payday = anchor + timedelta(days=_math.floor(diff / cadence_days) * cadence_days)

    # collect all paydays that fall within the month
    month_paydays = []
    p = prev_payday
    while p <= month_end:
        if p >= month_start:
            month_paydays.append(p)
        p += timedelta(days=cadence_days)

    periods = []
    if not month_paydays:
        # Entire month falls inside one pay period — proration = days_in_month / cadence
        periods.append({
            "label": f"{month_start.month}/{month_start.day}/{month_start.year}",
            "start": month_start.isoformat(), "end": month_end.isoformat(),
            "income_payday": prev_payday.isoformat(),
            "proration_factor": round(days_in_month / cadence_days, 6),
            "is_tail": True,
        })
        return periods

    # First column: month_start → first payday in month, funded by prev_payday.
    # If all cadence_days fall within the month this is a full period and gets a
    # normal start-date label.  Only use the "< X" label when the period actually
    # began before month_start (i.e. it is genuinely partial).
    fp = month_paydays[0]
    tail_days = (fp - month_start).days + 1
    next_start = fp + timedelta(days=1)
    is_partial_tail = tail_days < cadence_days
    label = (f"< {next_start.month}/{next_start.day}"
             if is_partial_tail
             else f"{month_start.month}/{month_start.day}/{month_start.year}")
    periods.append({
        "label": label,
        "start": month_start.isoformat(), "end": fp.isoformat(),
        "income_payday": prev_payday.isoformat(),
        "proration_factor": round(tail_days / cadence_days, 6),
        "is_tail": is_partial_tail,
    })

    # One column per payday in month: (payday+1) → next payday (or month_end)
    # Each column is funded by the payday at its START (pd).
    for i, pd in enumerate(month_paydays):
        col_start = pd + timedelta(days=1)
        col_end   = month_paydays[i + 1] if i + 1 < len(month_paydays) else month_end
        vis_s = max(col_start, month_start)
        vis_e = min(col_end, month_end)
        if vis_s <= vis_e:
            vis_days = (vis_e - vis_s).days + 1
            periods.append({
                "label": f"{col_start.month}/{col_start.day}/{col_start.year}",
                "start": vis_s.isoformat(), "end": vis_e.isoformat(),
                "income_payday": pd.isoformat(),  # paycheck received on this date
                "proration_factor": round(vis_days / cadence_days, 6),
                "is_tail": False,
            })
    return periods


@reports_bp.route("/paycalendar")
@login_required
def paycalendar():
    return render_template("paycalendar.html")


@reports_bp.route("/api/pay_calendar")
@login_required
def api_pay_calendar():
    from src.common.paths import PROJECT_ROOT
    import calendar as _cal

    month_param = (request.args.get("month") or "").strip()
    if not month_param:
        month_param = date.today().strftime("%Y-%m")
    try:
        yr, mn = int(month_param[:4]), int(month_param[5:7])
    except Exception:
        return jsonify({"error": "invalid month"}), 400

    _, days_in_month = _cal.monthrange(yr, mn)
    month_start = date(yr, mn, 1)
    month_end   = date(yr, mn, days_in_month)

    sched_path = PROJECT_ROOT / "config" / "schedules.json"
    with open(sched_path, encoding="utf-8") as f:
        sched = json.load(f)

    payday_cfg = sched.get("payday", {})
    periods = _calc_pay_periods(
        yr, mn,
        payday_cfg.get("anchor_date", ""),
        int(payday_cfg.get("cadence_days", 14)),
    )

    # date-string → period index lookup
    period_for_date = {}
    for idx, p in enumerate(periods):
        d = date.fromisoformat(p["start"])
        end_d = date.fromisoformat(p["end"])
        while d <= end_d:
            period_for_date[d.isoformat()] = idx
            d += timedelta(days=1)

    cadence_days = int(payday_cfg.get("cadence_days", 14))

    # Extend query window to find paychecks that fund the tail (may land before month_start)
    query_start = (month_start - timedelta(days=cadence_days)).isoformat()
    query_end   = month_end.isoformat()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row

    cash_days = {}   # date -> {out, in, txs_out, txs_in}
    cc_days   = {}   # date -> {total, txs}
    # paycheck_by_date: date -> total payroll deposit (Employer, name match)
    paycheck_by_date: dict[str, float] = {}

    try:
        # Depository transactions (extended range to capture tail-funding paycheck)
        for row in conn.execute("""
            SELECT t.date, t.name, t.merchant_name, t.amount
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.type = 'depository'
              AND t.date >= ? AND t.date <= ?
              AND IFNULL(t.pending, 0) = 0
            ORDER BY t.date ASC
        """, (query_start, query_end)).fetchall():
            d   = row["date"]
            amt = float(row["amount"])
            nm  = (row["merchant_name"] or row["name"] or "")[:50]

            # Track payroll deposits regardless of month boundary
            if amt < 0 and "EMPLOYER" in (row["name"] or "").upper():
                paycheck_by_date[d] = paycheck_by_date.get(d, 0.0) + abs(amt)

            # Only add to cash_days for dates within the actual month
            if d < month_start.isoformat():
                continue
            if d not in cash_days:
                cash_days[d] = {"out": 0.0, "in": 0.0, "txs_out": [], "txs_in": []}
            if amt > 0:
                cash_days[d]["out"] = round(cash_days[d]["out"] + amt, 2)
                cash_days[d]["txs_out"].append({"name": nm, "amount": amt})
            else:
                cash_days[d]["in"] = round(cash_days[d]["in"] + abs(amt), 2)
                cash_days[d]["txs_in"].append({"name": nm, "amount": abs(amt)})

        # Credit card charges (positive = purchase)
        for row in conn.execute("""
            SELECT t.date, t.name, t.merchant_name, t.amount,
                   a.name AS acct_name, tc.user_category
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            WHERE a.type = 'credit'
              AND t.amount > 0
              AND t.date >= ? AND t.date <= ?
              AND IFNULL(t.pending, 0) = 0
            ORDER BY t.date ASC
        """, (month_start.isoformat(), query_end)).fetchall():
            d   = row["date"]
            amt = float(row["amount"])
            nm  = (row["merchant_name"] or row["name"] or "")[:50]
            if d not in cc_days:
                cc_days[d] = {"total": 0.0, "txs": []}
            cc_days[d]["total"] = round(cc_days[d]["total"] + amt, 2)
            cc_days[d]["txs"].append({
                "name": nm, "amount": amt,
                "account": row["acct_name"],
                "category": row["user_category"] or "",
            })
    finally:
        conn.close()

    # Fallback paycheck amount: most recent known paycheck
    recent_paycheck = max(paycheck_by_date.values(), default=0.0)

    def _find_paycheck(income_payday_str: str) -> float:
        """Find paycheck deposited on or within ±2 days of the given date."""
        if not income_payday_str:
            return recent_paycheck
        pd = date.fromisoformat(income_payday_str)
        for delta in (0, -1, 1, -2, 2):
            d_str = (pd + timedelta(days=delta)).isoformat()
            if d_str in paycheck_by_date:
                return paycheck_by_date[d_str]
        return recent_paycheck  # fallback to most recent known amount

    # Period-level aggregates
    p_cash_out = [0.0] * len(periods)
    p_cash_in  = [0.0] * len(periods)
    p_cc       = [0.0] * len(periods)
    for d_str, dd in cash_days.items():
        idx = period_for_date.get(d_str)
        if idx is not None:
            p_cash_out[idx] = round(p_cash_out[idx] + dd["out"], 2)
            p_cash_in[idx]  = round(p_cash_in[idx]  + dd["in"],  2)
    for d_str, dd in cc_days.items():
        idx = period_for_date.get(d_str)
        if idx is not None:
            p_cc[idx] = round(p_cc[idx] + dd["total"], 2)

    for i, p in enumerate(periods):
        p["cash_out"] = p_cash_out[i]
        p["cash_in"]  = p_cash_in[i]
        p["cc_total"] = p_cc[i]
        # Prorated income: paycheck for this period × fraction of period in this month
        paycheck_amt = _find_paycheck(p.get("income_payday"))
        factor       = p.get("proration_factor", 1.0)
        p["prorated_income"] = round(paycheck_amt * factor, 2)
        p["paycheck_amount"] = round(paycheck_amt, 2)

    return jsonify({
        "month":           month_param,
        "periods":         periods,
        "cash_days":       cash_days,
        "cc_days":         cc_days,
        "period_for_date": period_for_date,
    })


# ---------------------------------------------------------------------------
# Monthly cashflow view
# ---------------------------------------------------------------------------

@reports_bp.route("/monthly")
@login_required
def monthly():
    return render_template("monthly.html")


def _credit_statement_map(conn) -> dict:
    """
    account_id -> statement info parsed from Plaid liabilities (credit cards).
    Populated by the /liabilities fetch into liabilities_raw; empty until then.
    """
    out: dict = {}
    try:
        rows = conn.execute("SELECT payload_json FROM liabilities_raw").fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            continue
        for cc in (payload.get("liabilities") or {}).get("credit") or []:
            aid = cc.get("account_id")
            if not aid:
                continue
            out[aid] = {
                "statement_balance": cc.get("last_statement_balance"),
                "statement_date":    cc.get("last_statement_issue_date"),
                "due_date":          cc.get("next_payment_due_date"),
                "min_payment":       cc.get("minimum_payment_amount"),
                "is_overdue":        cc.get("is_overdue"),
            }
    return out


def _schedule_due_map(conn) -> dict:
    """
    account_id -> {due_date, due_day, bill_name} for credit cards, derived from
    the autopay `day` in config/schedules.json. Used as a payment-due-date proxy
    when Plaid Liabilities data isn't available. Bills are matched to accounts by
    word overlap between the bill's `plaid_match` and the account's
    institution / name / mask.
    """
    import calendar as _cal
    out: dict = {}
    try:
        with open(PROJECT_ROOT / "config" / "schedules.json", encoding="utf-8") as f:
            sched = json.load(f)
    except Exception:
        return out

    bills = [b for b in sched.get("bills", []) if b.get("day") and b.get("plaid_match")]
    if not bills:
        return out

    accts = conn.execute("""
        SELECT a.account_id, a.name, a.mask, i.institution_name
        FROM accounts a JOIN items i ON i.item_id = a.item_id
        WHERE a.type = 'credit'
    """).fetchall()

    def toks(s):
        return set(re.findall(r"[a-z]+", (s or "").lower()))

    today = date.today()
    for a in accts:
        hay = toks(f"{a['institution_name']} {a['name']} {a['mask']}")
        best, best_score = None, 0
        for b in bills:
            score = len(toks(b.get("plaid_match")) & hay)
            if score > best_score:
                best, best_score = b, score
        if not best or best_score < 1:
            continue
        day = int(best["day"])
        y, m = today.year, today.month
        if day < today.day:          # already passed this month -> next month
            m += 1
            if m > 12:
                m, y = 1, y + 1
        day = min(day, _cal.monthrange(y, m)[1])   # clamp (e.g. day 31 in Feb)
        out[a["account_id"]] = {
            "due_date":  date(y, m, day).isoformat(),
            "due_day":   int(best["day"]),
            "bill_name": best["name"],
        }
    return out


@reports_bp.route("/api/account_balances")
@login_required
def api_account_balances():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT a.account_id, a.name, a.type, a.subtype,
                   a.current, a.available, a.balance_limit, a.updated_at,
                   i.institution_name
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            WHERE a.type IN ('depository', 'credit', 'investment')
            ORDER BY a.type DESC, a.subtype, a.name
        """).fetchall()
        stmt_map = _credit_statement_map(conn)
        sched_due = _schedule_due_map(conn)
    finally:
        conn.close()

    accounts = []
    for r in rows:
        stmt = stmt_map.get(r["account_id"], {})
        # Due date priority: Plaid Liabilities > autopay schedule (config).
        due_date   = stmt.get("due_date")
        due_source = "statement" if due_date else None
        if not due_date and r["account_id"] in sched_due:
            due_date   = sched_due[r["account_id"]]["due_date"]
            due_source = "autopay"
        accounts.append({
            "account_id":   r["account_id"],
            "name":         r["name"],
            "institution":  r["institution_name"],
            "type":         r["type"],
            "subtype":      r["subtype"],
            "current":      float(r["current"]) if r["current"] is not None else None,
            "available":    float(r["available"]) if r["available"] is not None else None,
            "limit":        float(r["balance_limit"]) if r["balance_limit"] is not None else None,
            "updated_at":   (r["updated_at"] or "")[:10],
            "source":       "plaid",
            "stmt_balance": stmt.get("statement_balance"),
            "stmt_date":    stmt.get("statement_date"),
            "due_date":     due_date,
            "due_source":   due_source,
            "min_payment":  stmt.get("min_payment"),
            "is_overdue":   stmt.get("is_overdue"),
        })

    # Append latest manual balances (statement uploads)
    try:
        conn2 = sqlite3.connect(str(DB_PATH), timeout=10)
        conn2.row_factory = sqlite3.Row
        _ensure_manual_balance_table(conn2)
        manual = conn2.execute("""
            SELECT id, institution, account_name, account_type, balance,
                   statement_date, payment_due_date, uploaded_at
            FROM manual_account_balances
            WHERE id IN (
                SELECT MAX(id) FROM manual_account_balances GROUP BY institution, account_name
            )
            ORDER BY institution, account_name
        """).fetchall()
        conn2.close()
        for m in manual:
            # For a manually entered statement, the recorded balance IS the
            # statement balance; current Plaid balance is unknown.
            accounts.append({
                "name":        m["account_name"],
                "institution": m["institution"],
                "type":        m["account_type"],
                "subtype":     m["account_type"],
                "current":     None,
                "available":   None,
                "limit":       None,
                "updated_at":  (m["statement_date"] or m["uploaded_at"] or "")[:10],
                "source":      "manual",
                "manual_id":   m["id"],
                "stmt_balance": float(m["balance"]) if m["balance"] is not None else None,
                "stmt_date":    m["statement_date"],
                "due_date":     m["payment_due_date"],
                "due_source":   "manual" if m["payment_due_date"] else None,
                "min_payment":  None,
                "is_overdue":   None,
            })
    except Exception:
        pass

    return jsonify(accounts)


@reports_bp.route("/api/account_cashflow")
@login_required
def api_account_cashflow():
    """Per-account cash flow for one month: money in (amount<0) vs out (amount>0).
    Used by the Monthly Cashflow page when a single account tile is selected."""
    account_id = (request.args.get("account_id") or "").strip()
    month      = (request.args.get("month") or _month_key(date.today())).strip()
    if not account_id:
        return jsonify({"error": "account_id required"}), 400

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        acct = conn.execute("""
            SELECT a.name, a.type, a.subtype, i.institution_name
            FROM accounts a JOIN items i ON i.item_id = a.item_id
            WHERE a.account_id = ?
        """, (account_id,)).fetchone()
        rows = conn.execute("""
            SELECT t.date, t.name, t.merchant_name, t.amount,
                   COALESCE(tc.user_category, '') AS category
            FROM transactions t
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            WHERE t.account_id = ? AND t.date LIKE ? AND IFNULL(t.pending, 0) = 0
            ORDER BY t.date ASC, t.amount DESC
        """, (account_id, f"{month}%")).fetchall()
    finally:
        conn.close()

    money_in, money_out = [], []
    tin = tout = 0.0
    for r in rows:
        amt = float(r["amount"] or 0)
        item = {
            "date":     r["date"],
            "label":    (r["merchant_name"] or r["name"] or "")[:48],
            "category": r["category"],
            "amount":   round(abs(amt), 2),
        }
        if amt < 0:        # money IN (deposit / credit / refund / payment received)
            money_in.append(item);  tin += abs(amt)
        elif amt > 0:      # money OUT (withdrawal / charge / transfer)
            money_out.append(item); tout += abs(amt)

    return jsonify({
        "account": {
            "name":        acct["name"] if acct else account_id,
            "institution": acct["institution_name"] if acct else "",
            "type":        acct["type"] if acct else "",
            "subtype":     acct["subtype"] if acct else "",
        },
        "month":     month,
        "money_in":  money_in,
        "money_out": money_out,
        "totals":    {"in": round(tin, 2), "out": round(tout, 2), "net": round(tin - tout, 2)},
    })


@reports_bp.route("/api/monthly_cashflow")
@login_required
def api_monthly_cashflow():
    from src.common.paths import PROJECT_ROOT
    import calendar as _cal

    month_param = (request.args.get("month") or "").strip()
    if not month_param:
        month_param = date.today().strftime("%Y-%m")
    try:
        yr, mn = int(month_param[:4]), int(month_param[5:7])
    except Exception:
        return jsonify({"error": "invalid month"}), 400

    _, days = _cal.monthrange(yr, mn)
    month_start = date(yr, mn, 1)
    month_end   = date(yr, mn, days)

    sched_path = PROJECT_ROOT / "config" / "schedules.json"
    with open(sched_path, encoding="utf-8") as f:
        sched = json.load(f)

    # Bill tx_match pattern → display name
    bill_patterns = {
        b["tx_match"].lower(): b["name"]
        for b in sched.get("bills", []) if b.get("tx_match")
    }
    # Known scheduled transfer outgoing patterns
    XFER_PATTERNS = {
        "schwab bank transfer":      "Schwab Checking",
        "schwab brokerage moneylink":"Schwab Brokerage",
        "synchrony bank transfer":   "Synchrony",
        "edward jones investment":   "Edward Jones",
        "american airlines":         "AA Credit Union",
        "5/3 online transfer to":    "53 Savings",
        "capital one transfer":      "Capital One Savings",
    }

    def _label_checking(name, amount):
        """Returns (group, label) for one checking transaction."""
        n = name.upper()
        if amount < 0:  # money IN to checking
            if "EMPLOYER" in n:
                return "payroll", "Employer Payroll"
            if "RECEIVED ZELLE PMT" in n:
                if "RENT PAYMENT" in n:
                    return "income_other", "House Holdings"
                who = name.split(" FROM ")[-1].strip().title() if " FROM " in n else "Zelle"
                return "income_other", f"Zelle ← {who}"
            if "5/3 ONLINE TRANSFER FROM" in n:
                return "income_other", "53 Savings ←"
            if "SCHWAB BROKERAGE MONEYLINK" in n:
                return "income_other", "Schwab ←"
            if "CAPITAL ONE TRANSFER" in n:
                return "income_other", "Capital One ←"
            return "income_other", name[:45].strip()
        # outgoing
        n_lower = name.lower()
        for pat, label in bill_patterns.items():
            if pat in n_lower:
                return "bills", label
        for pat, label in XFER_PATTERNS.items():
            if pat in n_lower:
                return "transfers", label
        # misc out — clean up display name
        if "ATM WITHDRAWAL" in n:
            return "misc_out", "ATM"
        if "SENT ZELLE PMT" in n and " TO " in n:
            return "misc_out", "Zelle → " + name.split(" TO ")[-1].strip().title()
        if "WEB INITIATED PAYMENT AT " in n:
            after = n.split("WEB INITIATED PAYMENT AT ")[1]
            _stop = {"CK","SV","WEBXFR","PURCHASE","TRANSFER","PAYMENT","BILLPAY","VILLAGE","GAS","BANK"}
            parts = [w for w in after.split()
                     if w not in _stop and not re.match(r'^[A-Z0-9]{8,}$', w)]
            label = " ".join(parts[:3]).title() if parts else after.split()[0].title()
            return "misc_out", label
        return "misc_out", re.sub(r'\s+\w{8,}\s*\d{6}\s*$', '', name).strip()[:40]

    result: dict = {
        "month": month_param,
        "checking": {"payroll": [], "income_other": [], "bills": [], "transfers": [], "misc_out": []},
        "cc_transactions": [],
        "category_totals": {},
    }

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    try:
        # --- Checking account ---
        for row in conn.execute("""
            SELECT t.date, t.name, t.amount
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.subtype = 'checking'
              AND t.date >= ? AND t.date <= ?
              AND IFNULL(t.pending, 0) = 0
            ORDER BY t.date ASC
        """, (month_start.isoformat(), month_end.isoformat())).fetchall():
            group, label = _label_checking(row["name"], row["amount"])
            result["checking"][group].append({
                "date":   row["date"],
                "label":  label,
                "amount": abs(float(row["amount"])),
            })

        # --- Credit card charges ---
        cat_totals: dict[str, float] = {}
        for row in conn.execute("""
            SELECT t.date, t.name, t.merchant_name, t.amount,
                   a.name AS acct_name,
                   tc.user_category, tc.user_subcategory
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            WHERE a.type = 'credit'
              AND t.amount > 0
              AND t.date >= ? AND t.date <= ?
              AND IFNULL(t.pending, 0) = 0
            ORDER BY t.date ASC
        """, (month_start.isoformat(), month_end.isoformat())).fetchall():
            cat   = row["user_category"] or "Uncategorized"
            amt   = float(row["amount"])
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            result["cc_transactions"].append({
                "date":     row["date"],
                "merchant": (row["merchant_name"] or row["name"])[:40],
                "category": cat,
                "subcategory": row["user_subcategory"] or "",
                "amount":   amt,
                "account":  row["acct_name"],
            })
    finally:
        conn.close()

    result["category_totals"] = dict(
        sorted(cat_totals.items(), key=lambda x: -x[1])
    )

    chk = result["checking"]
    total_in  = sum(e["amount"] for e in chk["payroll"] + chk["income_other"])
    total_out = sum(e["amount"] for e in chk["bills"] + chk["transfers"] + chk["misc_out"])
    result["totals"] = {
        "income":    round(total_in, 2),
        "bills":     round(sum(e["amount"] for e in chk["bills"]),     2),
        "transfers": round(sum(e["amount"] for e in chk["transfers"]), 2),
        "misc_out":  round(sum(e["amount"] for e in chk["misc_out"]),  2),
        "total_out": round(total_out, 2),
        "net":       round(total_in - total_out, 2),
        "cc_spend":  round(sum(r["amount"] for r in result["cc_transactions"]), 2),
    }
    return jsonify(result)


@reports_bp.route("/api/schedule_amounts", methods=["GET", "POST"])
@login_required
def api_schedule_amounts():
    """GET: return amount for all income + bills + transfers for a given month.
            Monthly override takes priority over global amount_override.
       POST: save overrides into monthly_overrides[month] in schedules.json."""
    from src.common.paths import PROJECT_ROOT
    sched_path = PROJECT_ROOT / "config" / "schedules.json"

    with open(sched_path, encoding="utf-8") as f:
        sched = json.load(f)

    month_param = (request.args.get("month") or "").strip()
    if not month_param:
        month_param = date.today().strftime("%Y-%m")
    month_overrides: dict = sched.setdefault("monthly_overrides", {}).get(month_param, {})

    def effective_amount(item):
        if item["name"] in month_overrides:
            return month_overrides[item["name"]]
        return item.get("amount_override")

    if request.method == "GET":
        return jsonify({
            "income":    [{"name": i["name"], "amount_override": effective_amount(i)} for i in sched.get("income", [])],
            "bills":     [{"name": b["name"], "amount_override": effective_amount(b)} for b in sched["bills"]],
            "transfers": [{"name": t["name"], "amount_override": effective_amount(t)} for t in sched["transfers"]],
        })

    # POST — save overrides into the month-specific bucket only
    body = request.get_json() or {}
    overrides = body.get("overrides", {})
    month_param = body.get("month", month_param)

    bucket = sched.setdefault("monthly_overrides", {}).setdefault(month_param, {})
    all_items = sched.get("income", []) + sched["bills"] + sched["transfers"]
    for item in all_items:
        name = item["name"]
        raw = str(overrides.get(name, "")).strip()
        if raw:
            try:
                bucket[name] = float(raw)
            except ValueError:
                pass
        elif name in bucket:
            del bucket[name]

    with open(sched_path, "w", encoding="utf-8") as f:
        json.dump(sched, f, indent=2)

    return jsonify({"ok": True})


@reports_bp.route("/api/calendar_2week")
@login_required
def api_calendar_2week():
    """
    Query:
      ?anchor=YYYY-MM-DD  (defaults today)
      ?days=14            (defaults 14)
      ?range=30d|ytd|12m  (optional; if provided, still hard-filters to window)
    Returns:
      [{date, dow, spend_total, txn_count}, ...] for the window ending at anchor (inclusive)
    """
    anchor_s = (request.args.get("anchor") or "").strip()
    days = int(request.args.get("days", "14"))
    days = max(7, min(60, days))

    anchor = date.today()
    if anchor_s:
        try:
            anchor = datetime.strptime(anchor_s, "%Y-%m-%d").date()
        except Exception:
            pass

    start = anchor - timedelta(days=days - 1)

    df = _load_tx_detail()
    df = _spend_rows(df)
    if df.empty:
        # still return the dates with zeros so the UI can render
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            out.append({"date": d.isoformat(), "dow": d.strftime("%a"), "spend_total": 0.0, "txn_count": 0})
        return jsonify(out)

    df = df[(df["date"].dt.date >= start) & (df["date"].dt.date <= anchor)]

    g = (
        df.groupby(df["date"].dt.date)
          .agg(spend_total=("spend", "sum"), txn_count=("spend", "count"))
    )

    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        row = g.loc[d] if d in g.index else None
        out.append({
            "date": d.isoformat(),
            "dow": d.strftime("%a"),
            "spend_total": round(float(row["spend_total"]), 2) if row is not None else 0.0,
            "txn_count": int(row["txn_count"]) if row is not None else 0,
        })
    return jsonify(out)


@reports_bp.route("/api/calendar_month")
@login_required
def api_calendar_month():
    """
    Query:
      ?month=YYYY-MM (defaults current month)
    Returns:
      { month, weeks: [[day|null,...7], ...] }
    Each day: {date, day, spend_total, txn_count}
    """
    month_s = (request.args.get("month") or "").strip()
    today = date.today()

    if month_s:
        try:
            y, m = map(int, month_s.split("-"))
            month_first = date(y, m, 1)
        except Exception:
            month_first = date(today.year, today.month, 1)
    else:
        month_first = date(today.year, today.month, 1)

    # compute last day of month
    next_month = date(month_first.year + (month_first.month == 12), 1 if month_first.month == 12 else month_first.month + 1, 1)
    month_last = next_month - timedelta(days=1)

    df = _load_tx_detail()
    df = _spend_rows(df)
    if not df.empty:
        df = df[(df["date"].dt.date >= month_first) & (df["date"].dt.date <= month_last)]
        g = (
            df.groupby(df["date"].dt.date)
              .agg(spend_total=("spend", "sum"), txn_count=("spend", "count"))
        )
    else:
        g = None

    # calendar starts on Sunday (0) for UI table
    # Python weekday: Mon=0..Sun=6, so Sunday index is 6
    start_pad = (month_first.weekday() + 1) % 7  # Sun=0..Sat=6
    days_in_month = month_last.day

    cells = []
    for _ in range(start_pad):
        cells.append(None)

    for day in range(1, days_in_month + 1):
        d = date(month_first.year, month_first.month, day)
        row = (g.loc[d] if (g is not None and d in g.index) else None)
        cells.append({
            "date": d.isoformat(),
            "day": day,
            "spend_total": round(float(row["spend_total"]), 2) if row is not None else 0.0,
            "txn_count": int(row["txn_count"]) if row is not None else 0,
        })

    # pad to full weeks
    while len(cells) % 7 != 0:
        cells.append(None)

    weeks = [cells[i:i+7] for i in range(0, len(cells), 7)]
    return jsonify({"month": f"{month_first.year:04d}-{month_first.month:02d}", "weeks": weeks})


@reports_bp.route("/api/day_spend_detail")
@login_required
def api_day_spend_detail():
    """
    Query:
      ?date=YYYY-MM-DD
    Returns: list of spend txns for that day (top 200 by amount desc)
    """
    d_s = (request.args.get("date") or "").strip()
    try:
        d = datetime.strptime(d_s, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "invalid date; expected YYYY-MM-DD"}), 400

    df = _load_tx_detail()
    df = _spend_rows(df)
    if df.empty:
        return jsonify([])

    df = df[df["date"].dt.date == d].copy()
    if df.empty:
        return jsonify([])

    # pick safe columns
    cols = []
    for c in ["date", "merchant_name", "name", "amount", "category", "transaction_id", "account_id"]:
        if c in df.columns:
            cols.append(c)

    df = df.sort_values("amount", ascending=False).head(200)[cols]
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return jsonify(df.to_dict(orient="records"))


# -------------------------------------------------------------------
# M4: Budget engine
# -------------------------------------------------------------------
def _month_key(dt: date) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"

@reports_bp.route("/api/budget", methods=["GET", "POST"])
@login_required
def api_budget():
    """
    GET  /api/budget?month=YYYY-MM -> { category: amount, ... }
    POST /api/budget { month?: 'YYYY-MM', items: [{category, amount}, ...] }
      - month optional; if omitted, acts as default template
    """
    ensure_summary_views_and_tables()
    month = request.args.get("month") if request.method == "GET" else None
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        month = payload.get("month")
        items = payload.get("items", [])
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        cur = conn.cursor()
        for it in items:
            cat = (it.get("category") or "").strip() or "Uncategorized"
            amt = float(it.get("amount") or 0.0)
            cur.execute(
                "INSERT INTO budgets (category, month, amount) VALUES(?, ?, ?) "
                "ON CONFLICT(category, month) DO UPDATE SET amount=excluded.amount;",
                (cat, month, amt)
            )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    # GET
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cur = conn.cursor()
    if month:
        cur.execute("SELECT category, amount FROM budgets WHERE month = ? ORDER BY category;", (month,))
        rows = cur.fetchall()
        if not rows:
            # fall back to monthless template
            cur.execute("SELECT category, amount FROM budgets WHERE month IS NULL ORDER BY category;")
            rows = cur.fetchall()
    else:
        cur.execute("SELECT category, amount FROM budgets WHERE month IS NULL ORDER BY category;")
        rows = cur.fetchall()
    conn.close()
    return jsonify({r[0]: round(float(r[1]), 2) for r in rows})

@reports_bp.route("/api/budget_vs_actual")
@login_required
def api_budget_vs_actual():
    """
    Budget vs actual for a given month (default: current month).
    Returns: [{category, budget, actual, variance}, ...]
    """
    ensure_summary_views_and_tables()
    month = request.args.get("month")
    if not month:
        month = _month_key(date.today())

    # Actuals
    df = _load_tx_detail()
    if df.empty:
        return jsonify([])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["month"] = df["date"].dt.to_period("M").astype(str)
    df = df[df["month"] == month]

    # Expenses only for actuals (exclude income-ish)
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    exp = df.loc[~df["is_income"]]

    actuals = exp.groupby("category")["amount"].sum().round(2)
    actuals = actuals[actuals.index.notnull()]

    # Budgets
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT category, amount FROM budgets WHERE month = ?;", (month,))
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT category, amount FROM budgets WHERE month IS NULL;")
        rows = cur.fetchall()
    conn.close()
    budgets = {r[0]: float(r[1]) for r in rows}

    cats = sorted(set(list(actuals.index) + list(budgets.keys())))
    out = []
    for c in cats:
        b = round(budgets.get(c, 0.0), 2)
        a = round(float(actuals.get(c, 0.0)), 2)
        out.append({"category": c, "budget": b, "actual": a, "variance": round(b - a, 2)})
    return jsonify(out)

# -------------------------------------------------------------------
# M4: Forecasts (simple MA projection)
# -------------------------------------------------------------------
@reports_bp.route("/api/forecast_cashflow")
@login_required
def api_forecast_cashflow():
    """
    Projects next 6 months inflow/outflow using simple trailing moving average.
    Returns: [{month, inflow, outflow, net}, ...]
    """
    df = _load_tx_detail()
    if df.empty or "date" not in df.columns:
        return jsonify([])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    df["month"] = df["date"].dt.to_period("M").astype(str)
    inflow = df.loc[df["is_income"]].groupby("month")["amount"].sum()
    outflow = df.loc[~df["is_income"]].groupby("month")["amount"].sum()
    hist = pd.concat([inflow.rename("inflow"), outflow.rename("outflow")], axis=1).fillna(0.0).sort_index()

    # moving average over last 3 months
    ma_window = max(1, min(3, len(hist)))
    inflow_ma = hist["inflow"].tail(ma_window).mean() if len(hist) else 0.0
    outflow_ma = hist["outflow"].tail(ma_window).mean() if len(hist) else 0.0

    # next 6 months keys
    if len(hist):
        last_key = hist.index[-1]
        year, month = map(int, last_key.split("-"))
    else:
        today = date.today()
        year, month = today.year, today.month

    def next_month(y, m):
        m += 1
        if m > 12:
            m = 1
            y += 1
        return y, m

    out = []
    for _ in range(6):
        year, month = next_month(year, month)
        mk = f"{year:04d}-{month:02d}"
        infl = float(inflow_ma)
        outf = float(outflow_ma)
        out.append({"month": mk, "inflow": round(infl, 2), "outflow": round(outf, 2), "net": round(infl - outf, 2)})
    return jsonify(out)

# -------------------------------------------------------------------
# M4: Daily allowance
# -------------------------------------------------------------------
@reports_bp.route("/api/daily_allowance")
@login_required
def api_daily_allowance():
    """
    Computes daily spend allowance for current month from budget vs actual.
    Returns: { month, today, days_remaining, remaining_budget, daily_allowance, mode }
      mode: "accumulation" if allowance > recent daily avg, else "depletion"
    """
    month = _month_key(date.today())
    # Actuals for current month
    df = _load_tx_detail()
    if df.empty or "date" not in df.columns:
        return jsonify({"error": "no data"}), 404
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["month"] = df["date"].dt.to_period("M").astype(str)
    df = df[df["month"] == month]
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    spend = df.loc[~df["is_income"], "amount"].sum()

    # Budget sum for month
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT SUM(amount) FROM budgets WHERE month = ?", (month,))
    s = cur.fetchone()[0]
    if s is None:
        cur.execute("SELECT SUM(amount) FROM budgets WHERE month IS NULL")
        s = cur.fetchone()[0]
    conn.close()
    total_budget = float(s or 0.0)

    today = date.today()
    days_in_month = (date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1) - date(today.year, today.month, 1)).days
    days_remaining = max(0, days_in_month - today.day + 1)  # include today forward

    remaining_budget = max(0.0, total_budget - spend)
    daily_allowance = round(remaining_budget / days_remaining, 2) if days_remaining > 0 else 0.0

    # Recent daily avg (last 14 days)
    last14 = _filter_by_range(_load_tx_detail(), "30d")
    if not last14.empty and "date" in last14.columns:
        last14 = last14.copy()
        last14["date"] = pd.to_datetime(last14["date"], errors="coerce")
        last14 = last14.dropna(subset=["date"])
        last14["is_income"] = last14.get("merchant_name", "").fillna("").str.contains(
            "payroll|deposit|refund|credit|reversal", case=False, na=False
        )
        window = last14[last14["date"] >= (pd.Timestamp(today) - pd.Timedelta(days=14))]
        daily_exp = window.loc[~window["is_income"]].groupby(window["date"].dt.date)["amount"].sum()
        recent_daily_avg = float(daily_exp.mean()) if len(daily_exp) else 0.0
    else:
        recent_daily_avg = 0.0

    mode = "accumulation" if daily_allowance >= recent_daily_avg else "depletion"

    return jsonify({
        "month": month,
        "today": today.isoformat(),
        "days_remaining": days_remaining,
        "remaining_budget": round(remaining_budget, 2),
        "daily_allowance": daily_allowance,
        "recent_daily_avg": round(recent_daily_avg, 2),
        "mode": mode
    })

# -------------------------------------------------------------------
# M5: Paycheck estimate + simple tax projection
# -------------------------------------------------------------------
@reports_bp.route("/api/paycheck_estimate", methods=["POST"])
@login_required
def api_paycheck_estimate():
    """
    Estimate net pay from gross amount, taxes, and deductions.
    Handles missing or invalid JSON input safely.
    """
    payload = request.get_json(silent=True) or {}

    try:
        gross = float(payload.get("gross", 0))
        pay_periods = int(payload.get("pay_periods", 26))
        federal_rate = float(payload.get("federal_rate", 0.18))
        state_rate = float(payload.get("state_rate", 0.05))
        other_deductions = float(payload.get("other_deductions", 0.0))
    except Exception:
        return jsonify({"error": "Invalid input format. Expected numeric JSON fields."}), 400

    if gross <= 0 or pay_periods <= 0:
        return jsonify({"error": "Gross pay and pay periods must be positive."}), 400

    federal_tax = gross * federal_rate
    state_tax = gross * state_rate
    net = gross - federal_tax - state_tax - other_deductions
    annual_net = net * pay_periods
    annual_gross = gross * pay_periods
    effective_rate = round(1 - (net / gross), 3) if gross else 0.0

    return jsonify({
        "gross": round(gross, 2),
        "net_paycheck": round(net, 2),
        "annual_net": round(annual_net, 2),
        "annual_gross": round(annual_gross, 2),
        "effective_rate": effective_rate,
        "federal_tax": round(federal_tax, 2),
        "state_tax": round(state_tax, 2),
        "other_deductions": round(other_deductions, 2)
    })


@reports_bp.route("/api/tax_projection")
@login_required
def api_tax_projection():
    """
    Very rough projection based on detected income credits YTD and provided flat rates.
    Query: ?federal_rate=0.18&state_rate=0.05
    """
    fed = float(request.args.get("federal_rate", "0.18"))
    state = float(request.args.get("state_rate", "0.05"))

    df = _load_tx_detail()
    if df.empty or "date" not in df.columns:
        return jsonify({"error": "no data"}), 404
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    y = df["date"].dt.year.max()
    ytd = df[df["date"].dt.year == y]
    ytd["is_income"] = ytd.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    income = ytd.loc[ytd["is_income"], "amount"].sum()
    fed_tax = income * fed
    state_tax = income * state
    return jsonify({
        "year": int(y),
        "income_ytd": round(float(income), 2),
        "federal_est": round(float(fed_tax), 2),
        "state_est": round(float(state_tax), 2),
        "total_est": round(float(fed_tax + state_tax), 2)
    })

# -------------------------------------------------------------------
# M5: Insights (stub rules)
# -------------------------------------------------------------------
@reports_bp.route("/api/insights/summary")
@login_required
def api_insights_summary():
    """
    Returns lightweight, rule-based financial insights.
    Automatically handles empty or missing transaction data safely.
    """
    df = _load_tx_detail()

    # ✅ Defensive checks: handle missing or empty data cleanly
    if df.empty or "date" not in df.columns or "amount" not in df.columns:
        return jsonify([])

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    if "category" not in df.columns:
        return jsonify([])

    # Identify income-like transactions
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )

    insights = []

    # --- Example 1: Dining spend last week vs 30-day baseline ---
    dining = df[(df["category"] == "Dining") & (~df["is_income"])]
    if not dining.empty:
        today = pd.Timestamp.today()
        last7 = dining[dining["date"] >= (today - pd.Timedelta(days=7))]["amount"].sum()
        last30 = dining[dining["date"] >= (today - pd.Timedelta(days=30))]["amount"].sum()
        if last30 > 0 and last7 > (last30 / 4):  # >25% of 30-day spend in just 7 days
            insights.append({
                "level": "info",
                "message": "Dining spend in the last week is higher than your 30-day average.",
                "meta": {
                    "last_7_days": round(float(last7), 2),
                    "last_30_days": round(float(last30), 2),
                    "percent_of_baseline": round((last7 / last30) * 100, 1)
                }
            })

    # --- Example 2: Detect missing income in the past 30 days ---
    income_df = df[df["is_income"]]
    if income_df.empty or income_df["date"].max() < (pd.Timestamp.today() - pd.Timedelta(days=30)):
        insights.append({
            "level": "warning",
            "message": "No recent income transactions detected in the past 30 days."
        })

    # --- Example 3: Large single transaction alert (> $1,000 outflow) ---
    big_tx = df[(~df["is_income"]) & (df["amount"].abs() > 1000)]
    if not big_tx.empty:
        insights.append({
            "level": "alert",
            "message": f"{len(big_tx)} unusually large transaction(s) over $1,000 detected.",
            "meta": {"examples": big_tx.head(3)[["date", "merchant_name", "amount"]].to_dict(orient="records")}
        })

    return jsonify(insights)

# ---------------------------------------------------------------------------
# Statement upload + reconciliation
# ---------------------------------------------------------------------------

def _ensure_statement_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS uploaded_statements (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            filename     TEXT NOT NULL,
            account_type TEXT NOT NULL,
            institution  TEXT,
            upload_ts    TEXT DEFAULT (datetime('now')),
            date_start   TEXT,
            date_end     TEXT,
            tx_count     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS statement_transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL
                         REFERENCES uploaded_statements(id) ON DELETE CASCADE,
            date         TEXT NOT NULL,
            description  TEXT,
            amount       REAL NOT NULL
        );
    """)


def _detect_csv_cols(columns: list) -> dict:
    DATE_K   = {"date","transaction date","posted date","post date","trans date",
                "activity date","posting date","value date","trans. date","settle date"}
    DESC_K   = {"description","merchant","payee","name","memo","transaction",
                "details","extended details","narrative","transaction description",
                "merchant name","transaction detail","memo/description","reference"}
    AMT_K    = {"amount","transaction amount","net amount","value"}
    DEBIT_K  = {"debit","withdrawals","withdrawal","charges","debit amount","out","debit/withdrawal"}
    CREDIT_K = {"credit","deposits","deposit","payments","credit amount","in","credit/deposit"}

    result = {k: None for k in ("date","desc","amount","debit","credit")}
    for col in columns:
        c = col.strip().lower()
        if   c in DATE_K   and result["date"]   is None: result["date"]   = col
        elif c in DESC_K   and result["desc"]   is None: result["desc"]   = col
        elif c in AMT_K    and result["amount"] is None: result["amount"] = col
        elif c in DEBIT_K  and result["debit"]  is None: result["debit"]  = col
        elif c in CREDIT_K and result["credit"] is None: result["credit"] = col
    return result


def _parse_amt(s) -> "float | None":
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ("-", "N/A", ""):
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.lstrip().startswith("-")
    s = s.replace(",", "").replace("$", "").replace(" ", "").strip("()").lstrip("+-")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _parse_date_s(s) -> "str | None":
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y",
                "%Y/%m/%d", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_statement_csv(file_bytes: bytes) -> "tuple[list, list]":
    import io as _io
    errors: list = []

    # Decode file
    content = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            content = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        return [], ["Cannot decode file — export as UTF-8 CSV"]

    # Skip leading non-data rows (bank metadata lines)
    lines = content.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        if line.count(",") >= 1 or line.count("\t") >= 1:
            header_idx = i
            break

    try:
        df = pd.read_csv(
            _io.StringIO("\n".join(lines[header_idx:])),
            skip_blank_lines=True, on_bad_lines="skip", dtype=str,
        )
    except Exception as exc:
        return [], [f"CSV parse error: {exc}"]

    if df.empty:
        return [], ["No data rows found in CSV"]

    col = _detect_csv_cols(list(df.columns))

    missing = []
    if not col["date"]:                                      missing.append("date")
    if not col["desc"]:                                      missing.append("description")
    if not col["amount"] and not (col["debit"] or col["credit"]): missing.append("amount")
    if missing:
        errors.append(
            f"Could not identify {', '.join(missing)} column(s). "
            f"Headers found: {list(df.columns)}"
        )
        return [], errors

    txs = []
    for _, row in df.iterrows():
        date_s = _parse_date_s(row.get(col["date"]))
        if not date_s:
            continue
        desc = str(row.get(col["desc"], "") or "").strip()

        if col["amount"]:
            amount = _parse_amt(row.get(col["amount"]))
        else:
            debit  = _parse_amt(row.get(col["debit"],  "")) or 0.0
            credit = _parse_amt(row.get(col["credit"], "")) or 0.0
            amount = abs(debit) - abs(credit)   # positive = net outflow

        if amount is None:
            continue
        txs.append({"date": date_s, "description": desc, "amount": round(amount, 2)})

    txs.sort(key=lambda x: x["date"])
    if not txs:
        errors.append("No valid transactions found after parsing — check column formats.")
    return txs, errors


def _parse_statement_pdf(file_bytes: bytes) -> "tuple[list, list]":
    """Parse a credit card PDF statement (Synchrony and similar formats)."""
    try:
        import pdfplumber
    except ImportError:
        return [], ["pdfplumber not installed — run: pip install pdfplumber"]

    import io as _io, re

    errors: list = []
    txs:    list = []

    # Date patterns: MM/DD, MM/DD/YY, MM/DD/YYYY
    DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)")
    # Amount at end of line: trailing "-" for credits (Synchrony style: 123.45-)
    AMT_RE2 = re.compile(r"\$?([\d,]+\.\d{2})(-?)\s*(?:CR)?\s*$", re.IGNORECASE)

    def _try_parse_date_with_year(s: str, year: int) -> "str | None":
        """Parse MM/DD or MM/DD/YY or MM/DD/YYYY; fill in year when missing."""
        parts = s.strip().split("/")
        if len(parts) == 2:
            try:
                m, d = int(parts[0]), int(parts[1])
                return date(year, m, d).isoformat()
            except ValueError:
                return None
        return _parse_date_s(s)

    def _sniff_year(text: str) -> int:
        """Pull the statement year from the PDF header text."""
        m = re.search(r"\b(20\d{2})\b", text)
        return int(m.group(1)) if m else datetime.today().year

    def _parse_amt_pdf(s: str) -> "float | None":
        """Parse an amount cell from a PDF table, handling trailing '-' for credits."""
        if not s:
            return None
        s = s.strip()
        is_credit = s.endswith("-") or s.upper().endswith("CR")
        s = re.sub(r"[CR\-]$", "", s, flags=re.IGNORECASE).strip()
        v = _parse_amt(s)
        if v is None:
            return None
        return -abs(v) if is_credit else abs(v)

    try:
        with pdfplumber.open(_io.BytesIO(file_bytes)) as pdf:
            first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
            year = _sniff_year(first_text)

            for page in pdf.pages:
                # ── Try structured table extraction first ──────────────────
                tables = page.extract_tables() or []
                for tbl in tables:
                    if not tbl:
                        continue
                    # Detect header row
                    header = [str(c or "").lower().strip() for c in tbl[0]]
                    col = _detect_csv_cols([str(c or "") for c in tbl[0]])

                    # If no recognisable headers try treating first row as data
                    data_rows = tbl[1:] if any(col[k] for k in col) else tbl

                    for row in data_rows:
                        if not row:
                            continue
                        cells = [str(c or "").strip() for c in row]

                        if col["date"]:
                            hi = header.index(col["date"].lower().strip()) if col["date"].lower().strip() in header else 0
                            date_s = _try_parse_date_with_year(cells[hi] if hi < len(cells) else "", year)
                        else:
                            # Heuristic: first cell that looks like a date
                            date_s = None
                            for cell in cells[:3]:
                                if DATE_RE.match(cell):
                                    date_s = _try_parse_date_with_year(cell, year)
                                    break

                        if not date_s:
                            continue

                        # Description: prefer mapped col, else second non-date cell
                        if col["desc"]:
                            di = header.index(col["desc"].lower().strip()) if col["desc"].lower().strip() in header else 1
                            desc = cells[di] if di < len(cells) else ""
                        else:
                            desc = cells[1] if len(cells) > 1 else ""

                        # Amount
                        if col["amount"]:
                            ai = header.index(col["amount"].lower().strip()) if col["amount"].lower().strip() in header else -1
                            amt = _parse_amt_pdf(cells[ai]) if 0 <= ai < len(cells) else None
                        elif col["debit"] or col["credit"]:
                            di2 = header.index(col["debit"].lower().strip())  if col["debit"]  and col["debit"].lower().strip()  in header else -1
                            ci2 = header.index(col["credit"].lower().strip()) if col["credit"] and col["credit"].lower().strip() in header else -1
                            dv = _parse_amt_pdf(cells[di2]) if 0 <= di2 < len(cells) else 0.0
                            cv = _parse_amt_pdf(cells[ci2]) if 0 <= ci2 < len(cells) else 0.0
                            amt = (abs(dv or 0) - abs(cv or 0)) or None
                        else:
                            # Last numeric-looking cell
                            amt = None
                            for cell in reversed(cells):
                                v = _parse_amt_pdf(cell)
                                if v is not None:
                                    amt = v
                                    break

                        if amt is None or desc == "":
                            continue
                        txs.append({"date": date_s, "description": desc[:80], "amount": round(amt, 2)})

                # ── Text fallback: line-by-line regex ──────────────────────
                if not tables:
                    text = page.extract_text() or ""
                    for line in text.splitlines():
                        line = line.strip()
                        m_date = DATE_RE.match(line)
                        if not m_date:
                            continue
                        m_amt = AMT_RE2.search(line)
                        if not m_amt:
                            continue
                        date_s = _try_parse_date_with_year(m_date.group(1), year)
                        if not date_s:
                            continue
                        raw_val = m_amt.group(1).replace(",", "")
                        is_cr   = bool(m_amt.group(2))  # trailing "-"
                        try:
                            amt = float(raw_val) * (-1 if is_cr else 1)
                        except ValueError:
                            continue
                        # Description = everything between date and amount
                        remainder = line[m_date.end():m_amt.start()].strip()
                        # Strip a second date (post date) if present
                        remainder = re.sub(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s*", "", remainder)
                        desc = remainder.strip()
                        if not desc:
                            continue
                        txs.append({"date": date_s, "description": desc[:80], "amount": round(amt, 2)})

    except Exception as exc:
        return [], [f"PDF parse error: {exc}"]

    # Deduplicate exact duplicates that can appear from overlapping table/text extraction
    seen: set = set()
    deduped = []
    for t in txs:
        key = (t["date"], t["description"], t["amount"])
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    deduped.sort(key=lambda x: x["date"])
    if not deduped:
        errors.append(
            "No transactions found in PDF. "
            "Ensure this is a credit card statement with a date + description + amount on each row."
        )
    return deduped, errors


def _extract_balance_from_pdf(file_bytes: bytes) -> dict:
    """
    Extract closing balance and statement date from a Synchrony or Schwab PDF.
    Returns {"balance": float|None, "statement_date": str|None, "label": str}
    """
    try:
        import pdfplumber
    except ImportError:
        return {"balance": None, "statement_date": None, "label": ""}

    import io as _io, re

    # (pattern, label) — searched case-insensitively against full text
    BALANCE_PATTERNS = [
        (r"new\s+balance\s+\$?([\d,]+\.\d{2})",               "New Balance"),
        (r"statement\s+balance\s*[:\-]?\s*\$?([\d,]+\.\d{2})", "Statement Balance"),
        (r"total\s+account\s+value\s*[:\-]?\s*\$?([\d,]+\.\d{2})", "Total Account Value"),
        (r"net\s+account\s+value\s*[:\-]?\s*\$?([\d,]+\.\d{2})",   "Net Account Value"),
        (r"total\s+market\s+value\s*[:\-]?\s*\$?([\d,]+\.\d{2})",  "Total Market Value"),
        (r"total\s+portfolio\s+value\s*[:\-]?\s*\$?([\d,]+\.\d{2})","Total Portfolio Value"),
        (r"portfolio\s+value\s*[:\-]?\s*\$?([\d,]+\.\d{2})",        "Portfolio Value"),
        (r"ending\s+balance\s*[:\-]?\s*\$?([\d,]+\.\d{2})",         "Ending Balance"),
        (r"closing\s+balance\s*[:\-]?\s*\$?([\d,]+\.\d{2})",        "Closing Balance"),
        (r"account\s+balance\s*[:\-]?\s*\$?([\d,]+\.\d{2})",        "Account Balance"),
    ]
    DATE_PATTERNS = [
        r"statement\s+(?:closing\s+)?date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        r"closing\s+date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        r"statement\s+date\s*[:\-]?\s*(\w+ \d{1,2},\s*\d{4})",
        r"(?:for\s+the\s+period|statement\s+period).*?to\s+(\w+ \d{1,2},\s*\d{4})",
        r"(?:for\s+the\s+period|statement\s+period).*?to\s+(\d{1,2}/\d{1,2}/\d{2,4})",
        r"as\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})",
        r"as\s+of\s+(\w+ \d{1,2},\s*\d{4})",
    ]

    all_text = ""
    try:
        with pdfplumber.open(_io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                all_text += (page.extract_text() or "") + "\n"
    except Exception as exc:
        return {"balance": None, "statement_date": None, "label": "", "error": str(exc)}

    lower = all_text.lower()
    balance, label = None, ""
    for pat, lbl in BALANCE_PATTERNS:
        m = re.search(pat, lower)
        if m:
            try:
                balance = float(m.group(1).replace(",", ""))
                label = lbl
                break
            except ValueError:
                continue

    stmt_date = None
    for pat in DATE_PATTERNS:
        m = re.search(pat, lower)
        if m:
            stmt_date = _parse_date_s(m.group(1))
            if stmt_date:
                break

    return {"balance": balance, "statement_date": stmt_date, "label": label}


def _ensure_manual_balance_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_account_balances (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            institution    TEXT NOT NULL,
            account_name   TEXT NOT NULL,
            account_type   TEXT NOT NULL,
            balance        REAL NOT NULL,
            statement_date TEXT,
            source_file    TEXT,
            uploaded_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add payment_due_date to pre-existing tables.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(manual_account_balances)")}
    if "payment_due_date" not in cols:
        conn.execute("ALTER TABLE manual_account_balances ADD COLUMN payment_due_date TEXT")
    conn.commit()


@reports_bp.route("/api/statement/extract_balance", methods=["POST"])
@login_required
def api_extract_balance():
    """Parse a statement PDF and return the detected balance without saving."""
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    file_bytes = f.read()
    if not (f.filename.lower().endswith(".pdf") or file_bytes[:4] == b"%PDF"):
        return jsonify({"error": "Only PDF files are supported for balance extraction"}), 400
    result = _extract_balance_from_pdf(file_bytes)
    result["filename"] = f.filename
    return jsonify(result)


@reports_bp.route("/api/statement/balance", methods=["POST"])
@login_required
def api_save_statement_balance():
    """Save a manually supplied or auto-extracted account balance."""
    # Accepts either JSON or multipart form (with optional file)
    if request.content_type and request.content_type.startswith("application/json"):
        body = request.get_json() or {}
        institution   = (body.get("institution") or "").strip()
        account_name  = (body.get("account_name") or "").strip()
        account_type  = (body.get("account_type") or "credit").strip().lower()
        balance       = body.get("balance")
        stmt_date     = (body.get("statement_date") or "").strip() or None
        due_date      = (body.get("payment_due_date") or "").strip() or None
        source_file   = (body.get("source_file") or "").strip() or None
    else:
        institution   = (request.form.get("institution") or "").strip()
        account_name  = (request.form.get("account_name") or "").strip()
        account_type  = (request.form.get("account_type") or "credit").strip().lower()
        raw_balance   = request.form.get("balance")
        stmt_date     = (request.form.get("statement_date") or "").strip() or None
        due_date      = (request.form.get("payment_due_date") or "").strip() or None
        source_file   = None
        balance       = None

        # If a PDF was uploaded, try auto-extraction first
        if "file" in request.files and request.files["file"].filename:
            f = request.files["file"]
            file_bytes = f.read()
            source_file = f.filename
            if f.filename.lower().endswith(".pdf") or file_bytes[:4] == b"%PDF":
                extracted = _extract_balance_from_pdf(file_bytes)
                if extracted["balance"] is not None:
                    balance = extracted["balance"]
                    if not stmt_date and extracted["statement_date"]:
                        stmt_date = extracted["statement_date"]

        if balance is None and raw_balance:
            try:
                balance = float(str(raw_balance).replace(",", "").replace("$", ""))
            except ValueError:
                pass

    if not institution:
        return jsonify({"error": "institution required"}), 400
    if not account_name:
        return jsonify({"error": "account_name required"}), 400
    if balance is None:
        return jsonify({"error": "balance could not be determined — enter it manually"}), 400

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        _ensure_manual_balance_table(conn)
        conn.execute(
            "INSERT INTO manual_account_balances "
            "(institution, account_name, account_type, balance, statement_date, payment_due_date, source_file) "
            "VALUES (?,?,?,?,?,?,?)",
            (institution, account_name, account_type, float(balance), stmt_date, due_date, source_file),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "balance": balance, "statement_date": stmt_date, "payment_due_date": due_date})


@reports_bp.route("/api/statement/balances")
@login_required
def api_list_statement_balances():
    """Return the latest balance entry for each (institution, account_name) pair."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        _ensure_manual_balance_table(conn)
        rows = conn.execute("""
            SELECT id, institution, account_name, account_type,
                   balance, statement_date, payment_due_date, source_file, uploaded_at
            FROM manual_account_balances
            WHERE id IN (
                SELECT MAX(id) FROM manual_account_balances
                GROUP BY institution, account_name
            )
            ORDER BY institution, account_name
        """).fetchall()
        conn.commit()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@reports_bp.route("/api/statement/balance/<int:balance_id>", methods=["DELETE"])
@login_required
def api_delete_statement_balance(balance_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        conn.execute("DELETE FROM manual_account_balances WHERE id = ?", (balance_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


def _reconcile(stmt_txs: list, db_txs: list) -> dict:
    """Match by abs(amount) within $0.02 and date within 2 days."""
    TOLERANCE = 0.02
    pool = list(db_txs)
    matched, only_stmt = [], []

    for s in stmt_txs:
        s_abs = abs(s["amount"])
        best_i, best_score = None, -1

        for i, d in enumerate(pool):
            try:
                day_delta = abs((date.fromisoformat(s["date"]) -
                                 date.fromisoformat(d["date"])).days)
            except Exception:
                continue
            if day_delta > 2:
                continue
            if abs(abs(d["amount"]) - s_abs) > TOLERANCE:
                continue
            score = 10 - day_delta
            # Bonus for description overlap
            s_w = s["description"].lower()[:8]
            d_w = (d.get("name") or "").lower()[:8]
            if s_w and d_w and s_w == d_w:
                score += 3
            if score > best_score:
                best_score, best_i = score, i

        if best_i is not None:
            matched.append({"statement": s, "db": pool.pop(best_i)})
        else:
            only_stmt.append(s)

    n = len(stmt_txs)
    return {
        "matched":            matched,
        "only_statement":     only_stmt,
        "only_db":            pool,
        "summary": {
            "statement_count":      n,
            "db_count":             len(db_txs),
            "matched_count":        len(matched),
            "only_statement_count": len(only_stmt),
            "only_db_count":        len(pool),
            "match_rate":           round(len(matched) / max(n, 1) * 100, 1),
        },
    }


@reports_bp.route("/accounts")
@reports_bp.route("/reconcile")   # legacy alias
@login_required
def reconcile_page():
    return render_template("reconcile.html")


@reports_bp.route("/api/reconcile/history")
@login_required
def api_reconcile_history():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        _ensure_statement_tables(conn)
        rows = conn.execute(
            "SELECT id, filename, account_type, institution, upload_ts, "
            "       date_start, date_end, tx_count "
            "FROM uploaded_statements ORDER BY upload_ts DESC LIMIT 50"
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@reports_bp.route("/api/reconcile/upload", methods=["POST"])
@login_required
def api_reconcile_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400

    account_type = request.form.get("account_type", "checking").lower()
    institution  = request.form.get("institution", "").strip()

    file_bytes = f.read()
    is_pdf = (f.filename or "").lower().endswith(".pdf") or file_bytes[:4] == b"%PDF"
    stmt_txs, parse_errors = (
        _parse_statement_pdf(file_bytes) if is_pdf else _parse_statement_csv(file_bytes)
    )
    if not stmt_txs:
        return jsonify({
            "error": parse_errors[0] if parse_errors else "No transactions found",
            "parse_errors": parse_errors,
        }), 400

    dates      = [t["date"] for t in stmt_txs]
    date_start = min(dates)
    date_end   = max(dates)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    db_txs: list = []
    statement_id = None

    try:
        _ensure_statement_tables(conn)

        cur = conn.execute(
            "INSERT INTO uploaded_statements"
            "(filename, account_type, institution, date_start, date_end, tx_count)"
            " VALUES(?,?,?,?,?,?)",
            (f.filename, account_type, institution, date_start, date_end, len(stmt_txs)),
        )
        statement_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO statement_transactions(statement_id, date, description, amount)"
            " VALUES(?,?,?,?)",
            [(statement_id, t["date"], t["description"], t["amount"]) for t in stmt_txs],
        )
        conn.commit()

        # Pull DB transactions for the same window
        if account_type in ("checking", "savings"):
            rows = conn.execute("""
                SELECT t.transaction_id, t.date, t.name, t.merchant_name,
                       t.amount, a.name acct_name
                FROM transactions t
                JOIN accounts a ON a.account_id = t.account_id
                WHERE a.subtype = ? AND t.date BETWEEN ? AND ?
                  AND IFNULL(t.pending,0)=0
                ORDER BY t.date
            """, (account_type, date_start, date_end)).fetchall()
        elif account_type == "credit":
            rows = conn.execute("""
                SELECT t.transaction_id, t.date, t.name, t.merchant_name,
                       t.amount, a.name acct_name
                FROM transactions t
                JOIN accounts a ON a.account_id = t.account_id
                WHERE a.type = 'credit' AND t.date BETWEEN ? AND ?
                  AND IFNULL(t.pending,0)=0
                ORDER BY t.date
            """, (date_start, date_end)).fetchall()
        else:
            rows = []  # mortgage/retirement — no Plaid data

        for r in rows:
            db_txs.append({
                "transaction_id": r["transaction_id"],
                "date":    r["date"],
                "name":    (r["merchant_name"] or r["name"] or "")[:60],
                "amount":  float(r["amount"]),
                "account": r["acct_name"],
            })
    finally:
        conn.close()

    result = _reconcile(stmt_txs, db_txs)
    result["parse_errors"]  = parse_errors
    result["date_range"]    = {"start": date_start, "end": date_end}
    result["account_type"]  = account_type
    result["filename"]      = f.filename
    result["statement_id"]  = statement_id
    return jsonify(result)


# ---------------------------------------------------------------------------
# Transaction management + classification rules
# ---------------------------------------------------------------------------

_COMMON_CATEGORIES = [
    "Bank Fees", "Bills & Utilities", "Coffee & Tea",
    "Credit Card Payment", "Education", "Entertainment",
    "Food & Drink", "Gas", "Gifts & Donations", "Groceries",
    "Health & Medical", "Home Improvement", "Housing",
    "Income", "Insurance", "Investment",
    "Loan Payments", "Personal Care", "Pets", "Pharmacy",
    "Restaurants", "Savings", "Services", "Shopping",
    "Streaming", "Subscriptions", "Transfer In", "Transfer Out",
    "Transportation", "Travel", "Other",
]


@reports_bp.route("/transactions")
@login_required
def transactions_page():
    return render_template("transactions.html")


@reports_bp.route("/api/transactions")
@login_required
def api_transactions():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    q        = (request.args.get("q") or "").strip().lower()
    cat      = (request.args.get("category") or "").strip()
    acct_sub = (request.args.get("account_subtype") or "").strip()
    acct_id  = (request.args.get("account_id") or "").strip()
    month    = (request.args.get("month") or "").strip()
    start    = (request.args.get("start") or "").strip()
    end      = (request.args.get("end") or "").strip()

    amt_min  = request.args.get("amt_min")
    amt_max  = request.args.get("amt_max")

    filters, params = ["IFNULL(t.pending, 0) = 0"], []

    if q:
        filters.append("(LOWER(COALESCE(t.merchant_name,'')) LIKE ? OR LOWER(t.name) LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if cat == "__none__":
        filters.append("(tc.user_category IS NULL OR tc.user_category = '')")
    elif cat:
        filters.append("tc.user_category = ?"); params.append(cat)
    if acct_sub:
        filters.append("a.subtype = ?"); params.append(acct_sub)
    if acct_id:
        filters.append("a.account_id = ?"); params.append(acct_id)
    if month:
        filters.append("t.date LIKE ?"); params.append(f"{month}%")
    elif start and end:
        filters.append("t.date BETWEEN ? AND ?"); params += [start, end]
    try:
        if amt_min:
            filters.append("ABS(t.amount) >= ?"); params.append(float(amt_min))
        if amt_max:
            filters.append("ABS(t.amount) <= ?"); params.append(float(amt_max))
    except ValueError:
        pass

    where = "WHERE " + " AND ".join(filters)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(f"""
            SELECT COUNT(*) FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            {where}
        """, params).fetchone()[0]

        rows = conn.execute(f"""
            SELECT t.transaction_id, t.date, t.name, t.merchant_name, t.amount,
                   a.name AS account_name, a.type AS account_type, a.subtype,
                   COALESCE(tc.user_category,'')  AS user_category,
                   COALESCE(tc.user_subcategory,'') AS user_subcategory,
                   COALESCE(tc.exclude_from_spend, 0) AS exclude_from_spend,
                   COALESCE(tc.merchant_normalized,
                       LOWER(COALESCE(t.merchant_name, t.name, ''))) AS merchant_norm
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            {where}
            ORDER BY t.date DESC, t.transaction_id
            LIMIT ? OFFSET ?
        """, params + [per_page, (page - 1) * per_page]).fetchall()
    finally:
        conn.close()

    return jsonify({
        "transactions": [dict(r) for r in rows],
        "total":    total,
        "page":     page,
        "pages":    math.ceil(total / per_page) if total else 1,
        "per_page": per_page,
    })


@reports_bp.route("/api/transactions/categories")
@login_required
def api_transaction_categories():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_category FROM transaction_classifications "
            "WHERE user_category IS NOT NULL AND user_category != '' "
            "ORDER BY user_category"
        ).fetchall()
    finally:
        conn.close()
    db_cats = [r["user_category"] for r in rows]
    merged  = sorted(set(_COMMON_CATEGORIES) | set(db_cats))
    return jsonify(merged)


@reports_bp.route("/api/transactions/accounts")
@login_required
def api_transaction_accounts():
    """Accounts that have transactions, for the specific-account filter dropdown."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT a.account_id, a.name, a.subtype, a.type, a.mask,
                   i.institution_name,
                   COUNT(t.transaction_id) AS n
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            JOIN transactions t ON t.account_id = a.account_id
            GROUP BY a.account_id
            ORDER BY i.institution_name, a.type, a.name
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        mask = f" ••{r['mask']}" if r["mask"] else ""
        out.append({
            "account_id": r["account_id"],
            "label":      f"{r['institution_name']} · {r['name']}{mask}",
            "subtype":    r["subtype"],
            "count":      r["n"],
        })
    return jsonify(out)


# ===================================================================
# Flow-type model (Stage 2 — read-only report; nothing else consumes it yet)
# -------------------------------------------------------------------
# A single per-transaction "flow type" so true expense / savings /
# investment / income can be reported per-account AND overall, with
# internal money movement netted out and refunds netted against expense.
# ===================================================================

_FLOW_TYPES = ("expense", "income", "refund", "savings", "investment", "internal_transfer")

# Authoritative recurring external-transfer destinations leaving a spending
# account (Plaid mislabels several of these as credit-card payments). Matched on
# the transaction name; specific enough not to catch the matching card payments
# ("Capital One Transfer" vs "Capital One CrCardPmt", "…Transfer to SV" vs
# "…to CC", "Synchrony Bank Transfer" vs "AMZ_STORECRD_PMT").
# Will be persisted as classification_rules in Stage 3.
# Matched in BOTH directions: leaving checking = contribution, returning to
# checking = withdrawal — so savings/investment can be netted (out − back).
_INVEST_DEST  = ("schwab brokerage moneylink", "edward jones", "transfer from brokerage")
_SAVINGS_DEST = ("synchrony bank", "online transfer to sv", "online transfer from sv",
                 "capital one transfer", "schwab bank transfer", "american airlines")


def _external_transfer_bucket(name: str):
    n = (name or "").lower()
    if any(p in n for p in _INVEST_DEST):
        return "investment"
    if any(p in n for p in _SAVINGS_DEST):
        return "savings"
    return None


def _derive_flow_type(acct_type, subtype, amount, user_category, exclude_reason, pfc_detailed, name="") -> str:
    """Map one transaction to a single flow type. Sign convention (Plaid):
    positive = money OUT of the account, negative = money IN.

    Principles:
      - Savings/investment is counted ONCE, on the leg leaving a spending
        account (checking/credit). That's the only place an unlinked
        destination (e.g. Synchrony) is even visible.
      - Activity ON a savings/brokerage account is therefore internal movement
        (already counted at the source), except interest/dividends = income.
      - The user's explicit category beats Plaid's guessed exclude_reason
        (Plaid mislabels Synchrony transfers as pfc_credit_card_payment)."""
    cat    = (user_category or "").lower()
    reason = (exclude_reason or "").lower()
    pfcd   = (pfc_detailed or "").upper()
    at     = (acct_type or "").lower()
    sub    = (subtype or "").lower()
    amt    = float(amount or 0)

    nm = (name or "").lower()

    # Money moving through a savings / brokerage / retirement account is the
    # receiving (or internal) leg — net it out. Keep interest/dividends as income.
    if sub == "savings" or at == "investment":
        return "income" if cat == "income" else "internal_transfer"

    # Checking-account specifics (depository).
    if at == "depository":
        # Received Zelle = money in from a person → income.
        if "received zelle" in nm and amt < 0:
            return "income"
        # Known external savings/investment destinations — matched in BOTH
        # directions so withdrawals (money back into checking) net against
        # contributions. Authoritative: overrides Plaid's mislabeled reason.
        b = _external_transfer_bucket(nm)
        if b:
            return b

    # --- spending accounts (checking / credit): explicit category wins ---
    if cat == "savings":
        return "savings"
    if cat == "investment":
        return "investment"
    if cat == "income":
        return "refund" if at == "credit" else "income"
    if cat in ("transfer in", "transfer out", "loan payments"):
        return "internal_transfer"

    # Credit-card mechanics
    if reason == "pfc_credit_card_payment" or "CREDIT_CARD_PAYMENT" in pfcd:
        return "internal_transfer"
    if at == "credit" and amt < 0:
        return "refund"
    if at == "credit" and amt > 0 and cat in ("savings", "investment"):
        return "expense"

    # Reason-based fallbacks when there is no explicit category
    if reason == "investment_transfer":
        return "investment"
    if reason == "savings_transfer_out":
        return "savings"
    if reason in ("transfer_in_excluded", "pfc_transfer_with_transfer_text",
                  "text_looks_like_payment_or_transfer"):
        return "internal_transfer"
    if "REFUND" in pfcd or "RETURN" in pfcd:
        return "refund"
    if at == "depository" and amt < 0:
        return "income"
    return "expense"


@reports_bp.route("/api/flow_type/report")
@login_required
def api_flow_type_report():
    """Read-only verification report: flow-type breakdown overall + per account."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT t.amount, t.name AS tx_name,
                   a.account_id, a.name AS acct_name, a.type AS acct_type,
                   a.subtype, i.institution_name,
                   tc.user_category, tc.exclude_reason,
                   tm.payload_json
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            JOIN items i ON i.item_id = a.item_id
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            LEFT JOIN transaction_meta tm ON tm.transaction_id = t.transaction_id
            WHERE IFNULL(t.pending, 0) = 0
        """).fetchall()
    finally:
        conn.close()

    # savings/investment are tracked NET: contributions (money leaving checking,
    # amt>0) minus withdrawals (money coming back, amt<0). Other types use abs.
    _NET = ("savings", "investment")

    def _blank():
        d = {ft: 0.0 for ft in _FLOW_TYPES}
        d.update(savings_out=0.0, savings_in=0.0, investment_out=0.0, investment_in=0.0)
        return d

    overall = _blank()
    per_acct: dict = {}
    for r in rows:
        pfc_detailed = ""
        if r["payload_json"]:
            try:
                pfc = (json.loads(r["payload_json"]).get("personal_finance_category") or {})
                pfc_detailed = pfc.get("detailed") or ""
            except Exception:
                pass
        ft = _derive_flow_type(r["acct_type"], r["subtype"], r["amount"],
                               r["user_category"], r["exclude_reason"], pfc_detailed,
                               r["tx_name"])
        amt = float(r["amount"] or 0)
        key = r["account_id"]
        if key not in per_acct:
            per_acct[key] = {
                "account": f"{r['institution_name']} · {r['acct_name']}",
                "type": r["acct_type"], "subtype": r["subtype"], **_blank(),
            }
        acct = per_acct[key]
        if ft in _NET:
            overall[ft] += amt          # signed net (out +, back −)
            acct[ft]    += amt
            side = "_out" if amt >= 0 else "_in"
            overall[ft + side] += abs(amt)
            acct[ft + side]    += abs(amt)
        else:
            overall[ft] += abs(amt)
            acct[ft]    += abs(amt)

    def _finalize(d):
        d = {**d}
        d["expense_net"] = round(d["expense"] - d["refund"], 2)
        for k in list(d.keys()):
            if isinstance(d[k], float):
                d[k] = round(d[k], 2)
        return d

    accounts = sorted(
        (_finalize(v) for v in per_acct.values()),
        key=lambda x: (-(x["expense_net"] + x["savings"] + x["investment"])),
    )
    return jsonify({
        "overall": _finalize(overall),
        "accounts": accounts,
        "note": "Read-only. expense_net = expense − refunds. savings/investment "
                "are NET (contributions − withdrawals); *_out/*_in show the gross "
                "sides. internal_transfer = movement between your own accounts.",
    })


@reports_bp.route("/api/transactions/classify", methods=["POST"])
@login_required
def api_classify_transaction():
    body           = request.get_json() or {}
    tx_id          = body.get("transaction_id")
    category       = body.get("category") or None
    subcategory    = body.get("subcategory") or None
    exclude        = bool(body.get("exclude_from_spend", False))
    create_rule    = bool(body.get("create_rule", False))
    rule_value     = (body.get("rule_match_value") or "").strip().lower()
    apply_existing = bool(body.get("apply_to_existing", False))
    # None = any amount; float = exact amount match (±$0.02)
    raw_amt_exact  = body.get("amount_exact")
    amount_exact: "float | None" = None
    if raw_amt_exact is not None:
        try:
            amount_exact = float(raw_amt_exact)
        except (TypeError, ValueError):
            pass

    if not tx_id:
        return jsonify({"error": "transaction_id required"}), 400

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    rule_id = None
    affected = 0
    try:
        tx = conn.execute(
            "SELECT LOWER(COALESCE(merchant_name, name, '')) AS mn, amount FROM transactions WHERE transaction_id = ?",
            (tx_id,)
        ).fetchone()
        merchant_norm = tx["mn"] if tx else rule_value

        conn.execute("""
            INSERT INTO transaction_classifications
            (transaction_id, user_category, user_subcategory, exclude_from_spend, merchant_normalized, updated_at)
            VALUES (?,?,?,?,?,datetime('now'))
            ON CONFLICT(transaction_id) DO UPDATE SET
                user_category       = excluded.user_category,
                user_subcategory    = excluded.user_subcategory,
                exclude_from_spend  = excluded.exclude_from_spend,
                merchant_normalized = excluded.merchant_normalized,
                updated_at          = excluded.updated_at
        """, (tx_id, category, subcategory, 1 if exclude else 0, merchant_norm))

        if create_rule and rule_value:
            cur = conn.execute("""
                INSERT INTO classification_rules
                (enabled, priority, match_field, match_op, match_value,
                 user_category, user_subcategory, exclude_from_spend, amount_exact,
                 created_at, updated_at)
                VALUES (1, 100, 'merchant_normalized', 'contains', ?, ?, ?, ?, ?,
                        datetime('now'), datetime('now'))
            """, (rule_value, category, subcategory, 1 if exclude else 0, amount_exact))
            rule_id = cur.lastrowid

            if apply_existing:
                # Amount filter clause — only applies when amount_exact is set
                amt_clause = "AND ABS(t.amount - ?) <= 0.02" if amount_exact is not None else ""
                amt_param  = [amount_exact] if amount_exact is not None else []

                affected = conn.execute(f"""
                    UPDATE transaction_classifications
                    SET user_category=?, user_subcategory=?, exclude_from_spend=?, updated_at=datetime('now')
                    WHERE transaction_id != ?
                      AND LOWER(COALESCE(merchant_normalized,'')) LIKE ?
                      AND transaction_id IN (
                          SELECT t.transaction_id FROM transactions t
                          WHERE t.transaction_id = transaction_classifications.transaction_id
                          {amt_clause}
                      )
                """, [category, subcategory, 1 if exclude else 0, tx_id, f"%{rule_value}%"] + amt_param).rowcount

                affected += conn.execute(f"""
                    INSERT INTO transaction_classifications
                    (transaction_id, user_category, user_subcategory, exclude_from_spend, merchant_normalized, updated_at)
                    SELECT t.transaction_id, ?, ?, ?, LOWER(COALESCE(t.merchant_name,t.name,'')), datetime('now')
                    FROM transactions t
                    LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
                    WHERE tc.transaction_id IS NULL
                      AND LOWER(COALESCE(t.merchant_name,t.name,'')) LIKE ?
                      AND t.transaction_id != ?
                      {amt_clause}
                """, [category, subcategory, 1 if exclude else 0, f"%{rule_value}%", tx_id] + amt_param).rowcount

        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "rule_id": rule_id, "affected": affected})


@reports_bp.route("/api/classification_rules")
@login_required
def api_list_rules():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT rule_id, enabled, priority, match_field, match_op, match_value, "
            "user_category, user_subcategory, exclude_from_spend, created_at "
            "FROM classification_rules ORDER BY priority, created_at"
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@reports_bp.route("/api/classification_rules/<int:rule_id>", methods=["DELETE"])
@login_required
def api_delete_rule(rule_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        conn.execute("DELETE FROM classification_rules WHERE rule_id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@reports_bp.route("/merchants")
@login_required
def merchants_page():
    return render_template("merchants.html")


@reports_bp.route("/api/merchants")
@login_required
def api_merchants():
    """Aggregate transactions by merchant with category, count, total, last date."""
    account_id = (request.args.get("account_id") or "").strip()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT
                COALESCE(t.merchant_name, t.name, '(unknown)') AS merchant,
                LOWER(COALESCE(t.merchant_name, t.name, ''))   AS merchant_key,
                COUNT(*)                                         AS tx_count,
                SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS total_out,
                SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount) ELSE 0 END) AS total_in,
                MAX(t.date)                                      AS last_date,
                -- mode category: most recent non-null user_category for this merchant
                (SELECT tc2.user_category
                 FROM transactions t2
                 LEFT JOIN transaction_classifications tc2 ON tc2.transaction_id = t2.transaction_id
                 WHERE LOWER(COALESCE(t2.merchant_name, t2.name, ''))
                       = LOWER(COALESCE(t.merchant_name, t.name, ''))
                   AND tc2.user_category IS NOT NULL AND tc2.user_category != ''
                 ORDER BY t2.date DESC LIMIT 1
                ) AS category,
                MAX(COALESCE(tc.exclude_from_spend, 0))          AS any_excluded
            FROM transactions t
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            WHERE IFNULL(t.pending, 0) = 0
              AND (? = '' OR t.account_id = ?)
            GROUP BY LOWER(COALESCE(t.merchant_name, t.name, ''))
            ORDER BY total_out DESC
        """, (account_id, account_id)).fetchall()
    finally:
        conn.close()

    return jsonify([{
        "merchant":    r["merchant"],
        "merchant_key": r["merchant_key"],
        "tx_count":    r["tx_count"],
        "total_out":   round(float(r["total_out"] or 0), 2),
        "total_in":    round(float(r["total_in"]  or 0), 2),
        "last_date":   r["last_date"] or "",
        "category":    r["category"] or "",
        "any_excluded":bool(r["any_excluded"]),
    } for r in rows])


@reports_bp.route("/api/merchants/classify", methods=["POST"])
@login_required
def api_classify_merchant():
    """Set category + optionally create rule for ALL transactions of a merchant."""
    body         = request.get_json() or {}
    merchant_key = (body.get("merchant_key") or "").strip().lower()
    category     = body.get("category") or None
    exclude      = bool(body.get("exclude_from_spend", False))
    create_rule  = bool(body.get("create_rule", True))

    if not merchant_key:
        return jsonify({"error": "merchant_key required"}), 400

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    updated = 0
    rule_id = None
    try:
        # Get all matching transaction_ids
        tx_ids = [r[0] for r in conn.execute("""
            SELECT t.transaction_id FROM transactions t
            WHERE LOWER(COALESCE(t.merchant_name, t.name, '')) LIKE ?
              AND IFNULL(t.pending, 0) = 0
        """, (f"%{merchant_key}%",)).fetchall()]

        for tx_id in tx_ids:
            conn.execute("""
                INSERT INTO transaction_classifications
                (transaction_id, user_category, exclude_from_spend, merchant_normalized, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(transaction_id) DO UPDATE SET
                    user_category      = excluded.user_category,
                    exclude_from_spend = excluded.exclude_from_spend,
                    merchant_normalized= excluded.merchant_normalized,
                    updated_at         = excluded.updated_at
            """, (tx_id, category, 1 if exclude else 0, merchant_key))
        updated = len(tx_ids)

        if create_rule and merchant_key:
            cur = conn.execute("""
                INSERT INTO classification_rules
                (enabled, priority, match_field, match_op, match_value,
                 user_category, exclude_from_spend, created_at, updated_at)
                VALUES (1, 100, 'merchant_normalized', 'contains', ?, ?, ?, datetime('now'), datetime('now'))
            """, (merchant_key, category, 1 if exclude else 0))
            rule_id = cur.lastrowid

        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "updated": updated, "rule_id": rule_id})


@reports_bp.route("/api/debug_state")
@login_required
def api_debug_state():
    df = _load_tx_detail()
    return jsonify({
        "env_target": ENV_TARGET,
        "db_path": str(DB_PATH),
        "tx_rows": int(len(df)),
        "columns": list(df.columns) if not df.empty else [],
    })


# # -------------------------------------------------------------------
# # M5: Password generator (utility only; no vault storage)
# # -------------------------------------------------------------------
# @reports_bp.route("/api/password_generate")
# @login_required
# def api_password_generate():
#     """
#     Generate a strong random password.
#     Query params:
#       length (default 16), special=true|false
#     """
#     length = max(8, min(128, int(request.args.get("length", "16"))))
#     use_special = request.args.get("special", "true").lower() != "false"

#     letters = string.ascii_letters
#     digits = string.digits
#     specials = "!@#$%^&*()-_=+[]{};:,.?/"

#     pool = letters + digits + (specials if use_special else "")
#     if not pool:
#         pool = letters + digits

#     # Guarantee at least one from each class when possible
#     pw = []
#     pw.append(random.choice(string.ascii_lowercase))
#     pw.append(random.choice(string.ascii_uppercase))
#     pw.append(random.choice(string.digits))
#     if use_special:
#         pw.append(random.choice(specials))
#     while len(pw) < length:
#         pw.append(random.choice(pool))
#     random.shuffle(pw)
#     return jsonify({"password": "".join(pw[:length])})


# ===================================================================
# Savings Goals
# -------------------------------------------------------------------
# Tracks progress toward a year-end savings target by measuring the
# real balance growth of savings-subtype depository accounts. A goal
# snapshots the total savings balance at creation (start_balance);
# progress = current total savings - start_balance.
# ===================================================================

def _ensure_savings_goals_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            target_amount  REAL NOT NULL,
            start_date     TEXT NOT NULL,
            end_date       TEXT NOT NULL,
            start_balance  REAL NOT NULL,
            status         TEXT DEFAULT 'active',
            achieved_at    TEXT,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _goals_connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


def _savings_accounts(conn):
    """All savings-subtype depository accounts with a known current balance."""
    return conn.execute("""
        SELECT account_id, name, current
        FROM accounts
        WHERE type = 'depository' AND subtype = 'savings'
          AND current IS NOT NULL
    """).fetchall()


def _total_savings_now(conn) -> float:
    return round(sum(float(r["current"]) for r in _savings_accounts(conn)), 2)


def _savings_balance_series(conn, start: date, end: date):
    """
    Reconstruct the total savings balance (summed across all savings
    accounts) for each day in [start, end]. Same technique as
    /api/account_history: balance_on_d = current - (txns dated after d).
    Returns a list of (YYYY-MM-DD, balance) tuples.
    """
    accts = _savings_accounts(conn)
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)

    series = {ds: 0.0 for ds in days}
    for acct in accts:
        rows = conn.execute("""
            SELECT date, SUM(amount) AS day_amt
            FROM transactions
            WHERE account_id = ? AND IFNULL(pending, 0) = 0
            GROUP BY date ORDER BY date ASC
        """, (acct["account_id"],)).fetchall()
        daily = {r["date"]: float(r["day_amt"]) for r in rows}
        all_dates_sorted = sorted(daily.keys())
        total_all = sum(daily.values())
        current_bal = float(acct["current"])

        running = 0.0
        for ds in all_dates_sorted:
            if date.fromisoformat(ds) < start:
                running += daily[ds]
            else:
                break

        for ds in days:
            if ds in daily:
                running += daily[ds]
            series[ds] += current_bal - (total_all - running)

    return [(ds, round(series[ds], 2)) for ds in days]


def _savings_balance_on(conn, on_day: date) -> float:
    """Total savings balance reconstructed for a single past date."""
    ser = _savings_balance_series(conn, on_day, on_day)
    return ser[0][1] if ser else _total_savings_now(conn)


def _round_nice(amount: float, step: int = 50) -> int:
    """Round to the nearest tidy figure for a friendly goal number."""
    if amount <= 0:
        return 0
    return int(round(amount / step) * step)


def _year_end(today: date) -> date:
    return date(today.year, 12, 31)


def _months_between(a: date, b: date) -> float:
    """Approximate whole+fractional months from a to b (>= a)."""
    if b <= a:
        return 0.0
    return max((b - a).days / 30.44, 0.0)


def _compute_goal_stats(goal, current_total: float) -> dict:
    """Live progress + pacing for one goal row."""
    today      = date.today()
    start_dt   = date.fromisoformat(goal["start_date"])
    end_dt     = date.fromisoformat(goal["end_date"])
    target     = float(goal["target_amount"])
    start_bal  = float(goal["start_balance"])

    progress   = round(current_total - start_bal, 2)
    pct        = round(100.0 * progress / target, 1) if target > 0 else 0.0

    days_total   = max((end_dt - start_dt).days, 1)
    days_elapsed = min(max((today - start_dt).days, 0), days_total)
    # Linear "should be here by now" target line.
    expected     = round(target * days_elapsed / days_total, 2)
    on_track     = progress >= expected

    # Projection: extend current pace to the deadline.
    pace_per_day = progress / days_elapsed if days_elapsed > 0 else 0.0
    projected    = round(pace_per_day * days_total, 2)

    months_left  = _months_between(today, end_dt)
    remaining    = max(target - progress, 0.0)
    need_monthly = round(remaining / months_left, 2) if months_left > 0 else remaining

    achieved = progress >= target or goal["status"] == "achieved"

    return {
        "id":            goal["id"],
        "name":          goal["name"],
        "target_amount": round(target, 2),
        "start_date":    goal["start_date"],
        "end_date":      goal["end_date"],
        "start_balance": round(start_bal, 2),
        "current_total": round(current_total, 2),
        "progress":      progress,
        "pct":           max(min(pct, 999), -999),
        "expected":      expected,
        "on_track":      bool(on_track),
        "projected_eoy": projected,
        "need_monthly":  need_monthly,
        "days_elapsed":  days_elapsed,
        "days_total":    days_total,
        "achieved":      bool(achieved),
        "status":        "achieved" if achieved else goal["status"],
    }


@reports_bp.route("/goals")
@login_required
def goals_page():
    return render_template("goals.html")


@reports_bp.route("/api/goals/suggest")
@login_required
def api_goals_suggest():
    """
    Propose an attainable year-end savings target by measuring the
    user's real savings growth over a recent lookback window and
    extrapolating a conservative fraction to the deadline.
    """
    today    = date.today()
    end_dt   = _year_end(today)
    lookback = max(int(request.args.get("lookback_days", "90")), 30)

    conn = _goals_connect()
    try:
        current_total = _total_savings_now(conn)
        past_day      = today - timedelta(days=lookback)
        past_total    = _savings_balance_on(conn, past_day)
    finally:
        conn.close()

    grew         = current_total - past_total
    months_obs   = max(lookback / 30.44, 0.5)
    pace_monthly = grew / months_obs                       # observed $/month
    months_left  = max(_months_between(today, end_dt), 0.5)

    # Conservative "starter" = 60% of observed pace; stretch = full pace.
    starter_raw = max(pace_monthly, 0.0) * months_left * 0.60
    stretch_raw = max(pace_monthly, 0.0) * months_left

    # Floors so there is always a small, attainable goal to start from.
    starter = max(_round_nice(starter_raw, 50), 250)
    stretch = max(_round_nice(stretch_raw, 100), starter + 250)

    if pace_monthly <= 0:
        note = ("Your savings balance hasn't grown over the last "
                f"{lookback} days, so we're starting with a small, fixed target. "
                "Hit it and we'll raise the bar.")
    else:
        note = (f"Over the last {lookback} days your savings grew about "
                f"${grew:,.0f} (~${pace_monthly:,.0f}/mo). "
                f"With ~{months_left:.1f} months left this year, this starter "
                "target is well within reach.")

    return jsonify({
        "suggested":      starter,
        "stretch":        stretch,
        "current_total":  current_total,
        "pace_monthly":   round(pace_monthly, 2),
        "observed_growth": round(grew, 2),
        "lookback_days":  lookback,
        "months_left":    round(months_left, 1),
        "end_date":       end_dt.isoformat(),
        "note":           note,
    })


@reports_bp.route("/api/goals", methods=["GET", "POST"])
@login_required
def api_goals():
    conn = _goals_connect()
    try:
        _ensure_savings_goals_table(conn)

        if request.method == "POST":
            body   = request.get_json(silent=True) or {}
            name   = (body.get("name") or "Year-End Savings").strip()[:80]
            try:
                target = float(body.get("target_amount"))
            except (TypeError, ValueError):
                return jsonify({"error": "target_amount must be a number"}), 400
            if target <= 0:
                return jsonify({"error": "target_amount must be positive"}), 400

            today      = date.today()
            start_date = (body.get("start_date") or today.isoformat())[:10]
            end_date   = (body.get("end_date") or _year_end(today).isoformat())[:10]
            start_bal  = _total_savings_now(conn)

            cur = conn.execute("""
                INSERT INTO savings_goals
                    (name, target_amount, start_date, end_date, start_balance, status)
                VALUES (?, ?, ?, ?, ?, 'active')
            """, (name, target, start_date, end_date, start_bal))
            conn.commit()
            new_id = cur.lastrowid
            goal = conn.execute("SELECT * FROM savings_goals WHERE id=?", (new_id,)).fetchone()
            return jsonify(_compute_goal_stats(goal, _total_savings_now(conn))), 201

        # GET — list all goals with live stats
        current_total = _total_savings_now(conn)
        rows = conn.execute(
            "SELECT * FROM savings_goals ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'achieved' THEN 1 ELSE 2 END, id DESC"
        ).fetchall()
        goals = [_compute_goal_stats(r, current_total) for r in rows]
    finally:
        conn.close()

    return jsonify({"current_total": current_total, "goals": goals})


@reports_bp.route("/api/goals/<int:goal_id>", methods=["DELETE"])
@login_required
def api_goal_delete(goal_id):
    conn = _goals_connect()
    try:
        _ensure_savings_goals_table(conn)
        conn.execute("DELETE FROM savings_goals WHERE id=?", (goal_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@reports_bp.route("/api/goals/<int:goal_id>/achieve", methods=["POST"])
@login_required
def api_goal_achieve(goal_id):
    conn = _goals_connect()
    try:
        _ensure_savings_goals_table(conn)
        conn.execute(
            "UPDATE savings_goals SET status='achieved', achieved_at=CURRENT_TIMESTAMP WHERE id=?",
            (goal_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@reports_bp.route("/api/goals/<int:goal_id>/series")
@login_required
def api_goal_series(goal_id):
    """Actual saved-so-far series vs. the linear target line, for charting."""
    conn = _goals_connect()
    try:
        _ensure_savings_goals_table(conn)
        goal = conn.execute("SELECT * FROM savings_goals WHERE id=?", (goal_id,)).fetchone()
        if not goal:
            return jsonify({"error": "goal not found"}), 404

        start_dt = date.fromisoformat(goal["start_date"])
        end_dt   = date.fromisoformat(goal["end_date"])
        today    = date.today()
        start_bal = float(goal["start_balance"])
        target    = float(goal["target_amount"])

        # Actual progress only up to today (no future balances to reconstruct).
        actual_end = min(today, end_dt)
        series = _savings_balance_series(conn, start_dt, actual_end) if actual_end >= start_dt else []
    finally:
        conn.close()

    actual = [{"date": ds, "saved": round(bal - start_bal, 2)} for ds, bal in series]

    target_line = [
        {"date": start_dt.isoformat(), "target": 0.0},
        {"date": end_dt.isoformat(),   "target": round(target, 2)},
    ]
    return jsonify({
        "name":        goal["name"],
        "start_date":  goal["start_date"],
        "end_date":    goal["end_date"],
        "target":      round(target, 2),
        "actual":      actual,
        "target_line": target_line,
    })


# ===================================================================
# Budget Framework
# -------------------------------------------------------------------
# A needs-based, tiered (Need / Want / Savings) budget layered on the
# existing category taxonomy. Targets are set manually; actuals and a
# 3-month reference average are derived from transactions so the user
# can budget against reality. Savings goals plug into the Savings tier.
# ===================================================================

_BUDGET_TIERS = ("need", "want", "savings")

# Default tier guess for known categories (user can override any of these).
_DEFAULT_TIER = {
    "Groceries": "need", "Utilities": "need", "Medical": "need",
    "Transportation": "need", "Loan Payments": "need", "Government": "need",
    "Bank Fees": "need", "Insurance": "need", "Housing": "need", "Rent": "need",
    "Mortgage": "need", "Gas": "need", "Childcare": "need",
    "Food & Drink": "want", "Shopping": "want", "Entertainment": "want",
    "Personal Care": "want", "Travel": "want", "Subscriptions": "want",
    "Home Improvement": "want", "Dining": "want", "Pet": "want", "Services": "want",
    "Savings": "savings", "Investment": "savings",
}

# Categories that are transfers/income, not budgetable spend.
_BUDGET_SKIP_CATS = {
    "income", "transfer in", "transfer out", "savings", "investment",
}


def _budget_tier_for(category: str) -> str:
    return _DEFAULT_TIER.get(category, "want")


def _budget_settings_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM budget_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _ensure_budget_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_plan (
            category      TEXT PRIMARY KEY,
            tier          TEXT NOT NULL DEFAULT 'need',
            target_amount REAL NOT NULL DEFAULT 0,
            sort_order    INTEGER DEFAULT 100,
            updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _budget_actuals_by_category(months_back: int = 0, month_key: "str | None" = None):
    """
    Returns (expense_by_cat: dict, income_total: float) for a single month
    (month_key 'YYYY-MM') or, when month_key is None, for the month that is
    `months_back` months before the current month.
    Expense excludes income/savings/investment/transfer rows.
    """
    df = _load_tx_detail()
    if df.empty or "date" not in df.columns:
        return {}, 0.0
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["month"] = df["date"].dt.to_period("M").astype(str)

    if month_key is None:
        anchor = (pd.Timestamp(date.today().replace(day=1)) -
                  pd.DateOffset(months=months_back))
        month_key = anchor.strftime("%Y-%m")
    sub = df[df["month"] == month_key]
    if sub.empty:
        return {}, 0.0

    income_total = float(sub.loc[sub["spending_type"] == "income", "amount"].abs().sum())

    exp = sub[sub["spending_type"] == "expense"].copy()
    exp = exp[exp["exclude_from_spend"] == 0]
    by_cat = exp.groupby("category")["amount"].apply(lambda s: float(s.abs().sum()))
    return {str(k): round(v, 2) for k, v in by_cat.items()}, round(income_total, 2)


def _budget_reference_avg(n_months: int = 3):
    """Average monthly expense per category over the last n complete-ish months."""
    totals: dict[str, float] = {}
    for mb in range(1, n_months + 1):
        cats, _ = _budget_actuals_by_category(months_back=mb)
        for c, v in cats.items():
            totals[c] = totals.get(c, 0.0) + v
    return {c: round(v / n_months, 2) for c, v in totals.items()}


@reports_bp.route("/budget")
@login_required
def budget_page():
    return render_template("budget.html")


@reports_bp.route("/api/budget_plan", methods=["GET", "POST"])
@login_required
def api_budget_plan():
    conn = _goals_connect()
    try:
        _ensure_budget_tables(conn)

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "expected_income" in body:
                conn.execute(
                    "INSERT INTO budget_settings (key, value) VALUES ('expected_income', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (str(float(body.get("expected_income") or 0)),),
                )
            for it in body.get("categories", []):
                cat = (it.get("category") or "").strip()
                if not cat:
                    continue
                tier = (it.get("tier") or "want").lower()
                if tier not in _BUDGET_TIERS:
                    tier = "want"
                target = float(it.get("target_amount") or 0)
                conn.execute(
                    "INSERT INTO budget_plan (category, tier, target_amount) VALUES (?,?,?) "
                    "ON CONFLICT(category) DO UPDATE SET "
                    "  tier=excluded.tier, target_amount=excluded.target_amount, "
                    "  updated_at=CURRENT_TIMESTAMP",
                    (cat, tier, target),
                )
            conn.commit()
            return jsonify({"status": "ok"})

        # ---- GET ----
        month = request.args.get("month") or _month_key(date.today())
        actuals, income_actual = _budget_actuals_by_category(month_key=month)
        ref_avg = _budget_reference_avg(3)

        saved_rows = {
            r["category"]: r for r in
            conn.execute("SELECT category, tier, target_amount FROM budget_plan").fetchall()
        }
        expected_income = float(_budget_settings_get(conn, "expected_income", 0) or 0)

        # Active savings-goal monthly requirement feeds the Savings tier.
        goal_need = 0.0
        goal_name = None
        try:
            _ensure_savings_goals_table(conn)
            g = conn.execute(
                "SELECT * FROM savings_goals WHERE status='active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if g:
                stats = _compute_goal_stats(g, _total_savings_now(conn))
                goal_need = stats["need_monthly"]
                goal_name = stats["name"]
        except Exception:
            pass

        # Union of categories: saved plan + observed actuals + reference, minus skips.
        all_cats = set(saved_rows) | set(actuals) | set(ref_avg)
        all_cats = {c for c in all_cats if c and c.lower() not in _BUDGET_SKIP_CATS}

        categories = []
        for cat in sorted(all_cats):
            saved = saved_rows.get(cat)
            categories.append({
                "category": cat,
                "tier":     (saved["tier"] if saved else _budget_tier_for(cat)),
                "target":   round(float(saved["target_amount"]), 2) if saved else 0.0,
                "actual":   round(actuals.get(cat, 0.0), 2),
                "ref_avg":  round(ref_avg.get(cat, 0.0), 2),
                "saved":    bool(saved),
            })

        # Tier rollups (savings tier target includes the goal requirement).
        tiers = {t: {"target": 0.0, "actual": 0.0} for t in _BUDGET_TIERS}
        for c in categories:
            tiers[c["tier"]]["target"] += c["target"]
            tiers[c["tier"]]["actual"] += c["actual"]
        tiers["savings"]["target"] += goal_need

        base_income = expected_income or income_actual or 0.0
        for t in _BUDGET_TIERS:
            tiers[t]["target"] = round(tiers[t]["target"], 2)
            tiers[t]["actual"] = round(tiers[t]["actual"], 2)
            tiers[t]["target_pct"] = round(100 * tiers[t]["target"] / base_income, 1) if base_income else 0.0
            tiers[t]["actual_pct"] = round(100 * tiers[t]["actual"] / base_income, 1) if base_income else 0.0

        planned_total = round(sum(tiers[t]["target"] for t in _BUDGET_TIERS), 2)
        actual_total  = round(sum(tiers[t]["actual"] for t in _BUDGET_TIERS), 2)

        return jsonify({
            "month": month,
            "income": {
                "expected": round(expected_income, 2),
                "actual":   round(income_actual, 2),
                "base":     round(base_income, 2),
            },
            "categories": categories,
            "tiers": tiers,
            "guardrails": {"need": 50, "want": 30, "savings": 20},
            "planned_total":     planned_total,
            "actual_total":      actual_total,
            "planned_surplus":   round(base_income - planned_total, 2),
            "actual_surplus":    round(base_income - actual_total, 2),
            "goal": {"need_monthly": round(goal_need, 2), "name": goal_name},
        })
    finally:
        conn.close()


# ===================================================================
# Local AI assistant (Ollama) — finance-aware, summary context, fully local
# ===================================================================

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

_AI_SYSTEM_PROMPT = (
    "You are a private, local financial assistant embedded in the user's personal "
    "finance dashboard. You run on the user's own machine via Ollama, so their data "
    "stays private. Answer questions about the user's money using ONLY the financial "
    "summary provided below. Be concise and concrete; use the actual dollar figures. "
    "If the summary doesn't contain the answer, say what's missing and suggest which "
    "dashboard page would have it (Monthly Cashflow, Accounts, Budget, Savings Goals, "
    "Transactions). Do not invent numbers. Note key context: the brokerage is being "
    "drawn down intentionally to pay for a car, so a negative net investment is expected."
)


def _flow_totals_summary(conn) -> dict:
    """Compact overall flow-type net totals (mirrors /api/flow_type/report)."""
    rows = conn.execute("""
        SELECT t.amount, t.name AS tx_name, a.type AS acct_type, a.subtype,
               tc.user_category, tc.exclude_reason, tm.payload_json
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
        LEFT JOIN transaction_meta tm ON tm.transaction_id = t.transaction_id
        WHERE IFNULL(t.pending, 0) = 0
    """).fetchall()
    tot = {ft: 0.0 for ft in _FLOW_TYPES}
    for r in rows:
        pfcd = ""
        if r["payload_json"]:
            try:
                pfcd = (json.loads(r["payload_json"]).get("personal_finance_category") or {}).get("detailed") or ""
            except Exception:
                pass
        ft  = _derive_flow_type(r["acct_type"], r["subtype"], r["amount"],
                                r["user_category"], r["exclude_reason"], pfcd, r["tx_name"])
        amt = float(r["amount"] or 0)
        tot[ft] += amt if ft in ("savings", "investment") else abs(amt)
    tot["expense_net"] = round(tot["expense"] - tot["refund"], 2)
    return {k: round(v, 2) for k, v in tot.items()}


def _build_ai_context() -> str:
    """A compact, readable finance summary fed to the local model."""
    lines = []
    conn = _goals_connect()
    try:
        lines.append(f"As of {date.today().isoformat()}.\n")

        lines.append("ACCOUNT BALANCES:")
        for r in conn.execute("""
            SELECT a.name, a.type, a.subtype, a.current, i.institution_name
            FROM accounts a JOIN items i ON i.item_id = a.item_id
            WHERE a.current IS NOT NULL
            ORDER BY a.type, a.subtype, a.name
        """).fetchall():
            lines.append(f"  - {r['institution_name']} {r['name']} ({r['subtype'] or r['type']}): ${r['current']:,.2f}")

        try:
            f = _flow_totals_summary(conn)
            lines.append("\nFLOW TOTALS (all history, net):")
            lines.append(f"  income ${f['income']:,.0f}; true expense ${f['expense_net']:,.0f}; "
                         f"net savings ${f['savings']:,.0f}; net investment ${f['investment']:,.0f} "
                         f"(negative = intentional brokerage drawdown for car)")
        except Exception:
            pass

        try:
            _ensure_savings_goals_table(conn)
            g = conn.execute("SELECT * FROM savings_goals WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            if g:
                st = _compute_goal_stats(g, _total_savings_now(conn))
                lines.append("\nSAVINGS GOAL:")
                lines.append(f"  {st['name']}: ${st['progress']:,.0f} of ${st['target_amount']:,.0f} by "
                             f"{st['end_date']} ({st['pct']}%); needs ${st['need_monthly']:,.0f}/mo; "
                             f"{'on track' if st['on_track'] else 'behind pace'}.")
        except Exception:
            pass

        try:
            _ensure_budget_tables(conn)
            plan = conn.execute("SELECT category, tier, target_amount FROM budget_plan WHERE target_amount > 0").fetchall()
            exp_income = float(_budget_settings_get(conn, "expected_income", 0) or 0)
            if plan or exp_income:
                tiers = {"need": 0.0, "want": 0.0, "savings": 0.0}
                for p in plan:
                    tiers[p["tier"]] = tiers.get(p["tier"], 0.0) + float(p["target_amount"])
                lines.append("\nBUDGET (monthly targets):")
                if exp_income:
                    lines.append(f"  expected income ${exp_income:,.0f}/mo")
                lines.append(f"  needs ${tiers['need']:,.0f}; wants ${tiers['want']:,.0f}; savings ${tiers['savings']:,.0f}")
        except Exception:
            pass
    finally:
        conn.close()
    return "\n".join(lines)


# ---- Tools the local model can call for specifics (beyond the summary) ----

_ACCT_ALIASES = {
    "amex": "american express", "53": "fifth third", "5/3": "fifth third",
    "cap one": "capital one", "capone": "capital one", "schwab": "charles schwab",
    "checking": "momentum checking", "brokerage": "individual",
}


def _resolve_account_id(name: str):
    """Fuzzy-match a free-text account name to (account_id, label)."""
    if not name:
        return None, None
    n = name.lower().strip()
    expanded = n
    for k, v in _ACCT_ALIASES.items():
        if k in n:
            expanded += " " + v
    want = set(re.findall(r"[a-z0-9]+", expanded))
    conn = _goals_connect()
    try:
        rows = conn.execute("""
            SELECT a.account_id, a.name, a.subtype, i.institution_name
            FROM accounts a JOIN items i ON i.item_id = a.item_id
        """).fetchall()
    finally:
        conn.close()
    best, best_score = None, 0
    for r in rows:
        label = f"{r['institution_name']} {r['name']} {r['subtype'] or ''}".lower()
        score = len(want & set(re.findall(r"[a-z0-9]+", label)))
        if n and n in label:
            score += 5
        if score > best_score:
            best, best_score = r, score
    if best and best_score > 0:
        return best["account_id"], f"{best['institution_name']} {best['name']}"
    return None, None


def _df_account_filter(df, account_id):
    """In-process account filter (no request dependency, for AI tools)."""
    if account_id and not df.empty and "account_id" in df.columns:
        return df[df["account_id"] == account_id]
    return df


def _tool_list_accounts(**_):
    conn = _goals_connect()
    try:
        rows = conn.execute("""
            SELECT a.name, a.type, a.subtype, a.current, i.institution_name
            FROM accounts a JOIN items i ON i.item_id = a.item_id
            WHERE a.current IS NOT NULL ORDER BY a.type, a.current DESC
        """).fetchall()
    finally:
        conn.close()
    return {"accounts": [{"name": f"{r['institution_name']} {r['name']}",
                          "type": r["subtype"] or r["type"],
                          "balance": round(float(r["current"]), 2)} for r in rows]}


def _tool_flow_summary(**_):
    conn = _goals_connect()
    try:
        return _flow_totals_summary(conn)
    finally:
        conn.close()


def _tool_account_cashflow(account="", month="", **_):
    aid, label = _resolve_account_id(account)
    if not aid:
        return {"error": f"no account matching '{account}'"}
    month = (month or _month_key(date.today()))[:7]
    conn = _goals_connect()
    try:
        rows = conn.execute("""
            SELECT t.name, t.merchant_name, t.amount, COALESCE(tc.user_category,'') AS cat
            FROM transactions t
            LEFT JOIN transaction_classifications tc ON tc.transaction_id = t.transaction_id
            WHERE t.account_id = ? AND t.date LIKE ? AND IFNULL(t.pending,0)=0
        """, (aid, f"{month}%")).fetchall()
    finally:
        conn.close()
    tin = tout = 0.0
    charges = []
    for r in rows:
        amt = float(r["amount"] or 0)
        if amt < 0:
            tin += abs(amt)
        elif amt > 0:
            tout += amt
            charges.append(((r["merchant_name"] or r["name"] or "")[:40], round(amt, 2), r["cat"]))
    charges.sort(key=lambda x: -x[1])
    return {"account": label, "month": month, "total_in": round(tin, 2),
            "total_out": round(tout, 2), "net": round(tin - tout, 2),
            "top_charges": [{"merchant": m, "amount": a, "category": c} for m, a, c in charges[:8]]}


def _tool_spending_by_category(range="12m", account="", **_):
    aid, label = _resolve_account_id(account) if account else (None, "all accounts")
    df = _df_account_filter(_filter_by_range(_load_tx_detail(), range or "12m"), aid)
    if df.empty:
        return {"by_category": {}}
    exp = df[(~df["is_income"]) & (df["exclude_from_spend"] == 0) & (df["amount"] > 0)]
    cats = exp.groupby("category")["amount"].sum().round(2).sort_values(ascending=False)
    return {"account": label, "range": range or "12m",
            "by_category": {str(k): float(v) for k, v in cats.head(15).items()}}


def _tool_top_merchants(range="12m", account="", limit=10, **_):
    aid, label = _resolve_account_id(account) if account else (None, "all accounts")
    df = _df_account_filter(_filter_by_range(_load_tx_detail(), range or "12m"), aid)
    if df.empty:
        return {"merchants": []}
    out = df[df["amount"] > 0].copy()
    out["m"] = out["merchant_name"].fillna(out["name"])
    g = out.groupby("m")["amount"].agg(["sum", "count"]).sort_values("sum", ascending=False).head(int(limit or 10))
    return {"account": label, "range": range or "12m",
            "merchants": [{"merchant": str(k), "total": round(float(r["sum"]), 2),
                           "count": int(r["count"])} for k, r in g.iterrows()]}


def _tool_budget_status(month="", **_):
    conn = _goals_connect()
    try:
        _ensure_budget_tables(conn)
        month = (month or _month_key(date.today()))[:7]
        actuals, income_actual = _budget_actuals_by_category(month_key=month)
        saved = {r["category"]: r for r in
                 conn.execute("SELECT category, tier, target_amount FROM budget_plan").fetchall()}
        expected_income = float(_budget_settings_get(conn, "expected_income", 0) or 0)
        tiers = {t: {"target": 0.0, "actual": 0.0} for t in _BUDGET_TIERS}
        overspend = []
        for cat in (set(saved) | set(actuals)):
            if not cat or cat.lower() in _BUDGET_SKIP_CATS:
                continue
            s      = saved.get(cat)
            tier   = s["tier"] if s else _budget_tier_for(cat)
            target = float(s["target_amount"]) if s else 0.0
            act    = round(actuals.get(cat, 0.0), 2)
            tiers.setdefault(tier, {"target": 0.0, "actual": 0.0})
            tiers[tier]["target"] += target
            tiers[tier]["actual"] += act
            if target > 0 and act > target:
                overspend.append({"category": cat, "target": target, "actual": act,
                                  "over": round(act - target, 2)})
        for t in tiers:
            tiers[t] = {k: round(v, 2) for k, v in tiers[t].items()}
        overspend.sort(key=lambda x: -x["over"])
        return {"month": month,
                "income": {"expected": round(expected_income, 2), "actual": round(income_actual, 2)},
                "tiers": tiers, "overspending": overspend[:6],
                "note": "target 0 = no budget set for that category yet"}
    finally:
        conn.close()


def _tool_savings_goal(**_):
    conn = _goals_connect()
    try:
        _ensure_savings_goals_table(conn)
        g = conn.execute("SELECT * FROM savings_goals WHERE status='active' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        if not g:
            return {"note": "No active savings goal. The user can set one on the Savings Goals page."}
        st = _compute_goal_stats(g, _total_savings_now(conn))
        keys = ("name", "target_amount", "progress", "pct", "projected_eoy",
                "need_monthly", "on_track", "end_date", "days_elapsed", "days_total")
        return {k: st[k] for k in keys}
    finally:
        conn.close()


_AI_TOOL_FNS = {
    "list_accounts":          _tool_list_accounts,
    "get_flow_summary":       _tool_flow_summary,
    "get_account_cashflow":   _tool_account_cashflow,
    "get_spending_by_category": _tool_spending_by_category,
    "get_top_merchants":      _tool_top_merchants,
    "get_budget_status":      _tool_budget_status,
    "get_savings_goal":       _tool_savings_goal,
}

_AI_TOOLS = [
    {"type": "function", "function": {
        "name": "list_accounts",
        "description": "List the user's accounts (name, type, current balance).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_flow_summary",
        "description": "Net flow totals over all history: income, true expense, net savings, net investment.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_account_cashflow",
        "description": "Money in vs out and top charges for one account in one month.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "description": "Account name, e.g. 'Amex' or 'Fifth Third checking'"},
            "month":   {"type": "string", "description": "Month as YYYY-MM, e.g. 2026-05"},
        }, "required": ["account", "month"]},
    }},
    {"type": "function", "function": {
        "name": "get_spending_by_category",
        "description": "Total spending grouped by category, optionally for one account.",
        "parameters": {"type": "object", "properties": {
            "range":   {"type": "string", "description": "30d, ytd, or 12m", "enum": ["30d", "ytd", "12m"]},
            "account": {"type": "string", "description": "Optional account name; omit for all accounts"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_top_merchants",
        "description": "Top merchants by spend, optionally for one account.",
        "parameters": {"type": "object", "properties": {
            "range":   {"type": "string", "enum": ["30d", "ytd", "12m"]},
            "account": {"type": "string", "description": "Optional account name; omit for all accounts"},
            "limit":   {"type": "integer", "description": "How many merchants (default 10)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_budget_status",
        "description": "Budget vs actual for a month: Need/Want/Savings tier targets vs actual "
                       "spending, expected vs actual income, and the biggest category overspends.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "Month as YYYY-MM; omit for current month"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_savings_goal",
        "description": "Active savings-goal projection: progress, target, % done, projected end-of-year "
                       "amount, required monthly contribution, and on-track status.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def _run_ai_tool(name, args):
    fn = _AI_TOOL_FNS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:
        return {"error": f"tool {name} failed: {e}"}


@reports_bp.route("/ai")
@login_required
def ai_page():
    return render_template("ai.html")


@reports_bp.route("/api/ai/status")
@login_required
def api_ai_status():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        r.raise_for_status()
        models = [m.get("name") for m in (r.json().get("models") or [])]
        return jsonify({"ok": True, "models": models, "default": OLLAMA_MODEL})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": OLLAMA_URL}), 503


_AI_TOOLS_HINT = (
    "\n\nYou also have TOOLS to fetch specifics beyond the summary: list_accounts, "
    "get_flow_summary, get_account_cashflow(account, month), "
    "get_spending_by_category(range, account?), get_top_merchants(range, account?), "
    "get_budget_status(month?), get_savings_goal(). "
    "Call a tool whenever the question needs a specific month, account, category, "
    "merchant, budget, or goal detail not in the summary. Refer to accounts by name "
    "(e.g. 'Amex', 'Fifth Third checking'). After getting tool results, answer "
    "concisely with the real figures. Never invent numbers — use a tool or say it's "
    "not available."
)


def _ollama_chat(messages, model, tools=None):
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json()


@reports_bp.route("/api/ai/chat", methods=["POST"])
@login_required
def api_ai_chat():
    body     = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    model    = (body.get("model") or OLLAMA_MODEL).strip()

    try:
        context = _build_ai_context()
    except Exception as e:
        context = f"(failed to build financial context: {e})"

    system = _AI_SYSTEM_PROMPT + _AI_TOOLS_HINT + "\n\n=== FINANCIAL SUMMARY ===\n" + context
    convo = [{"role": "system", "content": system},
             *[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]]

    def emit(obj):
        return json.dumps(obj, default=str) + "\n"

    def generate():
        try:
            # Agentic loop: let the model call tools, feed results back, repeat.
            for _round in range(5):
                data = _ollama_chat(convo, model, tools=_AI_TOOLS)
                msg  = data.get("message") or {}
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    convo.append(msg)
                    for tc in tool_calls:
                        fn   = tc.get("function") or {}
                        name = fn.get("name")
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try: args = json.loads(args)
                            except Exception: args = {}
                        yield emit({"type": "tool", "name": name, "args": args})
                        result = _run_ai_tool(name, args)
                        convo.append({"role": "tool", "content": json.dumps(result, default=str)})
                    continue
                # No tool call → this is the answer.
                yield emit({"type": "token", "text": msg.get("content", "") or "(no response)"})
                yield emit({"type": "done"})
                return
            # Hit the round cap — force a final answer with no tools.
            data = _ollama_chat(convo + [{"role": "user",
                    "content": "Answer now using the tool results above; do not call more tools."}], model)
            yield emit({"type": "token", "text": (data.get("message") or {}).get("content", "") or "(no response)"})
            yield emit({"type": "done"})
        except Exception as e:
            yield emit({"type": "error", "text": f"{e}. Is Ollama running at {OLLAMA_URL}?"})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson; charset=utf-8")


@reports_bp.route("/api/ai/context")
@login_required
def api_ai_context():
    """Expose the exact summary the AI sees (for the 'what can it see' panel)."""
    return jsonify({"context": _build_ai_context()})

