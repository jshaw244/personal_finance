import json
import sqlite3
from datetime import datetime
from typing import Any

from src.common.paths import DB_FILE as DB_PATH, SCHEMA_FILE


def get_connection() -> sqlite3.Connection:
    """Return a sqlite3 connection to the active DB with FK + Row dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _execute_schema_sql(conn: sqlite3.Connection) -> None:
    """Apply canonical schema.sql (production-safe: CREATE IF NOT EXISTS only)."""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)


# -----------------------------
# Core DB Setup
# -----------------------------
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        _execute_schema_sql(conn)
        conn.commit()


# -----------------------------
# Item helpers
# -----------------------------
def save_item(item_id: str, access_token: str, institution: str | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO items (item_id, access_token, institution, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE
            SET access_token=excluded.access_token,
                institution=excluded.institution,
                updated_at=CURRENT_TIMESTAMP
            """,
            (item_id, access_token, institution),
        )
        conn.commit()


def get_all_items() -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            "SELECT item_id, access_token, institution FROM items ORDER BY created_at;"
        )
        rows = cur.fetchall()
        return [{"item_id": r[0], "access_token": r[1], "institution": r[2]} for r in rows]


# -----------------------------
# Raw payload helper
# -----------------------------
def insert_plaid_raw(
    env_target: str,
    endpoint: str,
    item_id: str | None,
    request_id: str | None,
    payload: dict,
) -> None:
    """Store redacted Plaid response payload JSON in plaid_raw."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO plaid_raw (env_target, endpoint, item_id, request_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (env_target, endpoint, item_id, request_id, json.dumps(payload)),
        )
        conn.commit()


# -----------------------------
# Account helpers (Option 2)
# -----------------------------
def save_accounts(item_id: str, accounts: list[dict]) -> None:
    """
    Upsert Plaid accounts into accounts table where:
      - accounts.account_id is PRIMARY KEY
      - item_id links back to items(item_id)
    Also prunes accounts for this item not present in latest response.
    """
    if not accounts:
        return

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        seen_ids: list[str] = []

        for a in accounts:
            acct_id = a.get("account_id")
            if not acct_id:
                continue
            seen_ids.append(acct_id)

            balances = a.get("balances") or {}
            cur_bal = balances.get("current")
            avail_bal = balances.get("available")
            lim_bal = balances.get("limit")
            iso_ccy = balances.get("iso_currency_code")
            unoff_ccy = balances.get("unofficial_currency_code")

            cur.execute(
                """
                INSERT INTO accounts (
                    account_id, item_id,
                    name, official_name, mask, type, subtype,
                    current, available, balance_limit,
                    iso_currency_code, unofficial_currency_code,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM accounts WHERE account_id=?), ?),
                    ?
                )
                ON CONFLICT(account_id) DO UPDATE SET
                    item_id=excluded.item_id,
                    name=excluded.name,
                    official_name=excluded.official_name,
                    mask=excluded.mask,
                    type=excluded.type,
                    subtype=excluded.subtype,
                    current=excluded.current,
                    available=excluded.available,
                    balance_limit=excluded.balance_limit,
                    iso_currency_code=excluded.iso_currency_code,
                    unofficial_currency_code=excluded.unofficial_currency_code,
                    updated_at=excluded.updated_at
                """,
                (
                    acct_id,
                    item_id,
                    a.get("name"),
                    a.get("official_name"),
                    a.get("mask"),
                    a.get("type"),
                    a.get("subtype"),
                    cur_bal,
                    avail_bal,
                    lim_bal,
                    iso_ccy,
                    unoff_ccy,
                    acct_id,  # created_at lookup
                    now,      # fallback created_at
                    now,      # updated_at
                ),
            )

        # Prune accounts for this item not in latest response
        placeholders = ",".join(["?"] * len(seen_ids))
        cur.execute(
            f"""
            DELETE FROM accounts
            WHERE item_id = ?
              AND account_id NOT IN ({placeholders})
            """,
            (item_id, *seen_ids),
        )

        conn.commit()


# -----------------------------
# Transaction helpers
# -----------------------------
def save_transactions(item_id: str, transactions: list[dict]) -> None:
    if not transactions:
        return

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        for t in transactions:
            cur.execute(
                """
                INSERT INTO transactions (
                    transaction_id, item_id, account_id,
                    date, authorized_date,
                    name, merchant_name, amount,
                    iso_currency_code, unofficial_currency_code,
                    pending, pending_transaction_id,
                    category, payment_channel,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM transactions WHERE transaction_id=?), ?),
                    ?
                )
                ON CONFLICT(transaction_id) DO UPDATE SET
                    item_id=excluded.item_id,
                    account_id=excluded.account_id,
                    date=excluded.date,
                    authorized_date=excluded.authorized_date,
                    name=excluded.name,
                    merchant_name=excluded.merchant_name,
                    amount=excluded.amount,
                    iso_currency_code=excluded.iso_currency_code,
                    unofficial_currency_code=excluded.unofficial_currency_code,
                    pending=excluded.pending,
                    pending_transaction_id=excluded.pending_transaction_id,
                    category=excluded.category,
                    payment_channel=excluded.payment_channel,
                    updated_at=excluded.updated_at
                """,
                (
                    t.get("transaction_id"),
                    item_id,
                    t.get("account_id"),
                    t.get("date"),
                    t.get("authorized_date"),
                    t.get("name"),
                    t.get("merchant_name"),
                    t.get("amount"),
                    t.get("iso_currency_code"),
                    t.get("unofficial_currency_code"),
                    int(bool(t.get("pending"))),
                    t.get("pending_transaction_id"),
                    json.dumps(t.get("category")) if t.get("category") is not None else None,
                    t.get("payment_channel"),
                    t.get("transaction_id"),  # created_at lookup
                    now,
                    now,
                ),
            )

        conn.commit()


# -----------------------------
# Logging helper
# -----------------------------
def log_event_db(source: str, level: str, message: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO log_events (timestamp, source, level, message)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.utcnow().isoformat(sep=" ", timespec="seconds"), source, level, message),
        )
        conn.commit()
