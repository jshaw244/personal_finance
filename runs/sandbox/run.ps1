# runs/sandbox/run.ps1
# Purpose: One-click start for the sandbox environment.
# What it does:
#   - cd to project root
#   - create/activate .venv
#   - install deps from src\requirements.txt
#   - set ENV_TARGET=sandbox
#   - kill any process on port 5000
#   - auto-backup database + schema (with rotation)
#   - log events to app.log and DB log_events
#   - launch src.ingestion.app in a new PowerShell window
#   - open http://127.0.0.1:5000 and ngrok dashboard
#   - open a debug PowerShell window

# -----------------------------
# CONFIG
# -----------------------------
$ErrorActionPreference = "Stop"
$BackupKeep = 10

# -----------------------------
# Resolve project root & critical paths
# -----------------------------
# This script lives in: <project>\runs\sandbox\run.ps1 → go up two levels
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path   # make it a STRING
Set-Location -Path $ProjectRoot

# -----------------------------
# Sanity check: verify critical paths exist
# -----------------------------
$criticalPaths = @(
    "src\ingestion\app.py",
    "src\storage\db.py",
    "src\storage\schema.sql",
    "src\requirements.txt"
)

$missing = @()
foreach ($p in $criticalPaths) {
    $fullPath = Join-Path $ProjectRoot $p
    if (-not (Test-Path $fullPath)) {
        $missing += $fullPath
    }
}

if ($missing.Count -gt 0) {
    Write-Host "Critical files missing! Please check these paths:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
    exit 1
} else {
    Write-Host "All critical project paths verified." -ForegroundColor Green
}

# Precompute paths
$logDir     = Join-Path $ProjectRoot "logs"
$dataDir    = Join-Path $ProjectRoot "data"
$configDir  = Join-Path $ProjectRoot "config"
$schemaFile = Join-Path $ProjectRoot "src\storage\schema.sql"
$dbFile     = Join-Path $dataDir "plaid.db"
$backupDir  = Join-Path $ProjectRoot "backups"

Write-Host "Project Root: $ProjectRoot"
Write-Host "Logs Dir:     $logDir"
Write-Host "Data Dir:     $dataDir"

# -----------------------------
# Environment setup
# -----------------------------
$env:ENV_TARGET = "sandbox"

# -----------------------------
# Virtual Environment Setup
# -----------------------------
python --version | Out-Null

if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "Activating .venv..." -ForegroundColor Cyan
. .\.venv\Scripts\Activate.ps1

if (-not (Test-Path ".\src\requirements.txt")) {
    throw "Missing .\src\requirements.txt"
}

Write-Host "Ensuring latest Plaid SDK..." -ForegroundColor Cyan
pip install --upgrade plaid-python
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
            Write-Host "Process $($proc.ProcessName) (PID $procId) killed." -ForegroundColor Green
        } catch {
            Write-Host "ERROR: Failed to kill process PID $procId." -ForegroundColor Red
            exit 1
        }
    }
}

# -----------------------------
# Logging setup
# -----------------------------
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "app.log"

function Write-DbLog {
    param (
        [string]$level,
        [string]$message
    )

    # Build Python snippet safely using placeholders
    $pythonCode = @'
import sys, pathlib, os
project_root = r"__PROJECT_ROOT__"
sys.path.insert(0, str(pathlib.Path(project_root) / "src"))
from src.storage.db import log_event_db
log_event_db("runscript", "__LEVEL__", "__MESSAGE__")
'@

    # Fill placeholders (ProjectRoot is a STRING now; no .Replace() on PathInfo)
    $pythonCode = $pythonCode.Replace("__PROJECT_ROOT__", $ProjectRoot)
    $pythonCode = $pythonCode.Replace("__LEVEL__", $level)
    $pythonCode = $pythonCode.Replace("__MESSAGE__", $message)

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
            Write-Host "Removed old backup: $($f.Name)" -ForegroundColor DarkYellow
            Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Removed old backup: $($f.Name)"
            Write-DbLog "INFO" "Removed old backup: $($f.Name)"
        }
    }
}

