<#
.SYNOPSIS
    Wrapper for compare_db_snapshots.py
.DESCRIPTION
    Compares two Excel database snapshots and exports a summary report.
#>

param(
    [Parameter(Mandatory = $true)] [string]$OldSnapshot,
    [Parameter(Mandatory = $true)] [string]$NewSnapshot
)

$ErrorActionPreference = "Stop"
$projectRoot = "C:\DATA\personal_finance"
$pythonExe   = "python"
$scriptPath  = Join-Path $projectRoot "scripts\compare_db_snapshots.py"
$logFile     = Join-Path $projectRoot "logs\maintenance.log"

function Write-Log {
    param ([string]$Message)
    $entry = "[{0}] COMPARE_DB_SNAPSHOTS - {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logFile -Value $entry
    Write-Host $Message
}

Write-Log "Running database snapshot comparison..."
& $pythonExe $scriptPath $OldSnapshot $NewSnapshot
Write-Log "Snapshot comparison complete."
