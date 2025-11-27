# Reset the login user for /reports
# Creates user: finance_admin / NewPassword123

$ErrorActionPreference = "Stop"

$python = "C:\DATA\personal_finance\.venv\Scripts\python.exe"
$db = "C:\DATA\personal_finance\data\plaid_prod.db"

$script = @"
import sqlite3
from werkzeug.security import generate_password_hash

db = r"$db"
username = "finance_admin"
password = "NewPassword123"
pwd_hash = generate_password_hash(password)

conn = sqlite3.connect(db)
cur = conn.cursor()

# Ensure table exists
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password_hash TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

# Remove any existing user with same username
cur.execute("DELETE FROM users WHERE username = ?", (username,))

# Insert fresh credentials
cur.execute(
    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
    (username, pwd_hash)
)

conn.commit()
conn.close()

print("User reset complete.")
"@

$temp = [System.IO.Path]::GetTempFileName() + ".py"
$script | Out-File -FilePath $temp -Encoding UTF8

& $python $temp
Remove-Item $temp -Force

Write-Host "Reset done. User is now: finance_admin / NewPassword123"
