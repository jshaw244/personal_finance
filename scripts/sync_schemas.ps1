<#
.SYNOPSIS
    Synchronize SQLite databases (sandbox, development, production) with schema.sql.
.DESCRIPTION
    - Compares modification times of each target database with src/storage/schema.sql
    - If schema.sql is newer, or if -Force is used, rebuilds that database
    - Optionally includes sandbox (plaid.db) when -IncludeSandbox is set
    - Creates timestamped backup before rebuilding any database
.PARAMETER Force
    Force rebuild regardless of timestamps.
.PARAMETER IncludeSandbox
    Include sandbox database (plaid.db) in the sync process.
#>

param(
    [switch]$Force,
    [switch]$IncludeSandbox
)

$ErrorActionPreference = "Stop"
Set-Location "C:\DATA\personal_finance"

$pythonExe   = ".\.venv\Scripts\python.exe"
$schemaPath  = ".\src\storage\schema.sql"
$dataDir     = ".\data"

if (-not (Test-Path $schemaPath)) {
    Write-Host "ERROR: schema.sql not found at $schemaPath"
    exit 1
}

# Get schema file timestamp
$schemaTime = (Get-Item $schemaPath).LastWriteTimeUtc

# Determine databases to process
$targets = @()
if ($IncludeSandbox) { $targets += "plaid.db" }
$targets += "plaid_dev.db"
$targets += "plaid_prod.db"

foreach ($db in $targets) {
    $dbPath = Join-Path $dataDir $db
    $needsRebuild = $Force

    if (Test-Path $dbPath) {
        $dbTime = (Get-Item $dbPath).LastWriteTimeUtc
        if ($schemaTime -gt $dbTime) {
            $needsRebuild = $true
            Write-Host "NOTICE: $db is older than schema.sql. Rebuild required."
        } else {
            Write-Host "OK: $db is up to date."
        }
    } else {
        $needsRebuild = $true
        Write-Host "NOTICE: $db does not exist. It will be created."
    }

    if ($needsRebuild) {
        # Backup existing database if present
        if (Test-Path $dbPath) {
            $timestamp = Get-Date -Format "yyyyMMdd_HHmm"
            $backupPath = "$dbPath.bak_$timestamp"
            try {
                Copy-Item -Path $dbPath -Destination $backupPath -ErrorAction Stop
                Write-Host ("Backup created: {0}" -f $backupPath)
            } catch {
                Write-Host ("WARNING: Could not create backup for {0}: {1}" -f $dbPath, $_)
            }
        }

        # Read schema and rebuild
        $schema = Get-Content $schemaPath -Raw
        $tempPy = [IO.Path]::GetTempFileName() + ".py"

        @"
import sqlite3, pathlib

root = pathlib.Path(r"C:\DATA\personal_finance")
schema = r'''$schema'''
db_path = root / "data" / "$db"

print(f"Rebuilding {db_path} ...")
conn = sqlite3.connect(db_path)
conn.executescript(schema)
conn.commit()
conn.close()
print("Rebuild complete.")
"@ | Out-File -FilePath $tempPy -Encoding UTF8

        & $pythonExe $tempPy
        Remove-Item $tempPy -Force
    }
}

Write-Host "Schema synchronization complete."

