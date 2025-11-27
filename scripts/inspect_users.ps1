# scripts/inspect_users.ps1
# View users currently stored in the production reports DB

$pythonExe = ".\.venv\Scripts\python.exe"
$dbPath    = "C:\DATA\personal_finance\data\plaid_prod.db"

$code = @"
import sqlite3

db = r"$dbPath"
conn = sqlite3.connect(db)
cur = conn.cursor()

try:
    rows = cur.execute("SELECT username, password_hash, created_at FROM users").fetchall()
    print("Users table content:")
    if not rows:
        print("  (no users found)")
    for row in rows:
        print(row)
except Exception as e:
    print("Error:", e)

conn.close()
"@

# Run the Python code
$code | & $pythonExe
