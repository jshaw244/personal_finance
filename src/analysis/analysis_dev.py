#!/usr/bin/env python3
"""
analysis_dev.py
Development-layer analysis runner for personal_finance.

This script:
  - Connects to plaid_dev.db (development DB)
  - Builds core spend / trend / top-merchant summaries
  - Generates charts and tabular CSVs
  - Writes a timestamped Excel workbook in results/

Differences vs analysis.py:
  - Targets plaid_dev.db instead of plaid.db (sandbox)
  - Does not write back to the database
  - Assumes budgets and financial_health_history may be empty
  - Uses summary_monthly / summary_merchant if they exist

Outputs:
  results/dev_table_summary_YYYYMMDD_HHMM.csv
  results/dev_analysis_summary_YYYYMMDD_HHMM.xlsx
  results/dev_by_category.png
  results/dev_monthly_trend.png
"""

from __future__ import annotations

import sqlite3
import pandas as pd
import matplotlib.pyplot as plt

from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, Tuple

# --------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------
PROJECT_ROOT = Path("C:/DATA/personal_finance")
DB_PATH      = PROJECT_ROOT / "data" / "plaid_dev.db"
RESULTS_DIR  = PROJECT_ROOT / "results"
LOG_FILE     = PROJECT_ROOT / "logs" / "maintenance.log"

RESULTS_DIR.mkdir(exist_ok=True, parents=True)
LOG_FILE.parent.mkdir(exist_ok=True, parents=True)

ENV_NAME = "development"  # tag for filenames and logging


