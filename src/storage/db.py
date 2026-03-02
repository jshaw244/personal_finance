import json
import sqlite3
from datetime import datetime
from typing import Any

from src.common.paths import DB_FILE as DB_PATH, SCHEMA_FILE


def get_connection() -> sqlite3.Connection:
    """Return a sqlite3 connection to the active DB with FK enforcement, WAL mode, and 30s busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _db_connect() -> sqlite3.Connection:
    """
    Lightweight connection for use in `with _db_connect() as conn:` blocks.
    Sets WAL mode, 30s busy timeout, and FK enforcement on every connection.
    Does NOT set row_factory so tuple results are unchanged for existing callers.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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

    with _db_connect() as conn:
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
        #if seen_ids:
        #   placeholders = ",".join(["?"] * len(seen_ids))
        #   cur.execute(
        #       f"""
        #       DELETE FROM accounts
        #       WHERE item_id = ?
        #         AND account_id NOT IN ({placeholders})
        #       """,
        #       (item_id, *seen_ids),
        #   )

        conn.commit()


# -----------------------------
# Transaction helpers
# -----------------------------
def save_transactions(item_id: str, transactions: list[dict]) -> None:
    if not transactions:
        return

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    with _db_connect() as conn:
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
            cur.execute(
                    """
                    INSERT INTO transaction_meta (transaction_id, item_id, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(transaction_id) DO UPDATE SET
                    item_id=excluded.item_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                    """,
                    (tid, item_id, json.dumps(t, default=str), now),
            )

        conn.commit()

