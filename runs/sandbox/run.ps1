<#
.SYNOPSIS
    One-click start for the sandbox environment.
.DESCRIPTION
    - Creates/activates .venv if missing
    - Loads sandbox environment variables
    - Checks for port conflicts (sandbox/dev/prod)
    - Auto-backs up database and schema
    - Starts Flask app and ngrok tunnel
    - Launches schema watcher (active mode)
    - Launches debug terminal and optional analysis
    - Logs all actions to app.log and DB log_events
    - Stops any prior environment before starting
    - Automatically stops environment on exit
#>

param(
    [switch]$Maintenance,
    [switch]$IncludeAnalysis,
    [int]$AnalysisDays = 30,
    [string]$AnalysisStart = "",
    [string]$AnalysisEnd = ""
)

$ErrorActionPreference = "Stop"
$BackupKeep = 10

# -----------------------------
# Maintenance Mode
# -----------------------------
if ($Maintenance) {
    Write-Host "Entering maintenance mode..." -ForegroundColor Cyan
    $ProjectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    Set-Location -Path $ProjectRoot

    if (-not (Test-Path ".\.venv")) {
        Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
        python -m venv .venv
    }

    . .\.venv\Scripts\Activate.ps1
    $env:ENV_TARGET = "sandbox"
    $env:PYTHONPATH = $ProjectRoot

    Write-Host "`nMaintenance shell ready. Run scripts safely.`n" -ForegroundColor Green
    powershell -NoExit -Command {
        . .\.venv\Scripts\Activate.ps1
        $env:ENV_TARGET = "sandbox"
        $env:PYTHONPATH = (Get-Location).Path
        Write-Host "`nMaintenance shell active.`n" -ForegroundColor Green
    }
    exit 0
}

# -----------------------------
# Core Paths
# -----------------------------
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location -Path $ProjectRoot

$logDir        = Join-Path $ProjectRoot "logs"
$dataDir       = Join-Path $ProjectRoot "data"
$schemaFile    = Join-Path $ProjectRoot "src\storage\schema.sql"
$dbFile        = Join-Path $dataDir "plaid.db"
$backupDir     = Join-Path $ProjectRoot "backups"
$WatcherScript = Join-Path $ProjectRoot "runs\automation\watch_schema.ps1"
$StopScript    = Join-Path $ProjectRoot "scripts\stop_environment.ps1"

Write-Host "Project Root: $ProjectRoot"
Write-Host "Logs Dir:     $logDir"
Write-Host "Data Dir:     $dataDir"

# -----------------------------
# Stop any running environment first
# -----------------------------
if (Test-Path $StopScript) {
    Write-Host "Ensuring previous sandbox environment is stopped..."
    & $StopScript -Target sandbox
} else {
    Write-Host "Warning: stop_environment.ps1 not found; skipping cleanup." -ForegroundColor Yellow
}

# -----------------------------
# Verify Critical Paths
# -----------------------------
$criticalPaths = @(
    "src\ingestion\app.py",
    "src\storage\db.py",
    "src\storage\schema.sql",
    "src\requirements.txt"
)
$missing = @()
foreach ($p in $criticalPaths) {
    $fp = Join-Path $ProjectRoot $p
    if (-not (Test-Path $fp)) { $missing += $fp }
}
if ($missing.Count -gt 0) {
    Write-Host "Critical files missing:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host " - $_" -ForegroundColor Yellow }
    exit 1
}

# -----------------------------
# Environment Setup
# -----------------------------
$env:ENV_TARGET = "sandbox"
$env:PYTHONPATH = $ProjectRoot

# -----------------------------
# Virtual Environment
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
# Environment/Port Conflict Check
# -----------------------------
$PortMap = @{
    "sandbox"     = 5002
    "development" = 5001
    "production"  = 5000
}
Write-Host "Checking for running Flask environments..."
$conflicts = @()
foreach ($envName in $PortMap.Keys) {
    $port = $PortMap[$envName]
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        if ($conn) {
            $procId = $conn.OwningProcess
            $proc   = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $pname  = if ($proc) { $proc.ProcessName } else { "Unknown" }
            $conflicts += [PSCustomObject]@{
                Environment = $envName
                Port        = $port
                ProcessName = $pname
                PID         = $procId
            }
        }
    } catch {
        Write-Host "Warning: Unable to query port $port - $_"
    }
}
if ($conflicts.Count -gt 0) {
    Write-Host "`nConflicts detected:" -ForegroundColor Red
    $conflicts | ForEach-Object {
        Write-Host ("  {0,-12} Port {1}  PID {2,-6} ({3})" -f $_.Environment, $_.Port, $_.PID, $_.ProcessName)
    }
    Write-Host "`nClose these processes before running sandbox." -ForegroundColor Red
    exit 1
} else {
    Write-Host "No other environments detected. Ports 5000–5002 are free."
}

