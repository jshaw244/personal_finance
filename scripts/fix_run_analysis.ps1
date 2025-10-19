$path = "C:\DATA\personal_finance\scripts\run_analysis.ps1"

# 1) Known-good ASCII-only script (no smart punctuation, no BOM)
$code = @'
<#
.SYNOPSIS
    Run analysis pipeline for a given target environment (sandbox, development, production).
.DESCRIPTION
    - Uses the active .venv (assumes run.ps1 already activated it)
    - Supports -Days, -Start, and -End for analysis window
    - Runs test_<target>_analysis.py with arguments
    - Creates YAML session snapshot (make_session_snapshot.py)
    - Commits and tags results (analysis-YYYYMMDD_HHMM)
    - Rotates, compresses, and archives old results/snapshots
    - Shows a quick PowerShell summary preview
    - Opens latest Excel output automatically
#>

param(
    [string]$Target = "sandbox",
    [int]$Days = 30,
    [string]$Start = "",
    [string]$End = "",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

# --- Paths ---
$ProjectRoot    = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $ProjectRoot
$pythonExe      = ".\.venv\Scripts\python.exe"
$logFile        = "$ProjectRoot\logs\maintenance.log"
$resultsDir     = "$ProjectRoot\results"
$logsDir        = "$ProjectRoot\logs"
$archiveDir     = "$ProjectRoot\archive"
$snapScript     = "$ProjectRoot\scripts\make_session_snapshot.py"
$analysisScript = "$ProjectRoot\scripts\test_${Target}_analysis.py"
$timestamp      = Get-Date -Format "yyyyMMdd_HHmm"

# --- Helpers ---
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$ts] RUN_ANALYSIS - $Message"
    Write-Host $Message
}

function Rotate-OldResults {
    param(
        [string]$Dir,
        [string]$Pattern,
        [int]$Keep = 10
    )
    if (-not (Test-Path $Dir)) { return }
    $files = Get-ChildItem -Path $Dir -Filter $Pattern | Sort-Object LastWriteTime -Descending
    if ($files.Count -le $Keep) { return }
    $toRemove = $files | Select-Object -Skip $Keep
    try {
        $toRemove | ForEach-Object { Remove-Item $_.FullName -Force }
        Write-Log "Rotated old $Pattern files - kept $Keep, removed $($toRemove.Count)"
    } catch {
        Write-Log "Warning: rotation failed for $Pattern - $_"
    }
}

