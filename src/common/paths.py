from pathlib import Path

# --- Core project structure ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# --- Data ---
DATA_DIR = PROJECT_ROOT / "data"
DB_FILE = DATA_DIR / "plaid.db"

# --- Config ---
CONFIG_DIR = PROJECT_ROOT / "config" / "env"
ENV_FILE_SANDBOX = CONFIG_DIR / ".env.sandbox"
ENV_FILE_DEVELOPMENT = CONFIG_DIR / ".env.development"
ENV_FILE_PRODUCTION = CONFIG_DIR / ".env.production"

# --- Logs ---
LOG_DIR = PROJECT_ROOT / "logs"
APP_LOG = LOG_DIR / "app.log"
MAINTENANCE_LOG = LOG_DIR / "maintenance.log"

# --- Backups ---
BACKUP_DIR = PROJECT_ROOT / "backups"

# --- Scripts & Runs ---
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
SANDBOX_RUN = RUNS_DIR / "sandbox" / "run.ps1"

# --- Schema (if used) ---
SCHEMA_FILE = PROJECT_ROOT / "src" / "storage" / "schema.sql"
