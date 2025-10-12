#!/usr/bin/env python3
"""
Generate a lightweight session snapshot for the personal_finance project.

Outputs YAML to stdout and (optionally) writes to a file.
- No external deps (uses stdlib only)
- Safe on Windows/PowerShell
- Gracefully degrades if git/ngrok aren’t available
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error
import platform

# Optional: detect which PowerShell script called this snapshot
def detect_session_source():
    # Look at the parent process command line if available
    try:
        import psutil
        parent = psutil.Process().parent()
        cmdline = " ".join(parent.cmdline()).lower()
        for marker in ["create_docs_structure.ps1", "update_requirements.ps1", "run_analysis.ps1", "run.ps1"]:
            if marker.lower() in cmdline:
                return marker
    except Exception:
        pass
    # Fallback: check argv or default
    for arg in sys.argv:
        if arg.lower().endswith(".ps1"):
            return Path(arg).name
    return "manual_or_unknown"

# ---- Config (edit if your layout changes) ----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "plaid.db"
LOGS_DIR = PROJECT_ROOT / "logs"
# Timestamped output (e.g. session_snapshot_20251011_1518.yaml)
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M")
DEFAULT_OUT = LOGS_DIR / f"session_snapshot_{TIMESTAMP}.yaml"
SCHEMA_FILE = PROJECT_ROOT / "src" / "storage" / "schema.sql"
WEBHOOK_ENDPOINT_PREFIX = "/plaid"  # how your blueprint is mounted
# ---------------------------------------------

def rotate_snapshots(log_dir: Path, pattern: str = "session_snapshot_*.yaml", keep: int = 10) -> None:
    """Keep only the most recent <keep> YAML snapshots."""
    try:
        files = sorted(
            log_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass

def _read_env(k: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(k, default)
    return v if v is not None and str(v).strip() != "" else default

def get_git_info() -> Dict[str, Optional[str]]:
    def _run(args: List[str]) -> Optional[str]:
        try:
            return subprocess.check_output(args, cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return None

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    commit = _run(["git", "rev-parse", "--short", "HEAD"])
    dirty = _run(["git", "status", "--porcelain"])
    return {
        "branch": branch,
        "short_commit": commit,
        "dirty": bool(dirty)
    }

def get_versions() -> Dict[str, Optional[str]]:
    def _pyver() -> str:
        return ".".join(map(str, sys.version_info[:3]))
    def _pkg(name: str) -> Optional[str]:
        try:
            # importlib.metadata built-in on 3.8+
            from importlib.metadata import version, PackageNotFoundError
        except Exception:
            try:
                from importlib_metadata import version, PackageNotFoundError  # type: ignore
            except Exception:
                return None
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    return {
        "python": _pyver(),
        "plaid-python": _pkg("plaid-python"),
        "flask": _pkg("flask"),
        "python-dotenv": _pkg("python-dotenv")
    }

def get_db_counts(db_path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return out
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        def _count(table: str) -> Optional[int]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                return int(cur.fetchone()[0])
            except Exception:
                return None

        out.update({
            "items_count": _count("items"),
            "accounts_count": _count("accounts"),
            "transactions_count": _count("transactions"),
            "webhook_events_count": _count("webhook_events"),
        })

        # cursor table presence
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transaction_cursors';")
            out["cursor_table_present"] = cur.fetchone() is not None
        except Exception:
            out["cursor_table_present"] = False
    finally:
        con.close()
    return out

def get_last_webhook_events(db_path: Path, limit: int = 5) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows: List[Dict[str, Any]] = []
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT received_at, webhook_type, webhook_code, item_id
            FROM webhook_events
            ORDER BY received_at DESC
            LIMIT ?;
        """, (limit,))
        for r in cur.fetchall():
            rows.append({
                "received_at": r["received_at"],
                "webhook_type": r["webhook_type"],
                "webhook_code": r["webhook_code"],
                "item_id": r["item_id"],
            })
    except Exception:
        pass
    finally:
        con.close()
    return rows

def get_ngrok_public_url() -> Optional[str]:
    # Try local ngrok introspection API
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2.0) as resp:
            data = json.load(resp)
            tunnels = data.get("tunnels") or []
            if tunnels:
                # Return the first public URL
                return tunnels[0].get("public_url")
    except (urllib.error.URLError, TimeoutError, ValueError, Exception):
        return None
    return None