# -----------------------------
# Backup Database & Schema
# -----------------------------
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }

function Rotate-Backups {
    param([string]$backupDir,[string]$pattern,[int]$keep=10)
    if (-not (Test-Path $backupDir)) { return }
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
# Launch Schema Watcher (active sync)
# -----------------------------
if (Test-Path $WatcherScript) {
    $existingWatcher = Get-Process pwsh -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and ($_.Path -like "*watch_schema.ps1*")
    }
    if ($existingWatcher) {
        Write-Host "Schema watcher already running (PID: $($existingWatcher.Id)). Skipping auto-launch."
    } else {
        Write-Host "Launching schema watcher (active sync mode)..."
        $proc = Start-Process pwsh -PassThru -ArgumentList "-NoExit", "-File", "`"$WatcherScript`""
        Write-Host "Schema watcher launched. PID: $($proc.Id)"
    }
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
    Write-Host "Starting ngrok tunnel (port 5002)..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot'; ngrok http 5002"
    Start-Sleep -Seconds 4
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -UseBasicParsing
        $publicUrl = $resp.tunnels | Where-Object { $_.config.addr -match "5002" } | Select-Object -First 1 -ExpandProperty public_url
        if ($publicUrl) {
            Write-Host "ngrok public URL: $publicUrl"
            $env:PLAID_WEBHOOK_URL = "$publicUrl/plaid/webhook"
        }
    } catch { Write-Host "Could not fetch ngrok public URL" -ForegroundColor Yellow }
} else {
    Write-Host "ngrok not found in PATH. Skipping tunnel."
}

Write-Host "Starting Flask app (sandbox)..."
$flaskTitle = "personal_finance [SANDBOX] - Flask App"
$flaskCmd = @"
[Console]::Title = '$flaskTitle';
`$Host.UI.RawUI.WindowTitle = '$flaskTitle';
. .\.venv\Scripts\Activate.ps1;
`$env:ENV_TARGET = 'sandbox';
`$env:PYTHONPATH = '$ProjectRoot';
python -m src.ingestion.app
"@
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $flaskCmd
Start-Sleep -Seconds 3

# -----------------------------
# Open Browser & Debug Terminal
# -----------------------------
Start-Process "http://127.0.0.1:5002"
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
        $args = @("-Target", "sandbox", "-Days", $AnalysisDays)
        if ($AnalysisStart -and $AnalysisEnd) { $args = @("-Target", "sandbox", "-Start", $AnalysisStart, "-End", $AnalysisEnd) }
        Write-Host "Running analysis..."
        & powershell -ExecutionPolicy Bypass -File $runAnalysis @args
    } else {
        Write-Host "run_analysis.ps1 not found. Skipping analysis."
    }
}

# -----------------------------
# Register exit cleanup handler
# -----------------------------
if (Test-Path $StopScript) {
    Register-EngineEvent PowerShell.Exiting -Action {
        try {
            & $using:StopScript -Target sandbox | Out-Null
        } catch {
            Write-Host "Error during cleanup: $_"
        }
    } | Out-Null
    Write-Host "Cleanup handler registered. Sandbox will auto-stop on exit." -ForegroundColor DarkGray
}

# -----------------------------
# Launch background watchdog
# -----------------------------
$watchdogScript = Join-Path $ProjectRoot "scripts\watchdog_cleanup.ps1"
if (Test-Path $watchdogScript) {
    Start-Process pwsh -WindowStyle Hidden -ArgumentList "-File", "`"$watchdogScript`"", "-ParentPID", "$PID", "-TargetEnv", "sandbox"
    Write-Host "Watchdog launched (PID monitor = $PID)" -ForegroundColor DarkGray
} else {
    Write-Host "Warning: watchdog_cleanup.ps1 not found; sandbox will not auto-stop if window closed." -ForegroundColor Yellow
}

# -----------------------------
# Startup complete
# -----------------------------
Write-Host "`nSandbox environment startup complete. Flask and ngrok are running." -ForegroundColor Cyan
Write-Host "Close Flask/Watcher windows or exit PowerShell to stop automatically." -ForegroundColor DarkGray

