# ./scripts/create_dev_prod_dbs.ps1
<#
.SYNOPSIS
    Create empty development and production SQLite databases using schema.sql.
#>

Set-Location "C:\DATA\personal_finance"

$pythonExe = ".\.venv\Scripts\python.exe"
$schemaPath = ".\src\storage\schema.sql"

if (-not (Test-Path $schemaPath)) {
    Write-Host "Error: schema.sql not found at $schemaPath" -ForegroundColor Red
    exit 1
}

# Read schema file content safely
$schema = Get-Content $schemaPath -Raw

# Build a small temporary Python script
$tempPy = [IO.Path]::GetTempFileName() + ".py"

@"
import sqlite3, pathlib

root = pathlib.Path(r"C:\DATA\personal_finance")
schema = r'''$schema'''

for name in ["plaid_dev.db", "plaid_prod.db"]:
    db_path = root / "data" / name
    print(f"→ Creating {db_path} ...")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.commit()
    conn.close()

print("✅ Done.")
"@ | Out-File -FilePath $tempPy -Encoding UTF8

# Run it
& $pythonExe $tempPy

# Clean up
Remove-Item $tempPy -Force

