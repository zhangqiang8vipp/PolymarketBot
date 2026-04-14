"""
Historical 1m candles from Binance (used by compare_runs.py and bot resolution checks).

Env:
  BINANCE_REST_BASE — primary REST root (default https://api.binance.com/api/v3).
  BINANCE_REST_BASE_FALLBACKS — 逗号分隔备用 REST 根；不设则**只连主站**（避免无效镜像拖慢/报错）。
  若主站 451/被墙，请自行填经 curl 验证可用的根，例如部分网络可用 api-gcp。
  BINANCE_HTTP_RETRIES — 同一 REST 根上 GET 遇断线/超时的重试次数（默认 5）；BINANCE_HTTP_RETRY_BACKOFF_S 为退避秒数（默认 1.2）。
  BTC_KLINE_NO_COINBASE_FALLBACK — 设为 1/true 时关闭：Binance 不可用时（如 HTTP 451/403）对 BTCUSDT 自动改用 Coinbase Exchange 公共 1m 蜡烛与 BTC-USD ticker（fetch_btc_spot_price_usdt）。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from strategy import Candle


def _binance_rest_bases() -> List[str]:
    primary = os.environ.get("BINANCE_REST_BASE", "https://api.binance.com/api/v3").rstrip("/")
    raw = os.environ.get("BINANCE_REST_BASE_FALLBACKS", "").strip()
    if raw:
        extras = [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]
    else:
        # 默认不内置备用域名：不同地区/运营商对 gcp、*.binance.me 表现差异大（本机实测仅主站稳定 200）
        extras: List[str] = []
    seen: set[str] = set()
    out: List[str] = []
    for b in [primary, *extras]:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _binance_retryable_status(code: int) -> bool:
    return code in (451, 403, 404, 429) or code >= 500


def _binance_http_retries() -> int:
    raw = os.environ.get("BINANCE_HTTP_RETRIES", "5").strip()
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return 5


def _binance_http_retry_backoff_s() -> float:
    raw = os.environ.get("BINANCE_HTTP_RETRY_BACKOFF_S", "1.2").strip()
    try:
        return max(0.1, min(float(raw), 30.0))
    except ValueError:
        return 1.2


def binance_get(path: str, params: Dict[str, Any], timeout: int = 30) -> requests.Response:
    """
    GET {base}/{path} using BINANCE_REST_BASE then fallbacks on retryable HTTP or connection errors.
    同一根地址上 ConnectionError/Timeout/OSError 会退避重试（仅一个根时也能扛 WinError 10054 / 代理闪断）。
    """
    bases = _binance_rest_bases()
    retries = _binance_http_retries()
    backoff = _binance_http_retry_backoff_s()
    last: BaseException | None = None
    for i, base in enumerate(bases):
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        r: requests.Response | None = None
        conn_err: BaseException | None = None
        for attempt in range(retries):
            try:
                r = requests.get(url, params=params, timeout=timeout)
                conn_err = None
                break
            except (requests.ConnectionError, requests.Timeout, OSError) as e:
                conn_err = e
                last = e
                if attempt + 1 < retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                break
        if conn_err is not None and r is None:
            if i + 1 < len(bases):
                continue
            raise conn_err
        assert r is not None
        try:
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last = e
            resp = e.response
            if (
                resp is not None
                and _binance_retryable_status(resp.status_code)
                and i + 1 < len(bases)
            ):
                continue
            if resp is not None and resp.status_code == 451:
                raise RuntimeError(
                    "Binance HTTP 451（当前网络/地区无法访问该节点）。"
                    "可尝试：VPN；或设置 BINANCE_REST_BASE、BINANCE_REST_BASE_FALLBACKS（逗号分隔多个 REST 根）。"
                    f"\n最后请求: {resp.url}"
                ) from e
            raise
    if last:
        raise last
    raise RuntimeError("binance_get：未配置任何 Binance REST 根地址")


def _row_to_candle(row: list) -> Candle:
    o, h, low, c, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
    return Candle(open=o, high=h, low=low, close=c, volume=v)


def _coinbase_kline_fallback_disabled() -> bool:
    return os.environ.get("BTC_KLINE_NO_COINBASE_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_geo_or_access_blocked(exc: BaseException) -> bool:
    if isinstance(exc, RuntimeError) and "451" in str(exc):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (451, 403)
    return False


def fetch_btc_spot_price_usdt() -> float:
    """
    Binance BTCUSDT 最新价；遇 HTTP 451/403（或 RuntimeError 451 提示）时改用 Coinbase BTC-USD ticker。
    与 K 线共用开关 BTC_KLINE_NO_COINBASE_FALLBACK=1 可关闭 Coinbase 分支。
    """
    try:
        r = binance_get("ticker/price", params={"symbol": "BTCUSDT"}, timeout=15)
        return float(r.json()["price"])
    except (RuntimeError, requests.HTTPError) as e:
        if _coinbase_kline_fallback_disabled() or not _is_geo_or_access_blocked(e):
            raise
    r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=15)
    r.raise_for_status()
    return float(r.json()["price"])


def _iso_utc_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coinbase_fetch_candles_segment(start_ms: int, end_ms: int) -> list:
    """GET Coinbase Exchange BTC-USD 1m candles; each row [time_sec, low, high, open, close, volume]."""
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    end_eff = max(int(end_ms), int(start_ms) + 60_000)
    r = requests.get(
        url,
        params={
            "granularity": 60,
            "start": _iso_utc_ms(start_ms),
            "end": _iso_utc_ms(end_eff),
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _coinbase_rows_to_binance_shape(raw: list) -> List[list]:
    """Binance kline row: [open_ms, o, h, l, c, v, ...]."""
    by_open: dict[int, list] = {}
    for row in raw:
        if not row or len(row) < 6:
            continue
        t_sec, low, high, open_, close, vol = row[0], row[1], row[2], row[3], row[4], row[5]
        t_ms = int(float(t_sec)) * 1000
        by_open[t_ms] = [
            t_ms,
            float(open_),
            float(high),
            float(low),
            float(close),
            float(vol),
        ]
    return sorted(by_open.values(), key=lambda x: int(x[0]))


def _coinbase_chunked_candles(start_ms: int, end_ms: int) -> List[list]:
    """Coinbase allows at most ~300 candles per request; chunk in UTC."""
    out: list = []
    cur = start_ms
    max_span_ms = 299 * 60_000
    while cur < end_ms:
        seg_end = min(cur + max_span_ms, end_ms)
        chunk = _coinbase_fetch_candles_segment(cur, seg_end)
        out.extend(chunk)
        if seg_end >= end_ms:
            break
        cur = seg_end
        time.sleep(0.08)
    return out


def _coinbase_klines_as_binance_rows(
    symbol: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
) -> List[list]:
    if symbol.upper() != "BTCUSDT":
        raise RuntimeError("Coinbase 回退仅支持 BTCUSDT 1m")
    now_ms = int(time.time() * 1000)
    lim = max(1, min(int(limit), 1000))

    if start_ms is None and end_ms is None:
        e = now_ms + 60_000
        s = e - (lim + 12) * 60_000
        raw = _coinbase_chunked_candles(s, e)
        rows = _coinbase_rows_to_binance_shape(raw)
        if len(rows) > lim:
            rows = rows[-lim:]
        return rows

    if start_ms is not None and end_ms is None:
        e = int(start_ms) + lim * 60_000 + 180_000
        raw = _coinbase_chunked_candles(int(start_ms), e)
        rows = _coinbase_rows_to_binance_shape(raw)
        rows = [r for r in rows if int(r[0]) >= int(start_ms)]
        return rows[:lim]

    if start_ms is not None and end_ms is not None:
        raw = _coinbase_chunked_candles(int(start_ms), int(end_ms))
        rows = _coinbase_rows_to_binance_shape(raw)
        rows = [r for r in rows if int(start_ms) <= int(r[0]) < int(end_ms)]
        return rows[:lim]

    # start_ms None, end_ms set
    s = int(end_ms) - (lim + 12) * 60_000
    raw = _coinbase_chunked_candles(s, int(end_ms))
    rows = _coinbase_rows_to_binance_shape(raw)
    if len(rows) > lim:
        rows = rows[-lim:]
    return rows


def _binance_klines_params(
    symbol: str,
    start_ms: int | None,
    end_ms: int | None,
    limit: int,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {"symbol": symbol, "interval": "1m", "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms
    return params


def _fetch_klines_raw_binance_or_coinbase(
    symbol: str,
    start_ms: int | None,
    end_ms: int | None,
    limit: int,
) -> list:
    params = _binance_klines_params(symbol, start_ms, end_ms, limit)
    try:
        r = binance_get("klines", params=params, timeout=30)
        return r.json()
    except (RuntimeError, requests.HTTPError) as e:
        if _coinbase_kline_fallback_disabled() or not _is_geo_or_access_blocked(e):
            raise
        if symbol.upper() != "BTCUSDT":
            raise
        return _coinbase_klines_as_binance_rows(symbol, start_ms, end_ms, limit)


def fetch_klines_1m(
    symbol: str = "BTCUSDT",
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 1000,
) -> List[Candle]:
    """
    Fetch 1m klines [start_ms, end_ms). Binance max 1000 per request; caller may chunk.
    Binance 451/403 且标的为 BTCUSDT 时自动改用 Coinbase Exchange 公共 API（可关 BTC_KLINE_NO_COINBASE_FALLBACK）。
    """
    raw = _fetch_klines_raw_binance_or_coinbase(symbol, start_ms, end_ms, limit)
    return [_row_to_candle(row) for row in raw]


def fetch_klines_1m_ts(
    symbol: str = "BTCUSDT",
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 1000,
) -> List[tuple[int, Candle]]:
    """Same as fetch_klines_1m but keeps kline open time (ms)."""
    raw = _fetch_klines_raw_binance_or_coinbase(symbol, start_ms, end_ms, limit)
    return [(int(row[0]), _row_to_candle(row)) for row in raw]


def fetch_klines_range_hours(hours: int, symbol: str = "BTCUSDT") -> List[tuple[int, Candle]]:
    """Paginate Binance 1m candles for the last `hours` hours (open_time_ms, candle)."""
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    rows: List[tuple[int, Candle]] = []
    cur = start
    while cur < end:
        batch = fetch_klines_1m_ts(symbol=symbol, start_ms=cur, end_ms=end, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 60_000
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    return rows
