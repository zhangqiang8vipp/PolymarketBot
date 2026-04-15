"""
RTDS 完整调试脚本：连接后实时输出 buffer 内容与窗口边界对齐情况。
用于诊断 Polymarket 快照历史深度、tick 时序、以及窗口开盘价获取逻辑。
"""
from __future__ import annotations
import threading
import time
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from chainlink_rtds import ChainlinkBtcUsdRtds


def unix_ms_to_str(ms: int) -> str:
    """毫秒时间戳 → 可读字符串（UTC）。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def format_unix_s(s: int) -> str:
    return unix_ms_to_str(s * 1000)


def main():
    print("=" * 70)
    print("RTDS 完整诊断 — 连接 Polymarket WS 并实时跟踪 buffer 内容")
    print("=" * 70)

    feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"  [状态] {m}", flush=True))
    feed.start()

    # 等首包
    ok = feed.wait_for_ticks(1, timeout_s=15.0)
    if not ok:
        print("❌ 15s 内未收到任何 tick，检查网络")
        feed.stop()
        return

    print("\n[首包快照] 连接成功后立即打印 buffer 内容")
    _print_buffer(feed, label="首包快照")

    # 等 5 秒再看
    print("\n[等待 5s] ...")
    time.sleep(5)
    _print_buffer(feed, label="5s 后")

    # 打印最近 3 个 5 分钟窗口边界附近的 tick
    print("\n[窗口边界对齐分析]")
    _analyze_window_boundaries(feed)

    # 实时监听 20 秒，每 2 秒打印一次
    print("\n[实时监听 20s，每 2s 报告一次]")
    for i in range(10):
        time.sleep(2)
        n, mn, mx, lp = feed.buffer_stats()
        now = unix_ms_to_str(int(time.time() * 1000))
        if mn and mx:
            print(
                f"  [{now}] buffer[{n}] "
                f"min={unix_ms_to_str(mn)} val? "
                f"max={unix_ms_to_str(mx)} val={lp}"
            )
        else:
            print(f"  [{now}] buffer empty")

    print("\n[测试结束]")
    feed.stop()


def _print_buffer(feed: ChainlinkBtcUsdRtds, label: str = ""):
    """打印 buffer 里所有 tick。"""
    n, mn, mx, lp = feed.buffer_stats()
    print(f"\n  --- {label} ---")
    print(f"  tick 总数: {n}")
    if n == 0:
        print("  (空)")
        return
    print(f"  min ts={unix_ms_to_str(mn)} max ts={unix_ms_to_str(mx)}")
    print(f"  最新价格: {lp}")

    # 打印所有 tick
    with feed._lock:
        ticks_sorted = sorted(feed._ticks, key=lambda x: x[0])

    print(f"  {'#':>3} {'ts (UTC)':>15}  {'price':>10}  {'距窗口起点(s)':>12}")
    print(f"  {'-'*3} {'-'*15}  {'-'*10}  {'-'*12}")

    # 当前和最近 3 个窗口
    now_s = int(time.time())
    # 向上取整到 5 分钟
    cur_window = (now_s // 300 + 1) * 300
    windows = [cur_window - 600, cur_window - 300, cur_window, cur_window + 300]

    for idx, (ts_ms, val) in enumerate(ticks_sorted):
        ts_s = ts_ms // 1000
        nearest_diff = min(abs(ts_s - w) for w in windows)
        print(f"  {idx:>3} {unix_ms_to_str(ts_ms):>15}  {val:>10.2f}  {nearest_diff:>12}s")


def _analyze_window_boundaries(feed: ChainlinkBtcUsdRtds):
    """分析最近 3 个窗口边界是否有对应的 Chainlink tick。"""
    now_s = int(time.time())
    # 向上取整到 5 分钟
    cur_window = (now_s // 300 + 1) * 300
    windows = [cur_window - 600, cur_window - 300, cur_window]

    with feed._lock:
        ticks_sorted = sorted(feed._ticks, key=lambda x: x[0])

    for window_ts in windows:
        window_str = format_unix_s(window_ts)
        # 找 ≥ boundary 的第一条
        ge = [(ts_ms, v) for ts_ms, v in ticks_sorted if ts_ms >= window_ts * 1000]
        # 找 < boundary 且 ≤ 30s 前的
        before = [(ts_ms, v) for ts_ms, v in ticks_sorted
                  if ts_ms < window_ts * 1000 and (window_ts * 1000 - ts_ms) <= 30_000]

        print(f"\n  窗口 {window_ts} ({window_str}):")
        if ge:
            ts_ms, v = ge[0]
            lag_ms = ts_ms - window_ts * 1000
            print(f"    >=boundary first: ts={unix_ms_to_str(ts_ms)} price={v} lag={lag_ms}ms ({lag_ms/1000:.1f}s)")
        else:
            print(f"    >=boundary first: NONE (buffer earliest tick all < boundary)")

        print(f"    <boundary last (30s): {'NONE' if not before else unix_ms_to_str(ts_ms) + ' price=' + str(v)}")

    # 也检查 5 分钟窗口起始是否对齐 Chainlink tick
    print("\n  [Chainlink tick 间隔分析]")
    with feed._lock:
        ticks_sorted = sorted(feed._ticks, key=lambda x: x[0])
    if len(ticks_sorted) >= 2:
        intervals = [(ticks_sorted[i+1][0] - ticks_sorted[i][0]) / 1000
                     for i in range(min(10, len(ticks_sorted)-1))]
        avg = sum(intervals) / len(intervals)
        print(f"    最近 {len(intervals)} 个 tick 间隔: {[f'{x:.1f}s' for x in intervals]}")
        print(f"    平均间隔: {avg:.1f}s")


if __name__ == "__main__":
    main()
