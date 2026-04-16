#!/usr/bin/env python3
"""
RTDS Wait Boundary - Wait for next window boundary
Usage: python scripts/rtds_wait_boundary.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if __name__ == "__main__":
    from datetime import datetime, timezone
    import time

    WINDOW = 300  # 5 minutes

    while True:
        now = int(datetime.now(timezone.utc).timestamp())
        window = now - (now % WINDOW)
        next_boundary = window + WINDOW
        remaining = next_boundary - now

        print(f"Next boundary in: {remaining:.0f}s", end='\r')
        time.sleep(1)

        if remaining < 1:
            print(f"\n*** BOUNDARY REACHED: {next_boundary} ***")
