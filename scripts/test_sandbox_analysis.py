#!/usr/bin/env python3
"""
scripts/test_sandbox_analysis.py
Purpose:
    Trigger the full analysis pipeline for the sandbox database.

Usage:
    python scripts/test_sandbox_analysis.py
    python scripts/test_sandbox_analysis.py --days 60
    python scripts/test_sandbox_analysis.py --start 2025-01-01 --end 2025-03-01
"""

import argparse
from src.analysis.analysis import run_full_analysis

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run sandbox analysis (default = last 30 days).")
    parser.add_argument("--days", type=int, default=30, help="Number of trailing days (default=30)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    run_full_analysis(
        target="sandbox",
        days=args.days,
        start_date=args.start,
        end_date=args.end,
    )
