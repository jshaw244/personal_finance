# scripts/update_requirements.ps1
<#
.SYNOPSIS
    Automate environment freezes, session snapshots, commits, tagging, and optional pushing to GitHub.

.DESCRIPTION
    Designed for use inside an active virtual environment (.venv).
    Modes:
      • Default: freeze requirements → snapshot → commit → tag → (optional push)
      • --no-push: same as default but skip pushing
      • --tag-only: skip freeze/snapshot/commit and just tag the current commit

.USAGE
    ./scripts/update_requirements.ps1
    ./scripts/update_requirements.ps1 --no-push
    ./scripts/update_requirements.ps1 --tag-only
#>

param(
    [switch]$NoPush,
    [switch]$TagOnly
)

$ErrorActionPreference = "Stop"

# --- Step 1: Move to project root ---
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# --- Safety check: ensure we are on a branch (not detached HEAD) ---
if ($branch -eq "HEAD") {
    Write-Host "Detected detached HEAD. Attempting to check out default branch..."
    $defaultBranch = (git remote show origin | Select-String "HEAD branch:" | ForEach-Object { ($_ -split ":")[1].Trim() })
    if (-not $defaultBranch) { $defaultBranch = "main" }
    git checkout $defaultBranch
    $branch = $defaultBranch
} else {
    Write-Host "Current Git branch: $branch"
}



# --- Step 2: Tag-only mode ---
if ($TagOnly) {
    if (-not (Test-Path ".git")) {
        Write-Host "No Git repository detected - cannot tag."
        exit 1
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $tagName = "snapshot-$timestamp"

    Write-Host "Creating Git tag: $tagName"
    git tag -a $tagName -m "Manual tag created on $timestamp"

    if (-not $NoPush) {
        $push = Read-Host "Push tag to remote repository now? (y/n)"
        if ($push -match "^[Yy]") {
            try {
                git push origin $tagName
                Write-Host "Tag pushed successfully."
            } catch {
                Write-Host "Warning: push failed. Check network or credentials."
            }
        } else {
            Write-Host "Push skipped by user."
        }
    } else {
        Write-Host 'Push disabled (--no-push flag).'
    }

    Write-Host "Done. Tag-only mode complete."
    exit 0
}

# --- Step 3: Freeze environment ---
$reqPath = "src\requirements.txt"
Write-Host "Freezing environment to $reqPath ..."
pip freeze > $reqPath

# --- Step 4: Run session snapshot with timestamped filename ---
$snapshotScript = "scripts\make_session_snapshot.py"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$snapshotDir = "logs"
$snapshotFile = "session_snapshot_$timestamp.yaml"
$snapshotPath = Join-Path $snapshotDir $snapshotFile

if (Test-Path $snapshotScript) {
    Write-Host "Running $snapshotScript → $snapshotPath ..."
    python $snapshotScript $snapshotPath
} else {
    Write-Host "No make_session_snapshot.py found - skipping snapshot."
}

# --- Step 5: Commit, tag, and optional push ---
if (Test-Path ".git") {
    Write-Host "Staging all repository changes..."
    git add -A

    # Prompt for commit message
    $defaultMsg = "Automated update of requirements and session snapshot ($timestamp)"
    $commitMsg = Read-Host "Enter commit message [`$defaultMsg` for default]"
    if ([string]::IsNullOrWhiteSpace($commitMsg)) {
        $commitMsg = $defaultMsg
    }

    git commit -m $commitMsg

    # Create a Git tag for this snapshot
    $tagName = "snapshot-$timestamp"
    Write-Host "Creating Git tag: $tagName"
    git tag -a $tagName -m "Session snapshot created on $timestamp"

    # Handle push logic
    if (-not $NoPush) {
        $push = Read-Host "Push commit and tag to remote repository now? (y/n)"
        if ($push -match "^[Yy]") {
            $branch = git rev-parse --abbrev-ref HEAD
            Write-Host "Pushing to origin/$branch ..."
            try {
                git push origin $branch
                git push origin $tagName
                Write-Host "Push and tag complete."
            } catch {
                Write-Host "Warning: push failed. Check network or credentials."
            }
        } else {
            Write-Host "Push skipped by user."
        }
    } else {
        Write-Host 'Push disabled (--no-push flag).'
    }

} else {
    Write-Host "No Git repository detected - skipping commit, tag, and push."
}

# --- Step 6: Summary ---
Write-Host ""
Write-Host "Requirements and session snapshot updated."
Write-Host "Snapshot file: $snapshotPath"
Write-Host "Git tag: $tagName"
Write-Host "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "Done."