function Compress-OldResults {
    param(
        [string]$Dir,
        [string]$Pattern,
        [int]$Keep = 5
    )
    if (-not (Test-Path $Dir)) { return }
    $files = Get-ChildItem -Path $Dir -Filter $Pattern | Sort-Object LastWriteTime -Descending
    if ($files.Count -le $Keep) { return }
    $toArchive = $files | Select-Object -Skip $Keep
    if (-not (Test-Path $archiveDir)) { New-Item -ItemType Directory -Path $archiveDir | Out-Null }
    $zipName = Join-Path $archiveDir ("archive_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".zip")
    try {
        Compress-Archive -Path $($toArchive.FullName) -DestinationPath $zipName -Force
        $toArchive | ForEach-Object { Remove-Item $_.FullName -Force }
        Write-Log "Archived old $Pattern files into archive\$([System.IO.Path]::GetFileName($zipName))"
    } catch {
        Write-Log "Warning: compression failed for $Pattern - $_"
    }
}  # closes Compress-OldResults

function Cleanup-OldArchives {
    param(
        [string]$Dir,
        [int]$MaxAgeDays = 90
    )
    if (-not (Test-Path $Dir)) { return }
    $cutoff = (Get-Date).AddDays(-$MaxAgeDays)
    $oldZips = Get-ChildItem -Path $Dir -Filter "*.zip" | Where-Object { $_.LastWriteTime -lt $cutoff }
    if ($oldZips.Count -eq 0) { return }
    try {
        $oldZips | ForEach-Object { Remove-Item $_.FullName -Force }
        Write-Log "Deleted $($oldZips.Count) archive(s) older than $MaxAgeDays days."
    } catch {
        Write-Log "Warning: failed to delete some old archives - $_"
    }
}

# --- Step 1: Verify prerequisites ---
if (-not (Test-Path $analysisScript)) {
    Write-Host "Error: $analysisScript not found." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$ProjectRoot\data\plaid.db")) {
    Write-Host "Error: Database not found." -ForegroundColor Red
    exit 1
}

# --- Step 2: Run analysis ---
Write-Host "`nRunning $Target analysis pipeline..." -ForegroundColor Cyan

$argList = @()
if ($Days) { $argList += "--days"; $argList += $Days }
if ($Start -ne "") { $argList += "--start"; $argList += $Start }
if ($End -ne "") { $argList += "--end"; $argList += $End }

try {
    & $pythonExe $analysisScript @argList
    Write-Log "$Target analysis completed successfully. (Args: $($argList -join ' '))"
} catch {
    Write-Log "$Target analysis failed: $_"
    Write-Host "Error running analysis script: $_" -ForegroundColor Red
    exit 1
}

# --- Step 3: Generate session snapshot ---
Write-Host "`nCreating session snapshot..." -ForegroundColor Cyan
if (Test-Path $snapScript) {
    try {
        & $pythonExe $snapScript
        Write-Log "Session snapshot created successfully."
    } catch {
        Write-Log "Session snapshot failed: $_"
    }
} else {
    Write-Log "No make_session_snapshot.py found - skipping snapshot."
}

# --- Step 4: Git commit + tag + optional push ---
if (Test-Path ".git") {
    git add results logs
    $commitMsg = "Automated $Target analysis + snapshot ($timestamp)"
    git commit -m $commitMsg 2>$null
    if ($LASTEXITCODE -eq 0) {
        $tagName = "analysis-$timestamp"
        git tag -a $tagName -m "$Target analysis snapshot ($timestamp)"
        Write-Log "Created Git tag: $tagName"
        if (-not $NoPush) {
            $pushChoice = Read-Host "Push commit and tag to remote repository now? (y/n)"
            if ($pushChoice -match "^[Yy]") {
                git push origin HEAD
                git push origin $tagName
                Write-Log "Pushed commit and tag: $tagName"
            } else {
                Write-Log "Push skipped for tag: $tagName"
            }
        } else {
            Write-Log "Push disabled by --NoPush flag."
        }
    } else {
        Write-Log "No new changes detected - skipping tag."
    }
} else {
    Write-Log "No Git repository found - skipping commit/tag."
}

# --- Step 5: Rotate, compress, and clean archives ---
Write-Host "`nMaintaining old results and archives..." -ForegroundColor Cyan
Rotate-OldResults -Dir $resultsDir -Pattern "*.xlsx"
Rotate-OldResults -Dir $resultsDir -Pattern "*.csv"
Rotate-OldResults -Dir $logsDir -Pattern "session_snapshot_*.yaml"

Compress-OldResults -Dir $resultsDir -Pattern "*.xlsx"
Compress-OldResults -Dir $resultsDir -Pattern "*.csv"
Compress-OldResults -Dir $logsDir -Pattern "session_snapshot_*.yaml"

Cleanup-OldArchives -Dir $archiveDir -MaxAgeDays 90

# --- Step 6: Show quick summary (from latest CSV) ---
$latestCsv = Get-ChildItem -Path $resultsDir -Filter "${Target}_table_summary_*.csv" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if ($latestCsv) {
    Write-Host "`nLatest summary preview ($($latestCsv.Name)):" -ForegroundColor Green
    $csvHead = (Get-Content $latestCsv.FullName | Select-Object -First 10)
    $csvHead | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "No CSV summary found to preview." -ForegroundColor Yellow
}

# --- Step 7: Open latest Excel output ---
$latestExcel = Get-ChildItem -Path $resultsDir -Filter "${Target}_analysis_summary_*.xlsx" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if ($latestExcel) {
    Start-Process $latestExcel.FullName
    Write-Log "Opened Excel summary $($latestExcel.Name)"
} else {
    Write-Log "No Excel file found to open."
}

Write-Host "`n$Target analysis run complete." -ForegroundColor Cyan
Write-Log "$Target analysis run complete."
'@

# Write as ASCII (no BOM, no smart punctuation)
$code | Out-File -FilePath $path -Encoding ascii -Force

# 2) Show the exact lines around previous error spots (80-90 and 136-148)
$i=0; $first160 = Get-Content $path -First 160 | % { "{0,4}: {1}" -f (++$i), $_ }
$first160[79..89]; ""; $first160[135..147]; ""

# 3) Count braces and run the parser
$raw = Get-Content $path -Raw
$opens  = ($raw.ToCharArray() | ? {$_ -eq '{'}).Count
$closes = ($raw.ToCharArray() | ? {$_ -eq '}'}).Count
"Brace count -> { = $opens, } = $closes"

try {
    [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$null)
    "Parser: OK"
} catch {
    "Parser: ERROR"
    $_.Exception.Message
    $_.Exception.ErrorRecord.InvocationInfo.PositionMessage
}

# 4) Try executing it (expect normal runtime messages if prerequisites missing, but no syntax error)
"--- Running script ---"
powershell -NoProfile -ExecutionPolicy Bypass -File $path


