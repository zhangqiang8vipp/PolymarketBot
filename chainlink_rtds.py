"""
Polymarket RTDS — Chainlink BTC/USD (oracle-aligned with Price To Beat).

Docs: https://docs.polymarket.com/market-data/websocket/rtds
Subscribe topic `crypto_prices_chainlink`, filter `{"symbol":"btc/usd"}`。
服务端对快照可能带 `topic":"crypto_prices"`（与 `crypto_prices_chainlink` 并存），只要 `payload.symbol` 为 `btc/usd` 即解析。
Use payload.timestamp (ms) and payload.value; first tick with timestamp >= boundary captures open/close at window edges.

Env（高胜率默认值）：
  RTDS_AUTO_RECONNECT_STALE_S — 默认 300s（原 120s）；超过该墙钟秒数未写入 btc/usd 则 close WS 触发重连。
    高胜率建议 >=300s，避免频繁清缓冲丢失窗口边界数据。
    设为 0|off|false|none 可关闭此逻辑。
  RTDS_RECONNECT_CLEAR_BUFFER — 默认 0（原 1）；设为 0 时重连**不清空** tick 缓冲（仍 close WS），保证窗口边界数据不丢失。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore

DEFAULT_WS = "wss://ws-live-data.polymarket.com"
SYMBOL = "btc/usd"
KEEP_HISTORY_MS = 2 * 60 * 60 * 1000


def _rtds_auto_reconnect_stale_s() -> float:
    """
    仅看「久未成功解析并写入 btc/usd」的墙钟秒数：超过阈值则 close WS 触发重连。
    **不用** payload 时间戳相对墙钟的差值——Chainlink 的 payload 时间本身常晚于墙钟数十秒～数分钟，属正常，误用会频繁重连。
    RTDS_AUTO_RECONNECT_STALE_S=0|off|false|none 关闭（仅依赖断线重连）。
    高胜率建议：>=300s（5分钟），避免频繁清缓冲丢失窗口边界数据。
    """
    raw = os.environ.get("RTDS_AUTO_RECONNECT_STALE_S", "300").strip().lower()
    if raw in ("0", "off", "false", "none", ""):
        return 0.0
    try:
        return max(60.0, min(float(raw), 3600.0))
    except ValueError:
        return 300.0


def _rtds_auto_reconnect_min_interval_s() -> float:
    try:
        return max(15.0, min(float(os.environ.get("RTDS_AUTO_RECONNECT_MIN_INTERVAL_S", "45")), 600.0))
    except ValueError:
        return 45.0


def _rtds_watchdog_grace_s() -> float:
    """每次 WS on_open 后若干秒内不判陈旧，避免刚连上快照未齐就重连。"""
    try:
        return max(5.0, min(float(os.environ.get("RTDS_WATCHDOG_GRACE_S", "40")), 300.0))
    except ValueError:
        return 40.0


def _rtds_reconnect_clear_buffer() -> bool:
    """watchdog 触发的强制重连是否清空 tick；高胜率默认=0（保留缓冲，减轻丢窗口边界数据）。"""
    raw = os.environ.get("RTDS_RECONNECT_CLEAR_BUFFER", "0").strip().lower()
    return raw not in ("0", "false", "no", "off", "none")


def _normalize_payload_ts_ms(ts_raw: int) -> int:
    """Polymarket 文档为毫秒；若收到秒级时间戳（常见误解析），乘 1000。"""
    tr = int(ts_raw)
    if tr <= 0:
        return 0
    if tr < 1_000_000_000_000:
        return tr * 1000
    return tr


def _subscribe_msg() -> str:
    sub = {
        "topic": "crypto_prices_chainlink",
        "type": "*",
        "filters": json.dumps({"symbol": SYMBOL}),
    }
    return json.dumps({"action": "subscribe", "subscriptions": [sub]})


class ChainlinkBtcUsdRtds:
    """
    Background WebSocket client; buffers (oracle_timestamp_ms, value) for btc/usd.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        if websocket is None:
            raise RuntimeError("install websocket-client: pip install websocket-client")
        self._url = (url or os.environ.get("POLY_RTDS_WS", DEFAULT_WS)).strip()
        self._on_status = on_status
        self._lock = threading.Lock()
        self._ticks: List[Tuple[int, float]] = []
        self._stop = threading.Event()
        self._ws: Any = None
        self._thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
        # WS 健康：任意文本帧 vs 成功写入 btc/usd（用于判断「线活着但无 oracle」）
        self._last_frame_rx_wall: float = 0.0
        self._last_btc_tick_rx_wall: float = 0.0
        self._last_pong_rx_wall: float = 0.0
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_forced_reconnect_wall: float = 0.0
        self._connect_epoch_wall: float = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="rtds-ws", daemon=True)
        self._thread.start()
        self._ping_thread = threading.Thread(target=self._ping_loop, name="rtds-ping", daemon=True)
        self._ping_thread.start()
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, name="rtds-watchdog", daemon=True
            )
            self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _status(self, s: str) -> None:
        if self._on_status:
            try:
                self._on_status(s)
            except Exception:
                pass

    def _trim(self) -> None:
        cutoff = int(time.time() * 1000) - KEEP_HISTORY_MS
        self._ticks = [(t, v) for t, v in self._ticks if t >= cutoff]

    def _record(self, ts_ms: int, value: float) -> None:
        if ts_ms <= 0 or value <= 0:
            return
        with self._lock:
            self._ticks.append((ts_ms, value))
            self._last_btc_tick_rx_wall = time.time()
            self._trim()

    def _on_open(self, ws: Any) -> None:
        with self._lock:
            self._connect_epoch_wall = time.time()
        self._status("已连接")
        try:
            ws.send(_subscribe_msg())
        except Exception as e:
            self._status(f"订阅发送失败:{e}")

    def _on_message(self, _: Any, message: Any) -> None:
        if not message or message == "pong":
            if message == "pong":
                with self._lock:
                    self._last_pong_rx_wall = time.time()
            return
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                message = bytes(message).decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return
        with self._lock:
            self._last_frame_rx_wall = time.time()
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._parse_update(item)
            return
        if isinstance(data, dict):
            self._parse_update(data)

    def _parse_update(self, data: dict[str, Any]) -> None:
        # 实测：订阅 chainlink 后首包快照常为 topic=crypto_prices + symbol=btc/usd + data[]
        topic = data.get("topic")
        if topic not in ("crypto_prices_chainlink", "crypto_prices"):
            return
        pl = data.get("payload")
        if not isinstance(pl, dict):
            return
        sym = str(pl.get("symbol", "")).lower()
        # 历史快照常为 payload = { "data": [...] }；symbol 可能缺失，非空时必须为 btc/usd
        raw_data = pl.get("data")
        if isinstance(raw_data, list) and len(raw_data) > 0:
            if topic == "crypto_prices" and sym != SYMBOL:
                return
            if topic == "crypto_prices_chainlink" and sym and sym != SYMBOL:
                return
            for row in raw_data:
                if not isinstance(row, dict):
                    continue
                try:
                    ts_ms = _normalize_payload_ts_ms(int(row["timestamp"]))
                    val = float(row["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                self._record(ts_ms, val)
            return
        # 实时更新：payload = { symbol, timestamp, value }
        if sym != SYMBOL:
            return
        try:
            ts_ms = _normalize_payload_ts_ms(int(pl["timestamp"]))
            val = float(pl["value"])
        except (KeyError, TypeError, ValueError):
            return
        self._record(ts_ms, val)

    def _on_error(self, _: Any, error: Any) -> None:
        self._status(f"错误:{error}")

    def _on_close(self, *_args: Any) -> None:
        self._status("已断开")

    def _ping_loop(self) -> None:
        while not self._stop.is_set():
            w = self._ws
            try:
                if w:
                    w.send("ping")
            except Exception:
                pass
            self._stop.wait(5.0)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:

                def on_open(ws: Any) -> None:
                    self._ws = ws
                    self._on_open(ws)

                self._ws = websocket.WebSocketApp(
                    self._url,
                    on_open=on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=0, ping_timeout=None)
            except Exception as e:
                self._status(f"连接循环异常:{e}")
            if self._stop.is_set():
                break
            time.sleep(2.0)

    def _payload_lag_wall_s(self) -> Optional[float]:
        """缓冲内最大 payload 时间戳相对本机墙钟的滞后（秒）；无 tick 返回 None。"""
        with self._lock:
            if not self._ticks:
                return None
            mx = max(t for t, _ in self._ticks)
        now_ms = int(time.time() * 1000)
        return (now_ms - mx) / 1000.0

    def _btc_tick_rx_age_s(self) -> Optional[float]:
        """距上次成功写入 btc/usd 的墙钟秒数；从未写入返回 None。"""
        with self._lock:
            tw = self._last_btc_tick_rx_wall
        if tw <= 0.0:
            return None
        return time.time() - tw

    def _force_reconnect(self, reason: str) -> None:
        """主动断线以触发 _run_loop 内重连；默认清空缓冲避免陈旧 oracle 长期占位（可关 RTDS_RECONNECT_CLEAR_BUFFER）。"""
        self._last_forced_reconnect_wall = time.time()
        cleared = _rtds_reconnect_clear_buffer()
        if cleared:
            with self._lock:
                self._ticks.clear()
        buf_note = "已清空缓冲" if cleared else "保留 tick 缓冲"
        msg = f"watchdog 重连：{reason}"
        self._status(msg)
        print(
            f"[RTDS] {msg}（{buf_note}；无需重启 bot，仍优先 RTDS、缺数时走 Binance）",
            flush=True,
        )
        try:
            w = self._ws
            if w:
                w.close()
        except Exception:
            pass

    def _watchdog_loop(self) -> None:
        """仅在「久未写入 btc/usd」时重连；payload 墙钟滞后不作为判据（避免误触发）。"""
        interval = 12.0
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            thr = _rtds_auto_reconnect_stale_s()
            if thr <= 0:
                continue
            with self._lock:
                epoch = self._connect_epoch_wall
            if epoch <= 0.0:
                continue
            if time.time() - epoch < _rtds_watchdog_grace_s():
                continue
            if time.time() - self._last_forced_reconnect_wall < _rtds_auto_reconnect_min_interval_s():
                continue
            rx_ago = self._btc_tick_rx_age_s()
            if rx_ago is not None and rx_ago > thr:
                self._force_reconnect(f"久未写入 btc/usd 约 {rx_ago:.0f}s（阈值 {thr:.0f}s）")

    def wait_for_ticks(self, min_count: int = 1, timeout_s: float = 15.0, poll_s: float = 0.05) -> bool:
        """阻塞直到缓冲内至少有 min_count 条 tick，或超时。用于主线程等首包快照。"""
        deadline = time.time() + max(0.1, timeout_s)
        while time.time() < deadline:
            with self._lock:
                if len(self._ticks) >= min_count:
                    return True
            time.sleep(poll_s)
        return False

    def ws_health_summary(self) -> dict[str, Any]:
        """
        判断「WS 好了没」的粗指标（墙钟）：
        - last_frame_rx_s_ago：上次收到任意文本 WS 帧
        - last_btc_tick_rx_s_ago：上次成功写入 btc/usd 到缓冲
        - last_pong_rx_s_ago：服务端 pong（若有）
        """
        t = time.time()
        with self._lock:
            fw = self._last_frame_rx_wall
            tw = self._last_btc_tick_rx_wall
            pw = self._last_pong_rx_wall
        return {
            "last_frame_rx_s_ago": None if fw <= 0 else round(t - fw, 2),
            "last_btc_tick_rx_s_ago": None if tw <= 0 else round(t - tw, 2),
            "last_pong_rx_s_ago": None if pw <= 0 else round(t - pw, 2),
        }

    def ws_health_line(self) -> str:
        """一行中文结论，供 bot 启动/狙击日志打印。"""
        h = self.ws_health_summary()
        fa = h["last_frame_rx_s_ago"]
        ta = h["last_btc_tick_rx_s_ago"]
        pa = h["last_pong_rx_s_ago"]
        with self._lock:
            mx_ts = max((t for t, _ in self._ticks), default=0) if self._ticks else 0
        now_ms = int(time.time() * 1000)
        pay_lag_s = (now_ms - mx_ts) / 1000.0 if mx_ts > 0 else None
        pay_note = ""
        if pay_lag_s is not None and pay_lag_s > 60:
            pay_note = (
                f" | 缓冲内最新 **payload 时间戳** 落后墙钟约 {pay_lag_s:.0f}s "
                "（WS 可能仍活着，但 oracle 样本偏旧，收盘 tick 仍可能缺失）"
            )
        if fa is None:
            return "WS：尚未收到文本帧（连接可能未建立或刚建）" + pay_note
        if ta is None or ta > 120:
            sub = (
                f"WS 有文本帧（距今 {fa:.0f}s）但 btc/usd 缓冲久未更新（{ta if ta is not None else '从未'}）"
                " → 检查订阅/filters 或 RTDS 是否只推了非 chainlink 主题"
            )
        elif ta > 45:
            sub = f"WS 正常收帧；btc/usd 距今 {ta:.0f}s 无新 tick（偏慢，敏感结算可调大等待）"
        else:
            sub = f"WS 正常；btc/usd 缓冲约 {ta:.0f}s 内有更新 → 可认为 WS+解析链路可用"
        pong = f"；pong 距今 {pa:.0f}s" if pa is not None else ""
        return sub + pong + pay_note

    def buffer_stats(self) -> Tuple[int, Optional[int], Optional[int], Optional[float]]:
        """
        缓冲概况：tick 条数、时间戳毫秒最小/最大、最新一条价格（按时间戳最大）。
        用于启动自检与日志（与 Polymarket 页面对照时可知 RTDS 是否在喂数）。
        """
        with self._lock:
            if not self._ticks:
                return 0, None, None, None
            mn = min(t for t, _ in self._ticks)
            mx = max(t for t, _ in self._ticks)
            _ts, val = max(self._ticks, key=lambda x: x[0])
            return len(self._ticks), mn, mx, float(val)

    def latest_price(self) -> Optional[float]:
        """Most recent Chainlink btc/usd tick (highest payload timestamp in buffer)."""
        with self._lock:
            if not self._ticks:
                return None
            _ts, val = max(self._ticks, key=lambda x: x[0])
            return float(val)

    def earliest_tick_at_or_after(self, boundary_unix_s: int) -> Optional[Tuple[int, float]]:
        """≥ 边界的最早一条 (ts_ms, value)；不做滞后校验，供诊断或自定义逻辑。"""
        target_ms = int(boundary_unix_s) * 1000
        with self._lock:
            best: Optional[Tuple[int, float]] = None
            for ts_ms, v in self._ticks:
                if ts_ms >= target_ms and (best is None or ts_ms < best[0]):
                    best = (ts_ms, v)
            return best

    def first_price_at_or_after(
        self,
        boundary_unix_s: int,
        *,
        max_payload_lag_ms: Optional[int] = None,
    ) -> Optional[float]:
        """
        First oracle tick at/after this Unix boundary (inclusive), using payload.timestamp ms.

        max_payload_lag_ms: 若最早一条 ≥ 边界的 tick 满足 (ts_ms - boundary_ms) > 该值，
        则返回 None（常见于 WS 快照从窗口中途才开始、缓冲里缺「窗口起点」那条，误用中段价会偏离页面「目标价」）。
        收盘等场景勿传此参数。
        """
        target_ms = int(boundary_unix_s) * 1000
        with self._lock:
            best: Optional[Tuple[int, float]] = None
            for ts_ms, v in self._ticks:
                if ts_ms >= target_ms and (best is None or ts_ms < best[0]):
                    best = (ts_ms, v)
            if best is None:
                return None
            if max_payload_lag_ms is not None and (best[0] - target_ms) > int(max_payload_lag_ms):
                return None
            return best[1]

    def open_price_before_boundary_fallback(self, boundary_unix_s: int) -> Optional[float]:
        """
        When no tick exists with ts >= boundary (sparse Chainlink / alignment gap),
        use the latest tick strictly before the boundary, only if within
        RTDS_OPEN_FALLBACK_MAX_MS of the boundary (default 30000 = 30s).
        默认从 180s 收紧：否则会选「窗口起点前两分钟」的旧 oracle，与页面目标价偏差可达数十美元。
        """
        target_ms = int(boundary_unix_s) * 1000
        max_before_ms = int(float(os.environ.get("RTDS_OPEN_FALLBACK_MAX_MS", "30000")))
        with self._lock:
            best_before: Optional[Tuple[int, float]] = None
            for ts_ms, v in self._ticks:
                if ts_ms >= target_ms:
                    continue
                if target_ms - ts_ms > max_before_ms:
                    continue
                if best_before is None or ts_ms > best_before[0]:
                    best_before = (ts_ms, v)
            return best_before[1] if best_before else None

    def open_price_at_boundary(self, boundary_unix_s: int) -> Optional[Tuple[int, float]]:
        """
        Returns (ts_ms, price) of the first Chainlink tick with ts_ms >= boundary_unix_s * 1000.
        **不检查 lag**：不管这条 tick 晚到多少秒都返回。
        这与 Polymarket 页面「目标价」逻辑一致（取窗口边界后第一条 Chainlink oracle 更新）。
        参考你的 WS demo：sorted_buffer[0] = 边界后最早那条。
        """
        target_ms = int(boundary_unix_s) * 1000
        with self._lock:
            best: Optional[Tuple[int, float]] = None
            for ts_ms, v in self._ticks:
                if ts_ms >= target_ms and (best is None or ts_ms < best[0]):
                    best = (ts_ms, v)
            return best

    def diagnose_rtds_open_buffer(self, boundary_unix_s: int) -> str:
        """供 RTDS_FALLBACK_DEBUG=1 打印：为何可能拿不到开盘价（缓冲与时间轴）。"""
        target_ms = int(boundary_unix_s) * 1000
        now_ms = int(time.time() * 1000)
        max_before = int(float(os.environ.get("RTDS_OPEN_FALLBACK_MAX_MS", "30000")))
        with self._lock:
            if not self._ticks:
                return (
                    f"缓冲内 tick=0（未收到或未解析到 btc/usd）；"
                    f"窗口边界毫秒={target_ms} 本机当前毫秒={now_ms}"
                )
            mn = min(t for t, _ in self._ticks)
            mx = max(t for t, _ in self._ticks)
            n_ge = sum(1 for t, _ in self._ticks if t >= target_ms)
            before_ok = [t for t, _ in self._ticks if t < target_ms and target_ms - t <= max_before]
            return (
                f"tick数={len(self._ticks)} 缓冲内时间戳毫秒[最小={mn}, 最大={mx}] "
                f">=边界的条数={n_ge} 边界前{max_before}ms内可回补条数={len(before_ok)} "
                f"边界毫秒={target_ms} 本机毫秒={now_ms}"
            )

    def wait_first_price_at_or_after(
        self,
        boundary_unix_s: int,
        timeout_s: float = 90.0,
        poll_s: float = 0.05,
        *,
        max_payload_lag_ms: Optional[int] = None,
    ) -> float:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            px = self.first_price_at_or_after(
                boundary_unix_s, max_payload_lag_ms=max_payload_lag_ms
            )
            if px is not None:
                return float(px)
            time.sleep(poll_s)
        raise TimeoutError(f"no chainlink tick >= {boundary_unix_s} within {timeout_s}s")


if __name__ == "__main__":
    """快速自检：约 15s 内看是否收到 btc/usd tick（需网络与 websocket-client）。"""
    import sys

    print("RTDS 自检：连接 wss 并订阅 crypto_prices_chainlink / btc/usd …", flush=True)
    feed: Optional[ChainlinkBtcUsdRtds] = None
    try:
        feed = ChainlinkBtcUsdRtds(on_status=lambda m: print(f"  [状态] {m}", flush=True))
        feed.start()
        ok = feed.wait_for_ticks(1, timeout_s=15.0)
        n, mn, mx, lp = feed.buffer_stats()
        if ok and n > 0:
            print(
                f"  [结果] 可用：tick={n} 时间戳ms范围=[{mn}, {mx}] 最新价={lp}",
                flush=True,
            )
            print(f"  [WS] {feed.ws_health_line()}", flush=True)
            print(f"  [WS] json={json.dumps(feed.ws_health_summary())}", flush=True)
            sys.exit(0)
        print(f"  [结果] 15s 内无 tick（tick={n}）。检查网络、防火墙或 POLY_RTDS_WS。", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"  [结果] 失败: {e}", flush=True)
        sys.exit(2)
    finally:
        if feed is not None:
            try:
                feed.stop()
            except Exception:
                pass
