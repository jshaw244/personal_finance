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
    jsonify, request, redirect, url_for, flash, current_app
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
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
    conn = sqlite3.connect(str(DB_PATH))
    try:
        return pd.read_sql_query("SELECT * FROM transactions;", conn)
    finally:
        conn.close()

# -------------------------------------------------------------------
# Category derivation + normalization
# -------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "Groceries": ["walmart", "kroger", "aldi", "costco", "whole foods", "target", "jewel", "caputo"],
    "Dining": ["restaurant", "grill", "bar", "cafe", "pizza", "coffee", "burger", "7-11"],
    "Gas": ["shell", "bp", "exxon", "mobil", "marathon", "trip"],
    "Utilities": ["comcast", "att", "verizon", "village", "addison", "nicor", "comed", "duke energy", "electric", "gas bill"],
    "Entertainment": ["netflix", "spotify", "amc", "theater", "cinema", "disney"],
    "Travel": ["airlines", "hotel", "uber", "lyft", "delta", "southwest"],
    "Shopping": ["amazon", "best buy", "ebay", "target"],
    "Income": ["payroll", "deposit", "refund", "credit"],
    "Pet": ["pet", "dog", "cat", "groom"],
    "Other": []
}

def _derive_category(name: str, merchant: str) -> str:
    text = f"{name or ''} {merchant or ''}".lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return cat
    return "Other"

def _load_tx_detail() -> pd.DataFrame:
    # In production, always use the live database
    if ENV_TARGET == "production":
        df = _load_tx_detail_from_db()
    else:
        # In sandbox/dev, prefer latest Excel snapshot, then fallback to DB
        f = _latest_result_file()
        if f and f.suffix.lower() == ".xlsx":
            df = _load_tx_detail_from_excel(f)
            if df.empty:
                df = _load_tx_detail_from_db()
        else:
            df = _load_tx_detail_from_db()

    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    if "category" not in df.columns:
        df["category"] = None
    df["category"] = df["category"].fillna("").replace("", None)
    df["category"] = df["category"].replace(["None", "none"], None)

    df["category"] = df.apply(
        lambda r: _derive_category(
            r.get("name", ""), 
            r.get("merchant_name", "")
        ) if not r.get("category") else r.get("category"),
        axis=1,
    )

    if "amount" in df.columns:
        df["spending_type"] = df["amount"].apply(
            lambda x: "income" if x > 0 else "expense"
        )
    else:
        df["spending_type"] = "unknown"
    return df


def _filter_by_range(df: pd.DataFrame, range_key: str) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    today = datetime.today().date()
    if range_key == "30d":
        start = today - timedelta(days=30)
    elif range_key == "ytd":
        start = date(today.year, 1, 1)
    elif range_key == "12m":
        start = today.replace(day=1) - timedelta(days=365)
    else:
        return df
    return df[df["date"].dt.date >= start]

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
        conn = sqlite3.connect(str(DB_PATH))
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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "category" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404

    df = df.copy()
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )

    # Expenses only, as positive "spend"
    spend_df = df.loc[~df["is_income"]].copy()
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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "date" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )

    # Expenses only, as positive spend
    spend_df = df.loc[~df["is_income"]].copy()
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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "amount" not in df.columns:
        return jsonify({"error": "no transaction detail available"}), 404
    merchant_col = "merchant_name" if "merchant_name" in df.columns else ("name" if "name" in df.columns else None)
    if not merchant_col:
        return jsonify({"error": "missing merchant column"}), 400
    top = (df.groupby(merchant_col)["amount"]
           .sum().sort_values(ascending=False)
           .head(10).round(2).reset_index())
    top.columns = ["merchant", "amount"]
    return jsonify(top.to_dict(orient="records"))

# -------------------- KPI / Cashflow / Recurring / Top Tx --------------------
@reports_bp.route("/api/kpi_summary")
@login_required
def api_kpi_summary():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "amount" not in df.columns or "date" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    # Parse dates safely and drop rows with invalid dates
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].notna()]

    df = df[df["amount"].notna()]
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )

    # Treat spending as positive
    spend_df = df.loc[~df["is_income"]].copy()
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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "amount" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    df = df.copy()
    df["is_income"] = df.get("merchant_name", "").fillna("").str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )

    income = df.loc[df["is_income"], "amount"].sum()
    expenses = df.loc[~df["is_income"], "amount"].abs().sum()

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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "category" not in df.columns or "amount" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    df["month"] = df["date"].dt.to_period("M").astype(str)
    grouped = df.groupby(["month", "category"])["amount"].sum().reset_index()
    pivot = grouped.pivot(index="month", columns="category", values="amount").fillna(0)
    return jsonify(pivot.round(2).to_dict(orient="index"))

@reports_bp.route("/api/cashflow_summary")
@login_required
def api_cashflow_summary():
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
    if df.empty or "amount" not in df.columns or "date" not in df.columns:
        return jsonify({"error": "no transaction data"}), 404
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["merchant_name"] = df.get("merchant_name", "").fillna("")
    df["is_income"] = df["merchant_name"].str.contains(
        "payroll|deposit|refund|credit|reversal", case=False, na=False
    )
    df["month"] = df["date"].dt.to_period("M").astype(str)
    inflow = df.loc[df["is_income"]].groupby("month")["amount"].sum().rename("inflow")
    outflow = df.loc[~df["is_income"]].groupby("month")["amount"].sum().rename("outflow")
    cash = pd.concat([inflow, outflow], axis=1).fillna(0.0).sort_index()
    cash["net"] = cash["inflow"] - cash["outflow"]
    cash["running_balance"] = cash["net"].cumsum()
    out = []
    for idx, row in cash.round(2).iterrows():
        out.append({
            "month": idx,
            "inflow": float(row["inflow"]),
            "outflow": float(row["outflow"]),
            "net": float(row["net"]),
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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))

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
    current_app.logger.info("Recurring merchant scan: %d txns across %d merchants", len(df), df["merchant_name"].nunique())

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
    df = _filter_by_range(_load_tx_detail(), request.args.get("range", ""))
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
        conn = sqlite3.connect(str(DB_PATH))
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
    conn = sqlite3.connect(str(DB_PATH))
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
    conn = sqlite3.connect(str(DB_PATH))
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
    conn = sqlite3.connect(str(DB_PATH))
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


# -------------------------------------------------------------------
# M5: Password generator (utility only; no vault storage)
# -------------------------------------------------------------------
@reports_bp.route("/api/password_generate")
@login_required
def api_password_generate():
    """
    Generate a strong random password.
    Query params:
      length (default 16), special=true|false
    """
    length = max(8, min(128, int(request.args.get("length", "16"))))
    use_special = request.args.get("special", "true").lower() != "false"

    letters = string.ascii_letters
    digits = string.digits
    specials = "!@#$%^&*()-_=+[]{};:,.?/"

    pool = letters + digits + (specials if use_special else "")
    if not pool:
        pool = letters + digits

    # Guarantee at least one from each class when possible
    pw = []
    pw.append(random.choice(string.ascii_lowercase))
    pw.append(random.choice(string.ascii_uppercase))
    pw.append(random.choice(string.digits))
    if use_special:
        pw.append(random.choice(specials))
    while len(pw) < length:
        pw.append(random.choice(pool))
    random.shuffle(pw)
    return jsonify({"password": "".join(pw[:length])})
