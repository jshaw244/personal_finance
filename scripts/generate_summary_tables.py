"""
generate_summary_tables.py
----------------------------------
Lightweight utility to (re)create and refresh summary tables in the database
without exporting any files.

Intended for:
    - Flask startup (`ensure_summary_views_and_tables()`)
    - Scheduled or maintenance tasks that only need DB-level tables

Creates / refreshes:
    summary_monthly
    summary_merchant
"""

import sqlite3
import sys
from datetime import datetime
import pandas as pd
from src.common.paths import DB_FILE, LOG_DIR
from src.analysis.analysis_summary import (
    ensure_tables,
    build_summary_monthly,
    build_summary_merchant,
    refresh_tables,
)

def log_event(conn: sqlite3.Connection, source: str, level: str, message: str) -> None:
    """Write event to log_events and console."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {level.upper()} {source}: {message}"
    print(line)
    try:
        conn.execute(
            "INSERT INTO log_events (timestamp, source, level, message) VALUES (?, ?, ?, ?)",
            (ts, source, level.upper(), message),
        )
        conn.commit()
    except Exception:
        pass

def main() -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            log_event(conn, "generate_summary_tables", "INFO", f"Refreshing summary tables at {DB_FILE}")
            ensure_tables(conn)

            # Load transactions
            q = """
                SELECT transaction_id, date, amount, name, merchant_name, category, pending
                FROM transactions
                WHERE IFNULL(pending, 0) = 0
            """
            df_tx = pd.read_sql_query(q, conn)

            if df_tx.empty:
                log_event(conn, "generate_summary_tables", "WARN", "No transactions found (pending=0). Tables will be empty.")
                df_monthly = pd.DataFrame(columns=["month", "category", "total_spend", "num_txn", "avg_txn"])
                df_merchant = pd.DataFrame(columns=["merchant", "total_spend", "avg_cadence_days", "months_active"])
            else:
                # Build summaries
                df_monthly = build_summary_monthly(df_tx)
                df_merchant = build_summary_merchant(df_tx)

            refresh_tables(conn, df_monthly, df_merchant)
            log_event(conn, "generate_summary_tables", "INFO", "Summary tables refreshed successfully.")
        return 0
    except Exception as e:
        print(f"[ERROR] generate_summary_tables failed: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
