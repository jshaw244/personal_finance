import argparse
import sqlite3
from pathlib import Path


def init_db(db_path: Path, schema_path: Path) -> None:
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql not found: {schema_path}")

    schema = schema_path.read_text(encoding="utf-8")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()

    print(f"Initialized: {db_path}")


def main() -> int:
    p = argparse.ArgumentParser(description="Initialize a SQLite DB from schema.sql")
    p.add_argument("--db", required=True, help="Path to sqlite db file")
    p.add_argument("--schema", required=True, help="Path to schema.sql")
    args = p.parse_args()

    init_db(Path(args.db), Path(args.schema))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
