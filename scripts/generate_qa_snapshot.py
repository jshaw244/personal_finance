# scripts/generate_qa_snapshot.py
"""
Generate a QA Snapshot Excel workbook summarizing:
  • Environment and session details (from latest YAML snapshot)
  • Database health across all environments
  • Recent maintenance log lines
  • File metadata (modified times, sizes)

Output:
  results/qa_snapshot_YYYYMMDD_HHMM.xlsx

Also appends an entry to logs/maintenance.log.
"""

import os
import sys
import sqlite3
import glob
import yaml
import pandas as pd
from datetime import datetime
from pathlib import Path
from src.common.paths import PROJECT_ROOT, DATA_DIR, RESULTS_DIR, LOG_DIR, BACKUP_DIR

# --------------------------------------------------------------------
# Helper: safe YAML loader (fallback to minimal parser)
# --------------------------------------------------------------------
def load_yaml_safely(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        data[k.strip()] = v.strip()
        except Exception:
            return {}
        return data

# --------------------------------------------------------------------
# Helper: read table counts from a database
# --------------------------------------------------------------------
def get_db_counts(db_path: Path) -> dict:
    out = {"database": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return out
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    tables = ["items", "accounts", "transactions", "webhook_events", "log_events", "budgets", "financial_health_history"]
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        except Exception:
            out[t] = None
    con.close()
    return out

# --------------------------------------------------------------------
# Helper: get last N maintenance log lines
# --------------------------------------------------------------------
def get_recent_log_lines(log_path: Path, n: int = 50) -> list[str]:
    if not log_path.exists():
        return []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    return lines[-n:] if len(lines) > n else lines

# --------------------------------------------------------------------
# Helper: collect file metadata
# --------------------------------------------------------------------
def get_file_info(root: Path) -> list[dict]:
    files = []
    for path in root.rglob("*.*"):
        if path.is_file() and "venv" not in str(path):
            stat = path.stat()
            files.append({
                "file": str(path.relative_to(PROJECT_ROOT)),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb": round(stat.st_size / 1024, 2)
            })
    return sorted(files, key=lambda x: x["file"].lower())

# --------------------------------------------------------------------
# Helper: sanitize for Excel-safe exports
# --------------------------------------------------------------------
def clean_excel_string(s: str) -> str:
    """Remove illegal control characters before writing to Excel."""
    if not isinstance(s, str):
        return s
    # keep printable ASCII + tab/newline/carriage return
    return "".join(ch for ch in s if 32 <= ord(ch) <= 126 or ch in ("\n", "\r", "\t"))

def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Apply Excel-safe sanitization to all string columns in a DataFrame."""
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(clean_excel_string)
    return df

# --------------------------------------------------------------------
# Main QA snapshot build
# --------------------------------------------------------------------
def main() -> int:
    RESULTS_DIR.mkdir(exist_ok=True, parents=True)
    LOG_DIR.mkdir(exist_ok=True, parents=True)

    # 1. Locate most recent session snapshot YAML
    yaml_files = sorted(LOG_DIR.glob("session_snapshot_*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest_yaml = yaml_files[0] if yaml_files else None
    session_data = load_yaml_safely(latest_yaml) if latest_yaml else {}

    # 2. Build environment summary
    env_summary = []
    if session_data:
        for section, data in session_data.items():
            if isinstance(data, dict):
                for k, v in data.items():
                    env_summary.append({"section": section, "key": k, "value": str(v)})
            else:
                env_summary.append({"section": "root", "key": section, "value": str(data)})
    df_env = pd.DataFrame(env_summary)

    # 3. Database overview for all environments
    dbs = {
        "sandbox": DATA_DIR / "plaid.db",
        "development": DATA_DIR / "plaid_dev.db",
        "production": DATA_DIR / "plaid_prod.db",
    }
    db_overview = []
    for name, path in dbs.items():
        counts = get_db_counts(path)
        counts["environment"] = name
        db_overview.append(counts)
    df_db = pd.DataFrame(db_overview)

    # 4. Maintenance log extract
    maintenance_log = LOG_DIR / "maintenance.log"
    lines = get_recent_log_lines(maintenance_log, 50)
    if lines:
        clean_lines = [clean_excel_string(l.strip()) for l in lines]
        df_log = pd.DataFrame({"line": clean_lines})
    else:
        df_log = pd.DataFrame(columns=["line"])

    # 5. File metadata snapshot
    df_files = pd.DataFrame(get_file_info(PROJECT_ROOT))

    # 6. Sanitize all dataframes before writing
    for df in [df_env, df_db, df_log, df_files]:
        sanitize_dataframe(df)

    # 7. Export workbook
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    outfile = RESULTS_DIR / f"qa_snapshot_{ts}.xlsx"

    with pd.ExcelWriter(outfile, engine="openpyxl") as writer:
        df_env.to_excel(writer, index=False, sheet_name="environment_summary")
        df_db.to_excel(writer, index=False, sheet_name="database_overview")
        df_log.to_excel(writer, index=False, sheet_name="maintenance_log_extract")
        df_files.to_excel(writer, index=False, sheet_name="file_info")

    print(f"QA snapshot written to: {outfile}")

    # 8. Log event
    try:
        ts_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(maintenance_log, "a", encoding="utf-8") as f:
            f.write(f"[{ts_log}] QA SNAPSHOT — generated {outfile}\n")
    except Exception as e:
        print(f"Warning: could not log QA snapshot event: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
