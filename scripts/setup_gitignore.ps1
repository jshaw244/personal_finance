<#
.SYNOPSIS
    Ensures .gitignore contains all necessary patterns for generated files
    and removes any already-tracked result/log/output files from Git safely.
.DESCRIPTION
    - Adds patterns for results, logs, archive, Excel, CSV, PNG, TXT, and YAML outputs.
    - Creates .gitignore if missing.
    - Removes tracked generated files from the Git index (without deleting local copies).
    - Keeps all project source files intact.
.EXAMPLE
    .\setup_gitignore.ps1
#>

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$gitignorePath = Join-Path $repoRoot ".gitignore"

# 1) Ensure .gitignore exists
if (-not (Test-Path $gitignorePath)) {
    New-Item -Path $gitignorePath -ItemType File -Force | Out-Null
    Write-Host "Created new .gitignore" -ForegroundColor Cyan
}

# 2) Required ignore patterns
$ignorePatterns = @'
# Ignore runtime and analysis output
/results/
/logs/
/archive/
/*.xlsx
/*.csv
/*.png
/*.txt
session_snapshot_*.yaml
'@.Trim().Split("`n")

$current = Get-Content $gitignorePath -ErrorAction SilentlyContinue
$added = @()

foreach ($pattern in $ignorePatterns) {
    if ($current -notcontains $pattern) {
        Add-Content -Path $gitignorePath -Value $pattern
        $added += $pattern
    }
}

if ($added.Count -gt 0) {
    Write-Host "Added new ignore patterns:" -ForegroundColor Green
    $added | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "All ignore patterns already present." -ForegroundColor Yellow
}

# 3) Remove tracked files from index but keep local copies
$tracked = git ls-files results logs archive *.xlsx *.csv *.png *.txt 2>$null
if ($tracked) {
    Write-Host "`nRemoving tracked output files from Git index..." -ForegroundColor Cyan
    $tracked | ForEach-Object {
        try {
            git rm --cached $_ | Out-Null
            Write-Host "   Untracked: $_"
        } catch {
            Write-Host "   Skipped: $_"
        }
    }
} else {
    Write-Host "`nNo tracked output files found in index." -ForegroundColor Yellow
}

# 4) Stage .gitignore and commit cleanup
try {
    git add .gitignore | Out-Null
    git commit -m "Setup .gitignore and clean tracked outputs" | Out-Null
    Write-Host "Gitignore updated and cleanup committed." -ForegroundColor Green
} catch {
    Write-Host "Nothing new to commit." -ForegroundColor Yellow
}

Write-Host "Done. Future results and logs will be ignored by Git." -ForegroundColor Cyan

