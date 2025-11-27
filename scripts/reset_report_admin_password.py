import sqlite3
import pathlib
from werkzeug.security import generate_password_hash

from src.common.paths import DB_FILE

db_path = pathlib.Path(DB_FILE)
username = "finance_admin"
new_password = "NewPassword123"  # change if desired

print("\nUsing DB:", db_path)

if not db_path.exists():
    print("ERROR: Database file not found:", db_path)
    raise SystemExit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Make sure table exists
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = {t[0] for t in cur.fetchall()}
if "users" not in tables:
    print("ERROR: `users` table not found in DB")
    conn.close()
    raise SystemExit(1)

new_hash = generate_password_hash(new_password)

cur.execute(
    "UPDATE users SET password_hash = ? WHERE username = ?",
    (new_hash, username)
)

conn.commit()
conn.close()

print("\nPassword reset complete.")
print(f"Username: {username}")
print(f"New Password: {new_password}")
