# scripts/set_reports_password.ps1
# Reset reports login password in production DB

$pythonExe = ".\.venv\Scripts\python.exe"
$dbPath    = "C:\DATA\personal_finance\data\plaid_prod.db"
$newPass   = "FinanceApp2025!"   # <-- You can change this if desired

$code = @"
import sqlite3
from werkzeug.security import generate_password_hash

db = r"$dbPath"
pw = "$newPass"
conn = sqlite3.connect(db)
cur = conn.cursor()

hashed = generate_password_hash(pw, method="scrypt")
cur.execute("UPDATE users SET password_hash=? WHERE username='finance_admin'", (hashed,))
conn.commit()
conn.close()

print("Password updated successfully.")
print("New login username: finance_admin")
print("New login password:", pw)
"@

$code | & $pythonExe
