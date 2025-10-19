<#
.SYNOPSIS
  Quick sanity check for key /reports/api endpoints in sandbox.
  Includes authenticated login using Flask-Login session cookies.
#>

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

# ðŸ” Login credentials
$loginBody = @{
    username = "finance_admin"
    password = "StrongPass123"
}

# ðŸŸ¢ Perform login (Flask expects form data, not JSON)
Invoke-WebRequest -Method Post `
    -Uri "http://127.0.0.1:5002/reports/login" `
    -WebSession $session `
    -Body $loginBody `
    -ContentType "application/x-www-form-urlencoded" `
    | Out-Null

# Confirm login succeeded by checking session cookie
if (-not ($session.Cookies.GetCookies("http://127.0.0.1:5002") | Where-Object { $_.Name -eq "session" })) {
    Write-Host "âŒ Login failed â€” check username/password or Flask logs." -ForegroundColor Red
    exit 1
}
Write-Host "âœ… Logged in as finance_admin" -ForegroundColor Green

# Base API URL
$base = "http://127.0.0.1:5002/reports/api"

Write-Host "=== Testing /paycheck_estimate (POST JSON) ===" -ForegroundColor Cyan
$payload = @{
  gross = 3500
  pay_periods = 26
  federal_rate = 0.18
  state_rate = 0.05
  other_deductions = 150
} | ConvertTo-Json
Invoke-RestMethod -WebSession $session -Method Post -Uri "$base/paycheck_estimate" -ContentType "application/json" -Body $payload | ConvertTo-Json -Depth 5 | Write-Output
Write-Host "`n"

Write-Host "=== Testing /recurring_merchants (GET) ===" -ForegroundColor Cyan
Invoke-RestMethod -WebSession $session -Uri "$base/recurring_merchants" | ConvertTo-Json -Depth 5 | Write-Output
Write-Host "`n"

Write-Host "=== Testing /insights/summary (GET) ===" -ForegroundColor Cyan
Invoke-RestMethod -WebSession $session -Uri "$base/insights/summary" | ConvertTo-Json -Depth 5 | Write-Output
Write-Host "`n"

Write-Host "=== All tests completed successfully. ===" -ForegroundColor Green

