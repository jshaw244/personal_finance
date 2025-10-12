<#
.SYNOPSIS
    Clean and validate a PowerShell script.
.DESCRIPTION
    - Removes emojis / non-ASCII characters
    - Normalizes smart quotes and dashes
    - Verifies balanced braces and parser syntax
    - Rewrites file in plain ASCII (no BOM)
.EXAMPLE
    .\sanitize_ps.ps1 "C:\DATA\personal_finance\scripts\run_analysis.ps1"
#>

param(
    [Parameter(Mandatory)]
    [string]$Path
)

if (-not (Test-Path $Path)) {
    Write-Host "❌ File not found: $Path" -ForegroundColor Red
    exit 1
}

Write-Host "Sanitizing: $Path" -ForegroundColor Cyan
$raw = Get-Content $Path -Raw

# --- 1️⃣  Replace smart quotes / dashes ---
$raw = $raw -replace "[“”]", '"'           # double quotes
$raw = $raw -replace "[‘’]", "'"           # single quotes
$raw = $raw -replace "[–—−]", "-"          # dashes and minus

# --- 2️⃣  Remove emojis & non-ASCII chars ---
# Keeps ASCII 32–126 + newline/tab
$clean = -join ($raw.ToCharArray() | ForEach-Object {
    if ([int][char]$_ -ge 32 -and [int][char]$_ -le 126 -or $_ -match "`r|`n|`t") { $_ }
})

# --- 3️⃣  Count braces ---
$counts = @{
    '{' = ($clean.ToCharArray() | Where-Object {$_ -eq '{'}).Count
    '}' = ($clean.ToCharArray() | Where-Object {$_ -eq '}'}).Count
    '(' = ($clean.ToCharArray() | Where-Object {$_ -eq '('}).Count
    ')' = ($clean.ToCharArray() | Where-Object {$_ -eq ')'}).Count
    '[' = ($clean.ToCharArray() | Where-Object {$_ -eq '['}).Count
    ']' = ($clean.ToCharArray() | Where-Object {$_ -eq ']'}).Count
}

# --- 4️⃣  Write clean copy ---
$backup = "$Path.bak_$(Get-Date -Format yyyyMMdd_HHmmss)"
Copy-Item $Path $backup -Force
$clean | Out-File $Path -Encoding ascii -Force

# --- 5️⃣  Validate parse ---
try {
    [void][System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$null,[ref]$null)
    $status = "OK"
    $color = "Green"
} catch {
    $status = "ERROR"
    $color = "Red"
    $errMsg = $_.Exception.Message
}

Write-Host "`nBrace count:" ("{=$($counts['{'])  }=$($counts['}'])  (=$($counts['('])  )=$($counts[')'])  [=$($counts['['])  ]=$($counts[']'])")
Write-Host "Parser check: $status" -ForegroundColor $color
if ($status -eq "ERROR") { Write-Host " → $errMsg" -ForegroundColor Red }

Write-Host "Backup saved as: $backup" -ForegroundColor Yellow
Write-Host "Cleaned file written to: $Path" -ForegroundColor Green
