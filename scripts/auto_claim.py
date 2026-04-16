#!/usr/bin/env python3
"""
Auto Claim Script - Automatically claim winnings
Usage: python scripts/auto_claim.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if __name__ == "__main__":
    print("Auto Claim - configure POLYMARKET_PID in .env to enable")
    print("This script requires valid Polymarket credentials")
