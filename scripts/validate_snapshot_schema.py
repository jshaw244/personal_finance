#!/usr/bin/env python3
"""
validate_snapshot_schema.py
------------------------------------
Validate a session snapshot YAML file against the schema defined in
docs/automation/session_snapshot_schema.yaml. Optionally display a summary
and always log validation results to logs/validation_results.log.

Usage:
    python scripts/validate_snapshot_schema.py [snapshot.yaml] [--summary]
"""

from __future__ import annotations
import sys
import os
import yaml
from pathlib import Path
from datetime import datetime

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "docs" / "automation" / "session_snapshot_schema.yaml"
LOGS_DIR = PROJECT_ROOT / "logs"
VALIDATION_LOG = LOGS_DIR / "validation_results.log"

# --- Utilities ---
def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def find_latest_snapshot() -> Path | None:
    candidates = sorted(LOGS_DIR.glob("session_snapshot*.yaml"), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None

def write_validation_log(snapshot_path: Path, result: str, errors: list[str], snapshot: dict) -> None:
    """Append summary of validation results to validation_results.log"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    metrics = snapshot.get("db", {})
    git = snapshot.get("git", {})
    docs = snapshot.get("docs", {})
    line = (
        f"[{ts}] {snapshot_path.name} — RESULT: {result} — "
        f"Issues: {len(errors)} — "
        f"Items: {metrics.get('items_count', 'n/a')} — "
        f"Tx: {metrics.get('transactions_count', 'n/a')} — "
        f"WebhookEv: {metrics.get('webhook_events_count', 'n/a')} — "
        f"Branch: {git.get('branch', 'n/a')} — "
        f"Tag: {git.get('latest_tag', 'n/a')} — "
        f"Docs: {docs.get('file_count', 'n/a')} files"
    )
    VALIDATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with VALIDATION_LOG.open("a", encoding="utf-8") as logf:
        logf.write(line + "\n")

def validate_dict(data: dict, schema: dict, path: str = "root") -> list[str]:
    """Recursively verify that expected keys and types exist."""
    errors = []
    if "fields" in schema:
        schema = schema["fields"]
    for key, subschema in schema.items():
        if key not in data:
            errors.append(f"{path}: missing key '{key}'")
            continue
        expected = subschema.get("type")
        val = data[key]
        # Basic types
        if expected == "object" and not isinstance(val, dict):
            errors.append(f"{path}.{key}: expected object, got {type(val).__name__}")
        elif expected == "array" and not isinstance(val, list):
            errors.append(f"{path}.{key}: expected array, got {type(val).__name__}")
        elif expected == "string" and not isinstance(val, str):
            errors.append(f"{path}.{key}: expected string, got {type(val).__name__}")
        elif expected == "integer" and not isinstance(val, int):
            errors.append(f"{path}.{key}: expected integer, got {type(val).__name__}")
        elif expected == "boolean" and not isinstance(val, bool):
            errors.append(f"{path}.{key}: expected boolean, got {type(val).__name__}")
        # Nested objects
        if isinstance(val, dict) and "properties" in subschema:
            errors.extend(validate_dict(val, subschema["properties"], f"{path}.{key}"))
        if isinstance(val, list) and "items" in subschema:
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    errors.extend(validate_dict(item, subschema["items"], f"{path}.{key}[{i}]"))
    return errors

def validate_db_summary(snapshot: dict) -> list[str]:
    """Additional checks for db_summary block."""
    errors = []
    dbs = snapshot.get("db_summary")
    if not dbs:
        return errors
    for fld in ("transaction_table_rows", "webhook_table_rows"):
        if fld in dbs and not isinstance(dbs[fld], int):
            errors.append(f"db_summary.{fld}: expected integer, got {type(dbs[fld]).__name__}")
    if "last_inspected" in dbs:
        ts = dbs["last_inspected"]
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            errors.append(f"db_summary.last_inspected: invalid timestamp format '{ts}'")
    return errors

def print_summary(snapshot: dict) -> None:
    """Display concise metrics."""
    db = snapshot.get("db", {})
    git = snapshot.get("git", {})
    env = snapshot.get("env", {})
    docs = snapshot.get("docs", {})
    dbs = snapshot.get("db_summary", {})
    print("\nSnapshot Summary")
    print("-" * 70)
    print(f"Project:         {snapshot.get('project', 'unknown')}")
    print(f"Date (local):    {snapshot.get('date_local', 'n/a')}")
    print(f"Branch:          {git.get('branch', 'n/a')}")
    print(f"Commit:          {git.get('short_commit', 'n/a')}")
    print(f"Latest Tag:      {git.get('latest_tag', 'n/a')}")
    print(f"Environment:     {env.get('ENV_TARGET', 'n/a')} ({env.get('PLAID_ENV', 'n/a')})")
    print(f"Database Path:   {db.get('path', 'n/a')}")
    print(f"Items:           {db.get('items_count', 'n/a')}")
    print(f"Transactions:    {db.get('transactions_count', 'n/a')}")
    print(f"Webhook Events:  {db.get('webhook_events_count', 'n/a')}")
    print(f"Docs:            Exists={docs.get('exists', 'n/a')}  Files={docs.get('file_count', 'n/a')}")
    if dbs:
        print(f"DB Summary Rows: tx={dbs.get('transaction_table_rows', 'n/a')}, "
              f"webhooks={dbs.get('webhook_table_rows', 'n/a')}")
        print(f"DB Last Checked: {dbs.get('last_inspected', 'n/a')}")
    print("-" * 70)
    print()

def main(argv: list[str]) -> int:
    show_summary = "--summary" in argv
    args = [a for a in argv[1:] if a != "--summary"]
    target_file = Path(args[0]) if args else find_latest_snapshot()
    if not target_file or not target_file.exists():
        print("Error: no snapshot YAML file found.")
        return 1
    if not SCHEMA_PATH.exists():
        print(f"Error: schema file not found at {SCHEMA_PATH}")
        return 1

    print(f"Validating snapshot: {target_file}")
    snapshot = load_yaml(target_file)
    schema = load_yaml(SCHEMA_PATH)
    errors = validate_dict(snapshot, schema.get("snapshot", {}))
    errors += validate_db_summary(snapshot)

    if errors:
        print("\nValidation failed:")
        for e in errors:
            print(f"  - {e}")
        print(f"\n{len(errors)} issue(s) found.")
        write_validation_log(target_file, "FAIL", errors, snapshot)
        return 2

    print("\nValidation successful — snapshot structure matches schema.")
    print(f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if show_summary:
        print_summary(snapshot)
    write_validation_log(target_file, "PASS", errors, snapshot)
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
