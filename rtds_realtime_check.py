"""
诊断 Polymarket WS 实时推送行为：
1. 连接后是否持续有 tick 推送？
2. buffer 是否持续增长？
3. 当前窗口边界 tick 何时到达？
"""
from __future__ import annotations
import threading
import time
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from chainlink_rtds import ChainlinkBtcUsdRtds


def ts_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def main():
    now_s = int(time.time())
    cur_window = (now_s // 300 + 1) * 300  # next 5-min boundary
    print(f"Current wall clock UTC: {ts_str(int(time.time()*1000))}")
    print(f"Next  window boundary:  {ts_str(cur_window * 1000)}  (ts={cur_window})")
    print()

    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"  [WS] {m}", flush=True))
    feed.start()

    print("Waiting for first tick...")
    ok = feed.wait_for_ticks(1, timeout_s=15.0)
    if not ok:
        print("FAIL: no tick in 15s")
        feed.stop()
        return

    prev_n = 0
    print("\nMonitoring buffer growth (1s interval):")
    for i in range(30):
        n, mn, mx, lp = feed.buffer_stats()
        now_str = ts_str(int(time.time() * 1000))
        delta = n - prev_n
        prev_n = n
        print(f"  [{now_str}] buffer_n={n:3d} (+{delta})  min={ts_str(mn)} max={ts_str(mx)}  latest_price={lp}")
        if n != prev_n or i == 0:
            pass
        time.sleep(1.0)

    # Find ticks near window boundary
    print(f"\nSearching for boundary tick (ts >= {cur_window})...")
    ge_tick = None
    with feed._lock:
        for ts_ms, v in feed._ticks:
            if ts_ms >= cur_window * 1000:
                if ge_tick is None or ts_ms < ge_tick[0]:
                    ge_tick = (ts_ms, v)

    if ge_tick:
        ts_ms, v = ge_tick
        lag = ts_ms - cur_window * 1000
        print(f">>> FOUND: price={v}  ts={ts_str(ts_ms)}  lag={lag}ms ({lag/1000:.1f}s)")
    else:
        mn_val = feed.buffer_stats()[1]
        print(f">>> NOT FOUND. Buffer min ts={ts_str(mn_val) if mn_val else 'N/A'}")
        print(f"    (buffer spans {30*60}s before window boundary)")

    print("\nAll ticks in buffer:")
    with feed._lock:
        for ts_ms, v in sorted(feed._ticks, key=lambda x: x[0]):
            lag = ts_ms - cur_window * 1000
            marker = f" <-- boundary-差距={lag}ms" if abs(lag) < 120000 else ""
            print(f"  {ts_str(ts_ms)}  price={v}{marker}")

    feed.stop()


if __name__ == "__main__":
    main()
