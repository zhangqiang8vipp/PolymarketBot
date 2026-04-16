#!/usr/bin/env python3
"""
Compare Runs - Compare performance across multiple trading runs
Usage: python scripts/compare_runs.py
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.backtest import fetch_klines_1m, fetch_klines_range_hours
import argparse
import json
from typing import List, Dict, Any
from dataclasses import dataclass
import math

def main():
    parser = argparse.ArgumentParser(description="Compare trading run configurations")
    parser.add_argument("--hours", type=int, default=48, help="Hours of data to backtest")
    parser.add_argument("--output", type=str, default="results_fixed.xlsx", help="Output file")
    parser.add_argument("--initial", type=float, default=100.0, help="Initial bankroll")
    parser.add_argument("--min-bet", type=float, default=1.0, help="Minimum bet")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    print("=" * 60)
    print("Compare Trading Runs")
    print("=" * 60)
    print(f"\nFetching {args.hours} hours of K-line data...")

    # This is a placeholder - actual implementation would run backtests
    print("Feature: Run multiple backtest configurations and compare results")
    print(f"Output: {args.output}")

if __name__ == "__main__":
    main()
