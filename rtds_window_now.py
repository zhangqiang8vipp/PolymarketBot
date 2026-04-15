"""
RTDS 实时诊断：每秒输出当前窗口的边界 tick 价格。
用于对照 Polymarket 网页上的 Price to Beat。
"""
from __future__ import annotations
import threading
import time
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from chainlink_rtds import ChainlinkBtcUsdRtds


def unix_ms_to_str(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S.%f")[:-3]


def format_window(w: int) -> str:
    return f"{unix_ms_to_str(w * 1000)}"


def current_window() -> int:
    now_s = int(time.time())
    return (now_s // 300 + 1) * 300


def main():
    print("=" * 65)
    print("RTDS 实时窗口价格对照")
    print("=" * 65)

    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"  [状态] {m}", flush=True))
    feed.start()

    ok = feed.wait_for_ticks(1, timeout_s=15.0)
    if not ok:
        print("15s 内未收到 tick，检查网络")
        feed.stop()
        return

    target_window = current_window()
    print(f"\n当前窗口: {target_window} ({format_window(target_window)} UTC)")
    print(f"等待该窗口的 Chainlink 开盘 tick（最多 120s）...\n")

    deadline = time.time() + 120

    while time.time() < deadline:
        n, mn, mx, lp = feed.buffer_stats()
        now_s = int(time.time())

        # 找 ≥ target_window 的第一条
        ge_tick = None
        with feed._lock:
            for ts_ms, v in feed._ticks:
                if ts_ms >= target_window * 1000:
                    if ge_tick is None or ts_ms < ge_tick[0]:
                        ge_tick = (ts_ms, v)

        # 找 < target_window 且 ≤ 30s 前的
        before_tick = None
        with feed._lock:
            for ts_ms, v in feed._ticks:
                if ts_ms < target_window * 1000 and (target_window * 1000 - ts_ms) <= 30_000:
                    if before_tick is None or ts_ms > before_tick[0]:
                        before_tick = (ts_ms, v)

        now_str = unix_ms_to_str(int(time.time() * 1000))

        if ge_tick:
            ts_ms, v = ge_tick
            lag_ms = ts_ms - target_window * 1000
            print(
                f"[{now_str}] WIN! price={v:.4f}  ts={unix_ms_to_str(ts_ms)}  "
                f"lag={lag_ms}ms ({lag_ms/1000:.1f}s)  buffer_n={n}"
            )
            print(f"\n>>> 开盘价 candidate: {v:.4f}")
            break

        ge_note = "NONE"
        be_note = "NONE"
        if ge_tick is None:
            if mn:
                ge_note = f"min_in_buf={unix_ms_to_str(mn)} ({mn - target_window*1000}ms before boundary)"
        if before_tick:
            ts_ms, v = before_tick
            diff_ms = target_window * 1000 - ts_ms
            be_note = f"ts={unix_ms_to_str(ts_ms)} price={v:.4f} ({diff_ms}ms before boundary)"

        print(
            f"[{now_str}] 窗口 tick 未到 | buffer_n={n} | "
            f">=boundary={ge_note} | <boundary_30s={be_note}"
        )

        time.sleep(2.0)
    else:
        print("\n120s 内未等到窗口边界 tick，测试结束")

    feed.stop()


if __name__ == "__main__":
    main()