# -----------------------------
# Auto-backup DB + schema
# -----------------------------
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }

if (Test-Path $dbFile) {
    $backupFile = Join-Path $backupDir ("plaid_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".db")
    Copy-Item $dbFile $backupFile
    Write-Host "Database backed up to $backupFile" -ForegroundColor Green
    Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Database backed up to $backupFile"
    Write-DbLog "INFO" "Database backed up to $backupFile"

    Rotate-Backups -backupDir $backupDir -pattern "plaid_*.db" -keep $BackupKeep
}

if (Test-Path $schemaFile) {
    $schemaBackup = Join-Path $backupDir ("schema_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".sql")
    Copy-Item $schemaFile $schemaBackup
    Write-Host "Schema backed up to $schemaBackup" -ForegroundColor Green
    Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RUNSCRIPT] Schema backed up to $schemaBackup"
    Write-DbLog "INFO" "Schema backed up to $schemaBackup"

    Rotate-Backups -backupDir $backupDir -pattern "schema_*.sql" -keep $BackupKeep
}

# -----------------------------
# Auto-start ngrok
# -----------------------------
$publicUrl = $null
$ngrokCmd = Get-Command ngrok -ErrorAction SilentlyContinue
if ($ngrokCmd) {
    $ng = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($ng) { $ng | Stop-Process -Force }

    Write-Host "Starting ngrok tunnel → http://localhost:5000 ..." -ForegroundColor Cyan
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot'; ngrok http 5000"

    Start-Sleep -Seconds 4

    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -UseBasicParsing
        $publicUrl = $resp.tunnels[0].public_url
        if ($publicUrl) {
            Write-Host "ngrok public URL: $publicUrl" -ForegroundColor Green
            $env:PLAID_WEBHOOK_URL = "$publicUrl/plaid/webhook"
            Write-DbLog "INFO" "PLAID_WEBHOOK_URL set to $publicUrl/plaid/webhook"
        }
    } catch {
        Write-Host "Could not fetch ngrok public URL" -ForegroundColor Yellow
        Write-DbLog "WARNING" "Could not fetch ngrok public URL"
    }

    Start-Process "http://127.0.0.1:4040"
} else {
    Write-Host "ngrok not found in PATH. Install it from https://ngrok.com/download or via 'choco install ngrok'." -ForegroundColor Yellow
    Write-DbLog "WARNING" "ngrok not found in PATH; skipped tunnel startup."
}

# -----------------------------
# Launch Flask app
# -----------------------------
Write-Host "Starting Flask app (sandbox) in new window..." -ForegroundColor Green
Write-DbLog "INFO" "Starting Flask app (sandbox)"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd `"$ProjectRoot`"; .\.venv\Scripts\Activate.ps1; `$env:ENV_TARGET='sandbox'; `$env:PYTHONPATH='$ProjectRoot'; `$env:PLAID_WEBHOOK_URL='$env:PLAID_WEBHOOK_URL'; python -m src.ingestion.app"
Start-Sleep -Seconds 3

# -----------------------------
# Auto-open browser (local + public)
# -----------------------------
Start-Process "http://127.0.0.1:5000"
if ($publicUrl) { Start-Process $publicUrl }

# -----------------------------
# Launch debug terminal (safe)
# -----------------------------
Write-Host "Opening debug window (venv active)..." -ForegroundColor Cyan

$debugScriptPath = Join-Path $env:TEMP "debug_terminal.ps1"
@"
cd "$ProjectRoot"
. .\.venv\Scripts\Activate.ps1
`$env:PYTHONPATH = (Get-Location).Path
Write-Host 'Debug terminal ready. Schema + last 20 logs:'
python -m src.ingestion.debug_db schema
python -m src.ingestion.debug_db logs 20
"@ | Out-File -FilePath $debugScriptPath -Encoding UTF8 -Force

Start-Process powershell -ArgumentList @("-NoExit", "-File", $debugScriptPath)
