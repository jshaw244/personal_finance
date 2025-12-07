<#
.SYNOPSIS
    Unified launcher for sandbox, development, and production environments.

.DESCRIPTION
    - Activates or creates virtual environment
    - Loads environment variables for selected target
    - Auto-kills stale Flask/ngrok processes before startup
    - Backs up database and schema
    - Starts Flask app, ngrok tunnel, schema watcher, and optional analysis
    - Supports graceful cleanup and logging
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

# -------------------------------------------------------------------
# Core paths
# -------------------------------------------------------------------
$ProjectRoot  = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location -Path $ProjectRoot

$logDir        = Join-Path $ProjectRoot "logs"
$dataDir       = Join-Path $ProjectRoot "data"
$schemaFile    = Join-Path $ProjectRoot "src\storage\schema.sql"
$dbFile        = Join-Path $dataDir "plaid.db"
$backupDir     = Join-Path $ProjectRoot "backups"
$WatcherScript = Join-Path $ProjectRoot "runs\automation\watch_schema.ps1"

$portMap = @{
    "production"  = 5000
    "development" = 5001
    "sandbox"     = 5002
}
$port = $portMap[$Target]

Write-Host "`n=== Starting $Target environment ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot"
Write-Host "Data Dir:     $dataDir"
Write-Host "Port:         $port`n"

# --- Verify active database ---
$verifyScript = Join-Path $ProjectRoot "scripts\verify_active_db.ps1"
if (Test-Path $verifyScript) {
    Write-Host "Verifying active database state..."
    & powershell -ExecutionPolicy Bypass -File $verifyScript
    Write-Host "Database verification complete.`n"
} else {
    Write-Host "Warning: verify_active_db.ps1 not found." -ForegroundColor Yellow
}

# -------------------------------------------------------------------
# Maintenance shell
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# NEW: Auto-cleanup for stale Flask/ngrok processes
# -------------------------------------------------------------------
Write-Host "Checking for conflicting Flask/ngrok processes..." -ForegroundColor Cyan
foreach ($envName in $portMap.Keys) {
    $p = $portMap[$envName]
    $connections = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    if ($connections) {
        $procIds = $connections | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique
        foreach ($procId in $procIds) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc -and ($proc.ProcessName -match "python" -or $proc.ProcessName -match "ngrok")) {
                Write-Host ("Closing stale {0} process (PID {1}) on port {2}..." -f $proc.ProcessName, $procId, $p) -ForegroundColor Yellow
                Stop-Process -Id $procId -Force
            }
        }
    }
}
Write-Host "All Flask/ngrok conflicts cleared. Ports 5000–5002 free." -ForegroundColor Green

# -------------------------------------------------------------------
# Environment setup
# -------------------------------------------------------------------
$env:ENV_TARGET = $Target
$env:PYTHONPATH = $ProjectRoot

python --version | Out-Null
if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

if (-not (Test-Path ".\src\requirements.txt")) {
    throw "Missing .\src\requirements.txt"
}

# -------------------------------------------------------------------
# Backup database & schema
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Launch schema watcher
# -------------------------------------------------------------------
if (Test-Path $WatcherScript) {
    Write-Host "Launching schema watcher..."
    Start-Process pwsh -ArgumentList "-NoExit", "-File", "`"$WatcherScript`""
} else {
    Write-Host "Warning: Schema watcher not found at $WatcherScript" -ForegroundColor Yellow
}

# -------------------------------------------------------------------
# Launch Flask app + ngrok
# -------------------------------------------------------------------
$publicUrl = $null
if (Get-Command ngrok -ErrorAction SilentlyContinue) {
    $ng = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($ng) { $ng | Stop-Process -Force }
    Write-Host "Starting ngrok tunnel (port $port)..."
    Start-Process pwsh -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot'; ngrok http $port"
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

# -------------------------------------------------------------------
# Start Flask
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Open browser + debug terminal
# -------------------------------------------------------------------
Start-Process "http://127.0.0.1:$port"
if ($publicUrl) { Start-Process $publicUrl }

$debugScript = Join-Path $env:TEMP "debug_terminal.ps1"
@"
cd "$ProjectRoot"
. .\.venv\Scripts\Activate.ps1
`$env:ENV_TARGET = '$Target'
`$env:PYTHONPATH = (Get-Location).Path
Write-Host "Debug terminal ready for environment: $Target"
python -m src.ingestion.debug_db schema
python -m src.ingestion.debug_db logs 20
"@ | Out-File -FilePath $debugScript -Encoding UTF8 -Force

Start-Process pwsh -ArgumentList "-NoExit", "-File", "`"$debugScript`""

# -------------------------------------------------------------------
# Optional analysis
# -------------------------------------------------------------------
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
Write-Host "Close Flask/Watcher windows manually when finished (auto-cleanup handled on next run)." -ForegroundColor DarkGray
