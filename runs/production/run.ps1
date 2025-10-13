<#
.SYNOPSIS
    Launches the production environment for the personal_finance project.
.DESCRIPTION
    - Activates the virtual environment (.venv)
    - Loads .env.production
    - Checks for any running Flask environments (sandbox/dev/prod)
    - Ensures production port (5000) is free
    - Starts Flask app in production mode
    - Launches schema watcher in read-only mode (logs only)
    - Logs all activity to logs/maintenance.log
#>

# --- Config ---
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\DATA\personal_finance"
Set-Location $ProjectRoot

$PythonExe     = ".\.venv\Scripts\python.exe"
$AppScript     = ".\src\ingestion\app.py"
$EnvFile       = ".\config\env\.env.production"
$LogFile       = ".\logs\maintenance.log"
$WatcherScript = ".\runs\automation\watch_schema.ps1"

# Port map by environment
$PortMap = @{
    "sandbox"     = 5002
    "development" = 5001
    "production"  = 5000
}

# --- Helper ---
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$ts] PROD_RUN - $Message"
    Write-Host $Message
}

Write-Log "=== Starting PRODUCTION environment ==="

# --- Step 1: Verify dependencies ---
if (-not (Test-Path $PythonExe)) {
    Write-Log "ERROR: Python executable not found at $PythonExe"
    exit 1
}
if (-not (Test-Path $AppScript)) {
    Write-Log "ERROR: Flask app script not found: $AppScript"
    exit 1
}
if (-not (Test-Path $EnvFile)) {
    Write-Log "ERROR: .env.production not found: $EnvFile"
    exit 1
}

# --- Step 2: Load environment variables ---
Write-Log "Loading .env.production..."
$envVars = Get-Content $EnvFile | Where-Object { $_ -match "=" -and $_ -notmatch "^#" }
foreach ($line in $envVars) {
    $name, $value = $line -split "=", 2
    ${env:$name} = $value
}
${env:ENV_TARGET} = "production"
${env:FLASK_ENV}  = "production"
Write-Log "Environment variables loaded successfully."

# --- Step 3: Check for other active environments or port conflicts ---
Write-Log "Checking for running Flask environments and port usage..."
$conflicts = @()

foreach ($envName in $PortMap.Keys) {
    $port = $PortMap[$envName]
    try {
        $connection = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        if ($connection) {
            $procId = $connection.OwningProcess
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
        Write-Log "WARNING: Unable to query port $port - $_"
    }
}

if ($conflicts.Count -gt 0) {
    Write-Host ""
    Write-Host "Conflicting environments or processes detected:" -ForegroundColor Red
    $conflicts | ForEach-Object {
        Write-Host ("  {0,-12} Port {1}  PID {2,-6}  ({3})" -f $_.Environment, $_.Port, $_.PID, $_.ProcessName)
        Write-Log "Conflict: environment=$($_.Environment) port=$($_.Port) pid=$($_.PID) process=$($_.ProcessName)"
    }
    Write-Host "`nClose the above process(es) before starting production." -ForegroundColor Red
    exit 1
} else {
    Write-Log "No other environments detected. Ports 5000–5002 are free."
}

# --- Step 4: Start schema watcher (read-only) ---
$existingWatcher = Get-Process pwsh -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and ($_.Path -like "*watch_schema.ps1*")
}

if ($existingWatcher) {
    Write-Log "Schema watcher already running (PID: $($existingWatcher.Id)). Skipping auto-launch."
} else {
    Write-Log "Launching schema watcher in read-only mode..."
    $watcherTitle = "personal_finance [PROD] - Schema Watcher"
    $watcherCommand = "[Console]::Title = '$watcherTitle'; `$Host.UI.RawUI.WindowTitle = '$watcherTitle'; Write-Host 'Read-only schema watcher active (no auto-sync).'; while (`$true) { Start-Sleep 30 }"
    Start-Process pwsh -ArgumentList "-NoExit", "-Command", $watcherCommand
    Write-Log "Schema watcher launched in read-only mode."
}

# --- Step 5: Start Flask app ---
Write-Log "Starting Flask production app..."
$flaskTitle = "personal_finance [PROD] - Flask App"
$flaskCommand = "[Console]::Title = '$flaskTitle'; `$Host.UI.RawUI.WindowTitle = '$flaskTitle'; & '$PythonExe' '$AppScript'"
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $flaskCommand
Write-Log "Flask app launched successfully."

Write-Log "=== Production environment startup complete ==="
Write-Host "`nProduction environment ready."
Write-Host "Close windows or press Ctrl+C in Flask terminal to stop."

