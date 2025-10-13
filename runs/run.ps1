<#
.SYNOPSIS
    Unified launcher for sandbox, development, and production environments.

.DESCRIPTION
    - Activates or creates virtual environment
    - Loads environment variables for selected target
    - Backs up database and schema
    - Starts Flask app, ngrok tunnel, and schema watcher
    - Optional analysis execution
    - Supports graceful cleanup and logging
    - Replaces: runs\sandbox\run.ps1, runs\development\run.ps1, runs\production\run.ps1
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("sandbox", "development", "production")]
    [string]$Target,

    [switch]$Maintenance,
    [switch]$IncludeAnalysis,
    [int]$AnalysisDays = 30,
    [string]$AnalysisStart = "",
    [string]$AnalysisEnd = ""
)

$ErrorActionPreference = "Stop"
$BackupKeep = 10

# -----------------------------
# Core paths
# -----------------------------
$ProjectRoot  = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location -Path $ProjectRoot

$logDir        = Join-Path $ProjectRoot "logs"
$dataDir       = Join-Path $ProjectRoot "data"
$schemaFile    = Join-Path $ProjectRoot "src\storage\schema.sql"
$dbFile        = Join-Path $dataDir "plaid.db"
$backupDir     = Join-Path $ProjectRoot "backups"
$WatcherScript = Join-Path $ProjectRoot "runs\automation\watch_schema.ps1"
$StopScript    = Join-Path $ProjectRoot "scripts\stop_environment.ps1"

$portMap = @{
    "production"  = 5000
    "development" = 5001
    "sandbox"     = 5002
}
$port = $portMap[$Target]

Write-Host "`n=== Starting $Target environment ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot"
Write-Host "Log Dir:      $logDir"
Write-Host "Data Dir:     $dataDir"
Write-Host "Port:         $port`n"

# -----------------------------
# Maintenance shell
# -----------------------------
if ($Maintenance) {
    Write-Host "Entering maintenance mode..." -ForegroundColor Cyan
    if (-not (Test-Path ".\.venv")) {
        Write-Host "Creating virtual environment..." -ForegroundColor Yellow
        python -m venv .venv
    }
    . .\.venv\Scripts\Activate.ps1
    $env:ENV_TARGET = $Target
    $env:PYTHONPATH = $ProjectRoot
    Write-Host "`nMaintenance shell ready.`n" -ForegroundColor Green
    powershell -NoExit -Command {
        . .\.venv\Scripts\Activate.ps1
        $env:ENV_TARGET = $Target
        $env:PYTHONPATH = (Get-Location).Path
        Write-Host "`nMaintenance shell active.`n" -ForegroundColor Green
    }
    exit 0
}

# -----------------------------
# Stop any previous environment
# -----------------------------
if (Test-Path $StopScript) {
    Write-Host "Ensuring previous environment is stopped..."
    & $StopScript -Target $Target
} else {
    Write-Host "Warning: stop_environment.ps1 not found; skipping cleanup." -ForegroundColor Yellow
}

# -----------------------------
# Setup environment variables
# -----------------------------
$env:ENV_TARGET = $Target
$env:PYTHONPATH = $ProjectRoot

# -----------------------------
# Virtual environment
# -----------------------------
python --version | Out-Null
if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

if (-not (Test-Path ".\src\requirements.txt")) {
    throw "Missing .\src\requirements.txt"
}

# -----------------------------
# Backup Database & Schema
# -----------------------------
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }

function Rotate-Backups {
    param([string]$backupDir,[string]$pattern,[int]$keep=10)
    $files = Get-ChildItem -Path $backupDir -Filter $pattern | Sort-Object LastWriteTime -Descending
    if ($files.Count -gt $keep) {
        $files | Select-Object -Skip $keep | ForEach-Object { Remove-Item $_.FullName -Force }
    }
}

