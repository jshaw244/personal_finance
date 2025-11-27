# scripts/check_prod_view.ps1
Write-Host "Checking v_monthly_summary in active DB..."

$pythonExe = ".\.venv\Scripts\python.exe"
$pythonCode = @'
import sqlite3
from src.common.paths import DB_FILE

print("DB_FILE:", DB_FILE)
conn = sqlite3.connect(DB_FILE)

try:
    rows = conn.execute(
        "SELECT month, total_spend, num_txn FROM v_monthly_summary "
        "ORDER BY month DESC LIMIT 12"
    ).fetchall()
    print("v_monthly_summary rows:", len(rows))
    for r in rows:
        print(r)
except Exception as e:
    print("Error querying v_monthly_summary:", e)

conn.close()
'@

& $pythonExe -c $pythonCode
