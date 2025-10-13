# src/common/config.py
# Purpose: Centralize environment loading per target (sandbox|development|production)
# Usage:
#   from src.common.config import load_env
#   cfg = load_env(os.getenv("ENV_TARGET", "sandbox"))
#   # Then read from os.environ as usual, e.g., os.getenv("PLAID_CLIENT_ID")

import os
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv

def project_root() -> Path:
    from src.common.paths import PROJECT_ROOT
    return PROJECT_ROOT

def env_file_for(target: str) -> Path:
    target = (target or "sandbox").lower()
    return project_root() / "config" / "env" / f".env.{target}"

def load_env(target: Optional[str] = None) -> Dict[str, str]:
    target = (target or "sandbox").lower()
    path = env_file_for(target)
    print(f"DEBUG load_env: target={target}  path={path}")

    if not path.exists():
        raise FileNotFoundError(f"Missing env file: {path} (expected for target '{target}')")

    load_dotenv(path, override=True)

    # --- Inject consistent defaults ---
    os.environ.setdefault("ENV_TARGET", target)
    os.environ.setdefault("LOG_LEVEL", "INFO")

    # --- Optional: choose database path by target ---
    db_default = {
        "sandbox": "data/plaid.db",
        "development": "data/plaid_dev.db",
        "production": "data/plaid_prod.db",
    }.get(target, "data/plaid.db")

    os.environ.setdefault("DATABASE_URL", db_default)

    # --- Construct return dictionary for reference ---
    cfg = {
        "TARGET": target,
        "PLAID_CLIENT_ID": os.getenv("PLAID_CLIENT_ID", ""),
        "PLAID_SECRET": os.getenv("PLAID_SECRET", ""),
        "PLAID_ENV": os.getenv("PLAID_ENV", target),
        "DATABASE_URL": os.getenv("DATABASE_URL", db_default),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
    }

    # --- Validation: missing required fields ---
    missing = [k for k in ("PLAID_CLIENT_ID", "PLAID_SECRET") if not cfg[k]]
    if missing:
        raise EnvironmentError(
            f"Missing required env vars in {path}: " + ", ".join(missing)
        )

    # --- Warn if mismatched target vs .env content ---
    env_target = os.getenv("PLAID_ENV", "").lower()
    if env_target != target:
        print(
            f"Warning: .env file sets PLAID_ENV={env_target!r} "
            f"but you loaded target={target!r}. Check for mismatch."
        )

    return cfg
