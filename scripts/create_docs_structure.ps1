<#
.SYNOPSIS
  Create /docs structure, populate README.md stubs,
  log actions, commit to Git, run make_session_snapshot.py,
  and inspect DB state for full traceability.

.DESCRIPTION
  • Creates documentation structure (automation, ingestion, analysis, processing, storage)
  • Logs all actions to logs/maintenance.log
  • Commits + tags in Git (docs-YYYYMMDD_HHMM)
  • Runs make_session_snapshot.py → DOCS SUMMARY YAML
  • Runs inspect_snapshot_db_state.py → DB SUMMARY log
#>

$ErrorActionPreference = "Stop"

# --- Configuration ---
$projectRoot = "C:\DATA\personal_finance"
$docsRoot    = Join-Path $projectRoot "docs"
$logFile     = Join-Path $projectRoot "logs\maintenance.log"
$pythonExe   = "python"
$timestamp   = Get-Date -Format "yyyyMMdd_HHmm"
Set-Location $projectRoot

# --- Utility: Timestamped log writer ---
function Write-Log {
    param ([string]$Message)
    $ts = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $logFile -Value "[$ts] DOCS ACTION — $Message"
}

# --- Section Content ---
$automationDoc = @"
# Automation Module — PowerShell + Workflow Orchestration
**Path:** `/docs/automation`  
**Related Code:** `scripts/update_requirements.ps1`, `runs/sandbox/run.ps1`
"@

$ingestionDoc = @"
# Ingestion Module — Plaid Webhook & Data Sync
**Path:** `/docs/ingestion`  
**Related Code:** `src/ingestion/webhooks.py`, `scripts/trigger_webhook.py`
"@

$analysisDoc = @"
# Analysis Module — Data Exploration and Insight
**Path:** `/docs/analysis`  
**Related Code:** `src/analysis/analysis.py`, `scripts/explore_transactions.py`
"@

$processingDoc = @"
# Processing Module — Data Transformation and Cleaning
**Path:** `/docs/processing`  
**Related Code:** `src/processing/` (planned)
"@

$storageDoc = @"
# Storage Module — Database Design and Schema Management
**Path:** `/docs/storage`  
**Related Code:** `src/storage/db.py`, `src/storage/schema.sql`
"@

# Combine into hashtable after defining each here-string
$sections = New-Object 'System.Collections.Generic.Dictionary[string,string]'
$sections["automation"]  = $automationDoc
$sections["ingestion"]   = $ingestionDoc
$sections["analysis"]    = $analysisDoc
$sections["processing"]  = $processingDoc
$sections["storage"]     = $storageDoc



# --- Step 1: Create folder structure ---
Write-Host "Creating documentation structure under $docsRoot..."
if (!(Test-Path $docsRoot)) {
    New-Item -ItemType Directory -Path $docsRoot | Out-Null
    Write-Log "Created main docs folder"
}

foreach ($section in $sections.Keys) {
    $folder = Join-Path $docsRoot $section
    $readme = Join-Path $folder "README.md"

    if (!(Test-Path $folder)) {
        New-Item -ItemType Directory -Path $folder | Out-Null
        Write-Host "Created folder: $folder"
        Write-Log "Created docs folder: $section"
    }

    if (Test-Path $readme) {
        Write-Host "Skipped existing file: $readme"
    } else {
        $sections[$section] | Out-File -FilePath $readme -Encoding utf8
        Write-Host "Created README: $readme"
        Write-Log "Created README for $section"
    }
}

Write-Host "`nDocumentation structure setup complete."
Write-Log "Documentation structure setup complete"

# --- Step 2: Commit + Tag in Git ---
if (Test-Path ".git") {
    Write-Host "`nStaging new documentation files..."
    git add docs logs/maintenance.log

    $defaultMsg = "Add or update documentation stubs ($timestamp)"
    $commitMsg = Read-Host "Enter commit message [$defaultMsg]"
    if ([string]::IsNullOrWhiteSpace($commitMsg)) {
        $commitMsg = $defaultMsg
    }

    git commit -m $commitMsg
    if ($LASTEXITCODE -ne 0) {
        Write-Host "No new changes to commit. Skipping tag creation."
        Write-Log "Skipped Git commit — no changes detected"
    } else {
        $tagName = "docs-$timestamp"
        git tag -a $tagName -m "Documentation update ($timestamp)"
        Write-Host "Created Git tag: $tagName"
        Write-Log "Created Git commit + tag: $tagName"

        $pushChoice = Read-Host "Push commit and tag to remote repository now? (y/n)"
        if ($pushChoice -eq "y") {
            Write-Host "Pushing to origin..."
            git push origin HEAD
            git push origin $tagName
            Write-Host "Push complete."
            Write-Log "Pushed commit and tag: $tagName"
        } else {
            Write-Host "Push skipped."
            Write-Log "Push skipped for tag: $tagName"
        }
    }
} else {
    Write-Host "No Git repository detected — skipping commit, tag, and push."
    Write-Log "Skipped Git commit — no repository detected"
}

# --- Step 3: Append DOCS SUMMARY snapshot via Python ---
$makeSnapshot = Join-Path $projectRoot "scripts\make_session_snapshot.py"
if (Test-Path $makeSnapshot) {
    try {
        Write-Host "`nGenerating DOCS SUMMARY snapshot via make_session_snapshot.py ..."
        & $pythonExe $makeSnapshot | Out-Null
        Write-Log "Generated DOCS SUMMARY snapshot (YAML) via make_session_snapshot.py"
    } catch {
        Write-Host "Warning: could not run make_session_snapshot.py. Error: $_"
        Write-Log "Failed to run make_session_snapshot.py — $_"
    }
} else {
    Write-Host "make_session_snapshot.py not found — skipping DOCS SUMMARY snapshot."
    Write-Log "Skipped DOCS SUMMARY snapshot — script not found"
}

# --- Step 4: Run inspect_snapshot_db_state.py to log DB SUMMARY ---
$inspectScript = Join-Path $projectRoot "scripts\inspect_snapshot_db_state.py"
if (Test-Path $inspectScript) {
    try {
        Write-Host "`nRunning inspect_snapshot_db_state.py to record DB state ..."
        & $pythonExe $inspectScript | Out-Null
        Write-Log "Executed inspect_snapshot_db_state.py and appended DB SUMMARY"
    } catch {
        Write-Host "Warning: could not run inspect_snapshot_db_state.py. Error: $_"
        Write-Log "Failed to run inspect_snapshot_db_state.py — $_"
    }
} else {
    Write-Host "inspect_snapshot_db_state.py not found — skipping DB SUMMARY logging."
    Write-Log "Skipped DB SUMMARY logging — script not found"
}

# --- Step 5: Finalize ---
Write-Host "`nDocumentation creation + Git commit + snapshot + DB log complete."
Write-Log "Documentation creation + Git commit + snapshot + DB log complete"

