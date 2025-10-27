$ErrorActionPreference = "Stop"

Write-Host "Deactivating venv (if active)..."
if (Test-Path variable:VIRTUAL_ENV) { deactivate }

Write-Host "Switching to project directory..."
Set-Location "C:\DATA\personal_finance"

Write-Host "Running main launcher..."
& ".\runs\run.ps1"
