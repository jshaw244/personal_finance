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
            (env_target, endpoint, item_id, request_id, json.dumps(payload, default=str)),
        )
        conn.commit()

# -----------------------------
# Item helpers
# -----------------------------
def save_item(
    item_id: str,
    access_token: str,
    institution_id: str | None = None,
    institution_name: str | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO items (item_id, access_token, institution_id, institution_name, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE
            SET access_token=excluded.access_token,
                institution_id=excluded.institution_id,
                institution_name=excluded.institution_name,
                updated_at=CURRENT_TIMESTAMP
            """,
            (item_id, access_token, institution_id, institution_name),
        )
        conn.commit()


def get_all_items() -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            """
            SELECT item_id, access_token, institution_id, institution_name
            FROM items
            ORDER BY created_at;
            """
        )
        rows = cur.fetchall()
        return [
            {
                "item_id": r[0],
                "access_token": r[1],
                "institution_id": r[2],
                "institution_name": r[3],
            }
            for r in rows
        ]


def count_items_by_institution(institution_id: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            "SELECT COUNT(*) FROM items WHERE institution_id = ?",
            (institution_id,),
        )
        return int(cur.fetchone()[0])



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
        if seen_ids:
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
            tid = t.get("transaction_id")
            acct = t.get("account_id")
            if not tid or not acct:
                # Optional: log_event_db("db", "WARNING", f"Skipping txn missing ids: tid={tid} acct={acct}")
                continue
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
                    tid,
                    item_id,
                    acct,
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
                    tid,  # created_at lookup
                    now,
                    now,
                ),
            )

        conn.commit()

def get_transaction_cursor(item_id: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            "SELECT cursor FROM transaction_cursors WHERE item_id = ?",
            (item_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_transaction_cursor(item_id: str, cursor: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO transaction_cursors (item_id, cursor, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE SET
              cursor=excluded.cursor,
              updated_at=CURRENT_TIMESTAMP
            """,
            (item_id, cursor),
        )
        conn.commit()


def apply_removed_transactions(item_id: str, removed: list[dict]) -> None:
    """
    removed is the list from /transactions/sync response, like:
      [{"transaction_id": "..."}]
    """
    if not removed:
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        for r in removed:
            tid = r.get("transaction_id")
            if not tid:
                continue

            # delete from transactions
            cur.execute("DELETE FROM transactions WHERE transaction_id = ?", (tid,))

            # OPTIONAL: if you have transactions_removed table
            cur.execute(
                 """
                 INSERT INTO transactions_removed (transaction_id, item_id, removed_at)
                 VALUES (?, ?, CURRENT_TIMESTAMP)
                 ON CONFLICT(transaction_id) DO UPDATE SET
                   item_id=excluded.item_id,
                   removed_at=CURRENT_TIMESTAMP
                 """,
                 (tid, item_id),
             )

        conn.commit()


def count_transactions_canonical(
    days: int = 30,
    item_id: str | None = None,
    account_id: str | None = None,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")

        where = ["t.date >= date('now', ?)"]
        params: list[Any] = [f"-{int(days)} day"]

        if item_id:
            where.append("t.item_id = ?")
            params.append(item_id)

        if account_id:
            where.append("t.account_id = ?")
            params.append(account_id)

        sql = f"""
            SELECT COUNT(*)
            FROM transactions t
            WHERE {" AND ".join(where)}
        """
        return int(conn.execute(sql, params).fetchone()[0])


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

# -----------------------------
# canonical “read” helpers
# -----------------------------
def get_accounts_canonical(item_id: str | None = None) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        if item_id:
            rows = conn.execute(
                """
                SELECT
                    a.*,
                    i.institution_id,
                    i.institution_name
                FROM accounts a
                JOIN items i ON i.item_id = a.item_id
                WHERE a.item_id = ?
                ORDER BY i.institution_name, a.type, a.subtype, a.name
                """,
                (item_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    a.*,
                    i.institution_id,
                    i.institution_name
                FROM accounts a
                JOIN items i ON i.item_id = a.item_id
                ORDER BY i.institution_name, a.type, a.subtype, a.name
                """
            ).fetchall()

        return [dict(r) for r in rows]


def get_transactions_canonical(
    days: int = 30,
    item_id: str | None = None,
    account_id: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        where = ["t.date >= date('now', ?)"]
        params: list[Any] = [f"-{int(days)} day"]

        if item_id:
            where.append("t.item_id = ?")
            params.append(item_id)

        if account_id:
            where.append("t.account_id = ?")
            params.append(account_id)

        params.extend([int(limit), int(offset)])

        sql = f"""
            SELECT
                t.*,
                i.institution_id,
                i.institution_name,
                a.name AS account_name,
                a.type AS account_type,
                a.subtype AS account_subtype
            FROM transactions t
            JOIN items i ON i.item_id = t.item_id
            JOIN accounts a ON a.account_id = t.account_id
            WHERE {" AND ".join(where)}
            ORDER BY t.date DESC
            LIMIT ? OFFSET ?
        """

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

# -----------------------------
# Cascade delete local data for item
# -----------------------------
def delete_item_local(item_id: str) -> None:
    """
    Delete all local rows related to a Plaid item_id.
    Order matters if FK constraints exist.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        # Delete child rows first
        cur.execute("DELETE FROM transactions_removed WHERE item_id = ?", (item_id,))
        cur.execute("DELETE FROM transactions WHERE item_id = ?", (item_id,))
        cur.execute("DELETE FROM accounts WHERE item_id = ?", (item_id,))
        cur.execute("DELETE FROM transaction_cursors WHERE item_id = ?", (item_id,))

        # Finally delete the item itself
        cur.execute("DELETE FROM items WHERE item_id = ?", (item_id,))

        conn.commit()


def get_item_by_id(item_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT item_id, access_token, institution_id, institution_name
            FROM items
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
        return dict(row) if row else None

