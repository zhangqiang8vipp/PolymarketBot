"""
Polymarket BTC 5m Up/Down 复合加权 TA（与 bot 配合；说明见 TRADING_AND_SYSTEM_LOGIC.md）。
Positive score => Up, negative => Down.

⚠️ 重要：回测时传入 analyze() 的 candles 必须是"决策点之前的历史数据"，
不应包含窗口期内（window_open 到 decision 时刻）的 K 线，
否则会构成循环论证（用窗口内已发生的价格变动来预测同一窗口的结果）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class AnalysisResult:
    direction: int  # 1 = Up, -1 = Down
    score: float
    confidence: float
    details: dict[str, Any]


def _ema_series(closes: List[float], period: int) -> List[float]:
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    out: List[float] = []
    sma = sum(closes[:period]) / period
    out.append(sma)
    ema = sma
    for p in closes[period:]:
        ema = k * p + (1 - k) * ema
        out.append(ema)
    return out


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _micro_momentum(candles: List[Candle]) -> float:
    """最近 1 分钟动量（基于历史数据，不含窗口期）。"""
    if len(candles) < 2:
        return 0.0
    if candles[-1].close > candles[-2].close:
        return 2.0
    if candles[-1].close < candles[-2].close:
        return -2.0
    return 0.0


def _acceleration(candles: List[Candle]) -> float:
    """加速度：最近 1 分钟 vs 3 分钟前的趋势（基于历史数据）。"""
    if len(candles) < 3:
        return 0.0
    m0 = candles[-1].close - candles[-1].open
    m2 = candles[-3].close - candles[-3].open
    if m0 > 0 and m0 > m2:
        return 1.5
    if m0 < 0 and m0 < m2:
        return -1.5
    if m0 > 0 and m0 < m2:
        return -0.5
    if m0 < 0 and m0 > m2:
        return 0.5
    return 0.0


def _ema_cross(candles: List[Candle]) -> float:
    """EMA 金叉/死叉（基于历史数据）。"""
    closes = [c.close for c in candles]
    if len(closes) < 21:
        return 0.0
    e9 = _ema_series(closes, 9)
    e21 = _ema_series(closes, 21)
    if not e9 or not e21:
        return 0.0
    if e9[-1] > e21[-1]:
        return 1.0
    if e9[-1] < e21[-1]:
        return -1.0
    return 0.0


def _rsi_weight(candles: List[Candle]) -> float:
    """RSI 超买超卖信号（基于历史数据）。"""
    closes = [c.close for c in candles]
    rsi = _rsi(closes, 14)
    if rsi is None:
        return 0.0
    if rsi > 75:
        return -2.0
    if rsi < 25:
        return 2.0
    if rsi > 60:
        return -1.0
    if rsi < 40:
        return 1.0
    return 0.0


def _volume_surge(candles: List[Candle]) -> float:
    """成交量突增信号（基于历史数据）。"""
    if len(candles) < 6:
        return 0.0
    recent = candles[-3:]
    prior = candles[-6:-3]
    rv = sum(c.volume for c in recent) / 3.0
    pv = sum(c.volume for c in prior) / 3.0
    if pv == 0:
        return 0.0
    if rv < 1.5 * pv:
        return 0.0
    if candles[-1].close >= candles[-1].open:
        return 1.0
    return -1.0


def _tick_trend(tick_prices: List[float]) -> float:
    """Tick 级别价格趋势（仅实盘可用，回测为空列表）。"""
    if len(tick_prices) < 5:
        return 0.0
    first = tick_prices[0]
    last = tick_prices[-1]
    if first <= 0:
        return 0.0
    move_pct = (last - first) / first * 100.0
    if abs(move_pct) < 0.005:
        return 0.0
    ups = sum(1 for i in range(1, len(tick_prices)) if tick_prices[i] > tick_prices[i - 1])
    downs = sum(1 for i in range(1, len(tick_prices)) if tick_prices[i] < tick_prices[i - 1])
    n = ups + downs
    if n == 0:
        return 0.0
    if ups / n >= 0.60 and move_pct > 0:
        return 2.0
    if downs / n >= 0.60 and move_pct < 0:
        return -2.0
    return 0.0


def _trend_strength(candles: List[Candle]) -> float:
    """趋势强度：基于最近 N 根 K 线的方向一致性。"""
    if len(candles) < 10:
        return 0.0
    up_count = sum(1 for c in candles[-10:] if c.close > c.open)
    dn_count = 10 - up_count
    bias = up_count - dn_count  # -10 ~ +10
    if bias >= 7:
        return 2.0
    if bias <= -7:
        return -2.0
    if bias >= 4:
        return 1.0
    if bias <= -4:
        return -1.0
    return 0.0


def analyze(
    candles: List[Candle],
    tick_prices: Optional[List[float]] = None,
    window_open: Optional[float] = None,
    current_price: Optional[float] = None,
) -> AnalysisResult:
    """
    基于历史技术指标预测 5 分钟窗口方向（不含窗口期内数据！）。

    candles: 决策点之前的 1m K 线（oldest first），不应包含窗口期内的 K 线！
    tick_prices: 实盘时可选的 2s 采样数据（回测为空列表）。
    window_open / current_price: [已废弃，仅为向后兼容 bot.py]
        旧版代码用窗口内价格变动算分（含循环论证），新版本忽略这两个参数。

    返回：direction=1(Up)/-1(Down), score（综合评分）, confidence（0~1）
    """
    tick_prices = tick_prices or []
    details: dict[str, Any] = {}

    if not candles or len(candles) < 2:
        return AnalysisResult(1, 0.0, 0.0, {"error": "insufficient_candles"})

    score = 0.0

    # ── 历史 TA 信号（不含窗口期数据）────────────────────────────
    micro = _micro_momentum(candles)
    accel = _acceleration(candles)
    ema = _ema_cross(candles)
    rsi_w = _rsi_weight(candles)
    vol = _volume_surge(candles)
    trend = _trend_strength(candles)
    tick = _tick_trend(tick_prices)

    score += micro
    score += accel
    score += ema
    score += rsi_w
    score += vol
    score += trend
    score += tick

    details["micro_momentum"] = micro
    details["acceleration"] = accel
    details["ema_cross"] = ema
    details["rsi_w"] = rsi_w
    details["volume_surge"] = vol
    details["trend_strength"] = trend
    details["tick_trend"] = tick

    # score 范围约 -13 ~ +13（7 个子信号 × max 权重）
    direction = 1 if score >= 0 else -1
    confidence = min(abs(score) / 7.0, 1.0)  # 归一化到 0~1

    return AnalysisResult(direction=direction, score=score, confidence=confidence, details=details)
