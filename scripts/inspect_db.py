# scripts/inspect_db.py
import sqlite3
from pathlib import Path

DB_PATH = Path("data/plaid.db")

def inspect_db():
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH.resolve()}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("\n📊 Tables in database:")
    tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    if not tables:
        print("⚠️ No tables found.")
        return

    for (table_name,) in tables:
        print(f"\n🔎 {table_name}")
        columns = cur.execute(f"PRAGMA table_info({table_name});").fetchall()
        for col in columns:
            cid, name, ctype, notnull, default, pk = col
            print(f"   - {name} ({ctype}){' [PK]' if pk else ''}")

    conn.close()

if __name__ == "__main__":
    inspect_db()
