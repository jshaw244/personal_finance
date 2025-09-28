# runs/sandbox/run.ps1
# Purpose: One-click start for the sandbox environment.
# What it does:
#   - cd to project root
#   - create/activate .venv
#   - install deps from src\requirements.txt
#   - set ENV_TARGET=sandbox
#   - kill any process on port 5000
#   - auto-backup database + schema before launch (with rotation, keep last N of each)
#   - log events to app.log and database log_events
#   - launch src\app.py in a new PowerShell window
#   - auto-open default browser to http://127.0.0.1:5000
#   - auto-start ngrok http 5000 (public URL + dashboard open automatically)
#   - launch a second PowerShell window for debug (venv active + auto schema + logs)
Write-Host "PSScriptRoot: $PSScriptRoot"
Write-Host "ProjectRoot:  $ProjectRoot"
Write-Host "LogDir:       $logDir"

# -----------------------------
# Config
# -----------------------------
$ErrorActionPreference = "Stop"
$BackupKeep = 10   # Number of DB/schema backups to keep

# Move to project root (this file lives in runs\sandbox\)
Set-Location -Path (Resolve-Path "$PSScriptRoot\..\..")
#Set-Location -Path (Join-Path $PSScriptRoot "..\..\..")  # go to project root

# -----------------------------
# Environment setup
# -----------------------------
# Set environment target early (must be visible to all child processes)
$env:ENV_TARGET = "sandbox"

# Ensure Python is available
python --version | Out-Null

# Create venv if missing
if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}

# Activate venv
Write-Host "Activating .venv..." -ForegroundColor Cyan
. .\.venv\Scripts\Activate.ps1

# Install dependencies
if (-not (Test-Path ".\src\requirements.txt")) {
    throw "Missing .\src\requirements.txt"
}
Write-Host "Installing dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r .\src\requirements.txt

# -----------------------------
# Kill anything using port 5000 (before backups to avoid DB lock)
# -----------------------------
$portInUse = Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue
if ($portInUse) {
    $procId = $portInUse.OwningProcess
    $proc   = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        try {
            Write-Host "Killing process on port 5000: $($proc.ProcessName) (PID $procId)" -ForegroundColor Yellow
            Stop-Process -Id $procId -Force
            Write-Host "✅ Process $($proc.ProcessName) (PID $procId) killed." -ForegroundColor Green
        } catch {
            Write-Host "❌ ERROR: Failed to kill process PID $procId." -ForegroundColor Red
            exit 1
        }
    }
}

# -----------------------------
# Logging setup (fixed)
# -----------------------------

# Resolve project root by climbing 3 levels from runs/sandbox/
# Move to project root (this file lives in runs\sandbox\)
Set-Location -Path (Join-Path $PSScriptRoot "..\..\..")

# Immediately capture the *actual* root:
$ProjectRoot = (Get-Location).Path   # <- bulletproof

# Define logs directory from root (not from scripts/)
$logDir = Join-Path $ProjectRoot "logs"

# Create it if missing
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# Define log file path
$logFile = Join-Path $logDir "app.log"

function Write-DbLog {
    param([string]$level, [string]$message)

    $pythonCode = @"
import sys, pathlib
sys.path.insert(0, str(pathlib.Path().resolve() / 'src'))
from src.storage.db import log_event_db
log_event_db('runscript', r'''$level''', r'''$message''')
"@
    $pythonCode | python -
}

# -----------------------------
# Backup rotation helper
# -----------------------------
function Rotate-Backups {
    param(
        [string]$backupDir,
        [string]$pattern,
        [int]$keep = 10
    )
    if (-not (Test-Path $backupDir)) { return }
    $files = Get-ChildItem -Path $backupDir -Filter $pattern | Sort-Object LastWriteTime -Descending
    if ($files.Count -gt $keep) {
        $toRemove = $files | Select-Object -Skip $keep
        foreach ($f in $toRemove) {
            Remove-Item $f.FullName -Force
            Write-Host "🗑️ Removed old backup: $($f.Name)" -ForegroundColor DarkYellow
            Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Removed old backup: $($f.Name)"
            Write-DbLog "INFO" "Removed old backup: $($f.Name)"
        }
    }
}

# -----------------------------
# Auto-backup DB + schema before launch
# -----------------------------
$dbFile     = Join-Path $PWD "data\plaid.db"
$backupDir  = Join-Path $PWD "backups"
$schemaFile = Join-Path $PWD "src\schema.sql"

