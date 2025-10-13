<#
.SYNOPSIS
  Validate the latest session snapshot and display recent validation history.

.DESCRIPTION
  - Automatically finds the most recent session_snapshot_*.yaml in logs/
  - Runs validate_snapshot_schema.py with the --summary flag
  - Shows the last 5 validation results from logs/validation_results.log
  - Provides a one-command dashboard for snapshot schema integrity
#>

$ErrorActionPreference = "Stop"

# --- Configuration ---
$projectRoot = "C:\DATA\personal_finance"
$pythonExe   = "python"
$validator   = Join-Path $projectRoot "scripts\validate_snapshot_schema.py"
$logsDir     = Join-Path $projectRoot "logs"
$validationLog = Join-Path $logsDir "validation_results.log"

# --- Ensure prerequisites ---
if (!(Test-Path $validator)) {
    Write-Host "Error: validation script not found at $validator"
    exit 1
}

# --- Locate most recent snapshot ---
$snapshots = Get-ChildItem -Path $logsDir -Filter "session_snapshot*.yaml" | Sort-Object LastWriteTime -Descending
if ($snapshots.Count -eq 0) {
    Write-Host "No session_snapshot_*.yaml files found in $logsDir"
    exit 1
}

$latestSnapshot = $snapshots[0].FullName
Write-Host "Latest snapshot detected:`n  $latestSnapshot`n"

# --- Run validation ---
Write-Host "Running schema validation..."
& $pythonExe $validator $latestSnapshot --summary

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nValidation failed or partially completed."
} else {
    Write-Host "`nValidation completed successfully."
}

# --- Display recent validation history ---
if (Test-Path $validationLog) {
    Write-Host "`nRecent validation history (last 5 entries):"
    Write-Host "-----------------------------------------------------------"
    $lines = Get-Content $validationLog | Select-Object -Last 5
    $lines | ForEach-Object { Write-Host $_ }
    Write-Host "-----------------------------------------------------------"
} else {
    Write-Host "`nNo validation_results.log found yet."
}

Write-Host "`nDone."

