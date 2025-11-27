# =====================================================================
# check_prod_summary.ps1
# Checks summary_monthly, summary_merchant, and transaction counts
# for the active database selected by ENV_TARGET.
# =====================================================================

Write-Host "Running production summary check..."

$pythonExe = ".\.venv\Scripts\python.exe"

# Python code stored as a PowerShell literal string (ASCII only)
$pythonCode = @'
import sqlite3
from src.common.paths import DB_FILE

print("------------------------------------------------------------")
print("Active DB_FILE:", DB_FILE)
print("------------------------------------------------------------")

conn = sqlite3.connect(DB_FILE)

n_tx = conn.execute(
    "SELECT COUNT(*) FROM transactions WHERE IFNULL(pending,0)=0"
).fetchone()[0]
print("Non-pending transactions:", n_tx)

n_sm = conn.execute(
    "SELECT COUNT(*) FROM summary_monthly"
).fetchone()[0]
print("summary_monthly rows:", n_sm)

n_merch = conn.execute(
    "SELECT COUNT(*) FROM summary_merchant"
).fetchone()[0]
print("summary_merchant rows:", n_merch)

print("\nSample summary_monthly rows:")
for row in conn.execute(
    "SELECT month, category, total_spend, num_txn "
    "FROM summary_monthly ORDER BY month DESC, total_spend DESC LIMIT 10"
):
    print(row)

print("\nSample summary_merchant rows:")
for row in conn.execute(
    "SELECT merchant, total_spend, months_active "
    "FROM summary_merchant ORDER BY total_spend DESC LIMIT 10"
):
    print(row)

conn.close()
'@

# Execute Python using -c
& $pythonExe -c $pythonCode
