"""
Unified analytics suite for the personal_finance project.

Enhancements:
  • Adds flexible rolling window (default 30 days)
  • Supports --days, --start, --end overrides
  • Preserves all prior MoM, YTD, cumulative, chart, and Excel features
"""

from __future__ import annotations
import os
import sqlite3
import calendar
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Tuple, Dict, Any, Optional

import pandas as pd
import matplotlib.pyplot as plt

# -- Project paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = PROJECT_ROOT / "data" / "plaid.db"
RESULTS_DIR = PROJECT_ROOT / "results"
LOG_FILE = PROJECT_ROOT / "logs" / "maintenance.log"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)


# -------------------------------------------
# Logging
# -------------------------------------------
def write_log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] ANALYSIS — {message}\n")
    print(message)


# -------------------------------------------
# Utilities
# -------------------------------------------
def connect_to_db(env_target: str = "sandbox") -> sqlite3.Connection:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DATA_PATH}")
    write_log(f"Connecting to {env_target} database: {DATA_PATH}")
    conn = sqlite3.connect(str(DATA_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def last_full_month() -> Tuple[date, date]:
    """Return (start_date, end_date_exclusive) for the last full calendar month."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    end_exclusive = first_of_this_month
    # previous month
    if first_of_this_month.month == 1:
        start = date(first_of_this_month.year - 1, 12, 1)
    else:
        start = date(first_of_this_month.year, first_of_this_month.month - 1, 1)
    return start, end_exclusive


def resolve_analysis_window(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Tuple[date, date, str]:
    """Resolve analysis period, defaulting to rolling 30 days if no args."""
    if start_date and end_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        label = f"{start} → {end}"
        return start, end, label
    today = date.today()
    start = today - timedelta(days=days)
    label = f"Last {days} days ({start} → {today})"
    return start, today, label


def trailing_months_endpoints(n_months: int, end_exclusive: date) -> Tuple[date, date]:
    """Start date n months before end_exclusive; aligned to month starts."""
    y, m = end_exclusive.year, end_exclusive.month
    m -= n_months
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    return start, end_exclusive


def summarize_tables(conn: sqlite3.Connection) -> pd.DataFrame:
    tables = pd.read_sql_query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;", conn
    )
    rows = []
    for t in tables["name"]:
        try:
            n = pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {t};", conn)["n"][0]
        except Exception as e:
            n = f"Error: {e}"
        rows.append({"table": t, "row_count": n})
    return pd.DataFrame(rows)


# -------------------------------------------
# Data access
# -------------------------------------------
def load_all_transactions(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM transactions;", conn)


def load_transactions_for_period(conn: sqlite3.Connection, start: date, end_exclusive: date) -> pd.DataFrame:
    q = """
        SELECT *
        FROM transactions
        WHERE date >= ? AND date < ?
    """
    return pd.read_sql_query(q, conn, params=[start.isoformat(), end_exclusive.isoformat()])


# -------------------------------------------
# Analysis
# -------------------------------------------
def analyze_transactions_period(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if df.empty:
        out["total_spent"] = 0.0
        out["avg_spent"] = 0.0
        out["by_category"] = pd.Series(dtype=float)
        out["by_merchant"] = pd.Series(dtype=float)
        return out

    if "amount" in df.columns:
        out["total_spent"] = float(df["amount"].sum())
        out["avg_spent"] = float(df["amount"].mean())

    if "category" in df.columns and "amount" in df.columns:
        out["by_category"] = (
            df.groupby("category", dropna=False)["amount"].sum().sort_values(ascending=False)
        )

    if "merchant_name" in df.columns and "amount" in df.columns:
        out["by_merchant"] = (
            df.groupby("merchant_name", dropna=False)["amount"].sum().sort_values(ascending=False)
        )

    return out


def monthly_trend(df_all: pd.DataFrame, months: int, end_exclusive: date) -> pd.Series:
    if df_all.empty:
        return pd.Series(dtype=float)
    start, _ = trailing_months_endpoints(months, end_exclusive)
    df_all = df_all.copy()
    df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
    df_all = df_all[(df_all["date"] >= pd.Timestamp(start)) & (df_all["date"] < pd.Timestamp(end_exclusive))]
    if df_all.empty:
        return pd.Series(dtype=float)
    return df_all.groupby(df_all["date"].dt.to_period("M"))["amount"].sum().sort_index()


def year_to_date_total(df_all: pd.DataFrame, asof_month_end_exclusive: date) -> float:
    if df_all.empty:
        return 0.0
    prev_day = asof_month_end_exclusive - timedelta(days=1)
    start_of_year = date(prev_day.year, 1, 1)
    df = df_all.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    mask = (df["date"] >= pd.Timestamp(start_of_year)) & (df["date"] < pd.Timestamp(asof_month_end_exclusive))
    return float(df.loc[mask, "amount"].sum())


def month_over_month_change(df_all: pd.DataFrame, start: date, end_exclusive: date) -> Dict[str, float]:
    """Compare analysis month vs previous month."""
    cur_total = float(load_sum_for_window(df_all, start, end_exclusive))
    prev_end = start
    if start.month == 1:
        prev_start = date(start.year - 1, 12, 1)
    else:
        prev_start = date(start.year, start.month - 1, 1)
    prev_total = float(load_sum_for_window(df_all, prev_start, prev_end))
    delta = cur_total - prev_total
    pct = (delta / prev_total * 100.0) if prev_total != 0 else float("inf") if cur_total != 0 else 0.0
    return {"current": cur_total, "previous": prev_total, "delta": delta, "pct_change": pct}


def load_sum_for_window(df_all: pd.DataFrame, start: date, end_exclusive: date) -> float:
    if df_all.empty:
        return 0.0
    df = df_all.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    mask = (df["date"] >= pd.Timestamp(start)) & (df["date"] < pd.Timestamp(end_exclusive))
    return float(df.loc[mask, "amount"].sum())


# -------------------------------------------
# Plot helpers
# -------------------------------------------
def _save_bar(series: pd.Series, title: str, filename: str) -> Path:
    path = RESULTS_DIR / filename
    plt.figure(figsize=(8, 4))
    series.head(10).plot(kind="bar", title=title)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    write_log(f"Saved {title} → {path}")
    return path


def _save_line(series: pd.Series, title: str, filename: str) -> Path:
    path = RESULTS_DIR / filename
    plt.figure(figsize=(8, 4))
    series.sort_index().astype(float).plot(kind="line", marker="o", title=title)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    write_log(f"Saved {title} → {path}")
    return path


# -------------------------------------------
# Excel export (with chart embedding)
# -------------------------------------------
def autosize_columns(ws) -> None:
    for col in ws.columns:
        max_len = 0
        col_letter = getattr(col[0], "column_letter", None)
        for cell in col:
            try:
                v = cell.value
                l = len(str(v)) if v is not None else 0
                if l > max_len:
                    max_len = l
            except Exception:
                pass
        if col_letter:
            ws.column_dimensions[col_letter].width = min(max_len + 2, 60)


def export_to_excel(
    table_summary: pd.DataFrame,
    period_summaries: Dict[str, Any],
    monthly_series: pd.Series,
    cumulative_series: pd.Series,
    detail_df: pd.DataFrame,
    summary_cards: Dict[str, Any],
    target: str,
    ts: str,
    chart_paths: Dict[str, Optional[Path]],
) -> Path:
    from openpyxl.drawing.image import Image as XLImage

    excel_path = RESULTS_DIR / f"{target}_analysis_summary_{ts}.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        table_summary.to_excel(writer, sheet_name="TableCounts", index=False)
        if "by_category" in period_summaries:
            period_summaries["by_category"].to_frame("amount").to_excel(writer, sheet_name="ByCategory")
        if "by_merchant" in period_summaries:
            period_summaries["by_merchant"].head(25).to_frame("amount").to_excel(writer, sheet_name="TopMerchants")
        if not monthly_series.empty:
            monthly_series.to_frame("amount").to_excel(writer, sheet_name="MonthlyTrend")
        if not cumulative_series.empty:
            cumulative_series.to_frame("cumulative").to_excel(writer, sheet_name="Cumulative")
        if not detail_df.empty:
            cols = [c for c in ["date", "merchant_name", "category", "name", "amount", "transaction_id", "account_id"] if c in detail_df.columns]
            rem = [c for c in detail_df.columns if c not in cols]
            ordered = detail_df[cols + rem]
            ordered.to_excel(writer, sheet_name="Transactions_Detail", index=False)
        pd.DataFrame.from_records(
            [{"metric": k, "value": v} for k, v in summary_cards.items()]
        ).to_excel(writer, sheet_name="Summary", index=False)

        book = writer.book
        for name in ["Summary", "ByCategory", "TopMerchants", "MonthlyTrend", "Cumulative", "Transactions_Detail"]:
            if name in book.sheetnames:
                ws = book[name]
                ws.freeze_panes = "A2"
                autosize_columns(ws)
                for row in ws.iter_rows(min_row=2):
                    for cell in row:
                        if isinstance(cell.value, (int, float)):
                            cell.number_format = "#,##0.00"

        ws_summary = book["Summary"]
        next_anchor = "E2"
        if chart_paths.get("by_category") and chart_paths["by_category"].exists():
            img1 = XLImage(str(chart_paths["by_category"]))
            img1.anchor = next_anchor
            ws_summary.add_image(img1)
            next_anchor = "E18"
        if chart_paths.get("monthly") and chart_paths["monthly"].exists():
            img2 = XLImage(str(chart_paths["monthly"]))
            img2.anchor = next_anchor
            ws_summary.add_image(img2)

    write_log(f"Wrote combined Excel summary → {excel_path}")
    return excel_path


# -------------------------------------------
# Entrypoint
# -------------------------------------------
def run_full_analysis(
    target: str = "sandbox",
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    months_for_trend: int = 12,
) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    start, end_excl, label = resolve_analysis_window(days=days, start_date=start_date, end_date=end_date)
    write_log(f"Analysis window: {label}")

    conn = connect_to_db(target)
    try:
        table_summary = summarize_tables(conn)
        all_tx = load_all_transactions(conn)
        period_tx = load_transactions_for_period(conn, start, end_excl)

        period_stats = analyze_transactions_period(period_tx)
        m_series = monthly_trend(all_tx, months_for_trend, end_excl)
        c_series = m_series.cumsum() if not m_series.empty else pd.Series(dtype=float)

        mom = month_over_month_change(all_tx, start, end_excl)
        ytd_total = year_to_date_total(all_tx, end_excl)

        chart_paths = {
            "by_category": None,
            "monthly": None,
        }
        if "by_category" in period_stats and not period_stats["by_category"].empty:
            chart_paths["by_category"] = _save_bar(period_stats["by_category"], "Spending by Category", f"{target}_by_category.png")
        if not m_series.empty:
            chart_paths["monthly"] = _save_line(m_series, "Monthly Spending Trend", f"{target}_monthly_trend.png")

        summary_cards = {
            "Period": label,
            "Total Spent (Period)": period_stats.get("total_spent", 0.0),
            "Average Transaction (Period)": period_stats.get("avg_spent", 0.0),
            "MoM Current": mom["current"],
            "MoM Previous": mom["previous"],
            "MoM Delta": mom["delta"],
            "MoM % Change": mom["pct_change"],
            "YTD Total (as of period end)": ytd_total,
            "Rows in Transactions_Detail": int(period_tx.shape[0]),
        }

        excel_path = export_to_excel(
            table_summary=table_summary,
            period_summaries=period_stats,
            monthly_series=m_series,
            cumulative_series=c_series,
            detail_df=period_tx,
            summary_cards=summary_cards,
            target=target,
            ts=ts,
            chart_paths=chart_paths,
        )

        csv_path = RESULTS_DIR / f"{target}_table_summary_{ts}.csv"
        table_summary.to_csv(csv_path, index=False)
        write_log(f"Wrote CSV summary → {csv_path}")
        write_log("Full analysis pipeline complete.")
    finally:
        conn.close()