def build_snapshot() -> Dict[str, Any]:
    now_ct = datetime.now().astimezone()  # local time with tz
    git = get_git_info()
    versions = get_versions()
    db = get_db_counts(DB_PATH)
    recent_events = get_last_webhook_events(DB_PATH, limit=5)
    session_source = detect_session_source()

    return {
        "project": "personal_finance",
        "session_source": session_source,
        "root": str(PROJECT_ROOT),
        "date_local": now_ct.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
        },
        "versions": versions,
        "git": git,
        "env": {
            "ENV_TARGET": _read_env("ENV_TARGET"),
            "PLAID_ENV": (_read_env("PLAID_ENV") or "sandbox"),
        },
        "web": {
            "local_url": "http://127.0.0.1:5000",
            "webhook_endpoint_prefix": WEBHOOK_ENDPOINT_PREFIX,
            "ngrok_public_url": get_ngrok_public_url(),
        },
        "db": db,
        "recent_webhook_events": recent_events,
        "files_of_interest": {
            "schema_sql": str(SCHEMA_FILE),
            "webhooks_py": str(PROJECT_ROOT / "src" / "ingestion" / "webhooks.py"),
            "run_ps1": str(PROJECT_ROOT / "runs" / "sandbox" / "run.ps1"),
            "requirements": str(PROJECT_ROOT / "src" / "requirements.txt"),
            "logs_dir": str(LOGS_DIR),
        },
        "next_actions_placeholder": [
            # Fill these in when you start a session, or leave as-is.
            # e.g., "Finish ingestion sync testing", "Begin analysis.py categorization"
        ],
    }

def to_yaml(d: Any, indent: int = 0) -> str:
    """
    Minimal YAML emitter for scalars, lists, and dicts.
    Avoids external dependencies. Not for complex YAML features.
    """
    sp = "  " * indent
    if d is None:
        return "null"
    if isinstance(d, (str, int, float)):
        # Quote strings with special chars or leading/trailing spaces
        if isinstance(d, str):
            if d == "" or d.strip() != d or any(c in d for c in [":", "#", "-", "{", "}", "[", "]", ",", "&", "*", "!", "|", ">", "'", '"', "%", "@", "`"]):
                return json.dumps(d)  # JSON quoting is YAML-safe
        return str(d)
    if isinstance(d, bool):
        return "true" if d else "false"
    if isinstance(d, list):
        if not d:
            return "[]"
        lines = []
        for item in d:
            val = to_yaml(item, indent + 1)
            if "\n" in val:
                lines.append(f"{sp}- |\n" + "\n".join("  " * (indent + 1) + ln for ln in val.splitlines()))
            else:
                lines.append(f"{sp}- {val}")
        return "\n".join(lines)
    if isinstance(d, dict):
        if not d:
            return "{}"
        lines = []
        for k, v in d.items():
            key = str(k)
            val = to_yaml(v, indent + 1)
            if isinstance(v, (dict, list)) and val and "\n" in val:
                lines.append(f"{sp}{key}:\n{val}")
            else:
                lines.append(f"{sp}{key}: {val}")
        return "\n".join(lines)
    # Fallback: stringified
    return json.dumps(d)

def main(argv: List[str]) -> int:
    out_path = None
    if len(argv) >= 2:
        out_path = Path(argv[1]).resolve()

    snap = build_snapshot()
    yaml_text = to_yaml(snap)

    # Always print to stdout for easy copy/paste into chat
    print(yaml_text)

    # Optionally write to file (default path or user-provided)
    target = Path(out_path) if out_path else DEFAULT_OUT
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml_text, encoding="utf-8")
        rotate_snapshots(LOGS_DIR)
        # Also print a hint so you know where it went
        print(f"\n# written to: {target}")
    except Exception as e:
        print(f"\n# warning: could not write to file: {e}", file=sys.stderr)

    # Log to maintenance.log for audit trace
    try:
        log_path = PROJECT_ROOT / "logs" / "maintenance.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session_source = detect_session_source()
        git_info = get_git_info()
        commit = git_info.get("short_commit") or "unknown"
        branch = git_info.get("branch") or "unknown"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] SESSION SNAPSHOT — source: {session_source}, branch: {branch}, commit: {commit}, file: {target}\n")


    except Exception as e:
        print(f"# warning: could not write to maintenance.log: {e}", file=sys.stderr)
   
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
