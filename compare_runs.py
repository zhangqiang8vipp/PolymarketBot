"""
多组置信阈值 × 三种仓位模式的网格回测（逻辑见 TRADING_AND_SYSTEM_LOGIC.md）。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from openpyxl import Workbook

from backtest import fetch_klines_range_hours
from bot import directional_entry_from_window_pct
from strategy import Candle, analyze

WINDOW = 300
SNIPE_START = 10
THRESHOLDS = [i / 10.0 for i in range(9)]  # 0.0 .. 0.8
SIZING_MODES = ("flat", "safe", "aggressive")


@dataclass
class SimState:
    bankroll: float
    principal: float
    initial: float
    trades: int = 0
    wins: int = 0
    curve: List[float] = field(default_factory=list)
    trade_log: List[dict[str, Any]] = field(default_factory=list)


def bet_flat(initial: float, bankroll: float, min_bet: float) -> float:
    x = max(min_bet, initial * 0.10)
    return min(bankroll, x)


def bet_safe(_initial: float, bankroll: float, min_bet: float) -> float:
    return max(min_bet, min(bankroll, bankroll * 0.25))


def bet_aggressive(principal: float, bankroll: float, min_bet: float) -> float:
    if bankroll <= principal + 1e-12:
        return max(min_bet, bankroll)
    return max(min_bet, bankroll - principal)


def sizing_bet(mode: str, initial: float, principal: float, bankroll: float, min_bet: float) -> float:
    if bankroll < min_bet:
        return 0.0
    if mode == "flat":
        return bet_flat(initial, bankroll, min_bet)
    if mode == "safe":
        return bet_safe(initial, bankroll, min_bet)
    return bet_aggressive(principal, bankroll, min_bet)


def key(mode: str, th: float) -> str:
    return f"{mode}_{th:.1f}"


def idx_at_or_before(ts_list: List[int], target_ms: int) -> int:
    lo, hi = 0, len(ts_list) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ts_list[mid] <= target_ms:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def simulate(
    rows: List[Tuple[int, Candle]], min_bet: float, initial: float
) -> tuple[Dict[str, SimState], List[int]]:
    ts_list = [t for t, _ in rows]
    curve_steps: List[int] = []

    states: Dict[str, SimState] = {}
    for mode in SIZING_MODES:
        for th in THRESHOLDS:
            states[key(mode, th)] = SimState(
                bankroll=initial,
                principal=initial,
                initial=initial,
            )

    t0_sec = ts_list[0] // 1000
    t1_sec = ts_list[-1] // 1000
    first_win = ((t0_sec + WINDOW - 1) // WINDOW) * WINDOW
    last_win = (t1_sec // WINDOW) * WINDOW - WINDOW

    for window_ts in range(first_win, last_win + 1, WINDOW):
        start_ms = window_ts * 1000
        decision_ms = (window_ts + (WINDOW - SNIPE_START)) * 1000
        end_ms = (window_ts + WINDOW) * 1000 - 1
        i0 = idx_at_or_before(ts_list, start_ms)
        i1 = idx_at_or_before(ts_list, decision_ms)
        i_res = idx_at_or_before(ts_list, end_ms)
        if i0 < 0 or i1 < 0 or i_res < 0 or i1 < 40:
            continue
        window_open = rows[i0][1].open
        decision_px = rows[i1][1].close
        hist = [Candle(c.open, c.high, c.low, c.close, c.volume) for _, c in rows[: i1 + 1]]
        if len(hist) < 25:
            continue
        res = analyze(window_open, decision_px, hist[-60:], tick_prices=[])
        # 与 bot._binance_window_edge_prices 一致：窗内首根 1m 的 open、末根 1m 的 close。
        o0 = rows[i0][1].open
        c_end = rows[i_res][1].close
        outcome = 1 if c_end >= o0 else -1

        curve_steps.append(window_ts)

        for mode in SIZING_MODES:
            for th in THRESHOLDS:
                k = key(mode, th)
                st = states[k]
                if res.confidence < th:
                    st.curve.append(st.bankroll)
                    continue
                bet = sizing_bet(mode, st.initial, st.principal, st.bankroll, min_bet)
                if bet <= 0:
                    st.curve.append(st.bankroll)
                    continue
                w_pct = (decision_px - window_open) / window_open * 100.0
                entry = directional_entry_from_window_pct(int(res.direction), w_pct)
                win = outcome == res.direction
                st.bankroll -= bet
                shares = bet / entry
                if win:
                    st.bankroll += shares
                st.trades += 1
                if win:
                    st.wins += 1
                st.trade_log.append(
                    {
                        "window_ts": window_ts,
                        "bet": bet,
                        "win": win,
                        "entry": entry,
                        "score": res.score,
                        "conf": res.confidence,
                    }
                )
                st.curve.append(st.bankroll)

    return states, curve_steps


def write_workbook(states: Dict[str, SimState], path: str, curve_steps: List[int]) -> str:
    wb = Workbook()
    ws0 = wb.active
    ws0.title = "Summary"
    ws0.append(["mode", "threshold", "final_bankroll", "trades", "wins", "win_rate"])
    best_k = ""
    best_v = -1.0
    for k, st in states.items():
        mode, ths = k.rsplit("_", 1)
        th = float(ths)
        wr = st.wins / st.trades if st.trades else 0.0
        ws0.append([mode, th, st.bankroll, st.trades, st.wins, wr])
        if st.bankroll > best_v:
            best_v = st.bankroll
            best_k = k
    ws1 = wb.create_sheet("Best Config Trades")
    ws1.append(["window_ts", "bet", "win", "entry", "score", "conf"])
    if best_k:
        for row in states[best_k].trade_log:
            ws1.append(
                [
                    row["window_ts"],
                    row["bet"],
                    row["win"],
                    row["entry"],
                    row["score"],
                    row["conf"],
                ]
            )
    ws2 = wb.create_sheet("Bankroll Curves")
    keys = sorted(states.keys())
    ws2.append(["window_ts"] + keys)
    maxlen = max((len(st.curve) for st in states.values()), default=0)
    for i in range(maxlen):
        wts = curve_steps[i] if i < len(curve_steps) else ""
        row: List[Any] = [wts]
        for kk in keys:
            c = states[kk].curve
            row.append(c[i] if i < len(c) else "")
        ws2.append(row)
    wb.save(path)
    return best_k


def main() -> None:
    p = argparse.ArgumentParser(description="多组参数网格回测并导出 Excel")
    p.add_argument("--hours", type=int, default=72, help="拉取最近多少小时的 1m K 线")
    p.add_argument("--output", default="results.xlsx", help="输出 Excel 路径")
    p.add_argument("--min-bet", type=float, default=1.0, help="最小下注")
    p.add_argument("--initial", type=float, default=100.0, help="初始资金")
    args = p.parse_args()

    rows = fetch_klines_range_hours(args.hours)
    if len(rows) < 100:
        raise SystemExit("K 线数据不足，请增大 --hours 或检查网络/Binance")
    states, curve_steps = simulate(rows, args.min_bet, args.initial)
    best = write_workbook(states, args.output, curve_steps)
    print(f"已写入 {args.output} 最优配置={best}")


if __name__ == "__main__":
    main()
