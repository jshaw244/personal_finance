<#
.SYNOPSIS
    Stops all components (Flask, ngrok, schema watcher, debug) for a given environment.
#>

param(
    [Parameter(Mandatory=$true)][ValidateSet('sandbox','development','production')]
    [string]$Target
)

$ErrorActionPreference = "SilentlyContinue"
Write-Host "Initiating shutdown for $Target environment." -ForegroundColor Cyan

# -----------------------------
# Port Map
# -----------------------------
$PortMap = @{
    "sandbox"     = 5002
    "development" = 5001
    "production"  = 5000
}
$port = $PortMap[$Target]

# -----------------------------
# Kill Flask (by port)
# -----------------------------
try {
    $flaskConn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($flaskConn) {
        $pid = $flaskConn.OwningProcess
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Stopping Flask on port $port (PID $pid - $($proc.ProcessName))..."
            Stop-Process -Id $pid -Force
        }
    } else {
        Write-Host "No Flask process detected on port $port."
    }
} catch {
    Write-Host "Error while stopping Flask: $_"
}

# -----------------------------
# Kill ngrok
# -----------------------------
try {
    $ng = Get-Process | Where-Object { $_.ProcessName -like "ngrok*" }
    if ($ng) {
        Write-Host "Stopping ngrok tunnel (PID(s): $($ng.Id -join ', '))..."
        $ng | Stop-Process -Force
    } else {
        Write-Host "No ngrok process found."
    }
} catch {
    Write-Host "Error stopping ngrok: $_"
}

# -----------------------------
# Kill schema watcher
# -----------------------------
try {
    $watcher = Get-Process pwsh -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and ($_.CommandLine -match "watch_schema.ps1")
    }
    if ($watcher) {
        Write-Host "Stopping schema watcher (PID(s): $($watcher.Id -join ', '))..."
        $watcher | Stop-Process -Force
    } else {
        Write-Host "No active schema watcher detected."
    }
} catch {
    Write-Host "Error stopping schema watcher: $_"
}

# -----------------------------
# Kill debug terminal
# -----------------------------
try {
    $debug = Get-Process pwsh -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match "debug_terminal.ps1"
    }
    if ($debug) {
        Write-Host "Stopping debug terminal (PID(s): $($debug.Id -join ', '))..."
        $debug | Stop-Process -Force
    } else {
        Write-Host "No debug terminal detected."
    }
} catch {
    Write-Host "Error stopping debug terminal: $_"
}

# -----------------------------
# Summary
# -----------------------------
Write-Host "`nShutdown sequence complete for $Target environment." -ForegroundColor Cyan
Write-Host "All processes stopped for $Target.`n" -ForegroundColor Green

