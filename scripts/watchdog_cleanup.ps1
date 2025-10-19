<#
.SYNOPSIS
    Ensures full cleanup of sandbox environment when the main launcher window is closed.
#>

param(
    [Parameter(Mandatory=$true)][int]$ParentPID,
    [Parameter(Mandatory=$true)][string]$TargetEnv
)

$ErrorActionPreference = "SilentlyContinue"
$StopScript = Join-Path $PSScriptRoot "stop_environment.ps1"

Write-Host "[watchdog] Monitoring parent PID $ParentPID for $TargetEnv shutdown..." -ForegroundColor DarkGray

while ($true) {
    Start-Sleep -Seconds 5
    try {
        Get-Process -Id $ParentPID -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "[watchdog] Parent PID $ParentPID not found. Initiating cleanup..." -ForegroundColor Yellow
        if (Test-Path $StopScript) {
            # Launch cleanup visibly to ensure it runs with user privileges
            Start-Process pwsh -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass", "-File `"$StopScript`"", "-Target", "$TargetEnv"
            Write-Host "[watchdog] stop_environment.ps1 launched for $TargetEnv" -ForegroundColor Green
        } else {
            Write-Host "[watchdog] ERROR: stop_environment.ps1 not found!" -ForegroundColor Red
        }
        exit 0
    }
}


