import sqlite3
import pathlib
import os

# Determine which DB the app is actually using
# The app logs its active DB path in src/storage/db.py via DB_FILE
from src.common.paths import DB_FILE

db_path = pathlib.Path(DB_FILE)

print("\nChecking DB:", db_path, "\n")

if not db_path.exists():
    print("ERROR: Database file does not exist:", db_path)
    raise SystemExit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [t[0] for t in cur.fetchall()]
print("Tables:", tables)

def print_table(table_name):
    print(f"\nContents of {table_name}:")
    try:
        for row in conn.execute(f"SELECT * FROM {table_name}"):
            print(row)
    except Exception as e:
        print(f"(Error reading {table_name}):", e)

if "users" in tables:
    print_table("users")
elif "app_users" in tables:
    print_table("app_users")
else:
    print("\nNo users table found")

conn.close()
