# Project-scoped launcher (no global profile changes needed)
# Usage:
#   & "C:\DATA\personal_finance\01.connection\plaid\plaid.ps1" sandbox
#   & "C:\DATA\personal_finance\01.connection\plaid\plaid.ps1" development
#   & "C:\DATA\personal_finance\01.connection\plaid\plaid.ps1" production

param(
    [ValidateSet("sandbox","development","production")]
    [string]$EnvTarget = "sandbox"
)

$ErrorActionPreference = "Stop"

# Resolve project root as the folder where this file lives
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Optional: make sure scripts are allowed (run once per user if needed)
# Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force

# Dot-source project-local helper functions (kept in repo)
$snippet = Join-Path $root "scripts\plaid_profile_snippet.ps1"
if (-not (Test-Path $snippet)) {
    throw "Missing $snippet"
}
. $snippet

switch ($EnvTarget) {
    "sandbox"      { Start-PlaidSandbox }
    "development"  { Start-PlaidDev }
    "production"   { Start-PlaidProd }
}


