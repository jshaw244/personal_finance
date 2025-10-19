# Installs Plaid convenience functions into your PowerShell profile.
$ErrorActionPreference = "Stop"

# Determine the right profile path for the current host (Windows PowerShell vs PowerShell 7)
if (-not $PROFILE) {
    throw "PROFILE variable is not set in this host."
}

$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir | Out-Null
}

$snippetSource = Join-Path $PSScriptRoot "plaid_profile_snippet.ps1"
if (-not (Test-Path $snippetSource)) {
    throw "Could not find plaid_profile_snippet.ps1 next to this installer."
}

# Append or create profile with our snippet
$marker = "# === Plaid convenience functions ==="
$profileText = ""
if (Test-Path $PROFILE) { $profileText = Get-Content -Raw -Path $PROFILE } 
if ($profileText -notmatch [regex]::Escape($marker)) {
    Add-Content -Path $PROFILE -Value "`n`n# Added by Install-PlaidProfileShortcuts.ps1 on $(Get-Date)`n"
    Add-Content -Path $PROFILE -Value (Get-Content -Raw -Path $snippetSource)
    Write-Host "Plaid shortcuts added to your PowerShell profile:`n$PROFILE" -ForegroundColor Green
} else {
    Write-Host "Plaid shortcuts already present in your profile." -ForegroundColor Yellow
}

Write-Host "Close and reopen PowerShell, then run: Start-PlaidSandbox" -ForegroundColor Cyan


