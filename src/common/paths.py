import os
from pathlib import Path

# -------------------------------------------------------------------
# Core project structure
# -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# --- Data ---
DATA_DIR = PROJECT_ROOT / "data"

# -------------------------------------------------------------------
# Environment-aware database path
# -------------------------------------------------------------------
env_target = os.getenv("ENV_TARGET", "sandbox").lower()

DB_MAP = {
    "sandbox": DATA_DIR / "plaid.db",
    "development": DATA_DIR / "plaid_dev.db",
    "production": DATA_DIR / "plaid_prod.db",
}

DB_FILE = DB_MAP.get(env_target, DATA_DIR / "plaid.db")

# Optional debugging (shows up in your console)
print(f"DEBUG DB_FILE: {DB_FILE}")

# -------------------------------------------------------------------
# Config, logs, backups, scripts, schema, results
# -------------------------------------------------------------------
CONFIG_DIR = PROJECT_ROOT / "config" / "env"
ENV_FILE_SANDBOX = CONFIG_DIR / ".env.sandbox"
ENV_FILE_DEVELOPMENT = CONFIG_DIR / ".env.development"
ENV_FILE_PRODUCTION = CONFIG_DIR / ".env.production"

LOG_DIR = PROJECT_ROOT / "logs"
APP_LOG = LOG_DIR / "app.log"
MAINTENANCE_LOG = LOG_DIR / "maintenance.log"

BACKUP_DIR = PROJECT_ROOT / "backups"

SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
SANDBOX_RUN = RUNS_DIR / "sandbox" / "run.ps1"

SCHEMA_FILE = PROJECT_ROOT / "src" / "storage" / "schema.sql"

RESULTS_DIR = PROJECT_ROOT / "results"
