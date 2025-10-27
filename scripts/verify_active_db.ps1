<#
.SYNOPSIS
    Verifies and initializes SQLite databases for each environment (sandbox, development, production).

.DESCRIPTION
    - Confirms existence of each database
    - Creates missing databases from src\storage\schema.sql
    - Optional -Rebuild <env> parameter allows controlled resets
      * Automatically backs up before rebuild
      * Double confirmation required for production
      * Logs rebuild events to logs\maintenance.log
#>

param(
    [ValidateSet("sandbox","development","production")]
    [string]$Rebuild
)

$ErrorActionPreference = "Stop"

# --- Core paths ---
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$dataDir     = Join-Path $ProjectRoot "data"
$schemaFile  = Join-Path $ProjectRoot "src\storage\schema.sql"
$backupDir   = Join-Path $ProjectRoot "backups"
$logDir      = Join-Path $ProjectRoot "logs"
$logFile     = Join-Path $logDir "maintenance.log"

if (-not (Test-Path $schemaFile)) {
    Write-Host "Error: schema.sql not found at $schemaFile" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $backupDir)) {
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
}
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# --- Environment database mapping ---
$map = @{
    "sandbox"     = Join-Path $dataDir "plaid.db"
    "development" = Join-Path $dataDir "plaid_dev.db"
    "production"  = Join-Path $dataDir "plaid_prod.db"
}
$targets = $map.Keys

# --- Read schema content ---
$schema = Get-Content $schemaFile -Raw

# --- Helper: write to maintenance log ---
function Write-MaintenanceLog($message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$timestamp] $message"
    Add-Content -Path $logFile -Value $entry
}

# --- Helper: create a new database ---
function New-Database($target, $path) {
    Write-Host "Creating $target database at $path ..."
    $python = ".\.venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }

    $cmd = @"
import sqlite3
schema = r'''$schema'''
conn = sqlite3.connect(r'''$path''')
conn.executescript(schema)
conn.commit()
conn.close()
print('Initialized:', r'''$path''')
"@
    $tmp = [IO.Path]::GetTempFileName() + ".py"
    $cmd | Out-File -FilePath $tmp -Encoding UTF8
    & $python $tmp
    Remove-Item $tmp -Force
    Write-MaintenanceLog "Database initialized: $target ($path)"
}

# --- Helper: backup an existing database ---
function Backup-Database($target, $path) {
    if (Test-Path $path) {
        $timestamp = Get-Date -Format "yyyyMMdd_HHmm"
        $backup = Join-Path $backupDir ("${target}_$timestamp.db")
        Copy-Item $path $backup -Force
        Write-Host "Backup created: $backup"
        Write-MaintenanceLog "Database backed up: $target -> $backup"
    } else {
        Write-Host "No database to back up for $target."
    }
}

# --- Optional rebuild logic ---
if ($Rebuild) {
    $path = $map[$Rebuild]

    Write-Host "`n*** REBUILD REQUESTED for environment: $Rebuild ***" -ForegroundColor Yellow

    # --- Safety checks ---
    if ($Rebuild -eq "production") {
        $confirm1 = Read-Host "WARNING: Are you sure you want to rebuild PRODUCTION? (yes/no)"
        if ($confirm1 -ne "yes") {
            Write-Host "Aborted: rebuild cancelled." -ForegroundColor Red
            exit 0
        }
        $confirm2 = Read-Host "Has the production database been backed up? (yes/no)"
        if ($confirm2 -ne "yes") {
            Write-Host "Aborted: please back up production before rebuilding." -ForegroundColor Red
            exit 0
        }
    }

    # --- Backup first ---
    Backup-Database $Rebuild $path

    # --- Delete and rebuild ---
    if (Test-Path $path) {
        Remove-Item $path -Force
        Write-Host "Deleted existing $Rebuild database."
        Write-MaintenanceLog "Deleted existing database for $Rebuild"
    }
    New-Database $Rebuild $path
    Write-MaintenanceLog "Rebuild completed for environment: $Rebuild"
    Write-Host "`nRebuild complete for $Rebuild.`n" -ForegroundColor Green
}

# --- Verification summary ---
Write-Host ""
Write-Host "=== Active Database Mapping (verify and initialize) ==="
foreach ($t in $targets) {
    $path = $map[$t]
    $exists = Test-Path $path
    if (-not $exists) {
        Write-Host ("{0,-12} -> {1,-65} creating new database..." -f $t.ToUpper(), $path)
        New-Database $t $path
    } else {
        Write-Host ("{0,-12} -> {1,-65} exists" -f $t.ToUpper(), $path)
    }
}
Write-Host "======================================================="
Write-Host ""
