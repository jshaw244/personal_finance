<# .\scripts\verify_active_db.ps1
.SYNOPSIS
    Verifies and initializes SQLite databases (sandbox, development, production).

.DESCRIPTION
    - If -Target is provided: verifies only that environment
    - If -Rebuild is provided: rebuilds only that environment (backs up first; extra prompts for prod)
    - Otherwise: verifies all environments
    - Uses scripts\init_db.py to initialize from src\storage\schema.sql (avoids quoting issues)
#>

param(
    [ValidateSet("sandbox","development","production")]
    [string]$Target,

    [ValidateSet("sandbox","development","production")]
    [string]$Rebuild
)

$ErrorActionPreference = "Stop"

# --- Core paths ---
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location -Path $ProjectRoot

$dataDir    = Join-Path $ProjectRoot "data"
$schemaFile = Join-Path $ProjectRoot "src\storage\schema.sql"
$backupDir  = Join-Path $ProjectRoot "backups"
$logDir     = Join-Path $ProjectRoot "logs"
$logFile    = Join-Path $logDir "maintenance.log"
$initScript = Join-Path $ProjectRoot "scripts\init_db.py"

if (-not (Test-Path $schemaFile)) { throw "schema.sql not found: $schemaFile" }
if (-not (Test-Path $initScript)) { throw "init_db.py not found: $initScript" }
if (-not (Test-Path $backupDir))  { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }
if (-not (Test-Path $logDir))     { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# --- Env DB map ---
$map = @{
    "sandbox"     = Join-Path $dataDir "plaid.db"
    "development" = Join-Path $dataDir "plaid_dev.db"
    "production"  = Join-Path $dataDir "plaid_prod.db"
}

function Write-MaintenanceLog([string]$message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$ts] $message"
}

function Resolve-Python {
    $venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    return "python"
}

function Backup-Database([string]$envName, [string]$dbPath) {
    if (Test-Path $dbPath) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmm"
        $dest = Join-Path $backupDir ("${envName}_$stamp.db")
        Copy-Item $dbPath $dest -Force
        Write-Host "Backup created: $dest" -ForegroundColor Green
        Write-MaintenanceLog "Database backed up: $envName -> $dest"
    }
}

function New-Database([string]$envName, [string]$dbPath) {
    $python = Resolve-Python
    Write-Host "Creating $envName database at $dbPath ..." -ForegroundColor Yellow
    & $python $initScript --db "$dbPath" --schema "$schemaFile"
    Write-MaintenanceLog "Database initialized: $envName ($dbPath)"
}

# --- Determine scope ---
# Priority: Rebuild > Target > All
$targetsToCheck = @()
if ($Rebuild) {
    $targetsToCheck = @($Rebuild)
} elseif ($Target) {
    $targetsToCheck = @($Target)
} else {
    $targetsToCheck = @("sandbox","development","production")
}

# --- Print ACTIVE DB accurately (based on args only) ---
if ($Rebuild) {
    Write-Host ("ACTIVE DB (REBUILD {0}): {1}" -f $Rebuild.ToUpper(), $map[$Rebuild]) -ForegroundColor Cyan
} elseif ($Target) {
    Write-Host ("ACTIVE DB ({0}): {1}" -f $Target.ToUpper(), $map[$Target]) -ForegroundColor Cyan
} else {
    Write-Host "ACTIVE DB: (none specified) — verifying all environments." -ForegroundColor Cyan
}

# --- Rebuild flow ---
if ($Rebuild) {
    $dbPath = $map[$Rebuild]
    Write-Host "`n*** REBUILD REQUESTED for environment: $Rebuild ***" -ForegroundColor Yellow

    if ($Rebuild -eq "production") {
        $c1 = Read-Host "WARNING: rebuild PRODUCTION? (yes/no)"
        if ($c1 -ne "yes") { Write-Host "Aborted." -ForegroundColor Red; exit 0 }
        $c2 = Read-Host "Has production DB been backed up? (yes/no)"
        if ($c2 -ne "yes") { Write-Host "Aborted." -ForegroundColor Red; exit 0 }
    }

    Backup-Database $Rebuild $dbPath

    if (Test-Path $dbPath) {
        Remove-Item $dbPath -Force
        Write-Host "Deleted existing $Rebuild database." -ForegroundColor Yellow
        Write-MaintenanceLog "Deleted existing database for $Rebuild"
    }

    New-Database $Rebuild $dbPath
    Write-Host "`nRebuild complete for $Rebuild.`n" -ForegroundColor Green
}

# --- Verification ---
Write-Host "`n=== Database Verification ===" -ForegroundColor Cyan
foreach ($t in $targetsToCheck) {
    $dbPath = $map[$t]
    if (-not (Test-Path $dbPath)) {
        Write-Host ("{0,-12} -> {1}  (missing; creating...)" -f $t.ToUpper(), $dbPath) -ForegroundColor Yellow
        New-Database $t $dbPath
    } else {
        Write-Host ("{0,-12} -> {1}  (exists)" -f $t.ToUpper(), $dbPath) -ForegroundColor Green
    }
}
Write-Host "============================" -ForegroundColor Cyan
Write-Host ""
