"""
polymarket_outcomes 时间段专项回测 + 与真实结果对比。

目标：
1. 拉取 polymarket_outcomes.json 覆盖时间段的 Binance 1m K 线
2. 在同样时间段跑回测（用新的统一入场价逻辑）
3. 按窗口对比：回测预测方向 vs Polymarket 实际结果
4. 对比 bot 实盘记录（若 dry_run_bankroll.json 或交易日志存在）

逻辑与 compare_runs.py 完全一致（引用同一套 trading_logic）。
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from backtest import fetch_klines_1m_ts
from strategy import AnalysisResult, Candle, analyze
from trading_logic import compute_bet, estimate_entry_for_backtest


# ─── 窗口参数（与 bot.py / compare_runs.py 一致）─────────────
WINDOW = 300        # 5 分钟窗口（秒）
SNIPE_START = 10    # 窗口结束前 10s 狙击
MIN_CANDLES_FOR_TA = 60


def _safe_find(ls: List, pred):
    for i, x in enumerate(ls):
        if pred(x):
            return i
    return None


def fetch_klines_range_ms(
    start_ms: int,
    end_ms: int,
    symbol: str = "BTCUSDT",
) -> List[Tuple[int, Candle]]:
    """分页拉取指定时间范围的 Binance 1m K 线（自动翻页）。"""
    rows: List[Tuple[int, Candle]] = []
    cur = start_ms
    while cur < end_ms:
        batch = fetch_klines_1m_ts(symbol=symbol, start_ms=cur, end_ms=end_ms, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 60_000
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    return rows


def load_outcomes(path: str = "polymarket_outcomes.json") -> Dict[int, str]:
    """返回 {window_ts: resolution}，window_ts 为 5 分钟窗口起始（Unix 秒）。"""
    with open(path) as f:
        raw = json.load(f)
    out: Dict[int, str] = {}
    for ts_str, val in raw.items():
        ts = int(ts_str)
        if val.get("closed") and val.get("resolution"):
            out[ts] = val["resolution"]
    return out


def outcomes_time_range(outcomes: Dict[int, str]) -> Tuple[int, int]:
    """返回 (最早窗口时间, 最晚窗口时间)，单位秒。"""
    tss = list(outcomes.keys())
    return min(tss), max(tss)


def binance_to_window(binance_ts_ms: int) -> int:
    """Binance 1m K 线 open_time_ms → Polymarket 5 分钟窗口起始秒。"""
    return (binance_ts_ms // 1000 // WINDOW) * WINDOW


def fetch_klines_for_outcomes(
    outcomes: Dict[int, str],
    margin_before: int = 3600,
    margin_after: int = 600,
) -> List[Tuple[int, Candle]]:
    """
    拉取覆盖 outcomes 时间段所需的 Binance K 线。
    margin_before：在第一个窗口前多拉 1 小时（确保有足够历史 K 线做 TA）
    margin_after：在最后一个窗口后多拉 10 分钟（确保窗口结算数据完整）
    """
    t_min, t_max = outcomes_time_range(outcomes)
    start_ms = (t_min - margin_before) * 1000
    end_ms = (t_max + margin_after) * 1000
    print(f"[K线] 时间段: {datetime.fromtimestamp(t_min)} ~ {datetime.fromtimestamp(t_max)}")
    rows = fetch_klines_range_ms(start_ms=start_ms, end_ms=end_ms)
    return rows


def simulate_vs_outcomes(
    rows: List[Tuple[int, Candle]],
    outcomes: Dict[int, str],
    min_bet: float = 1.0,
    initial: float = 100.0,
    sizing_mode: str = "aggressive",
    confidence_th: float = 0.0,
) -> Dict[str, any]:
    """
    在 outcomes 时间段跑回测，按窗口逐条对比回测预测 vs Polymarket 实际。
    返回对比结果 dict。
    """
    ts_list = [t for t, _ in rows]
    t0_sec = ts_list[0] // 1000
    t1_sec = ts_list[-1] // 1000

    bankroll = initial
    principal = initial

    results: List[Dict] = []

    # 找第一个完整 5 分钟窗口起始
    first_window = (t0_sec // WINDOW + 1) * WINDOW

    window_ts = first_window
    while window_ts <= t1_sec:
        w_start = window_ts
        w_end = window_ts + WINDOW

        # 找索引
        i_start = _safe_find(ts_list, lambda t: t // 1000 >= w_start)
        i_decide = _safe_find(ts_list, lambda t: t // 1000 >= w_start + SNIPE_START)
        i_end = _safe_find(ts_list, lambda t: t // 1000 >= w_end)

        if i_start is None or i_end is None:
            window_ts += WINDOW
            continue

        window_open = rows[i_start][1].close
        decision_px = rows[i_decide][1].close if i_decide is not None else window_open
        window_end_px = rows[i_end][1].close

        # 历史 K 线
        if i_start < MIN_CANDLES_FOR_TA:
            window_ts += WINDOW
            continue
        hist_candles = [rows[i][1] for i in range(i_start - MIN_CANDLES_FOR_TA, i_start)]

        # TA 分析
        res: AnalysisResult = analyze(hist_candles)

        # Polymarket 真实结果
        real_outcome_ts = binance_to_window(rows[i_end][0])
        real_resolution = outcomes.get(real_outcome_ts)

        # 过滤
        skip_reason: Optional[str] = None
        if res.confidence < confidence_th:
            skip_reason = f"conf_th"
        if bankroll < min_bet:
            skip_reason = "bankroll"
        if real_outcome_ts not in outcomes:
            skip_reason = "no_outcome"

        # 方向正确性
        real_dir = 1 if real_resolution == "Up" else -1 if real_resolution == "Down" else 0
        predicted_correct = (res.direction == real_dir) if real_dir != 0 else None

        # 计算
        entry: Optional[float] = None
        bet_val: Optional[float] = None
        pnl: Optional[float] = None

        if skip_reason is None and res.direction != 0:
            entry = estimate_entry_for_backtest(res.direction, window_open, decision_px)
            bet_val = compute_bet(sizing_mode, bankroll, principal, min_bet)
            if bet_val > 0:
                shares = bet_val / entry
                win = predicted_correct
                bankroll -= bet_val
                if win:
                    bankroll += shares
                    pnl = shares - bet_val
                else:
                    pnl = -bet_val

        results.append({
            "window_ts": window_ts,
            "direction": res.direction,
            "confidence": res.confidence,
            "score": res.score,
            "entry": entry,
            "bet": bet_val,
            "pnl": pnl,
            "bankroll": bankroll,
            "real_resolution": real_resolution,
            "real_dir": real_dir,
            "predicted_correct": predicted_correct,
            "skip_reason": skip_reason,
        })

        window_ts += WINDOW

    traded = [r for r in results if r["skip_reason"] is None and r["bet"] is not None]
    skipped = [r for r in results if r["skip_reason"] is not None]
    correct = [r for r in traded if r["predicted_correct"] is True]
    wrong = [r for r in traded if r["predicted_correct"] is False]

    win_rate = len(correct) / len(traded) if traded else 0.0
    total_pnl = sum(r["pnl"] or 0 for r in traded)

    return {
        "results": results,
        "summary": {
            "total_windows": len(results),
            "traded": len(traded),
            "skipped": len(skipped),
            "correct": len(correct),
            "wrong": len(wrong),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "final_bankroll": bankroll,
            "multiplier": bankroll / initial,
        },
    }


def print_comparison_table(results: List[Dict], limit: int = 80) -> None:
    traded = [r for r in results if r["skip_reason"] is None and r["bet"] is not None]
    if not traded:
        print("[对比表] 无交易记录")
        return
    print("\n" + "=" * 130)
    print(f"{'窗口时间':<22} {'方向':>5} {'置信度':>8} {'入场价':>7} {'下注':>8} {'盈亏':>8} {'真实结果':>10} {'正确':>6}")
    print("-" * 130)
    for idx, r in enumerate(traded):
        if idx >= limit:
            print(f"...（还有 {len(traded) - idx} 笔）")
            break
        ts_str = datetime.fromtimestamp(r["window_ts"]).strftime("%Y-%m-%d %H:%M:%S")
        dir_str = {1: "Up", -1: "Down", 0: "None"}.get(r["direction"], "?")
        real_str = r["real_resolution"] or "?"
        correct_str = "YES" if r["predicted_correct"] else "NO "
        entry_s = f"{r['entry']:.4f}" if r["entry"] else "N/A"
        bet_s = f"{r['bet']:.2f}" if r["bet"] else "N/A"
        pnl_s = f"{r['pnl']:+.4f}" if r["pnl"] else "N/A"
        print(f"{ts_str:<22} {dir_str:>5} {r['confidence']:>8.3f} {entry_s:>7} {bet_s:>8} {pnl_s:>8} {real_str:>10} {correct_str:>6}")


def print_summary(summary: Dict, sizing_mode: str, conf_th: float) -> None:
    print("\n" + "=" * 80)
    print(f"配置: sizing={sizing_mode}, confidence_th={conf_th:.1f}")
    print("=" * 80)
    print(f"  总窗口数    : {summary['total_windows']}")
    print(f"  实际交易    : {summary['traded']}")
    print(f"  跳过        : {summary['skipped']}")
    print(f"  预测正确    : {summary['correct']}")
    print(f"  预测错误    : {summary['wrong']}")
    print(f"  回测胜率    : {summary['win_rate']:.1%}")
    print(f"  总盈亏      : {summary['total_pnl']:+.4f}")
    print(f"  终资金      : {summary['final_bankroll']:.4f}")
    print(f"  资金倍数    : {summary['multiplier']:.4f}x")
    print("=" * 80)


def main() -> None:
    print("[对比回测] 加载 polymarket_outcomes.json...")
    outcomes = load_outcomes()
    t_min, t_max = outcomes_time_range(outcomes)
    print(f"[数据] {len(outcomes)} 个窗口, {datetime.fromtimestamp(t_min)} ~ {datetime.fromtimestamp(t_max)}")

    print("[K线] 拉取 Binance 1m K 线（分页拉取，请稍候）...")
    rows = fetch_klines_for_outcomes(outcomes)

    if len(rows) < 100:
        print("[错误] K 线数据不足，请检查网络")
        return

    print(f"[K线] 成功获取 {len(rows)} 根 K 线\n")

    configs = [
        ("aggressive", 0.0),
        ("aggressive", 0.3),
        ("aggressive", 0.5),
        ("safe", 0.0),
        ("safe", 0.3),
        ("flat", 0.0),
        ("flat", 0.3),
    ]

    best_wr = -1.0
    best_config = None
    best_data = None

    for sizing, conf_th in configs:
        summary = simulate_vs_outcomes(
            rows, outcomes,
            min_bet=1.0,
            initial=100.0,
            sizing_mode=sizing,
            confidence_th=conf_th,
        )
        s = summary["summary"]
        wr = s["win_rate"]
        print(f"  {sizing:<12} conf_th={conf_th:.1f}  -> 胜率={wr:.1%} ({s['correct']}/{s['traded']})  资金={s['final_bankroll']:.2f}  pnl={s['total_pnl']:+.2f}")

        if wr > best_wr and s["traded"] >= 5:
            best_wr = wr
            best_config = (sizing, conf_th)
            best_data = summary

    if best_data is not None:
        sizing, conf_th = best_config
        print(f"\n最优配置: {sizing} + conf_th={conf_th:.1f}")
        print_summary(best_data["summary"], sizing, conf_th)
        print_comparison_table(best_data["results"], limit=80)

    # 打印所有配置横向对比
    print("\n[全配置横向对比]")
    print(f"{'模式':<12} {'置信度':>8} {'胜率':>8} {'交易数':>6} {'总盈亏':>10} {'终资金':>10}")
    print("-" * 65)
    for sizing, conf_th in configs:
        summary = simulate_vs_outcomes(
            rows, outcomes,
            min_bet=1.0,
            initial=100.0,
            sizing_mode=sizing,
            confidence_th=conf_th,
        )
        s = summary["summary"]
        print(f"{sizing:<12} {conf_th:>8.1f} {s['win_rate']:>8.1%} {s['traded']:>6} {s['total_pnl']:>10.4f} {s['final_bankroll']:>10.4f}")


if __name__ == "__main__":
    main()
