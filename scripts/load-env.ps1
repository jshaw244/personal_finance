# load-env.ps1
# Purpose: Load all key=value pairs from env\.env.sandbox into this PowerShell session

$envFile = ".\env\.env.sandbox"

if (-Not (Test-Path $envFile)) {
    Write-Host "❌ Env file not found: $envFile" -ForegroundColor Red
    exit 1
}

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.+)$') {
        $key = $matches[1].Trim()
        $val = $matches[2].Trim()
        ${env:$key} = $val
    }
}

Write-Host "✅ Loaded environment variables from $envFile" -ForegroundColor Green
Write-Host "PLAID_CLIENT_ID: $env:PLAID_CLIENT_ID"
Write-Host "PLAID_ENV:       $env:PLAID_ENV"

