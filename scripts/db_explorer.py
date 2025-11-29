import sqlite3
import sys
from textwrap import shorten

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def list_tables(conn: sqlite3.Connection):
    print("\n=== Tables ===")
    rows = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
    """).fetchall()
    for r in rows:
        print(f"- {r['name']}")

def show_table_schema(conn: sqlite3.Connection, table: str):
    print(f"\n=== Schema for [{table}] ===")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for r in rows:
        cid = r["cid"]
        name = r["name"]
        col_type = r["type"]
        notnull = "NOT NULL" if r["notnull"] else ""
        dflt = f"DEFAULT {r['dflt_value']}" if r["dflt_value"] is not None else ""
        pk = "PRIMARY KEY" if r["pk"] else ""
        parts = " ".join(p for p in [col_type, notnull, dflt, pk] if p)
        print(f"{cid:2d}: {name} {parts}")

def preview_rows(conn: sqlite3.Connection, table: str, limit: int = 10):
    print(f"\n=== First {limit} rows of [{table}] ===")
    rows = conn.execute(f"SELECT * FROM {table} LIMIT {limit}").fetchall()
    if not rows:
        print("(no rows)")
        return

    cols = rows[0].keys()
    print(" | ".join(cols))
    print("-" * 80)
    for r in rows:
        line = " | ".join(str(r[c]) for c in cols)
        print(shorten(line, width=200, placeholder="…"))

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/explore_db.py path\\to\\database.db")
        sys.exit(1)

    db_path = sys.argv[1]
    conn = connect(db_path)

    # 1) Show tables
    list_tables(conn)

    # 2) For a few common tables in your Plaid app, show schemas and samples if present
    for table in ["items", "accounts", "transactions", "budgets", "financial_health_history"]:
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            continue  # table doesn’t exist, skip

        show_table_schema(conn, table)
        preview_rows(conn, table, limit=10)

    conn.close()

if __name__ == "__main__":
    main()
