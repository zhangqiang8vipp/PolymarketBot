"""
统一交易逻辑模块（bot.py 和 compare_runs.py 共用）。

版本演化记录见 CHANGELOG.md。

设计原则：
- 所有方向单的核心逻辑都在这里，禁止在 bot.py 或 compare_runs.py 里重写
- 回测调用：estimate_entry_for_backtest（估算入场价）
- 实盘调用：estimate_entry_from_window_pct（用 Binance 窗口偏离）+ entry_from_best_asks（用真实盘口）
"""

from __future__ import annotations

from typing import Optional


# ─── 入场价估算 ────────────────────────────────────────────

def token_price_from_delta(abs_window_pct: float) -> float:
    """
    将 |窗口偏离%%| 映射为 Polymarket token 价格（0~0.97）。
    量纲：abs_window_pct = |current - window_open| / window_open * 100
    来自 bot.py v1.0，v2.0 继续沿用。
    """
    d = abs_window_pct
    if d < 0.005:
        return 0.50
    if d < 0.02:
        return 0.50 + (0.05) * (d - 0.005) / (0.02 - 0.005)
    if d < 0.05:
        return 0.55 + (0.10) * (d - 0.02) / (0.05 - 0.02)
    if d < 0.10:
        return 0.65 + (0.15) * (d - 0.05) / (0.10 - 0.05)
    if d < 0.15:
        return 0.80 + (0.12) * (d - 0.10) / (0.15 - 0.10)
    return min(0.97, 0.92 + 0.05 * min(1.0, (d - 0.15) / 0.05))


def directional_entry_from_window_pct(direction: int, w_pct: float) -> float:
    """
    基于 Binance 窗口偏离估算入场价。
    - 领先方向（价已走出来）：用 token_price_from_delta
    - 逆势：更便宜，用 1 - token_price_from_delta
    量纲：w_pct = (current - window_open) / window_open * 100
    """
    d = token_price_from_delta(abs(w_pct))
    if direction == 1:
        if w_pct >= 0:
            return d
        return max(0.03, min(0.97, 1.0 - d))
    if w_pct <= 0:
        return d
    return max(0.03, min(0.97, 1.0 - d))


def estimate_entry_for_backtest(
    direction: int,
    window_open: float,
    decision_px: float,
) -> float:
    """
    回测专用入场价估算。

    用窗口开始后 SNIPE_START 秒内的价格（decision_px）计算偏离，
    再通过 directional_entry_from_window_pct 估算入场价。

    注意：这是对实盘真实盘口价的估算，实盘用 entry_from_best_asks。
    两者的差距是回测与实盘的不可消除误差来源，需单独记录。
    """
    if window_open <= 0 or decision_px <= 0:
        return 0.5
    w_pct = (decision_px - window_open) / window_open * 100.0
    return directional_entry_from_window_pct(direction, w_pct)


# ─── 仓位计算 ────────────────────────────────────────────

def compute_bet(mode: str, bankroll: float, principal: float, min_bet: float) -> float:
    """
    方向单仓位计算（名义，未应用 MAX_USD 封顶）。

    模式说明：
    - safe：     固定 25% 当前 bankroll
    - degen：    全押 bankroll
    - flat：     固定 10% 初始本金（保守模式，回测专用）
    - aggressive / 默认：只拿利润下注，保留本金

    来自 bot.py v1.0，v2.0 统一为 compare_runs.py 的仓位逻辑。
    """
    if bankroll < min_bet:
        return 0.0
    if mode == "safe":
        return max(min_bet, min(bankroll, bankroll * 0.25))
    if mode == "degen":
        return max(min_bet, bankroll)
    if mode == "flat":
        x = max(min_bet, principal * 0.10)
        return min(bankroll, x)
    # 默认 aggressive：只拿利润下注
    if bankroll <= principal + 1e-9:
        return max(min_bet, bankroll)
    return max(min_bet, bankroll - principal)


def size_by_edge(
    bankroll: float,
    edge: float,
    max_usd: Optional[float],
    min_bet: float,
) -> float:
    """
    Edge sizing：bankroll * frac * min(1, edge * scale)。

    来自 bot.py v1.0，v2.0 保留为可选模块，回测默认关闭。
    需要 _edge_sizing_edge_scale() 和 _edge_sizing_bankroll_frac() 参数，
    这两个函数定义在 bot.py 里，回测里默认用 frac=0.25, scale=1.0。
    """
    frac = 0.25  # 回测默认值；实盘从 bot.py 的 _edge_sizing_bankroll_frac() 读取
    k = min(1.0, max(0.0, float(edge)) * 1.0)  # scale=1.0 同上
    bet = float(bankroll) * frac * k
    if max_usd is not None:
        bet = min(bet, float(max_usd))
    bet = min(bet, float(bankroll))
    return max(float(min_bet), bet)
