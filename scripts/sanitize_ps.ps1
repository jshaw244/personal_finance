<#
.SYNOPSIS
    Cleans PowerShell files of encoding and formatting issues that cause phantom parse errors.
.DESCRIPTION
    - Recursively scans for .ps1 files under a given path
    - Removes non-printable characters
    - Normalizes line endings to CRLF
    - Saves files as UTF8 with BOM
    - Logs all actions to logs/sanitizer.log
.PARAMETER Path
    Root directory to scan
.PARAMETER Pattern
    Wildcard pattern (default: *.ps1)
#>

param(
    [string]$Path = ".",
    [string]$Pattern = "*.ps1"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path "$Path").Path
$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir "sanitizer.log"

function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$ts] SANITIZER - $msg"
    Write-Host $msg
}

Write-Host "Scanning for PowerShell files under $ProjectRoot ..." -ForegroundColor Cyan
$files = Get-ChildItem -Path $ProjectRoot -Recurse -Filter $Pattern -File

foreach ($file in $files) {
    try {
        $raw = Get-Content $file.FullName -Raw -ErrorAction Stop

        # Remove non-printable and zero-width characters
        $clean = ($raw -replace '[\x00-\x08\x0B\x0C\x0E-\x1F\u200B-\u200D\uFEFF]', '')

        # Normalize line endings to CRLF
        $clean = $clean -replace "`r?`n", "`r`n"

        # Force UTF8 with BOM to ensure PowerShell parser compatibility
        $clean | Out-File -FilePath $file.FullName -Encoding UTF8 -Force

        Write-Log "Sanitized $($file.FullName)"
    }
    catch {
        Write-Log "ERROR processing $($file.FullName): $_"
    }
}

Write-Log "Sanitization complete. Total files: $($files.Count)"
Write-Host "Sanitization complete. Log written to $LogFile" -ForegroundColor Green

