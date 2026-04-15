"""
监控 Polymarket WS 实时推送行为：
持续监听直到窗口边界 tick 出现（或超时），记录期间 buffer 是否增长。
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
    cur_window = (now_s // 300 + 1) * 300
    print(f"Wall clock:       {ts_str(int(time.time()*1000))}")
    print(f"Window boundary:  {ts_str(cur_window*1000)}  (unix={cur_window})")
    print(f"Will wait up to 120s for boundary tick...\n")

    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"  [WS] {m}", flush=True))
    feed.start()

    ok = feed.wait_for_ticks(1, timeout_s=15.0)
    if not ok:
        print("FAIL")
        feed.stop()
        return

    n0, mn0, mx0, lp0 = feed.buffer_stats()
    print(f"Snapshot received: n={n0}  range=[{ts_str(mn0)}, {ts_str(mx0)}]  latest={lp0}\n")

    deadline = time.time() + 120
    prev_n = n0
    last_report_wall = time.time()
    report_interval = 10.0  # 每 10s 详细报告

    while time.time() < deadline:
        n, mn, mx, lp = feed.buffer_stats()
        now_wall = time.time()

        # 每 2s 打印一行简报
        if now_wall - last_report_wall >= 2.0:
            delta = n - prev_n
            prev_n = n
            ge_lag = "?"
            if mx and mx >= cur_window * 1000:
                ge_lag = f"YES ts={ts_str(mx)}"
            elif mn:
                ge_lag = f"no (max={ts_str(mx)}, {cur_window*1000 - mx}ms before boundary)"
            print(
                f"  [{ts_str(int(time.time()*1000))}] n={n:3d}(+{delta:2d}) "
                f"buf_range=[{ts_str(mn)}, {ts_str(mx)}] >=boundary:{ge_lag}"
            )
            last_report_wall = now_wall

        # 检查是否有 >= boundary 的 tick
        found = False
        with feed._lock:
            for ts_ms, v in feed._ticks:
                if ts_ms >= cur_window * 1000:
                    lag_ms = ts_ms - cur_window * 1000
                    print(f"\n>>> BOUNDARY TICK FOUND!")
                    print(f"    price={v}")
                    print(f"    payload_ts={ts_str(ts_ms)}  unix={ts_ms//1000}")
                    print(f"    lag={lag_ms}ms ({lag_ms/1000:.1f}s after boundary)")
                    print(f"    Buffer size at detection: {n}")
                    found = True
                    break

        if found:
            break
        time.sleep(0.5)

    if not found:
        n, mn, mx, _ = feed.buffer_stats()
        print(f"\nTimeout. Final buffer: n={n} range=[{ts_str(mn)}, {ts_str(mx)}]")
        print(f"No tick with ts >= {cur_window*1000} ({ts_str(cur_window*1000)})")

    feed.stop()


if __name__ == "__main__":
    main()
