# src/analysis/analysis_summary.py
"""
Builds durable analysis summary tables and exports an Excel workbook.

Creates / refreshes:
  - summary_monthly (month, category, total_spend, num_txn, avg_txn)
  - summary_merchant (merchant, total_spend, avg_cadence_days, months_active)

Exports:
  results/sandbox_analysis_summary_YYYYMMDD_HHMM.xlsx

Assumptions:
  - transactions(date TEXT 'YYYY-MM-DD', amount REAL, merchant_name TEXT, category TEXT, pending INTEGER)
  - Use only non-pending transactions (pending = 0)
  - "Spend" is absolute value of outflow; we use ABS(amount) for robustness
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.common.paths import DB_FILE, RESULTS_DIR, LOG_DIR
from os import getenv

# ---------- Logging helper -> log_events table ----------
def _log_event(conn: sqlite3.Connection, source: str, level: str, message: str) -> None:
    try:
        conn.execute(
            """
            INSERT INTO log_events (timestamp, source, level, message)
            VALUES (datetime('now'), ?, ?, ?)
            """,
            (source, level.upper(), message),
        )
        conn.commit()
    except Exception:
        # Last-ditch: avoid crashing on logging failure
        pass

# ---------- SQL DDL ----------
SQL_CREATE_SUMMARY_MONTHLY = """
CREATE TABLE IF NOT EXISTS summary_monthly (
    month TEXT NOT NULL,
    category TEXT NOT NULL,
    total_spend REAL NOT NULL,
    num_txn INTEGER NOT NULL,
    avg_txn REAL NOT NULL,
    PRIMARY KEY (month, category)
)
"""

SQL_CREATE_SUMMARY_MERCHANT = """
CREATE TABLE IF NOT EXISTS summary_merchant (
    merchant TEXT PRIMARY KEY,
    total_spend REAL NOT NULL,
    avg_cadence_days REAL,
    months_active INTEGER NOT NULL
)
"""

SQL_IDX_MONTHLY = """
CREATE INDEX IF NOT EXISTS idx_summary_monthly_month ON summary_monthly(month)
"""
SQL_IDX_MERCHANT = """
CREATE INDEX IF NOT EXISTS idx_summary_merchant_spend ON summary_merchant(total_spend DESC)
"""

# ---------- Core build functions ----------
def build_summary_monthly(df_tx: pd.DataFrame) -> pd.DataFrame:
    # Normalize month and category
    df = df_tx.copy()
    df["month"] = df["date"].str.slice(0, 7)
    df["category"] = df["category"].fillna("Uncategorized")
    # absolute spend for robustness
    df["abs_amount"] = df["amount"].abs()

    g = df.groupby(["month", "category"], as_index=False).agg(
        total_spend=("abs_amount", "sum"),
        num_txn=("abs_amount", "count"),
        avg_txn=("abs_amount", "mean"),
    )

    # Order for readability
    g = g.sort_values(["month", "total_spend"], ascending=[True, False], ignore_index=True)
    return g

def build_summary_merchant(df_tx: pd.DataFrame) -> pd.DataFrame:
    df = df_tx.copy()
    df["merchant"] = df["merchant_name"].fillna(df["name"]).fillna("Unknown")
    df["month"] = df["date"].str.slice(0, 7)
    df["abs_amount"] = df["amount"].abs()

    # total spend and months active
    agg = df.groupby("merchant", as_index=False).agg(
        total_spend=("abs_amount", "sum"),
        months_active=("month", lambda s: s.nunique()),
    )

    # cadence: average days between consecutive transactions per merchant
    # compute per-merchant diffs
    df_dates = df[["merchant", "date"]].copy()
    # convert to datetime once
    df_dates["date_dt"] = pd.to_datetime(df_dates["date"], errors="coerce")
    df_dates = df_dates.dropna(subset=["date_dt"])
    df_dates = df_dates.sort_values(["merchant", "date_dt"])
    # group diff in days
    def _avg_diff_days(s: pd.Series) -> float | None:
        if len(s) <= 1:
            return None
        diffs = s.diff().dt.days.dropna()
        if diffs.empty:
            return None
        return float(diffs.mean())

    cadence = (
        df_dates.groupby("merchant")["date_dt"]
        .apply(_avg_diff_days)
        .reset_index()
        .rename(columns={"date_dt": "avg_cadence_days"})
    )

    out = agg.merge(cadence, on="merchant", how="left")
    out = out.sort_values(["total_spend"], ascending=False, ignore_index=True)
    return out

def ensure_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(SQL_CREATE_SUMMARY_MONTHLY)
    cur.execute(SQL_CREATE_SUMMARY_MERCHANT)
    cur.execute(SQL_IDX_MONTHLY)
    cur.execute(SQL_IDX_MERCHANT)
    conn.commit()

def refresh_tables(conn: sqlite3.Connection, df_monthly: pd.DataFrame, df_merchant: pd.DataFrame) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM summary_monthly")
    cur.execute("DELETE FROM summary_merchant")
    conn.commit()

    # bulk insert using executemany
    cur.executemany(
        "INSERT INTO summary_monthly (month, category, total_spend, num_txn, avg_txn) VALUES (?, ?, ?, ?, ?)",
        df_monthly[["month", "category", "total_spend", "num_txn", "avg_txn"]].itertuples(index=False, name=None),
    )
    cur.executemany(
        "INSERT INTO summary_merchant (merchant, total_spend, avg_cadence_days, months_active) VALUES (?, ?, ?, ?)",
        df_merchant[["merchant", "total_spend", "avg_cadence_days", "months_active"]].itertuples(index=False, name=None),
    )
    conn.commit()

def export_excel(df_monthly: pd.DataFrame, df_merchant: pd.DataFrame, path_out: Path) -> Path:
    path_out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path_out, engine="openpyxl") as xw:
        df_monthly.to_excel(xw, index=False, sheet_name="summary_monthly")
        df_merchant.to_excel(xw, index=False, sheet_name="summary_merchant")

        # lightweight overview sheet
        overview = pd.DataFrame(
            {
                "metric": [
                    "generated_at",
                    "rows_summary_monthly",
                    "rows_summary_merchant",
                ],
                "value": [
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    len(df_monthly),
                    len(df_merchant),
                ],
            }
        )
        overview.to_excel(xw, index=False, sheet_name="overview")
    return path_out

def main() -> int:
    RESULTS_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    with sqlite3.connect(DB_FILE) as conn:
        _log_event(conn, "analysis_summary", "INFO", "Starting analysis summary build")

        # Ensure base tables
        ensure_tables(conn)

        # Load transactions (pending = 0 only, with required columns present)
        q = """
            SELECT
                transaction_id,
                date,
                amount,
                name,
                merchant_name,
                category,
                pending
            FROM transactions
            WHERE IFNULL(pending, 0) = 0
        """
        df_tx = pd.read_sql_query(q, conn)

        if df_tx.empty:
            _log_event(conn, "analysis_summary", "WARN", "No non-pending transactions found; tables will be empty")
            df_monthly = pd.DataFrame(columns=["month", "category", "total_spend", "num_txn", "avg_txn"])
            df_merchant = pd.DataFrame(columns=["merchant", "total_spend", "avg_cadence_days", "months_active"])
        else:
            # fill required columns to avoid KeyErrors
            for col in ["date", "amount", "name", "merchant_name", "category"]:
                if col not in df_tx.columns:
                    df_tx[col] = None

            df_monthly = build_summary_monthly(df_tx)
            df_merchant = build_summary_merchant(df_tx)

        # Refresh DB tables
        refresh_tables(conn, df_monthly, df_merchant)

        # Export Excel
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        env = getenv("ENV_TARGET", "sandbox").lower()
        outfile = RESULTS_DIR / f"{env}_analysis_summary_{ts}.xlsx"
        export_excel(df_monthly, df_merchant, outfile)

        _log_event(conn, "analysis_summary", "INFO", f"Analysis summary completed: {outfile}")
        print(f"Analysis summary written to: {outfile}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
