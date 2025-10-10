# scripts/explore_transactions.py
#!/usr/bin/env python3
"""
Explore transactions table contents and print quick summary stats.

Usage:
    python scripts/explore_transactions.py
"""

import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = Path("data/plaid.db")

def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)

    # Load all transactions into a DataFrame
    df = pd.read_sql_query("SELECT * FROM transactions", con)
    con.close()

    if df.empty:
        print("No transactions found in the database.")
        return

    print("=" * 80)
    print(f"Database: {DB_PATH}")
    print(f"Total transactions: {len(df)}")
    print("=" * 80)

    # Date range and column summary
    if "date" in df.columns:
        print(f"Date range: {df['date'].min()}  →  {df['date'].max()}")
    print()

    # Basic stats
    print("Amount statistics (in USD):")
    print(df["amount"].describe().to_string())
    print()

    # Top merchants
    if "merchant_name" in df.columns:
        top_merchants = df["merchant_name"].value_counts().head(10)
        print("Top 10 merchants:")
        print(top_merchants.to_string())
        print()

    # Category preview
    if "category" in df.columns:
        print("Sample categories:")
        sample_cats = df["category"].dropna().head(10).to_list()
        for cat in sample_cats:
            print("  -", cat)
        print()

    # Income vs Expense summary
    if "amount" in df.columns:
        income = df[df["amount"] > 0]["amount"].sum()
        expenses = df[df["amount"] < 0]["amount"].sum()
        net = income + expenses
        print("Income / Expense Summary:")
        print(f"  Total income : ${income:,.2f}")
        print(f"  Total expense: ${expenses:,.2f}")
        print(f"  Net balance  : ${net:,.2f}")
        print()

    print("=" * 80)
    print("Done.")
    print("=" * 80)

if __name__ == "__main__":
    main()
