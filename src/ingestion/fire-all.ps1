# src/ingestion/fire-all.ps1
# Purpose: Call Plaid's sandbox/transactions/create for every access_token in items table
# Auto-load environment variables
. "$PSScriptRoot\..\..\scripts\load-env.ps1"
# Make sure env vars are loaded (PLAID_CLIENT_ID, PLAID_SECRET)
if (-not $env:PLAID_CLIENT_ID -or -not $env:PLAID_SECRET) {
    Write-Host "❌ Missing PLAID_CLIENT_ID or PLAID_SECRET in environment. Check your .env.sandbox file." -ForegroundColor Red
    exit 1
}

# Get all items from the DB (returns JSON array)
$itemsJson = python src/debug_db.py items
if (-not $itemsJson) {
    Write-Host "❌ No items found in DB. Connect banks first." -ForegroundColor Red
    exit 1
}

# Convert JSON string to objects
$items = $itemsJson | ConvertFrom-Json

foreach ($item in $items) {
    $accessToken = $item.access_token
    $institution = $item.institution

    Write-Host "➡️  Creating transactions for ${institution} ..." -ForegroundColor Cyan

    $body = @{
        client_id    = $env:PLAID_CLIENT_ID
        secret       = $env:PLAID_SECRET
        access_token = $accessToken
        count        = 5
    } | ConvertTo-Json -Compress

    try {
        $resp = Invoke-RestMethod -Method POST "https://sandbox.plaid.com/sandbox/transactions/create" `
            -ContentType "application/json" `
            -Body $body

        Write-Host "✅ ${institution}: Created $($resp.transactions.Count) transactions" -ForegroundColor Green
    }
    catch {
        Write-Host "❌ ${institution}: Error firing transactions" -ForegroundColor Red
        Write-Host $_.Exception.Message
    }
}

