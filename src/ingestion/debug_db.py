import sqlite3
import sys
import csv
import time
import logging
import shutil
import json
from datetime import datetime, timedelta
from pathlib import Path
from src.common.utils import convert, to_safe_json
from src.storage.db import log_event_db, get_connection

from src.common.paths import PROJECT_ROOT, DB_FILE
print("DEBUG DB_FILE:", DB_FILE)  # TEMPORARY DEBUG
# -----------------------------
# Logging setup
# -----------------------------
from src.common.paths import LOG_DIR, DB_FILE
LOG_FILE = LOG_DIR / "maintenance.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def log_event(message):
    logging.info(message)
    print(message)
    log_event_db("debug", "INFO", message)

def log_error(message):
    logging.error(message)
    print("ERROR:", message)
    log_event_db("debug", "ERROR", message)

# -----------------------------
# Backup helper
# -----------------------------
def backup_db():
    """Make a timestamped copy of plaid.db into /backups folder."""
    if DB_FILE.exists():
        backup_dir = DB_FILE.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
        backup_file = backup_dir / f"plaid_{ts}.db"
        shutil.copy(DB_FILE, backup_file)
        log_event(f"💾 Database backed up to {backup_file}")
    else:
        log_error("No database file found to back up.")

# -----------------------------
# Maintenance helpers
# -----------------------------
def update_maintenance(key):
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO maintenance_log (key, last_run)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET last_run=excluded.last_run
        """, (key, now))

def get_last_run(key):
    with get_connection() as conn:
        cur = conn.execute("SELECT last_run FROM maintenance_log WHERE key=?", (key,))
        row = cur.fetchone()
    return row[0] if row else None

def vacuum_db(force=False):
    """Compact DB if forced or if 7+ days since last run."""
    last_run = get_last_run("vacuum")
    should_run = force
    if last_run and not force:
        last_time = datetime.fromisoformat(last_run)
        if datetime.utcnow() - last_time > timedelta(days=7):
            should_run = True
    elif not last_run:
        should_run = True

    if should_run:
        # Backup before vacuum if forced
        if force:
            backup_db()
        with get_connection() as conn:
            conn.execute("VACUUM")
        update_maintenance("vacuum")
        log_event("Database vacuumed (compacted).")
    else:
        log_event("Skipping VACUUM (recently run).")

def analyze_db():
    """Always update statistics."""
    with get_connection() as conn:
        conn.execute("ANALYZE")
    update_maintenance("analyze")
    log_event("Database analyzed (statistics updated).")

def status_db():
    """Show DB size, counts, date ranges, and maintenance history."""
    size = DB_FILE.stat().st_size
    size_mb = size / (1024 * 1024)
    print(f"Database file: {DB_FILE}")
    print(f"File size: {size_mb:.2f} MB ({size:,} bytes)")

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            print("No tables found.")
            return

        print("\nRow counts:")
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")

        if "transactions" in tables:
            cur = conn.execute("SELECT MIN(date), MAX(date) FROM transactions")
            first, last = cur.fetchone()
            print("\nTransactions date range:")
            print(f"  First: {first}")
            print(f"  Last:  {last}")

        print("\nMaintenance:")
        for key in ["analyze", "vacuum"]:
            last = get_last_run(key)
            print(f"  Last {key}: {last or 'never'}")

def maintain_db(force=False):
    """Run maintenance: analyze always, vacuum conditionally or forced."""
    log_event("Running maintenance...")
    analyze_db()
    vacuum_db(force=force)
    print("\n--- Database Status ---")
    status_db()

# -----------------------------
# Data fetch helpers
# -----------------------------
def fetch_items(all_columns=False):
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        if all_columns:
            rows = conn.execute("SELECT * FROM items").fetchall()
        else:
            rows = conn.execute(
                "SELECT item_id, access_token, institution FROM items"
            ).fetchall()
    return [dict(row) for row in rows]

def fetch_transactions(limit=20, item_id=None, start_date=None, end_date=None):
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []
        if item_id:
            query += " AND item_id = ?"
            params.append(item_id)
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]

def fetch_latest(table, limit=10):
    """Fetch latest rows from any table, ordered by date if present, else rowid."""
    # Special-case: allow 'accounts' even without a table (routes to accounts command)
    if table.lower() == "accounts":
        _print_accounts(limit)
        return []

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        query = f"SELECT * FROM {table}"
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cur.fetchall()]
        if not cols:
            raise sqlite3.OperationalError(f"no such table: {table}")
        if "date" in cols:
            query += " ORDER BY date DESC LIMIT ?"
        else:
            query += " ORDER BY rowid DESC LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
    for r in rows:
        print(to_safe_json(dict(r)))
    return [dict(row) for row in rows]

def fetch_since(table, start_date, end_date=None, item_id=None):
    """Fetch all rows since a given date (optionally with end_date and item_id)."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        query = f"SELECT * FROM {table} WHERE 1=1"
        params = []
        if item_id:
            query += " AND item_id = ?"
            params.append(item_id)
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC"
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]