if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }

# Backup database
if (Test-Path $dbFile) {
    $backupFile = Join-Path $backupDir ("plaid_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".db")
    Copy-Item $dbFile $backupFile
    Write-Host "💾 Database backed up to $backupFile" -ForegroundColor Green
    Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Database backed up to $backupFile"
    Write-DbLog "INFO" "Database backed up to $backupFile"

    Rotate-Backups -backupDir $backupDir -pattern "plaid_*.db" -keep $BackupKeep
}

# Backup schema
if (Test-Path $schemaFile) {
    $schemaBackup = Join-Path $backupDir ("schema_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".sql")
    Copy-Item $schemaFile $schemaBackup
    Write-Host "📄 Schema backed up to $schemaBackup" -ForegroundColor Green
    Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Schema backed up to $schemaBackup"
    Write-DbLog "INFO" "Schema backed up to $schemaBackup"

    Rotate-Backups -backupDir $backupDir -pattern "schema_*.sql" -keep $BackupKeep
}

# -----------------------------
# Auto-start ngrok first (so webhook URL is ready)
# -----------------------------
$publicUrl = $null
$ngrokCmd = Get-Command ngrok -ErrorAction SilentlyContinue
if ($ngrokCmd) {
    $ng = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($ng) { $ng | Stop-Process -Force }

    Write-Host "Starting ngrok tunnel → http://localhost:5000 ..." -ForegroundColor Cyan
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PWD'; ngrok http 5000"

    Start-Sleep -Seconds 4

    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -UseBasicParsing
        $publicUrl = $resp.tunnels[0].public_url
        if ($publicUrl) {
            Write-Host "🌐 ngrok public URL: $publicUrl" -ForegroundColor Green
            $env:PLAID_WEBHOOK_URL = "$publicUrl/webhook"
            Write-DbLog "INFO" "PLAID_WEBHOOK_URL set to $publicUrl/webhook"
        }
    } catch {
        Write-Host "⚠️ Could not fetch ngrok public URL" -ForegroundColor Yellow
        Write-DbLog "WARNING" "Could not fetch ngrok public URL"
    }

    Start-Process "http://127.0.0.1:4040"
} else {
    Write-Host "⚠️ ngrok not found in PATH. Install it from https://ngrok.com/download or via 'choco install ngrok'." -ForegroundColor Yellow
    Write-DbLog "WARNING" "ngrok not found in PATH; skipped tunnel startup."
}

# -----------------------------
# Launch Flask (after ngrok and env vars are ready)
# -----------------------------
Write-Host "🚀 Starting Flask app (sandbox) in new window..." -ForegroundColor Green
Write-DbLog "INFO" "Starting Flask app (sandbox)"
#Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd `"$PWD`"; .\.venv\Scripts\Activate.ps1; $env:ENV_TARGET='sandbox'; $env:PYTHONPATH='$PWD'; $env:PLAID_WEBHOOK_URL='$env:PLAID_WEBHOOK_URL'; python -m src.ingestion.app"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd `"$PWD`"; .\.venv\Scripts\Activate.ps1; `$env:ENV_TARGET='sandbox'; `$env:PYTHONPATH='$PWD'; `$env:PLAID_WEBHOOK_URL='https://74e183db7563.ngrok-free.app/webhook'; python -m src.ingestion.app"
Start-Sleep -Seconds 3

# -----------------------------
# Auto-open browser (local + public)
# -----------------------------
Start-Process "http://127.0.0.1:5000"
if ($publicUrl) { Start-Process $publicUrl }

# -----------------------------
# Launch debug PowerShell window
# -----------------------------
Write-Host "Opening debug window (venv active)..." -ForegroundColor Cyan
#Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd `"$PWD`"; .\.venv\Scripts\Activate.ps1; $env:PYTHONPATH='$PWD'; Write-Host '✅ Debug terminal ready. Schema + last 20 logs:'; python -m src.ingestion.debug_db schema; python -m src.ingestion.debug_db logs 20"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd `"$PWD`"; .\.venv\Scripts\Activate.ps1; `$env:PYTHONPATH = (Get-Location).Path; Write-Host '✅ Debug terminal ready. Schema + last 20 logs:'; python -m src.ingestion.debug_db schema; python -m src.ingestion.debug_db logs 20"