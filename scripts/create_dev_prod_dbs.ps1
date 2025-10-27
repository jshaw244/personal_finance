# ./scripts/create_dev_prod_dbs.ps1
<#
.SYNOPSIS
    Create empty development and production SQLite databases using schema.sql.
.DESCRIPTION
    - Reads schema from src\storage\schema.sql
    - Creates data\plaid_dev.db and data\plaid_prod.db (no data)
    - Logs result to logs\maintenance.log
#>

Set-Location "C:\DATA\personal_finance"
$ErrorActionPreference = "Stop"

$pythonExe   = ".\.venv\Scripts\python.exe"
$schemaPath  = ".\src\storage\schema.sql"
$logFile     = ".\logs\maintenance.log"

if (-not (Test-Path $schemaPath)) {
    Write-Host "Error: schema.sql not found at $schemaPath" -ForegroundColor Red
    exit 1
}

# Read schema
$schema = Get-Content $schemaPath -Raw

# Temporary Python helper
$tempPy = [IO.Path]::GetTempFileName() + ".py"

@"
import sqlite3, pathlib, datetime, os

root = pathlib.Path(r"C:\DATA\personal_finance")
schema = r'''$schema'''
log_file = root / "logs" / "maintenance.log"

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(msg)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] [CREATE_DEV_PROD_DBS] {msg}\n")

for name in ["plaid_dev.db", "plaid_prod.db"]:
    db_path = root / "data" / name
    if db_path.exists():
        log(f"Existing {db_path.name} found; deleting old copy.")
        os.remove(db_path)
    log(f"Creating {db_path.name} ...")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    log(f"Created {db_path.name} successfully.")

log("All databases created successfully.")
"@ | Out-File -FilePath $tempPy -Encoding UTF8

# Execute
& $pythonExe $tempPy
Remove-Item $tempPy -Force