def fetch_logs(limit=20, since=None, level=None, source=None):
    """Fetch logs from log_events table with optional filters."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM log_events WHERE 1=1"
        params = []
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        if level:
            query += " AND level = ?"
            params.append(level.upper())
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(r) for r in rows]

# -----------------------------
# Accounts helper (NEW)
# -----------------------------
def _accounts_view_exists():
    with get_connection() as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='v_accounts'")
        return cur.fetchone() is not None

def _fetch_accounts_from_view(limit=None):
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM v_accounts"
        if limit:
            q += " ORDER BY rowid DESC LIMIT ?"
            rows = conn.execute(q, (limit,)).fetchall()
        else:
            rows = conn.execute(q).fetchall()
    return [dict(r) for r in rows]

def _fetch_accounts_from_http(base_url="http://127.0.0.1:5000", timeout=5, limit=None):
    """Call running Flask app /accounts when no accounts table/view exists."""
    import urllib.request, urllib.error
    url = f"{base_url}/accounts"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            accounts = data.get("accounts") or []
            if isinstance(limit, int):
                accounts = accounts[:limit]
            return accounts
    except urllib.error.URLError as e:
        log_error(f"Failed to GET {url}: {e}")
        return []

def _print_accounts(limit=None):
    """Print accounts either from v_accounts view or via live HTTP fallback."""
    if _accounts_view_exists():
        rows = _fetch_accounts_from_view(limit)
        if not rows:
            print("No accounts found in v_accounts.")
        else:
            print(to_safe_json(rows))
        return

    rows = _fetch_accounts_from_http(limit=limit)
    if not rows:
        print("No accounts available (no v_accounts view and /accounts not reachable).")
    else:
        print(to_safe_json(rows))

# -----------------------------
# Schema & debug helpers
# -----------------------------
def show_schema(cols_filter=None):
    with get_connection() as conn:
        cur = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        rows = cur.fetchall()
    if not rows:
        print("No tables found.")
        return
    if cols_filter and cols_filter == "name":
        for name, _ in rows:
            print(name)
    else:
        for name, sql in rows:
            print(f"\nTable: {name}\n{sql}\n")

def describe_table(table):
    """Show columns, types, and constraints for a given table."""
    with get_connection() as conn:
        cur = conn.execute(f"PRAGMA table_info({table})")
        rows = cur.fetchall()
    if not rows:
        print(f"Table '{table}' does not exist.")
        return
    headers = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
    print(" | ".join(headers))
    print("-" * 70)
    for r in rows:
        print(" | ".join(str(x) if x is not None else "" for x in r))

def show_indexes(table=None):
    with get_connection() as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            print("No tables found.")
            return
        if table and table not in tables:
            print(f"Table '{table}' does not exist.")
            return
        target_tables = [table] if table else tables
        for t in target_tables:
            print(f"\nIndexes for table: {t}")
            idx_list = conn.execute(f"PRAGMA index_list({t})").fetchall()
            if not idx_list:
                print("  (none)")
                continue
            for idx in idx_list:
                idx_name = idx[1]
                unique = "UNIQUE" if idx[2] else ""
                print(f"  {idx_name} {unique}")
                idx_info = conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
                for col in idx_info:
                    print(f"    column: {col[2]}")

def show_counts(table_name=None):
    with get_connection() as conn:
        if table_name:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                print(f"{table_name}: {count} rows")
            except sqlite3.OperationalError:
                print(f"Table '{table_name}' does not exist.")
        else:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            if not tables:
                print("No tables found.")
                return
            for table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{table}: {count} rows")

# -----------------------------
# CLI entrypoint
# -----------------------------
def main():
    if len(sys.argv) == 1:
        items = fetch_items(all_columns=False)
        if not items:
            print("No items stored yet.")
        else:
            print(to_safe_json(items))
        return

    arg = sys.argv[1].lower()

    if arg == "backup":
        backup_db()

    elif arg == "items":
        items = fetch_items(all_columns=False)
        if not items:
            print("No items stored yet.")
        else:
            print(to_safe_json(items))

    elif arg == "accounts":  # NEW: show accounts (view or live)
        limit = None
        if len(sys.argv) >= 3 and sys.argv[2].isdigit():
            limit = int(sys.argv[2])
        _print_accounts(limit)

    elif arg == "all":
        items = fetch_items(all_columns=True)
        if not items:
            print("No items stored yet.")
        else:
            print(to_safe_json(items))

    elif arg in ("txns", "transactions"):
        limit = 20
        item_id, start_date, end_date = None, None, None
        if len(sys.argv) >= 3:
            try:
                limit = int(sys.argv[2])
            except ValueError:
                item_id = sys.argv[2]
        if len(sys.argv) >= 4:
            start_date = sys.argv[3]
        if len(sys.argv) >= 5:
            end_date = sys.argv[4]
        txns = fetch_transactions(limit=limit, item_id=item_id,
                                  start_date=start_date, end_date=end_date)
        if not txns:
            print("No transactions stored yet.")
        else:
            print(to_safe_json(txns))

    elif arg == "latest":
        if len(sys.argv) < 3:
            print("Usage: latest [table] [limit]")
            return
        table = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) >= 4 else 10
        # NEW: if user asks "latest accounts", route to accounts logic
        if table.lower() == "accounts":
            _print_accounts(limit)
        else:
            try:
                rows = fetch_latest(table, limit=limit)
                if not rows:
                    print(f"No rows found in {table}.")
            except sqlite3.OperationalError as e:
                print(str(e))

    elif arg == "since":
        if len(sys.argv) < 3:
            print("Usage: since [table] [start_date] [end_date] [item_id]")
            return
        table = sys.argv[2]
        start_date = sys.argv[3] if len(sys.argv) >= 4 else None
        end_date = sys.argv[4] if len(sys.argv) >= 5 else None
        item_id = sys.argv[5] if len(sys.argv) >= 6 else None
        rows = fetch_since(table, start_date, end_date, item_id)
        if not rows:
            print(f"No rows found in {table} since {start_date or 'the beginning'}.")
        else:
            print(to_safe_json(rows))

    elif arg == "logs":
        limit = 20
        since, level, source = None, None, None

        if len(sys.argv) >= 3 and sys.argv[2].isdigit():
            limit = int(sys.argv[2])
        if len(sys.argv) >= 3 and sys.argv[2].lower() == "since":
            since = sys.argv[3] if len(sys.argv) >= 4 else None
            limit = 1000
        if len(sys.argv) >= 3 and sys.argv[2].lower() == "level":
            level = sys.argv[3] if len(sys.argv) >= 4 else None
        if len(sys.argv) >= 3 and sys.argv[2].lower() == "source":
            source = sys.argv[3] if len(sys.argv) >= 4 else None

        logs = fetch_logs(limit=limit, since=since, level=level, source=source)
        if not logs:
            print("No logs found.")
        else:
            for r in logs:
                ts = r["timestamp"]
                src = r["source"]
                lvl = r["level"]
                msg = r["message"]

                # Color coding
                if lvl == "ERROR":
                    color = "\033[91m"  # red
                elif lvl == "WARNING":
                    color = "\033[93m"  # yellow
                elif lvl == "INFO":
                    color = "\033[92m"  # green
                else:
                    color = "\033[0m"   # default

                reset = "\033[0m"
                print(f"{color}{ts} [{src}] {lvl}: {msg}{reset}")

    elif arg == "schema":
        cols_filter = sys.argv[2] if len(sys.argv) >= 3 else None
        show_schema(cols_filter=cols_filter)

    elif arg == "describe":
        if len(sys.argv) < 3:
            print("Usage: describe [table]")
            return
        table = sys.argv[2]
        describe_table(table)

    elif arg == "indexes":
        table = sys.argv[2] if len(sys.argv) >= 3 else None
        show_indexes(table)

    elif arg == "count":
        table = sys.argv[2] if len(sys.argv) >= 3 else None
        show_counts(table)

    elif arg == "maintain":
        force = len(sys.argv) >= 3 and sys.argv[2] == "--force"
        maintain_db(force=force)

    elif arg == "status":
        status_db()

    else:
        print("Usage:")
        print("  python src/debug_db.py backup                 # backup DB now")
        print("  python src/debug_db.py items                  # list stored items")
        print("  python src/debug_db.py accounts [N]           # show accounts (DB view or live HTTP)")
        print("  python src/debug_db.py all                    # list items with all columns")
        print("  python src/debug_db.py txns [N]               # list last N transactions")
        print("  python src/debug_db.py latest [table] [N]     # show latest N rows from a table")
        print("  python src/debug_db.py since [table] [date]   # show rows since a date")
        print("  python src/debug_db.py logs [N]               # show last N logs (default 20)")
        print("  python src/debug_db.py logs since YYYY-MM-DD  # show logs since a date")
        print("  python src/debug_db.py logs level ERROR       # filter logs by level")
        print("  python src/debug_db.py logs source app        # filter logs by source (app/debug/runscript)")
        print("  python src/debug_db.py schema [name]          # show schema (or just names)")
        print("  python src/debug_db.py describe [table]       # describe table structure")
        print("  python src/debug_db.py indexes [table]        # show indexes")
        print("  python src/debug_db.py count [table]          # count rows")
        print("  python src/debug_db.py maintain [--force]     # run maintenance tasks")
        print("  python src/debug_db.py status                 # show DB size, counts, ranges")
        sys.exit(1)

if __name__ == "__main__":
    main()
