from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Rule:
    """
    Simple rule for classifying transactions.

    - field: transaction key to inspect (e.g., 'name', 'merchant_name', 'account_id')
    - contains_any: list of substrings; if any appear (case-insensitive), rule matches
    - category: category to set when matched (e.g., 'Transfer', 'Groceries')
    - subcategory: optional finer label
    - exclude_if_contains_any: if any of these substrings appear, do NOT match
    - direction: 'outflow'|'inflow'|None (optional)
    """
    field: str
    contains_any: Tuple[str, ...]
    category: str
    subcategory: Optional[str] = None
    exclude_if_contains_any: Tuple[str, ...] = ()
    direction: Optional[str] = None


def _norm(s: Any) -> str:
    return (str(s) if s is not None else "").strip().lower()


def _get_direction(txn: Dict[str, Any]) -> str:
    """
    Normalizes direction. Many Plaid payloads encode outflow as positive amount.
    We treat:
      amount > 0  => outflow (expense/payment)
      amount < 0  => inflow  (refund/income)
    Adjust if your app uses the opposite convention.
    """
    amt = txn.get("amount")
    try:
        amt_f = float(amt)
    except Exception:
        amt_f = 0.0
    return "outflow" if amt_f > 0 else ("inflow" if amt_f < 0 else "neutral")


def default_rules() -> List[Rule]:
    """
    Starter set. Customize to your data.
    The big win: mark card payments/transfers so they don't inflate expenses.
    """
    return [
        # Credit card payments / transfers (exclude from "true expenses")
        Rule(field="name", contains_any=("payment thank you", "autopay", "online payment"), category="Transfer", subcategory="Credit Card Payment"),
        Rule(field="merchant_name", contains_any=("payment", "autopay"), category="Transfer", subcategory="Credit Card Payment"),

        # Common transfer keywords
        Rule(field="name", contains_any=("transfer", "ach transfer", "online transfer", "external transfer"), category="Transfer", subcategory="Transfer"),
        Rule(field="name", contains_any=("zelle", "venmo", "cash app", "paypal"), category="Transfer", subcategory="P2P"),

        # Fees
        Rule(field="name", contains_any=("fee", "service charge", "monthly maintenance"), category="Fees", subcategory="Bank Fee"),

        # Groceries examples
        Rule(field="merchant_name", contains_any=("kroger", "aldi", "whole foods", "trader joe", "jewel", "meijer"), category="Groceries"),
        Rule(field="name", contains_any=("kroger", "aldi", "whole foods", "trader joe", "jewel", "meijer"), category="Groceries"),
    ]


def apply_classification_rules(
    txn: Dict[str, Any],
    rules: Optional[Iterable[Rule]] = None,
    *,
    set_fields: Tuple[str, str, str] = ("category", "subcategory", "classification_rule"),
) -> Dict[str, Any]:
    """
    Apply rules to a single transaction dict and return an updated copy.

    Writes:
      - category
      - subcategory
      - classification_rule (a short identifier of what matched)

    If nothing matches, leaves existing values intact.
    """
    if rules is None:
        rules = default_rules()

    category_key, subcategory_key, rule_key = set_fields

    direction = _get_direction(txn)

    # Build a combined searchable string for a field, but we still follow per-field rules.
    updated = dict(txn)

    for r in rules:
        if r.direction is not None and r.direction != direction:
            continue

        hay = _norm(txn.get(r.field))
        if not hay:
            continue

        # Exclusions
        if r.exclude_if_contains_any and any(ex in hay for ex in map(_norm, r.exclude_if_contains_any)):
            continue

        # Match
        if any(needle in hay for needle in map(_norm, r.contains_any)):
            # Only set if not already set, or you can choose to always overwrite.
            updated[category_key] = updated.get(category_key) or r.category
            if r.subcategory is not None:
                updated[subcategory_key] = updated.get(subcategory_key) or r.subcategory
            updated[rule_key] = f"{r.field}:{r.contains_any[0]}"
            break

    return updated
