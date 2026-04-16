"""
Binance K 线获取模块

提供 BTC/USD K 线数据和实时价格获取功能。
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Optional

import requests

# ── Binance API 配置 ────────────────────────────────────────────────────────

BINANCE_REST_BASE = os.environ.get(
    "BINANCE_REST_BASE", "https://api.binance.com/api/v3"
)
BINANCE_HTTP_RETRIES = int(os.environ.get("BINANCE_HTTP_RETRIES", "5"))


def _binance_get(endpoint: str, params: Optional[dict] = None) -> Any:
    """Binance GET 请求，带重试。"""
    url = f"{BINANCE_REST_BASE}/{endpoint}"
    for attempt in range(BINANCE_HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2)
                continue
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return None


def fetch_btc_spot_price_usdt() -> float:
    """
    获取 BTC/USDT 当前价格（Binance ticker）。
    失败时返回 0（上层调用会处理）。
    """
    data = _binance_get("ticker/price", {"symbol": "BTCUSDT"})
    if data and "price" in data:
        return float(data["price"])
    return 0.0


# ── K 线数据结构 ──────────────────────────────────────────────────────────────

# Binance 1m K 线字段：
# [open_time, open, high, low, close, volume, close_time, ...]
OPEN, HIGH, LOW, CLOSE, VOLUME = 1, 2, 3, 4, 5


def fetch_klines_1m(
    symbol: str = "BTCUSDT",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 60,
) -> List[Any]:
    """
    获取 Binance K 线数据。

    Args:
        symbol: 交易对
        start_ms: 起始时间（毫秒）
        end_ms: 结束时间（毫秒）
        limit: 最大数量 (1-1000)

    Returns:
        K 线列表，每条为 [timestamp, open, high, low, close, volume, ...]
    """
    params: dict[str, Any] = {"symbol": symbol, "interval": "1m", "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms

    data = _binance_get("klines", params)
    if data:
        return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in data]
    return []


def fetch_klines_1m_ts(
    start_ts: int,
    end_ts: int,
    symbol: str = "BTCUSDT",
    max_rows: int = 1000,
) -> List[Any]:
    """
    按 Unix 时间戳（秒）获取 K 线。

    Args:
        start_ts: 起始 Unix 秒
        end_ts: 结束 Unix 秒
        symbol: 交易对
        max_rows: 最大行数

    Returns:
        K 线列表
    """
    return fetch_klines_1m(
        symbol=symbol,
        start_ms=start_ts * 1000,
        end_ms=end_ts * 1000,
        limit=min(max_rows, 1000),
    )


def fetch_klines_range_hours(
    hours: int = 48,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
) -> List[Any]:
    """
    获取最近 N 小时的 K 线数据。

    Args:
        hours: 小时数
        symbol: 交易对
        interval: K 线周期 (1m, 5m, 1h, etc.)

    Returns:
        K 线列表
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000

    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        batch = _fetch_klines_batch(symbol, interval, current_start, end_ms)
        if not batch:
            break
        all_klines.extend(batch)
        current_start = batch[-1][0] + 1

    return all_klines


def _fetch_klines_batch(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
) -> List[Any]:
    """单次获取 K 线批次。"""
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    data = _binance_get("klines", params)
    if data:
        return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in data]
    return []


if __name__ == "__main__":
    # 测试：获取最近 5 条 K 线
    klines = fetch_klines_1m(limit=5)
    print(f"获取到 {len(klines)} 条 K 线")
    for k in klines:
        print(f"  {k[0]}: O={k[1]:.2f} H={k[2]:.2f} L={k[3]:.2f} C={k[4]:.2f}")

    price = fetch_btc_spot_price_usdt()
    print(f"\nBTC/USDT 当前价格: ${price:.2f}")
