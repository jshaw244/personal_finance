<#
.SYNOPSIS
    Watches src/storage/schema.sql for changes and automatically syncs databases.

.DESCRIPTION
    - Monitors the main schema file for modifications.
    - When a change is detected, runs scripts/sync_schemas.ps1.
    - Logs all activity to logs/schema_watcher.log.
    - Runs continuously until stopped manually (Ctrl+C).
    - Designed for PowerShell 7+ and integrated use within the /runs system.
#>

# --- Config ---
$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\DATA\personal_finance"
$SchemaFile  = Join-Path $ProjectRoot "src\storage\schema.sql"
$SyncScript  = Join-Path $ProjectRoot "scripts\sync_schemas.ps1"
$LogFile     = Join-Path $ProjectRoot "logs\schema_watcher.log"

# --- Ensure log directory exists ---
$logDir = Split-Path $LogFile
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

# --- Helper: Write message to console and log ---
function Write-Log {
    param([string]$Message)
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$timestamp] WATCH_SCHEMA - $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $Message
}

# --- Startup info ---
Write-Log "Watcher starting. Monitoring $SchemaFile for changes."
Write-Log "Log file: $LogFile"
Write-Log "Press Ctrl+C to stop."

# --- Initialize watcher ---
$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = Split-Path $SchemaFile
$watcher.Filter = Split-Path $SchemaFile -Leaf
$watcher.NotifyFilter = [IO.NotifyFilters]'LastWrite'

# --- Event handler: on schema file change ---
$action = {
    param($source, $eventArgs)
    $path = $eventArgs.FullPath
    $time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Write-Log "Schema change detected at $time â€” running incremental migration..."
    try {
        & .\.venv\Scripts\python.exe .\scripts\migrate_schema.py | Out-Host
        Write-Log "Migration completed successfully."
    } catch {
        Write-Log "ERROR: Migration failed: $_"
    }
}

# --- Register events ---
$changedEvent = Register-ObjectEvent -InputObject $watcher -EventName Changed -Action $action

# --- Enable watcher ---
$watcher.EnableRaisingEvents = $true

try {
    while ($true) {
        Start-Sleep -Seconds 5
    }
} finally {
    Unregister-Event -SourceIdentifier $changedEvent.Name
    $watcher.Dispose()
    Write-Log "Watcher stopped."
}


