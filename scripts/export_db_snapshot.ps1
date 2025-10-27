<#
.SYNOPSIS
    Wrapper for scripts/export_db_snapshot.py
.DESCRIPTION
    Runs the Python exporter to create a full Excel snapshot of the SQLite database.
    Includes:
      - Timestamped Excel export (schema + contents)
      - Automatic archive and checksum
      - Logging to logs/maintenance.log
#>

$ErrorActionPreference = "Stop"

# --- Configuration ---
$projectRoot = "C:\DATA\personal_finance"
$pythonExe   = "python"
$scriptPath  = Join-Path $projectRoot "scripts\export_db_snapshot.py"
$logDir      = Join-Path $projectRoot "logs"
$logFile     = Join-Path $logDir "maintenance.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log {
    param ([string]$Message)
    $entry = "[{0}] EXPORT_DB_SNAPSHOT - {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logFile -Value $entry
    Write-Host $Message
}

# --- Begin ---
Write-Log "Starting database export snapshot at $timestamp"
if (-not (Test-Path $scriptPath)) {
    Write-Log "ERROR: Python script not found at $scriptPath"
    exit 1
}

# --- Activate virtual environment if available ---
$venvActivate = Join-Path $projectRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Log "Activating virtual environment..."
    . $venvActivate
} else {
    Write-Log "Warning: No virtual environment found, proceeding with system Python."
}

# --- Run exporter ---
try {
    Write-Log "Running export_db_snapshot.py ..."
    & $pythonExe $scriptPath
    if ($LASTEXITCODE -eq 0) {
        Write-Log "Database export completed successfully."
    } else {
        Write-Log "Database export finished with non-zero exit code $LASTEXITCODE."
    }
}
catch {
    Write-Log "ERROR during export: $_"
    exit 1
}

# --- End ---
Write-Log "Database export snapshot process finished."
