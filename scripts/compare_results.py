#!/usr/bin/env python3
"""
Compare Results - Analyze trading results from multiple runs
Usage: python scripts/compare_results.py
"""

import sys
import os
import json
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def main():
    data_dir = project_root / "data"

    print("=" * 60)
    print("Polymarket Bot - Results Comparison")
    print("=" * 60)

    # Check for available result files
    xlsx_files = list(data_dir.glob("*.xlsx"))
    if xlsx_files:
        print(f"\nFound {len(xlsx_files)} result files:")
        for f in xlsx_files:
            print(f"  - {f.name}")
    else:
        print("\nNo result files found in data/")

    # Check for trading journal
    journal = data_dir / "trading_journal.csv"
    if journal.exists():
        try:
            import pandas as pd
            df = pd.read_csv(journal)
            print(f"\nTrading Journal: {len(df)} trades")
            if 'pnl' in df.columns:
                total_pnl = df['pnl'].sum()
                print(f"  Total PnL: ${total_pnl:.2f}")
        except Exception as e:
            print(f"Error reading journal: {e}")
    else:
        print("\nNo trading journal found in data/")

    # Check for polymarket outcomes
    outcomes_file = project_root / "polymarket_outcomes.json"
    if outcomes_file.exists():
        with open(outcomes_file) as f:
            outcomes = json.load(f)
        print(f"\nPolymarket Outcomes: {len(outcomes)} windows")

if __name__ == "__main__":
    main()