def get_transaction_cursor(item_id: str) -> str | None:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            "SELECT cursor FROM transaction_cursors WHERE item_id = ?",
            (item_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_transaction_cursor(item_id: str, cursor: str) -> None:
    with _db_connect() as conn:
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

    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        for r in removed:
            tid = r.get("transaction_id")
            if not tid:
                continue

            # delete from transactions
            cur.execute("DELETE FROM transactions WHERE transaction_id = ?", (tid,))

            # classifications will cascade because it FK's to transactions, but explicit is fine too:
            cur.execute("DELETE FROM transaction_classifications WHERE transaction_id = ?", (tid,))

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
    with _db_connect() as conn:
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
# Liabilities helper
# -----------------------------

def upsert_liabilities_raw(item_id: str, payload: dict) -> None:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO liabilities_raw (item_id, payload_json, captured_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE SET
              payload_json=excluded.payload_json,
              captured_at=CURRENT_TIMESTAMP
            """,
            (item_id, json.dumps(payload, default=str)),
        )
        conn.commit()

def upsert_recurring_raw(item_id: str, payload: dict) -> None:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO recurring_raw (item_id, payload_json, captured_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE SET
              payload_json=excluded.payload_json,
              captured_at=CURRENT_TIMESTAMP
            """,
            (item_id, json.dumps(payload, default=str)),
        )
        conn.commit()

# -----------------------------
# Transaction classification rules
# -----------------------------
import re

_EXCLUDE_PATTERNS = [
    r"\bpayment\b", r"\bpymt\b", r"\bautopay\b",
    r"\btransfer\b", r"\bach\b",
    r"\bcard\s*payment\b", r"\bcredit\s*card\s*payment\b",
]

def normalize_merchant(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()

    # common cleanup
    s = s.replace("*", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)     # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()    # collapse spaces
    return s

def get_transaction_basic(transaction_id: str) -> dict | None:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
              transaction_id,
              item_id,
              account_id,
              name,
              merchant_name
            FROM transactions
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
        return dict(row) if row else None


def get_top_merchants(days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
              COALESCE(c.merchant_normalized, LOWER(TRIM(COALESCE(t.merchant_name, t.name)))) AS merchant,
              SUM(t.amount) AS total_spend,
              COUNT(*) AS txn_count
            FROM transactions t
            LEFT JOIN transaction_classifications c
              ON c.transaction_id = t.transaction_id
            WHERE t.date >= date('now', ?)
              AND t.amount > 0
              AND COALESCE(c.exclude_from_spend, 0) = 0
            GROUP BY merchant
            ORDER BY total_spend DESC
            LIMIT ?
            """,
            (f"-{int(days)} day", int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

def get_credit_card_statement_summary(item_id: str | None = None) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        # account_id -> account name + institution
        acct_rows = conn.execute(
            """
            SELECT a.account_id, a.name AS account_name, i.institution_name, a.mask
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            """
        ).fetchall()
        acct_map = {r["account_id"]: dict(r) for r in acct_rows}

        if item_id:
            lr = conn.execute("SELECT item_id, payload_json, captured_at FROM liabilities_raw WHERE item_id = ?", (item_id,)).fetchall()
        else:
            lr = conn.execute("SELECT item_id, payload_json, captured_at FROM liabilities_raw").fetchall()

    out: list[dict[str, Any]] = []
    for r in lr:
        payload = json.loads(r["payload_json"])
        liab = (payload.get("liabilities") or {})
        credit_list = liab.get("credit") or []

        for cc in credit_list:
            aid = cc.get("account_id")
            meta = acct_map.get(aid, {})
            out.append({
                "item_id": r["item_id"],
                "captured_at": r["captured_at"],
                "institution_name": meta.get("institution_name"),
                "account_id": aid,
                "account_name": meta.get("account_name"),
                "mask": meta.get("mask"),
                "is_overdue": cc.get("is_overdue"),
                "last_statement_issue_date": cc.get("last_statement_issue_date"),
                "last_statement_balance": cc.get("last_statement_balance"),
                "minimum_payment_amount": cc.get("minimum_payment_amount"),
                "next_payment_due_date": cc.get("next_payment_due_date"),
                "last_payment_amount": cc.get("last_payment_amount"),
                "last_payment_date": cc.get("last_payment_date"),
            })
    return out


def get_recurring_raw(item_id: str | None = None) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        if item_id:
            rows = conn.execute(
                "SELECT item_id, captured_at, payload_json FROM recurring_raw WHERE item_id = ?",
                (item_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT item_id, captured_at, payload_json FROM recurring_raw ORDER BY captured_at DESC",
            ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            # keep payload as JSON object (nicer to inspect)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                d["payload"] = d.pop("payload_json")
            out.append(d)
        return out

def upsert_transaction_classification(
    transaction_id: str,
    exclude_from_spend: int = 0,
    exclude_reason: str | None = None,
    user_category: str | None = None,
    user_subcategory: str | None = None,
    merchant_normalized: str | None = None,
) -> None:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO transaction_classifications (
              transaction_id,
              exclude_from_spend,
              exclude_reason,
              user_category,
              user_subcategory,
              merchant_normalized,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(transaction_id) DO UPDATE SET
              exclude_from_spend=excluded.exclude_from_spend,
              exclude_reason=excluded.exclude_reason,
              user_category=COALESCE(excluded.user_category, transaction_classifications.user_category),
              user_subcategory=COALESCE(excluded.user_subcategory, transaction_classifications.user_subcategory),
              merchant_normalized=COALESCE(excluded.merchant_normalized, transaction_classifications.merchant_normalized),
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                transaction_id,
                int(bool(exclude_from_spend)),
                exclude_reason,
                user_category,
                user_subcategory,
                merchant_normalized,
            ),
        )
        conn.commit()


def upsert_transaction_classifications_batch(rows: list[dict]) -> int:
    """
    Write all classification rows in a single transaction.
    Each dict must have: transaction_id, exclude_from_spend, exclude_reason,
                         user_category, user_subcategory, merchant_normalized
    Returns the number of rows written.
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO transaction_classifications (
          transaction_id, exclude_from_spend, exclude_reason,
          user_category, user_subcategory, merchant_normalized, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(transaction_id) DO UPDATE SET
          exclude_from_spend=excluded.exclude_from_spend,
          exclude_reason=excluded.exclude_reason,
          user_category=COALESCE(excluded.user_category, transaction_classifications.user_category),
          user_subcategory=COALESCE(excluded.user_subcategory, transaction_classifications.user_subcategory),
          merchant_normalized=COALESCE(excluded.merchant_normalized, transaction_classifications.merchant_normalized),
          updated_at=CURRENT_TIMESTAMP
    """
    params = [
        (
            r["transaction_id"],
            int(bool(r.get("exclude_from_spend", 0))),
            r.get("exclude_reason"),
            r.get("user_category"),
            r.get("user_subcategory"),
            r.get("merchant_normalized"),
        )
        for r in rows
    ]
    with _db_connect() as conn:
        conn.executemany(sql, params)
        conn.commit()
    return len(rows)


def get_transactions_for_classification(days: int = 365, item_id: str | None = None) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        where = ["t.date >= date('now', ?)"]
        params: list[Any] = [f"-{int(days)} day"]

        if item_id:
            where.append("t.item_id = ?")
            params.append(item_id)

        sql = f"""
            SELECT
              t.transaction_id,
              t.item_id,
              t.account_id,
              t.date,
              t.name,
              t.merchant_name,
              t.amount,
              tm.payload_json AS meta_json
            FROM transactions t
            LEFT JOIN transaction_meta tm
              ON tm.transaction_id = t.transaction_id
            WHERE {" AND ".join(where)}
        """
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def insert_classification_rule(
    match_field: str,
    match_op: str,
    match_value: str,
    account_id: str | None,
    exclude_from_spend: int = 0,
    exclude_reason: str | None = None,
    user_category: str | None = None,
    user_subcategory: str | None = None,
    merchant_normalized: str | None = None,
    priority: int = 100,
) -> int:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            """
            INSERT INTO classification_rules (
              enabled, priority,
              match_field, match_op, match_value,
              account_id,
              exclude_from_spend, exclude_reason,
              user_category, user_subcategory, merchant_normalized,
              created_at, updated_at
            )
            VALUES (
              1, ?,
              ?, ?, ?,
              ?,
              ?, ?,
              ?, ?, ?,
              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (
                int(priority),
                match_field, match_op, match_value,
                account_id,
                int(bool(exclude_from_spend)), exclude_reason,
                user_category, user_subcategory, merchant_normalized,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def classification_exists(transaction_id: str) -> bool:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        row = conn.execute(
            "SELECT 1 FROM transaction_classifications WHERE transaction_id = ? LIMIT 1",
            (transaction_id,),
        ).fetchone()
        return row is not None


def _rule_matches(rule: dict, name_norm: str, merchant_norm: str) -> bool:
    field = rule["match_field"]
    op = rule["match_op"]
    val = rule["match_value"] or ""

    candidates = []
    if field == "name":
        candidates = [name_norm]
    elif field == "merchant_name":
        candidates = [merchant_norm]
    else:  # 'either'
        candidates = [merchant_norm, name_norm]

    for c in candidates:
        if not c:
            continue
        if op == "equals" and c == val:
            return True
        if op == "contains" and val and val in c:
            return True
    return False


def apply_best_rule_to_transaction(transaction_id: str) -> dict | None:
    """
    Apply the best matching enabled rule to a transaction,
    but DO NOT override if the user has already classified it.
    Returns the rule applied (dict) or None.
    """
    if classification_exists(transaction_id):
        return None

    tx = get_transaction_basic(transaction_id)
    if not tx:
        return None

    name_norm = normalize_merchant(tx.get("name"))
    merchant_norm = normalize_merchant(tx.get("merchant_name"))

    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        rules = conn.execute(
            """
            SELECT *
            FROM classification_rules
            WHERE enabled = 1
              AND (account_id IS NULL OR account_id = ?)
            ORDER BY priority ASC, rule_id ASC
            """,
            (tx["account_id"],),
        ).fetchall()

    for r in rules:
        rule = dict(r)
        if _rule_matches(rule, name_norm, merchant_norm):
            upsert_transaction_classification(
                transaction_id=transaction_id,
                exclude_from_spend=rule.get("exclude_from_spend", 0),
                exclude_reason=rule.get("exclude_reason"),
                user_category=rule.get("user_category"),
                user_subcategory=rule.get("user_subcategory"),
                merchant_normalized=rule.get("merchant_normalized") or rule.get("match_value"),
            )
            return rule

    return None

def apply_classification_rules(*args, **kwargs):
    """
    Placeholder: classification is optional.
    Keep this function to satisfy imports even if rules are not implemented yet.
    """
    return 0

def apply_rules_bulk(days: int = 365, item_id: str | None = None) -> dict:
    """
    Apply rules to recent transactions; does not override existing classifications.
    """
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        where = ["date >= date('now', ?)"]
        params: list[Any] = [f"-{int(days)} day"]
        if item_id:
            where.append("item_id = ?")
            params.append(item_id)

        rows = conn.execute(
            f"""
            SELECT transaction_id
            FROM transactions
            WHERE {" AND ".join(where)}
            ORDER BY date DESC
            """,
            params,
        ).fetchall()

    applied = 0
    for r in rows:
        rule = apply_best_rule_to_transaction(r["transaction_id"])
        if rule:
            applied += 1

    return {"scanned": len(rows), "applied": applied}

def _resolve_53_checking_account_id() -> str | None:
    aid = (os.getenv("FIFTH_THIRD_CHECKING_ACCOUNT_ID") or "").strip()
    if aid:
        return aid

    # best-effort fallback (works if you have accounts/items populated)
    if not DB_PATH.exists():
        return None
    try:
        conn = _db_connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT a.account_id
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            WHERE LOWER(i.institution_name) LIKE '%fifth%'
              AND LOWER(a.subtype) = 'checking'
            ORDER BY a.updated_at DESC
            LIMIT 1
        """).fetchone()
        conn.close()
        return row["account_id"] if row else None
    except Exception:
        return None



# -----------------------------
# Logging helper
# -----------------------------
def log_event_db(source: str, level: str, message: str) -> None:
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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

def get_spend_canonical(
    days: int = 30,
    item_id: str | None = None,
    account_id: str | None = None,
    include_excluded: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row

        where = ["t.date >= date('now', ?)", "t.amount > 0"]
        params: list[Any] = [f"-{int(days)} day"]

        if item_id:
            where.append("t.item_id = ?")
            params.append(item_id)

        if account_id:
            where.append("t.account_id = ?")
            params.append(account_id)

        if not include_excluded:
            where.append("COALESCE(c.exclude_from_spend, 0) = 0")

        params.extend([int(limit), int(offset)])

        sql = f"""
            SELECT
              t.*,
              i.institution_id,
              i.institution_name,
              a.name AS account_name,
              a.type AS account_type,
              a.subtype AS account_subtype,
              COALESCE(c.exclude_from_spend, 0) AS exclude_from_spend,
              c.exclude_reason,
              c.merchant_normalized,
              c.user_category,
              c.user_subcategory
            FROM transactions t
            JOIN items i ON i.item_id = t.item_id
            JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN transaction_classifications c ON c.transaction_id = t.transaction_id
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
    with _db_connect() as conn:
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
    with _db_connect() as conn:
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

