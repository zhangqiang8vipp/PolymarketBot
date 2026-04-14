"""
Polymarket BTC 5m Up/Down 复合加权 TA（与 bot 配合；说明见 TRADING_AND_SYSTEM_LOGIC.md）。
Positive score => Up, negative => Down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class Candle:
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


def _window_delta_weight(window_pct: float) -> float:
    a = abs(window_pct)
    if a > 0.10:
        return 7.0
    if a > 0.02:
        return 5.0
    if a > 0.005:
        return 3.0
    if a > 0.001:
        return 1.0
    return 0.0


def _micro_momentum(candles: List[Candle]) -> float:
    if len(candles) < 2:
        return 0.0
    if candles[-1].close > candles[-2].close:
        return 2.0
    if candles[-1].close < candles[-2].close:
        return -2.0
    return 0.0


def _acceleration(candles: List[Candle]) -> float:
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


def analyze(
    window_open_price: float,
    current_price: float,
    candles: List[Candle],
    tick_prices: Optional[List[float]] = None,
) -> AnalysisResult:
    """
    window_open_price: BTC at window start (e.g. 1m open at window_ts).
    current_price: latest spot / last close proxy.
    candles: 1m bars, oldest first, includes current minute.
    tick_prices: optional 2s poll samples during snipe window.
    """
    tick_prices = tick_prices or []
    details: dict[str, Any] = {}

    if window_open_price <= 0 or current_price <= 0:
        return AnalysisResult(1, 0.0, 0.0, {"error": "invalid_prices"})

    window_pct = (current_price - window_open_price) / window_open_price * 100.0
    w = _window_delta_weight(window_pct)
    w_score = w if window_pct > 0 else (-w if window_pct < 0 else 0.0)
    details["window_pct"] = window_pct
    details["window_delta_w"] = w_score

    score = w_score
    score += _micro_momentum(candles)
    score += _acceleration(candles)
    score += _ema_cross(candles)
    score += _rsi_weight(candles)
    score += _volume_surge(candles)
    score += _tick_trend(tick_prices)

    details["micro_momentum"] = _micro_momentum(candles)
    details["acceleration"] = _acceleration(candles)
    details["ema_cross"] = _ema_cross(candles)
    details["rsi_w"] = _rsi_weight(candles)
    details["volume_surge"] = _volume_surge(candles)
    details["tick_trend"] = _tick_trend(tick_prices)

    direction = 1 if score >= 0 else -1
    confidence = min(abs(score) / 7.0, 1.0)
    return AnalysisResult(direction=direction, score=score, confidence=confidence, details=details)