if (Test-Path $dbFile) {
    $bfile = Join-Path $backupDir ("plaid_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".db")
    Copy-Item $dbFile $bfile
    Write-Host "Database backed up: $bfile"
    Rotate-Backups -backupDir $backupDir -pattern "plaid_*.db"
}
if (Test-Path $schemaFile) {
    $sfile = Join-Path $backupDir ("schema_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".sql")
    Copy-Item $schemaFile $sfile
    Write-Host "Schema backed up: $sfile"
    Rotate-Backups -backupDir $backupDir -pattern "schema_*.sql"
}

# -----------------------------
# Launch Schema Watcher
# -----------------------------
if (Test-Path $WatcherScript) {
    Write-Host "Launching schema watcher..."
    Start-Process pwsh -ArgumentList "-NoExit", "-File", "`"$WatcherScript`""
    Write-Host "Schema watcher running."
} else {
    Write-Host "Warning: Schema watcher not found at $WatcherScript" -ForegroundColor Yellow
}

# -----------------------------
# Launch Flask App + ngrok
# -----------------------------
$publicUrl = $null
if (Get-Command ngrok -ErrorAction SilentlyContinue) {
    $ng = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($ng) { $ng | Stop-Process -Force }
    Write-Host "Starting ngrok tunnel (port $port)..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot'; ngrok http $port"
    Start-Sleep -Seconds 4
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -UseBasicParsing
        $publicUrl = $resp.tunnels | Where-Object { $_.config.addr -match "$port" } | Select-Object -First 1 -ExpandProperty public_url
        if ($publicUrl) {
            Write-Host "ngrok public URL: $publicUrl"
            $env:PLAID_WEBHOOK_URL = "$publicUrl/plaid/webhook"
        }
    } catch { Write-Host "Could not fetch ngrok public URL" -ForegroundColor Yellow }
} else {
    Write-Host "ngrok not found in PATH. Skipping tunnel."
}

# -----------------------------
# Start Flask
# -----------------------------
$flaskTitle = "personal_finance [$Target] - Flask App"
$flaskCmd = @"
[Console]::Title = '$flaskTitle';
`$Host.UI.RawUI.WindowTitle = '$flaskTitle';
. .\.venv\Scripts\Activate.ps1;
`$env:ENV_TARGET = '$Target';
`$env:PYTHONPATH = '$ProjectRoot';
python -m src.ingestion.app
"@
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $flaskCmd
Start-Sleep -Seconds 3

# -----------------------------
# Open browser + debug terminal
# -----------------------------
Start-Process "http://127.0.0.1:$port"
if ($publicUrl) { Start-Process $publicUrl }

$debugScript = Join-Path $env:TEMP "debug_terminal.ps1"
@"
cd "$ProjectRoot"
. .\.venv\Scripts\Activate.ps1
`$env:PYTHONPATH = (Get-Location).Path
Write-Host 'Debug terminal ready.'
python -m src.ingestion.debug_db schema
python -m src.ingestion.debug_db logs 20
"@ | Out-File -FilePath $debugScript -Encoding UTF8 -Force
Start-Process powershell -ArgumentList @("-NoExit", "-File", $debugScript)

# -----------------------------
# Optional Analysis
# -----------------------------
if ($IncludeAnalysis) {
    $runAnalysis = Join-Path $ProjectRoot "scripts\run_analysis.ps1"
    if (Test-Path $runAnalysis) {
        $args = @("-Target", $Target, "-Days", $AnalysisDays)
        if ($AnalysisStart -and $AnalysisEnd) { $args = @("-Target", $Target, "-Start", $AnalysisStart, "-End", $AnalysisEnd) }
        Write-Host "Running analysis..."
        & powershell -ExecutionPolicy Bypass -File $runAnalysis @args
    } else {
        Write-Host "run_analysis.ps1 not found. Skipping analysis."
    }
}

Write-Host "`n=== $Target environment startup complete ===" -ForegroundColor Cyan
Write-Host "Close Flask/Watcher windows or run stop_environment.ps1 manually to stop." -ForegroundColor DarkGray

