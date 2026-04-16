#!/usr/bin/env python3
"""
RTDS Realtime Check - Monitor RTDS connection and data
Usage: python scripts/rtds_realtime_check.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if __name__ == "__main__":
    from core.chainlink_rtds import ChainlinkBtcUsdRtds

    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"[RTDS] {m}"))
    feed.start()

    try:
        while True:
            import time
            time.sleep(5)
            stats = feed.buffer_stats()
            if stats[0] > 0:
                print(f"[RTDS] Buffer: {stats[0]} ticks, Latest: ${stats[3]:.2f}")
    except KeyboardInterrupt:
        feed.stop()
        print("\n[RTDS] Stopped")
