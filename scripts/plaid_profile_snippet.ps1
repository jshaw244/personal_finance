# scripts/plaid_profile_snippet.ps1
# project root = parent of /scripts
$projRoot = Split-Path -Parent $PSScriptRoot

function Start-PlaidSandbox {
    $script = Join-Path $projRoot "scripts\runs\sandbox\run.ps1"
    & $script
}

function Start-PlaidDev {
    $script = Join-Path $projRoot "scripts\runs\development\run.ps1"
    & $script
}

function Start-PlaidProd {
    $script = Join-Path $projRoot "scripts\runs\production\run.ps1"
    & $script
}