#!/usr/bin/env python3
"""
RTDS Debug - Debug RTDS connection
Usage: python scripts/rtds_debug.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if __name__ == "__main__":
    os.environ['RTDS_DEBUG'] = '1'

    from core.chainlink_rtds import ChainlinkBtcUsdRtds

    print("Starting RTDS with debug logging...")
    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"[RTDS] {m}"))
    feed.start()

    try:
        import time
        while True:
            time.sleep(10)
            stats = feed.buffer_stats()
            print(f"[DEBUG] Buffer stats: {stats}")
    except KeyboardInterrupt:
        feed.stop()