# --------------------------------------------------------------------
# Logging helper
# --------------------------------------------------------------------
def write_log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] ANALYSIS_DEV - {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------
def connect_dev_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    write_log(f"Opening development database: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def summarize_tables(conn: sqlite3.Connection) -> pd.DataFrame:
    q_tables = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    tables = pd.read_sql_query(q_tables, conn)
    rows = []
    for t in tables["name"]:
        try:
            n = pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {t};", conn)["n"][0]
        except Exception as e:
            n = f"Error: {e}"
        rows.append({"table": t, "row_count": n})
    df = pd.DataFrame(rows)
    return df


def load_transactions(conn: sqlite3.Connection) -> pd.DataFrame:
    # pull all transactions; leave filtering for analysis functions
    q = """
        SELECT
            transaction_id,
            date,
            amount,
            name,
            merchant_name,
            category,
            pending,
            item_id,
            account_id,
            iso_currency_code
        FROM transactions
    """
    df = pd.read_sql_query(q, conn)
    return df


def load_summary_monthly(conn: sqlite3.Connection) -> pd.DataFrame:
    q = """
        SELECT
            month,
            category,
            total_spend,
            num_txn,
            avg_txn
        FROM summary_monthly
        ORDER BY month, total_spend DESC
    """
    try:
        return pd.read_sql_query(q, conn)
    except Exception:
        return pd.DataFrame(columns=["month","category","total_spend","num_txn","avg_txn"])


def load_summary_merchant(conn: sqlite3.Connection) -> pd.DataFrame:
    q = """
        SELECT
            merchant,
            total_spend,
            avg_cadence_days,
            months_active
        FROM summary_merchant
        ORDER BY total_spend DESC
    """
    try:
        return pd.read_sql_query(q, conn)
    except Exception:
        return pd.DataFrame(columns=["merchant","total_spend","avg_cadence_days","months_active"])


# --------------------------------------------------------------------
# Time window helpers
# --------------------------------------------------------------------
def resolve_period(days: int = 30,
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None
                   ) -> Tuple[date, date, str]:
    """
    Return (start_date, end_date_exclusive, label)
    Default is rolling last N days (30).
    """
    if start_date and end_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_excl = datetime.strptime(end_date, "%Y-%m-%d").date()
        label = f"{start} -> {end_excl}"
        return start, end_excl, label
    today = date.today()
    start = today - timedelta(days=days)
    label = f"Last {days} days ({start} -> {today})"
    return start, today, label


def load_period_slice(df_tx: pd.DataFrame,
                      start_d: date,
                      end_excl: date) -> pd.DataFrame:
    if df_tx.empty:
        return df_tx.iloc[0:0]
    df = df_tx.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    mask = (df["date"] >= pd.Timestamp(start_d)) & (df["date"] < pd.Timestamp(end_excl))
    return df.loc[mask].copy()


# --------------------------------------------------------------------
# Analysis helpers
# --------------------------------------------------------------------
def summarize_period(df_slice: pd.DataFrame) -> Dict[str, Any]:
    """
    Return totals, averages, by_category, by_merchant for the given slice.
    """
    out: Dict[str, Any] = {}
    if df_slice.empty:
        out["total_spent"] = 0.0
        out["avg_spent"] = 0.0
        out["by_category"] = pd.Series(dtype=float)
        out["by_merchant"] = pd.Series(dtype=float)
        return out

    # amount
    out["total_spent"] = float(df_slice["amount"].sum())
    out["avg_spent"] = float(df_slice["amount"].mean())

    # by category
    out["by_category"] = (
        df_slice
        .groupby("category", dropna=False)["amount"]
        .sum()
        .sort_values(ascending=False)
    )

    # by merchant
    # merchant fallback for nulls
    if "merchant_name" in df_slice.columns:
        merchant_series = df_slice["merchant_name"].fillna(df_slice.get("name"))
    else:
        merchant_series = df_slice.get("name")
    df_tmp = df_slice.copy()
    df_tmp["merchant_final"] = merchant_series.fillna("Unknown")
    out["by_merchant"] = (
        df_tmp
        .groupby("merchant_final", dropna=False)["amount"]
        .sum()
        .sort_values(ascending=False)
    )

    return out


def build_monthly_series(df_all: pd.DataFrame) -> pd.Series:
    """
    Sum amount by calendar month (YYYY-MM).
    """
    if df_all.empty:
        return pd.Series(dtype=float)
    tmp = df_all.copy()
    tmp["date"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp = tmp.dropna(subset=["date"])
    ser = (
        tmp
        .groupby(tmp["date"].dt.to_period("M"))["amount"]
        .sum()
        .sort_index()
    )
    ser.index = ser.index.astype(str)
    return ser

def compare_budget_vs_actual(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Join budgets with actual spending (summary_monthly).
    Returns a DataFrame with columns:
      month, category, budget_amount, actual_spend, variance, variance_pct
    """
    q = """
        SELECT
            b.month AS month,
            b.category AS category,
            b.amount AS budget_amount,
            s.total_spend AS actual_spend
        FROM budgets b
        LEFT JOIN summary_monthly s
          ON b.month = s.month AND b.category = s.category
        ORDER BY b.month, b.category
    """
    df = pd.read_sql_query(q, conn)
    if df.empty:
        write_log("No budget or summary data available for comparison.")
        return df

    df["actual_spend"] = df["actual_spend"].fillna(0.0)
    df["variance"] = df["budget_amount"] - df["actual_spend"]
    df["variance_pct"] = df.apply(
        lambda r: (r["variance"] / r["budget_amount"] * 100.0)
        if r["budget_amount"] != 0 else 0.0,
        axis=1,
    )
    return df

# --------------------------------------------------------------------
# Plot helpers
# --------------------------------------------------------------------
def save_bar(series: pd.Series, title: str, filename: str) -> Path:
    out_path = RESULTS_DIR / filename
    if series.empty:
        return out_path
    plt.figure(figsize=(8,4))
    series.head(10).plot(kind="bar", title=title)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    write_log(f"Saved chart {out_path}")
    return out_path


def save_line(series: pd.Series, title: str, filename: str) -> Path:
    out_path = RESULTS_DIR / filename
    if series.empty:
        return out_path
    plt.figure(figsize=(8,4))
    series.sort_index().astype(float).plot(kind="line", marker="o", title=title)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    write_log(f"Saved chart {out_path}")
    return out_path

def save_budget_chart(df: pd.DataFrame) -> Optional[Path]:
    if df.empty:
        return None
    path = RESULTS_DIR / "dev_budget_vs_actual.png"
    pivot = (
        df.pivot(index="category", columns="month", values="variance_pct")
        .fillna(0)
        .mean(axis=1)
        .sort_values()
    )
    plt.figure(figsize=(8, 5))
    pivot.plot(kind="barh", color=["#4CAF50" if v >= 0 else "#F44336" for v in pivot])
    plt.axvline(0, color="black", linewidth=0.8)
    plt.title("Budget vs Actual (Avg Variance % by Category)")
    plt.xlabel("Variance % (positive = under budget)")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    write_log(f"Saved budget variance chart → {path}")
    return path

# --------------------------------------------------------------------
# Excel / CSV export
# --------------------------------------------------------------------
def autosize_columns(ws) -> None:
    for col in ws.columns:
        max_len = 0
        col_letter = getattr(col[0], "column_letter", None)
        for cell in col:
            val = cell.value
            l = len(str(val)) if val is not None else 0
            if l > max_len:
                max_len = l
        if col_letter:
            ws.column_dimensions[col_letter].width = min(max_len + 2, 60)


def export_results(
    df_tables: pd.DataFrame,
    df_tx_window: pd.DataFrame,
    period_stats: Dict[str, Any],
    monthly_series: pd.Series,
    df_budget: pd.DataFrame,
    chart_paths: Dict[str, Path],
    label: str,
    timestamp: str,
) -> Path:
    """
    Write:
      - dev_analysis_summary_YYYYMMDD_HHMM.xlsx
      - dev_table_summary_YYYYMMDD_HHMM.csv
    """
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import numbers
    import pandas as pd

    # CSV of table row counts
    csv_path = RESULTS_DIR / f"dev_table_summary_{timestamp}.csv"
    df_tables.to_csv(csv_path, index=False)
    write_log(f"Wrote table summary CSV -> {csv_path}")

    # Excel workbook
    xlsx_path = RESULTS_DIR / f"dev_analysis_summary_{timestamp}.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        # 1. tables / counts
        df_tables.to_excel(writer, sheet_name="DB_TableCounts", index=False)

        # 2. period breakdown
        if "by_category" in period_stats:
            period_stats["by_category"].to_frame("amount").to_excel(
                writer, sheet_name="ByCategory"
            )
        if "by_merchant" in period_stats:
            period_stats["by_merchant"].head(25).to_frame("amount").to_excel(
                writer, sheet_name="TopMerchants"
            )

        # 3. monthly trend
        if not monthly_series.empty:
            monthly_series.to_frame("amount").to_excel(
                writer, sheet_name="MonthlyTrend"
            )

        # 4. raw window detail
        if not df_tx_window.empty:
            cols = [
                c for c in
                ["date","merchant_name","category","name","amount",
                 "transaction_id","account_id","item_id","iso_currency_code"]
                if c in df_tx_window.columns
            ]
            rem = [c for c in df_tx_window.columns if c not in cols]
            ordered = df_tx_window[cols + rem]
            ordered.to_excel(writer, sheet_name="WindowDetail", index=False)

        # 5. summary card
        summary_rows = [
            ("Period Window", label),
            ("Total Spent (Window)", period_stats.get("total_spent", 0.0)),
            ("Avg Transaction (Window)", period_stats.get("avg_spent", 0.0)),
            ("Rows In Window", int(df_tx_window.shape[0])),
        ]
        pd.DataFrame(summary_rows, columns=["metric","value"]).to_excel(
            writer, sheet_name="Summary", index=False
        )

        # 6. budget vs actual
        if not df_budget.empty:
            df_budget.to_excel(writer, sheet_name="Budget_vs_Actual", index=False)
            ws = writer.book["Budget_vs_Actual"]
            autosize_columns(ws)

        # formatting
        book = writer.book
        for sheetname in book.sheetnames:
            ws = book[sheetname]
            ws.freeze_panes = "A2"
            autosize_columns(ws)
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, (int, float)):
                        cell.number_format = numbers.FORMAT_NUMBER_COMMA_SEPARATED1

        # embed charts into Summary
        ws_summary = book["Summary"]
        next_anchor = "E2"
        if chart_paths.get("by_category") and chart_paths["by_category"].exists():
            img1 = XLImage(str(chart_paths["by_category"]))
            img1.anchor = next_anchor
            ws_summary.add_image(img1)
            next_anchor = "E20"
        if chart_paths.get("monthly") and chart_paths["monthly"].exists():
            img2 = XLImage(str(chart_paths["monthly"]))
            img2.anchor = next_anchor
            ws_summary.add_image(img2)
            next_anchor = "E38"
        if chart_paths.get("budget_vs_actual") and chart_paths["budget_vs_actual"].exists():
            img3 = XLImage(str(chart_paths["budget_vs_actual"]))
            img3.anchor = next_anchor
            ws_summary.add_image(img3)

    write_log(f"Wrote Excel analysis workbook -> {xlsx_path}")
    return xlsx_path


# --------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------
def run_analysis_dev(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> None:
    """
    Read-only analysis against plaid_dev.db.
    Produces dev_analysis_summary_<ts>.xlsx and related artifacts.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    start_d, end_excl, label = resolve_period(
        days=days, start_date=start_date, end_date=end_date
    )
    write_log(f"Analysis window: {label}")

    conn = connect_dev_db()
    try:
        df_tables = summarize_tables(conn)
        df_all = load_transactions(conn)
        df_window = load_period_slice(df_all, start_d, end_excl)
        period_stats = summarize_period(df_window)
        monthly_series = build_monthly_series(df_all)

        # budget comparison
        df_budget = compare_budget_vs_actual(conn)
        budget_chart = save_budget_chart(df_budget)

        # charts
        chart_paths = {
            "by_category": save_bar(
                period_stats.get("by_category", pd.Series(dtype=float)),
                "Spending by Category (window)",
                "dev_by_category.png",
            ),
            "monthly": save_line(
                monthly_series,
                "Monthly Spending Trend (all time)",
                "dev_monthly_trend.png",
            ),
            "budget_vs_actual": budget_chart,
        }

        export_results(
            df_tables=df_tables,
            df_tx_window=df_window,
            period_stats=period_stats,
            monthly_series=monthly_series,
            df_budget=df_budget,
            chart_paths=chart_paths,
            label=label,
            timestamp=ts,
        )

        write_log("Development analysis complete.")
    finally:
        conn.close()


if __name__ == "__main__":
    run_analysis_dev()
