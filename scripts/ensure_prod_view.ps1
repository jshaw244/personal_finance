# =====================================================================
# ensure_prod_view.ps1
# Ensures that v_monthly_summary exists in the active SQLite database.
# Uses src.common.paths.DB_FILE, so it respects ENV_TARGET.
# =====================================================================

Write-Host "Ensuring v_monthly_summary exists in active DB..."

$pythonExe = ".\.venv\Scripts\python.exe"

$pythonCode = @'
import sqlite3
from src.common.paths import DB_FILE

print("Active DB_FILE:", DB_FILE)

conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

# Create the view based on summary_monthly.
# Adjust aggregation here if you later change the schema.
sql = """
CREATE VIEW IF NOT EXISTS v_monthly_summary AS
SELECT
    month,
    SUM(total_spend) AS total_spend,
    SUM(num_txn)     AS num_txn,
    AVG(avg_txn)     AS avg_txn
FROM summary_monthly
GROUP BY month
"""
cur.executescript(sql)
conn.commit()

print("v_monthly_summary has been created or already existed.")
conn.close()
'@

& $pythonExe -c $pythonCode
