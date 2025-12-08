"""
One-off patch: ensure accounts table has `current` and `available` columns.

Run with:
    $env:ENV_TARGET = "production"
    python .\scripts\patch_accounts_columns.py
"""

import os
import sqlite3
from typing import List, Tuple

from src.common.config import load_env
from src.common.paths import PROJECT_ROOT, DB_FILE as DB_PATH


def log(msg: str) -> None:
    print(msg)


def get_columns(cur: sqlite3.Cursor, table: str) -> List[Tuple]:
    cur.execute(f"PRAGMA table_info({table})")
    return cur.fetchall()


def column_exists(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cols = get_columns(cur, table)
    return any(c[1] == col for c in cols)  # c[1] is column name


def main() -> None:
    env_target = os.getenv("ENV_TARGET", "sandbox")
    cfg = load_env(env_target)  # ensures DB_FILE is aligned
    db_path = str(DB_PATH)

    log(f"ENV_TARGET = {env_target}")
    log(f"DB_FILE    = {db_path}")

    if not os.path.exists(db_path):
        log(f"ERROR: Database file not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Show current columns first
    log("\nCurrent accounts columns:")
    for col in get_columns(cur, "accounts"):
        # col = (cid, name, type, notnull, dflt_value, pk)
        log(f"  - {col[1]} ({col[2]})")

    # Patch list
    patches = [
        ("current", "REAL"),
        ("available", "REAL"),
        # iso_currency_code / unofficial_currency_code already exist per your schema,
        # so we don't touch those here.
    ]

    for col_name, col_type in patches:
        if column_exists(cur, "accounts", col_name):
            log(f"Column '{col_name}' already exists on accounts — skipping.")
        else:
            stmt = f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type}"
            log(f"Adding column '{col_name}' with: {stmt}")
            cur.execute(stmt)

    conn.commit()

    # Show final columns
    log("\nFinal accounts columns:")
    for col in get_columns(cur, "accounts"):
        log(f"  - {col[1]} ({col[2]})")

    conn.close()
    log("\nPatch completed.")


if __name__ == "__main__":
    main()
