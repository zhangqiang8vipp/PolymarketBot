#!/usr/bin/env python3
"""
RTDS Window Now - Check current RTDS window
Usage: python scripts/rtds_window_now.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if __name__ == "__main__":
    from datetime import datetime, timezone

    WINDOW = 300  # 5 minutes

    now = int(datetime.now(timezone.utc).timestamp())
    window = now - (now % WINDOW)
    close = window + WINDOW
    remaining = close - now

    print(f"Current Window: {window}")
    print(f"Window Close: {close}")
    print(f"Time Remaining: {remaining:.0f}s")
