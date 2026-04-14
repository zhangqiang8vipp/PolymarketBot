"""
Polymarket BTC 5 分钟 Up/Down 狙击机器人（完整逻辑见 TRADING_AND_SYSTEM_LOGIC.md）。
"""

from __future__ import annotations

import argparse
import builtins
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from backtest import fetch_btc_spot_price_usdt, fetch_klines_1m
from strategy import AnalysisResult, Candle, analyze

try:
    from chainlink_rtds import ChainlinkBtcUsdRtds
except ImportError:
    ChainlinkBtcUsdRtds = None  # type: ignore

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
except ImportError:
    ClobClient = None  # type: ignore

load_dotenv()

# 结构化/分级日志（控制台 + 可选 LOG_FILE）；关键中文提示仍可用 print。
log = logging.getLogger("pm.bot")


_LOGGING_SETUP = False


def setup_logging() -> None:
    """LOG_LEVEL=DEBUG|INFO|WARNING；LOG_FILE=路径 时额外写文件。"""
    global _LOGGING_SETUP
    if _LOGGING_SETUP:
        return
    _LOGGING_SETUP = True
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%H:%M:%S"
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        root.addHandler(sh)
    log_path = os.environ.get("LOG_FILE", "").strip()
    if log_path:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(fh)
    # 第三方库往 root 打 INFO/ERROR，易被误认为「机器人卡住」；需要排 WS 时再设 WEBSOCKET_LOG=1
    if os.environ.get("WEBSOCKET_LOG", "").strip().lower() not in ("1", "true", "yes", "on"):
        logging.getLogger("websocket").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _ensure_utf8_stdio() -> None:
    """Windows 控制台默认编码常非 UTF-8，含非 ASCII 的 print 会报错。"""
    if sys.platform != "win32":
        return
    import io

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        enc = (getattr(stream, "encoding", None) or "").lower()
        if enc == "utf-8":
            try:
                if hasattr(stream, "reconfigure") and (getattr(stream, "errors", None) or "") != "replace":
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            continue
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            try:
                setattr(
                    sys,
                    name,
                    io.TextIOWrapper(
                        buf,
                        encoding="utf-8",
                        errors="replace",
                        line_buffering=name == "stdout",
                    ),
                )
                continue
            except Exception:
                pass
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_ensure_utf8_stdio()

_PRINT_TS_HOOK = False


def _log_wall_ms() -> str:
    """本地 wall 时间 HH:MM:SS.mmm（毫秒三位）。"""
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _install_log_timestamp_print() -> None:
    """
    为后续所有 print 行前加 [HH:MM:SS.mmm]。
    环境变量 LOG_TS_MS=0|false|off 可关闭（默认开启）。
    """
    global _PRINT_TS_HOOK
    if _PRINT_TS_HOOK:
        return
    if os.environ.get("LOG_TS_MS", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    _orig = builtins.print

    def _print_with_ts(*args: Any, **kwargs: Any) -> None:
        _orig(f"[{_log_wall_ms()}]", *args, **kwargs)

    builtins.print = _print_with_ts
    _PRINT_TS_HOOK = True


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"

# 干跑/实盘收盘后处理与主线程并发时，保护 bankroll / trades 等字段。
_BOT_STATE_LOCK = threading.RLock()

WINDOW = 300
SNIPE_DEADLINE = 5
POLL = 2.0
ORDER_RETRY = 3.0
SPIKE_JUMP = 1.5
MIN_SHARES_POLY = 5
GTC_LIMIT_PRICE = 0.95


def _snipe_start_s() -> int:
    """
    距收盘多少秒开始进入狙击轮询（默认 10）。环境变量 SNIPE_START 可改，须大于 SNIPE_DEADLINE。
    """
    raw = os.environ.get("SNIPE_START", "10").strip()
    try:
        v = int(round(float(raw)))
    except ValueError:
        v = 10
    lo = SNIPE_DEADLINE + 1
    hi = WINDOW - 5
    return max(lo, min(v, hi))


def _snipe_price_source() -> str:
    """SNIPE_PRICE_SOURCE=oracle|binance（默认 oracle）；非法值按 oracle。"""
    raw = os.environ.get("SNIPE_PRICE_SOURCE", "oracle").strip().lower()
    return raw if raw in ("oracle", "binance") else "oracle"


def _enable_arbitrage_log() -> bool:
    """ENABLE_ARBITRAGE_LOG=1/true/on 时仅打印 Up/Down 双边 best ask 与价差预警，不下单。"""
    v = os.environ.get("ENABLE_ARBITRAGE_LOG", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _arbitrage_sum_alert() -> float:
    """ARBITRAGE_SUM_ALERT：Up ask + Down ask 低于该值时打 ARBITRAGE_ALERT（默认 0.99）。"""
    raw = os.environ.get("ARBITRAGE_SUM_ALERT", "0.99").strip()
    try:
        v = float(raw)
    except ValueError:
        return 0.99
    if not (0.0 < v <= 1.5):
        return 0.99
    return v


def _arbitrage_poll_interval_s() -> float:
    """狙击阶段内重复探测双边卖一的间隔（秒）；0 表示仅在周期开头测一次（旧行为）。"""
    raw = os.environ.get("ARBITRAGE_POLL_S", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _enable_arbitrage_trade() -> bool:
    """ENABLE_ARBITRAGE_TRADE=1/true/on 且非 dry-run、有 client 时，在价差触发下双边 FOK（各一半美元）。"""
    v = os.environ.get("ENABLE_ARBITRAGE_TRADE", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _arbitrage_trade_usd() -> float:
    """双边套利总美元：优先 ARBITRAGE_TRADE_USD，其次 MAX_USD，默认 10。"""
    for key in ("ARBITRAGE_TRADE_USD", "MAX_USD"):
        raw = os.environ.get(key, "").strip()
        if raw:
            try:
                return max(0.01, float(raw))
            except ValueError:
                break
    return 10.0


def _max_directional_usd() -> Optional[float]:
    """方向单名义上限：仅读 MAX_USD；未设置则不封顶。"""
    raw = os.environ.get("MAX_USD", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _fixed_directional_usd() -> Optional[float]:
    """
    若设置 FIXED_DIRECTIONAL_USD（正数），方向单名义固定为该值，不随 degen/safe/Kelly 随资金放大；
    仍会与 MAX_USD、当前 bankroll、MIN_BET 取约束。不设则走原有逻辑。
    """
    raw = os.environ.get("FIXED_DIRECTIONAL_USD", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _enable_kelly() -> bool:
    """ENABLE_KELLY=1：方向单用 confidence + quarter-Kelly（见 _kelly_scale）。"""
    return os.environ.get("ENABLE_KELLY", "0").strip() == "1"


def _kelly_scale() -> float:
    """全 Kelly 分数前的乘子：默认 0.25 = quarter-Kelly。可用 KELLY_SCALE=0.125 等调整。"""
    raw = os.environ.get("KELLY_SCALE", "0.25").strip()
    try:
        v = float(raw)
        return max(0.001, min(1.0, v))
    except ValueError:
        return 0.25


def _kelly_directional_bet(
    bankroll: float,
    confidence: float,
    min_bet: float,
    max_usd: Optional[float],
) -> Optional[float]:
    """
    edge := confidence（0~1）。名义 stake = bankroll * KELLY_SCALE * edge（默认 KELLY_SCALE=0.25）。
    再与 MAX_USD、bankroll 取 min。若最终 < MIN_BET 返回 None。

    可选 KELLY_MODE=binary：改用 even-money 全 Kelly 分数 f*=max(0,2p-1)，名义=bankroll*KELLY_SCALE*f*。
    """
    p = min(1.0, max(0.0, float(confidence)))
    ks = _kelly_scale()
    mode = os.environ.get("KELLY_MODE", "linear").strip().lower()
    if mode == "binary":
        f_full = max(0.0, 2.0 * p - 1.0)
        f_eff = ks * f_full
        edge = p
    else:
        f_full = 0.0
        f_eff = ks * p
        edge = p
    calc = bankroll * f_eff
    bet = min(calc, bankroll)
    if max_usd is not None:
        bet = min(bet, max_usd)
    cap_s = "无上限" if max_usd is None else f"{max_usd:.4f}"
    print(
        f"[凯利] 模式={mode} 置信度={p:.4f} 边际(edge)={edge:.4f} Kelly乘子={ks:.4f} "
        f"有效Kelly比例={f_eff:.4f} 计算名义={calc:.4f} 名义上限(max_usd)={cap_s} 最终下注={bet:.4f}"
    )
    if bet < min_bet:
        print(f"[凯利] 跳过：最终下注 {bet:.4f} < 最小下单(MIN_BET) {min_bet}")
        return None
    return float(bet)


def _clob_host() -> str:
    return os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")


def now() -> float:
    return time.time()


def current_window_ts(t: Optional[float] = None) -> int:
    tt = int(t if t is not None else time.time())
    return tt - (tt % WINDOW)


def window_slug(window_ts: int) -> str:
    return f"btc-updown-5m-{window_ts}"


def fetch_btc_price() -> float:
    return fetch_btc_spot_price_usdt()


def get_best_ask(token_id: str, client: Optional[Any]) -> Optional[float]:
    """CLOB 最优卖价（买 Up/Down 一侧的最低 ask）。client 为 None 时用公开 GET /book。"""
    if not token_id:
        return None
    if client is not None:
        try:
            book = client.get_order_book(token_id)
            asks = book.asks or []
            if not asks:
                return None
            return float(asks[0].price)
        except Exception:
            pass
    try:
        r = requests.get(f"{_clob_host()}/book", params={"token_id": token_id}, timeout=15)
        r.raise_for_status()
        data = r.json()
        asks = data.get("asks") or []
        if not asks:
            return None
        return float(asks[0]["price"])
    except Exception:
        return None


def _direction_orderbook_max_sum() -> Optional[float]:
    """DIRECTION_ORDERBOOK_MAX_SUM=1.05：双边卖一合计大于该值则不做方向单（防「1.98 还送钱」）。"""
    raw = os.environ.get("DIRECTION_ORDERBOOK_MAX_SUM", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _direction_only_when_book_sum_lt() -> Optional[float]:
    """
    DIRECTION_ONLY_WHEN_BOOK_SUM_LT=0.99：仅当 up_ask+down_ask < 该值才允许方向单；
    用于「只吃明显偏松盘 / 有 mispricing 才赌方向」类策略。
    """
    raw = os.environ.get("DIRECTION_ONLY_WHEN_BOOK_SUM_LT", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _direction_strategy() -> str:
    """
    DIRECTION_STRATEGY=
      ta（默认）|reversal：反转偏离 |imbalance：前 N 档盘口失衡定方向。
    """
    v = os.environ.get("DIRECTION_STRATEGY", "ta").strip().lower()
    if v in ("reversal", "imbalance"):
        return v
    return "ta"


def _reversal_min_abs_pct() -> float:
    """REVERSAL_MIN_ABS_PCT：反转策略要求的最小 |窗口偏离%%|，默认 0.08（即 0.08%%）。"""
    raw = os.environ.get("REVERSAL_MIN_ABS_PCT", "0.08").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.08


def _use_book_ask_for_entry() -> bool:
    """USE_BOOK_ASK_FOR_ENTRY=1：方向单 entry 用真实 best ask，不用模型曲线。"""
    return os.environ.get("USE_BOOK_ASK_FOR_ENTRY", "").strip().lower() in ("1", "true", "yes", "on")


def _min_decision_confidence() -> float:
    """MIN_DECISION_CONFIDENCE=0.2：仅 DIRECTION_STRATEGY=ta 时，低于则跳过（反转模式用合成置信度）。"""
    raw = os.environ.get("MIN_DECISION_CONFIDENCE", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.0


def _spike_jump() -> float:
    """SPIKE_JUMP：尖峰阈值；设为 999 等可实质关闭尖峰提前下单。"""
    raw = os.environ.get("SPIKE_JUMP", "").strip()
    if not raw:
        return float(SPIKE_JUMP)
    try:
        return float(raw)
    except ValueError:
        return float(SPIKE_JUMP)


def should_trade_by_orderbook_for_direction(
    up_ask: Optional[float],
    down_ask: Optional[float],
    *,
    max_sum: float,
) -> bool:
    if up_ask is None or down_ask is None:
        print("[过滤] 方向单：盘口不全，跳过", flush=True)
        return False
    total = float(up_ask) + float(down_ask)
    if total > max_sum:
        print(f"[过滤] 盘口过贵 sum={total:.3f} > {max_sum} → 跳过方向单", flush=True)
        return False
    if total < 0.98:
        print(f"[优势] 盘口合计={total:.3f} < 0.98（偏松或存在套利空间）", flush=True)
    return True


def decide_reversal_direction(
    window_open: float,
    current_price: float,
    *,
    min_abs_pct: float,
) -> int:
    """
    反转：已涨相对开盘则押 Down(-1)，已跌则押 Up(1)；|偏离|过小返回 0 不交易。
    min_abs_pct 与 w_pct 同量纲：(current-open)/open*100。
    """
    if window_open <= 0 or current_price <= 0:
        return 0
    w_pct = (current_price - window_open) / window_open * 100.0
    if abs(w_pct) <= min_abs_pct:
        return 0
    return -1 if w_pct > 0 else 1


def entry_from_best_asks(direction: int, up_ask: Optional[float], down_ask: Optional[float]) -> Optional[float]:
    if direction == 1:
        return float(up_ask) if up_ask is not None else None
    return float(down_ask) if down_ask is not None else None


def _level_size(level: Any) -> float:
    """CLOB 单层 size：兼容 py_clob 对象 / dict / [price, size] 列表。"""
    if level is None:
        return 0.0
    try:
        if hasattr(level, "size"):
            return float(level.size)
        if isinstance(level, dict):
            for k in ("size", "sz", "amount"):
                if k in level and level[k] is not None:
                    return float(level[k])
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return float(level[1])
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def get_orderbook_imbalance(token_id: str, client: Optional[Any], depth: int = 3) -> Optional[float]:
    """
    前 depth 档 bid/ask 量加总，返回 (bid_vol - ask_vol) / (bid_vol + ask_vol)，∈[-1,1]。
    无 client 时用公开 GET /book；失败返回 None。
    """
    if not token_id or depth < 1:
        return None
    bids: List[Any] = []
    asks: List[Any] = []
    if client is not None:
        try:
            ob = client.get_order_book(token_id)
            bids = list(ob.bids or [])
            asks = list(ob.asks or [])
        except Exception as e:
            log.debug("get_orderbook_imbalance client: %s", e)
            return None
    else:
        try:
            r = requests.get(f"{_clob_host()}/book", params={"token_id": token_id}, timeout=15)
            r.raise_for_status()
            data = r.json()
            bids = list(data.get("bids") or [])
            asks = list(data.get("asks") or [])
        except Exception as e:
            log.debug("get_orderbook_imbalance rest: %s", e)
            return None
    bid_vol = sum(_level_size(x) for x in bids[:depth])
    ask_vol = sum(_level_size(x) for x in asks[:depth])
    tot = bid_vol + ask_vol
    if tot <= 0:
        return 0.0
    return (bid_vol - ask_vol) / tot


def _imbalance_depth() -> int:
    raw = os.environ.get("ORDERBOOK_IMBALANCE_DEPTH", "3").strip()
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return 3


def _imbalance_threshold() -> float:
    raw = os.environ.get("IMBALANCE_THRESHOLD", "0.25").strip()
    try:
        return max(0.01, min(float(raw), 0.99))
    except ValueError:
        return 0.25


def decide_from_imbalance(imb_up: float, imb_down: float, threshold: float) -> int:
    """单侧失衡超阈才给方向；双侧同时过阈视为噪音→0。"""
    u = imb_up > threshold
    d = imb_down > threshold
    if u and d:
        return 0
    if u:
        return 1
    if d:
        return -1
    return 0


def estimate_fair_prob(window_open: float, current_price: float) -> float:
    """用 (现价-开盘)/开盘 经 sigmoid 得「涨」概率估计 ∈(0,1)。"""
    if window_open <= 0 or current_price <= 0:
        return 0.5
    pct = (current_price - window_open) / window_open
    try:
        scale = float(os.environ.get("FAIR_PROB_SIGMOID_SCALE", "50").strip())
    except ValueError:
        scale = 50.0
    scale = max(1.0, min(scale, 500.0))
    return 1.0 / (1.0 + math.exp(-pct * scale))


def has_price_edge(
    direction: int,
    price: float,
    fair_prob: float,
    min_edge: float,
) -> Tuple[bool, float]:
    """
    direction=1 买 Up：edge = fair_prob - price；
    direction=-1 买 Down：edge = (1-fair_prob) - price。
    """
    if direction == 1:
        edge = float(fair_prob) - float(price)
    else:
        edge = (1.0 - float(fair_prob)) - float(price)
    return edge > float(min_edge), edge


def _min_price_edge() -> float:
    raw = os.environ.get("MIN_PRICE_EDGE", "0.03").strip()
    try:
        return max(0.0, min(float(raw), 0.5))
    except ValueError:
        return 0.03


def _use_fair_prob_edge() -> bool:
    return os.environ.get("USE_FAIR_PROB_EDGE", "").strip().lower() in ("1", "true", "yes", "on")


def _use_edge_position_sizing() -> bool:
    return os.environ.get("USE_EDGE_POSITION_SIZING", "").strip().lower() in ("1", "true", "yes", "on")


def _edge_sizing_bankroll_frac() -> float:
    raw = os.environ.get("EDGE_SIZING_BANKROLL_FRAC", "0.02").strip()
    try:
        return max(0.001, min(float(raw), 0.5))
    except ValueError:
        return 0.02


def _edge_sizing_edge_scale() -> float:
    raw = os.environ.get("EDGE_SIZING_EDGE_SCALE", "10").strip()
    try:
        return max(0.1, min(float(raw), 100.0))
    except ValueError:
        return 10.0


def size_by_edge(
    bankroll: float,
    edge: float,
    max_usd: Optional[float],
    min_bet: float,
) -> float:
    k = min(1.0, max(0.0, float(edge)) * _edge_sizing_edge_scale())
    frac = _edge_sizing_bankroll_frac()
    bet = float(bankroll) * frac * k
    if max_usd is not None:
        bet = min(bet, float(max_usd))
    bet = min(bet, float(bankroll))
    return max(float(min_bet), bet)


def _min_seconds_before_close_for_trade() -> Optional[float]:
    raw = os.environ.get("MIN_SECONDS_BEFORE_CLOSE_FOR_TRADE", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _loss_streak_cooldown_enabled() -> bool:
    return os.environ.get("LOSS_STREAK_COOLDOWN", "").strip().lower() in ("1", "true", "yes", "on")


def _loss_streak_should_pause(state: BotState) -> bool:
    """干跑：最近 N 条结算里输 ≥ M 且总笔数已够则暂停一轮。"""
    if not state.dry_run or not _loss_streak_cooldown_enabled():
        return False
    try:
        min_tr = int(os.environ.get("LOSS_STREAK_MIN_TRADES", "6").strip())
    except ValueError:
        min_tr = 6
    if state.trades < min_tr:
        return False
    try:
        window_n = int(os.environ.get("LOSS_STREAK_WINDOW", "5").strip())
    except ValueError:
        window_n = 5
    window_n = max(2, min(window_n, 50))
    try:
        max_loss = int(os.environ.get("LOSS_STREAK_MAX_LOSSES", "4").strip())
    except ValueError:
        max_loss = 4
    settles = [h for h in state.dry_history if h.get("kind") == "directional_settle"]
    if len(settles) < window_n:
        return False
    recent = settles[-window_n:]
    losses = sum(1 for x in recent if not bool(x.get("win")))
    return losses >= max_loss


def execute_arbitrage_trade(client: Any, up_tid: str, down_tid: str, bet_usd: float) -> bool:
    """双边各一半美元 FOK 市价买；仅当两腿都 post 成功返回 True（单边成功会打警告）。"""
    half = bet_usd / 2.0
    ok_a = ok_b = False
    for label, tid in (("Up", up_tid), ("Down", down_tid)):
        leg_cn = "涨(Up)" if label == "Up" else "跌(Down)"
        try:
            mo = MarketOrderArgs(
                token_id=tid,
                amount=float(half),
                side="BUY",
                price=0.0,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            print(f"[套利交易] {leg_cn} 金额(美元)={half:.4f} 已提交 响应={resp!r}")
            if label == "Up":
                ok_a = True
            else:
                ok_b = True
        except Exception as e:
            print(f"[套利交易] {leg_cn} 失败: {e}")
    if ok_a ^ ok_b:
        print(
            "[套利交易] 警告：仅成交一条腿，请手动核对敞口；"
            "程序未按部分成交调整资金。"
        )
    return ok_a and ok_b


class ArbitrageCycleDone(Exception):
    """本窗口狙击过程中套利已成交，不再继续方向单。"""


def log_up_down_ask_spread(
    window_ts: int,
    up_tid: str,
    down_tid: str,
    client: Optional[Any],
    dry_run: bool,
    state: BotState,
    silent: bool = False,
) -> bool:
    """
    Up/Down 双边 best ask 检测。
    - ENABLE_ARBITRAGE_LOG=1：打印日志与告警。
    - ENABLE_ARBITRAGE_TRADE=1 且非 dry-run、有 client：当 sum < ARBITRAGE_SUM_ALERT 时双边 FOK；
      两腿都成功则扣减 bankroll 并返回 True（本窗口跳过方向单）。
    silent=True：周期性轮询时少打常规 [套利日志]，仍打 [套利告警] 与下单相关输出。
    """
    log_ok = _enable_arbitrage_log()
    trade_ok = _enable_arbitrage_trade() and client is not None and not dry_run
    if not log_ok and not trade_ok:
        return False

    thresh = _arbitrage_sum_alert()
    up_ask = get_best_ask(up_tid, client)
    down_ask = get_best_ask(down_tid, client)
    if up_ask is None or down_ask is None:
        if log_ok and not silent:
            print(
                f"[套利日志] 窗口={window_ts} "
                f"涨卖一={up_ask} 跌卖一={down_ask}（盘口不全，跳过价差合计）"
            )
        elif log_ok and silent:
            # 后台轮询时默认也打一行，否则长时间无输出会误以为线程未跑
            print(
                f"[套利/后台] 窗口={window_ts} 盘口不全 涨卖一={up_ask} 跌卖一={down_ask} "
                f"（检查 token / CLOB_HOST / 网络；仍会继续轮询）",
                flush=True,
            )
        return False

    total = up_ask + down_ask
    edge = 1.0 - total
    if log_ok and silent:
        if os.environ.get("ARBITRAGE_POLL_SUMMARY", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        ):
            print(
                f"[套利/后台] 窗口={window_ts} "
                f"涨卖一={up_ask:.4f} 跌卖一={down_ask:.4f} 合计={total:.4f} "
                f"(告警阈值合计<{thresh:.4f})；ARBITRAGE_POLL_SUMMARY=0 可关闭本行",
                flush=True,
            )
        else:
            print(
                f"[套利/后台] 窗口={window_ts} "
                f"涨卖一={up_ask:.4f} 跌卖一={down_ask:.4f} 合计={total:.4f}（简要行已关，此为心跳）",
                flush=True,
            )
    if log_ok and not silent:
        print(
            f"[套利日志] 窗口={window_ts} "
            f"涨卖一={up_ask:.4f} 跌卖一={down_ask:.4f} 合计={total:.4f} "
            f"相对1.00的隐含优势={edge * 100:+.2f}% 告警阈值(合计低于)={thresh:.4f}"
        )

    if total >= thresh:
        return False

    if log_ok:
        extra = ""
        if trade_ok:
            extra = " 已开启套利实盘开关(ENABLE_ARBITRAGE_TRADE=1)：若资金足够可能自动下双边 FOK。"
        print(
            f"[套利告警] 合计={total:.4f} < {thresh:.4f} "
            f"（扣费前相对 1.00 约 {edge * 100:.2f}% 优势）{extra}"
        )

    if not trade_ok:
        if _enable_arbitrage_trade() and dry_run:
            print("[套利交易] 已跳过：干跑模式（不下真实单）")
        elif _enable_arbitrage_trade() and client is None:
            print("[套利交易] 已跳过：无 CLOB 客户端（未连接实盘）")
        return False

    bet = _arbitrage_trade_usd()
    with _BOT_STATE_LOCK:
        if state.bankroll < bet:
            print(f"[套利交易] 已跳过：资金 {state.bankroll:.4f} < 所需 {bet:.4f}")
            return False

    est = edge * bet
    print(
        f"[套利交易] 正在下双边 FOK，合计美元={bet:.4f} "
        f"（每边 {bet/2:.4f}）估算优势(美元)~{est:.4f}（粗略，未计手续费/滑点）"
    )
    if execute_arbitrage_trade(client, up_tid, down_tid, bet):
        with _BOT_STATE_LOCK:
            state.bankroll -= bet
            state.trades += 1
        print(f"[套利交易] 完成；资金余额->{state.bankroll:.4f}")
        return True
    return False


def _rtds_snipe_status_suffix(chainlink_feed: Optional[Any]) -> str:
    """狙击阶段日志用：RTDS 是否在喂数。"""
    if chainlink_feed is None:
        return "RTDS=未接"
    stats = getattr(chainlink_feed, "buffer_stats", None)
    if not callable(stats):
        return "RTDS=已接（无 buffer_stats）"
    n, mn, mx, lp = stats()
    if n <= 0:
        return "RTDS=已接但缓冲 tick=0（oracle 现价可能走 Binance）"
    return (
        f"RTDS=已接 tick={n} 最新oracle≈{lp:.2f} "
        f"payload时间戳ms∈[{mn},{mx}]"
    )


def snipe_current_price(chainlink_feed: Optional[Any]) -> float:
    """
    狙击轮询里的现价：由 SNIPE_PRICE_SOURCE 控制。
    - oracle：有 RTDS feed 且 latest_price 可用则用 Chainlink，否则 Binance ticker。
    - binance：始终 Binance ticker（便于对比/复现旧行为）。
    """
    if _snipe_price_source() == "binance":
        return fetch_btc_price()
    if chainlink_feed is not None:
        try:
            px = chainlink_feed.latest_price()
            if px is not None:
                return float(px)
        except Exception:
            pass
    return fetch_btc_price()


def fetch_recent_candles_1m(limit: int = 60) -> List[Candle]:
    return fetch_klines_1m("BTCUSDT", start_ms=None, end_ms=None, limit=limit)


def fetch_window_open_price_binance(window_ts: int) -> float:
    # 与 _binance_window_edge_prices 一致：自窗口起点起取 1 根 1m，不设 endTime，避免与 Binance endTime 语义混用。
    rows = fetch_klines_1m("BTCUSDT", start_ms=window_ts * 1000, end_ms=None, limit=1)
    if not rows:
        raise RuntimeError("缺少窗口开盘价对应的 1 分钟 K 线")
    return float(rows[0].open)


def _rtds_open_max_payload_lag_ms() -> Optional[int]:
    """
    最早一条 ≥ 窗口起点的 Chainlink payload 时间戳，允许比窗口起点晚多少毫秒；
    超出则视为未捕获页面「目标价」那一刻（缓冲常从窗口中途才有 tick），不用该价。
    RTDS_OPEN_MAX_PAYLOAD_LAG_MS=0|off|false|none 关闭检查（恢复旧行为，易与页面偏差）。
    """
    raw = os.environ.get("RTDS_OPEN_MAX_PAYLOAD_LAG_MS", "12000").strip().lower()
    if raw in ("0", "off", "false", "none"):
        return None
    try:
        v = int(float(raw))
    except ValueError:
        return 12000
    return max(1, v)


def _rtds_open_accept_late_tick() -> bool:
    """
    RTDS_OPEN_ACCEPT_LATE_TICK=1：当「最早 ≥ 边界的 tick」已超过 RTDS_OPEN_MAX_PAYLOAD_LAG_MS、
    且无边界前回补时，仍采用该晚到 tick 的价格作开盘价（与页面严格目标价可能不一致，但可避免纯 Binance 混源）。
    """
    return os.environ.get("RTDS_OPEN_ACCEPT_LATE_TICK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _chainlink_window_open_px(feed: Any, window_ts: int) -> Tuple[Optional[float], str]:
    """
    窗口开盘价：优先边界后第一条 tick（可选最大 payload 滞后）；若无则短暂等待；仍无则用边界前最近一条（见 RTDS_OPEN_FALLBACK_MAX_MS）。
    返回 (价格或 None, 人类可读来源子类，便于与 Polymarket 页面对照)。
    """
    lag = _rtds_open_max_payload_lag_ms()
    target_ms = int(window_ts) * 1000
    px = feed.first_price_at_or_after(window_ts, max_payload_lag_ms=lag)
    if px is not None:
        return float(px), "≥窗口起点首条 RTDS Chainlink tick（对齐页面「目标价」思路）"
    skipped_wait_for_late = False
    if lag is not None and hasattr(feed, "earliest_tick_at_or_after"):
        tup = feed.earliest_tick_at_or_after(window_ts)
        if tup is not None and tup[0] - target_ms > lag:
            skipped_wait_for_late = True
            dt_ms = float(tup[0] - target_ms)
            dt_s = dt_ms / 1000.0
            lag_s = float(lag) / 1000.0
            print(
                f"[开盘价] RTDS：最早 ≥ 窗口起点的 tick 的 payload 时间戳比边界晚 {dt_s:.1f}s（{dt_ms:.0f}ms），"
                f"超过允许滞后 RTDS_OPEN_MAX_PAYLOAD_LAG_MS={lag}ms（≈{lag_s:.1f}s）；"
                "疑未收到窗口起点附近样本；不采用严格对齐价、跳过 CHAINLINK_OPEN_WAIT_S 等待，改边界前回补、"
                "晚到 tick 接受（RTDS_OPEN_ACCEPT_LATE_TICK）或 Binance。",
                flush=True,
            )
    if not skipped_wait_for_late:
        try:
            w = float(os.environ.get("CHAINLINK_OPEN_WAIT_S", "5"))
            if w > 0:
                v = float(
                    feed.wait_first_price_at_or_after(
                        window_ts, timeout_s=w, max_payload_lag_ms=lag
                    )
                )
                return v, f"等待 CHAINLINK_OPEN_WAIT_S={w:g}s 后 ≥窗口起点首条 tick"
        except TimeoutError:
            pass
    fb = feed.open_price_before_boundary_fallback(window_ts)
    if fb is not None:
        return float(fb), "窗口起点前最近一条 tick 回补（RTDS_OPEN_FALLBACK_MAX_MS 内，与严格边界价可能略有偏差）"
    if skipped_wait_for_late and _rtds_open_accept_late_tick() and hasattr(
        feed, "earliest_tick_at_or_after"
    ):
        tup2 = feed.earliest_tick_at_or_after(window_ts)
        if tup2 is not None:
            late_ms = float(tup2[0] - target_ms)
            late_s = late_ms / 1000.0
            return float(tup2[1]), (
                f"≥窗口起点首条 RTDS（payload 晚 {late_s:.1f}s / {late_ms:.0f}ms，"
                "RTDS_OPEN_ACCEPT_LATE_TICK=1；与页面「目标价」秒级对齐仍可能偏差）"
            )
    if skipped_wait_for_late:
        return None, (
            "有 tick 但最早 ≥ 起点的 payload 已晚于 RTDS_OPEN_MAX_PAYLOAD_LAG_MS，"
            "且无窗口起点前的 tick 可回补（常见：在本 5m 窗口中途才启动/才连上 RTDS，链上样本从「窗内」才开始）；"
            "可设 RTDS_OPEN_ACCEPT_LATE_TICK=1 仍用晚到首条 Chainlink，或增大 RTDS_OPEN_MAX_PAYLOAD_LAG_MS（毫秒）"
        )
    return None, "缓冲内无 ≥ 边界的 tick（或等待超时）且无边界前回补"


def window_open_oracle(
    window_ts: int,
    feed: Optional[Any],
) -> Tuple[float, str]:
    """
    Price To Beat：优先 Polymarket RTDS Chainlink btc/usd；否则 Binance 1m 开盘价。
    返回 (价格, 来源说明) — 第二项务必打日志，便于判断与网页是否同源。
    """
    if feed is None:
        p = fetch_window_open_price_binance(window_ts)
        return (
            float(p),
            "Binance 1m K 线 BTCUSDT 开盘价（未接 RTDS / 已禁用；与网页 Chainlink 目标价可能不一致）",
        )
    px, how = _chainlink_window_open_px(feed, window_ts)
    if px is not None:
        return float(px), f"Polymarket RTDS — {how}"
    print(
        "[开盘价] 本窗口无法用 RTDS 对齐 Chainlink「起点价」（与启动自检里 WS 是否活着不是一回事："
        "自检看的是**最近**有无 tick；起点价需要**窗口边界附近**的 payload）。原因摘要："
        f"{how} → 回退 Binance 1m K 线（与网页目标价可能偏差）",
        flush=True,
    )
    if os.environ.get("RTDS_FALLBACK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        diag = getattr(feed, "diagnose_rtds_open_buffer", None)
        if callable(diag):
            print(f"[开盘价] RTDS 诊断({window_ts}): {diag(window_ts)}", flush=True)
    else:
        print(
            "[开盘价] 提示：设置 RTDS_FALLBACK_DEBUG=1 可打印缓冲与时间轴诊断",
            flush=True,
        )
    p = fetch_window_open_price_binance(window_ts)
    return (
        float(p),
        f"Binance 1m K 线 BTCUSDT 开盘价（RTDS 无可用：{how}）",
    )


def _binance_window_edge_prices(window_ts: int) -> Tuple[float, float]:
    """
    窗口内 **恰好 WINDOW//60 根** 连续 1m K（BTCUSDT）：
    - open = 第 1 根 open（窗口起点所在分钟）
    - close = **第 WINDOW//60 根** 的 close（窗口内最后一整分钟的收盘）

    旧实现曾把 endTime 扩到窗尾后再 +60s，多取一根「窗后」分钟，误用 k[-1].close，
    常与 Polymarket Chainlink 窗尾价反向（你遇到的 Binance 判涨、页面判跌）。
    """
    start_ms = window_ts * 1000
    n = max(1, WINDOW // 60)
    k = fetch_klines_1m("BTCUSDT", start_ms=start_ms, end_ms=None, limit=n)
    if len(k) < n:
        raise RuntimeError(f"结算用 K 线不足：需要 {n} 根 1m，实际 {len(k)}")
    return float(k[0].open), float(k[n - 1].close)


def resolve_binance_direction(window_ts: int) -> int:
    """1 = Up wins, -1 = Down wins (last 1m close vs first 1m open in window)."""
    o0, c_end = _binance_window_edge_prices(window_ts)
    return 1 if c_end >= o0 else -1


def resolve_window_direction_with_meta(
    window_ts: int,
    feed: Optional[Any],
    *,
    dry_run: bool = False,
) -> Tuple[int, dict[str, Any]]:
    """
    返回 (涨跌结果 1=Up, -1=Down, meta)。
    meta 含 settle_method、用到的 open/close、缺失原因与 RTDS 诊断，便于日志与训练落盘。
    """
    meta: dict[str, Any] = {"window_ts": window_ts}
    if dry_run and os.environ.get("DRY_RUN_BINANCE_SETTLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        print("[结算] 干跑 DRY_RUN_BINANCE_SETTLE：仅用 Binance K 线判定涨跌", flush=True)
        bo, bc = _binance_window_edge_prices(window_ts)
        meta["settle_method"] = "binance_klines_only"
        meta["binance_open"] = bo
        meta["binance_close"] = bc
        meta["open_used"] = bo
        meta["close_used"] = bc
        meta["missing"] = []
        return (1 if bc >= bo else -1), meta

    close_boundary_s = window_ts + WINDOW
    if feed is not None:
        open_px, open_how = _chainlink_window_open_px(feed, window_ts)
        close_px = feed.first_price_at_or_after(close_boundary_s)
        if close_px is None:
            try:
                close_wait = float(os.environ.get("CHAINLINK_CLOSE_WAIT_S", "120"))
                if dry_run:
                    # 默认 90s：Chainlink 收盘 tick 常晚于边界；过小易误落 Binance（你日志里缓冲已早 200s+ 则另见下方诊断）
                    cap = float(os.environ.get("DRY_RUN_CHAINLINK_CLOSE_WAIT_S", "90"))
                    close_wait = min(close_wait, max(0.5, cap))
                print(
                    f"[结算] Chainlink 尚无窗口收盘边界({close_boundary_s})后 tick，"
                    f"轮询等待至多 {close_wait:g}s…",
                    flush=True,
                )
                close_px = feed.wait_first_price_at_or_after(close_boundary_s, timeout_s=close_wait)
            except TimeoutError:
                print(
                    "[结算] Chainlink 收盘 tick 等待超时，将回退 Binance K 线",
                    flush=True,
                )
                close_px = None
        meta["open_rtds"] = open_px
        meta["open_how"] = open_how
        meta["close_rtds"] = close_px
        if open_px is not None and close_px is not None:
            meta["settle_method"] = "rtds_chainlink"
            meta["open_used"] = float(open_px)
            meta["close_used"] = float(close_px)
            return (1 if close_px >= open_px else -1), meta

        missing: List[str] = []
        if open_px is None:
            missing.append("open_chainlink(窗口起点无≥边界的 tick 且无可用回补)")
        if close_px is None:
            missing.append(
                f"close_chainlink(窗口收盘边界={close_boundary_s} 后无 tick 或等待超时)"
            )
        meta["missing"] = missing
        diag_o = ""
        diag_c = ""
        if hasattr(feed, "diagnose_rtds_open_buffer"):
            try:
                diag_o = str(feed.diagnose_rtds_open_buffer(window_ts))
                diag_c = str(feed.diagnose_rtds_open_buffer(close_boundary_s))
            except Exception as e:
                diag_c = f"诊断异常:{e}"
        meta["diagnostic_open_boundary"] = diag_o
        meta["diagnostic_close_boundary"] = diag_c
        stats = getattr(feed, "buffer_stats", None)
        if callable(stats):
            try:
                meta["buffer_stats"] = stats()
            except Exception:
                pass
        if hasattr(feed, "ws_health_summary"):
            try:
                meta["ws_health"] = feed.ws_health_summary()
                if hasattr(feed, "ws_health_line"):
                    meta["ws_health_line"] = feed.ws_health_line()
            except Exception:
                pass
        buf = meta.get("buffer_stats")
        buf_s = (
            f"tick={buf[0]} ts_ms∈[{buf[1]},{buf[2]}] latest={buf[3]}"
            if isinstance(buf, tuple) and buf and buf[0]
            else f"tick={buf[0] if buf else '?'}"
        )
        ws_line = ""
        if hasattr(feed, "ws_health_line"):
            try:
                ws_line = "\n  WS 链路: " + str(feed.ws_health_line())
            except Exception as e:
                ws_line = f"\n  WS 链路: (诊断失败:{e})"
        timeline_note = ""
        if (
            isinstance(buf, tuple)
            and len(buf) >= 3
            and buf[2] is not None
            and int(buf[0]) > 0
        ):
            close_ms = int(close_boundary_s) * 1000
            mx_ts = int(buf[2])
            if mx_ts < close_ms:
                gap_s = (close_ms - mx_ts) / 1000.0
                past_close_s = now() - float(close_boundary_s)
                meta["latest_payload_before_close_boundary_s"] = gap_s
                meta["wall_seconds_after_close_boundary"] = past_close_s
                timeline_note = (
                    f"\n  时间轴解读: 缓冲内「最新 payload 时间戳」比收盘边界早约 {gap_s:.0f}s；"
                    f"本机 wall 已过收盘边界约 {past_close_s:.0f}s。\n"
                    f"  含义: 这段时间内未收到 timestamp≥收盘边界的 Chainlink 更新（WS 断流、本机休眠、或 RTDS 暂停推送时常见）。\n"
                    f"  若 gap 远大于 DRY_RUN_CHAINLINK_CLOSE_WAIT_S，单纯「加长等待」仍等不到 tick；"
                    "应查 RTDS_DEBUG、网络/防火墙、或 DRY_RUN_BINANCE_SETTLE=1 接受 Binance 结算。\n"
                )
        bo_fb, bc_fb = _binance_window_edge_prices(window_ts)
        d_fb = 1 if bc_fb >= bo_fb else -1
        meta["settle_method"] = "binance_klines_fallback"
        meta["binance_open"] = bo_fb
        meta["binance_close"] = bc_fb
        # 回退时输赢**只**看 Binance 窗口首尾；勿与上方 RTDS 快照混为一谈（后者可能陈旧或未覆盖收盘）
        meta["open_used"] = bo_fb
        meta["close_used"] = bc_fb
        verdict = "Up(涨)" if d_fb == 1 else "Down(跌)"
        print(
            "[结算] RTDS Chainlink 不足以做本窗口 oracle 结算 → 改用 **Binance K 线首尾** 判定涨跌。\n"
            f"  **实际用于输赢**: Binance open={bo_fb:.2f} close={bc_fb:.2f} → {verdict}\n"
            f"  RTDS 快照(未参与比对或残缺): open_rtds={open_px!s} close_rtds={close_px!s}\n"
            f"  缺失: {'；'.join(missing)}\n"
            f"  缓冲: {buf_s}\n"
            f"{ws_line}\n"
            f"  起点边界诊断: {diag_o or '(无)'}\n"
            f"  收盘边界诊断: {diag_c or '(无)'}\n"
            f"{timeline_note}"
            "  修复建议: 仍无包时重启 bot 以重建 WS；检查 POLY_RTDS_WS；"
            "增大 RTDS_BUFFER_WAIT_S / DRY_RUN_CHAINLINK_CLOSE_WAIT_S（仅对「晚到但会到」的包有效）；"
            "DRY_RUN_BINANCE_SETTLE=1 强制 Binance；RTDS_FALLBACK_DEBUG=1 看开盘价侧。",
            flush=True,
        )
        log.warning(
            "settle_fallback_binance window=%s adjudicate_binance open=%.2f close=%.2f -> %s "
            "rtds_snap_open=%s rtds_snap_close=%s missing=%s buf=%s",
            window_ts,
            bo_fb,
            bc_fb,
            verdict,
            open_px,
            close_px,
            missing,
            buf_s,
        )
        return d_fb, meta

    bo, bc = _binance_window_edge_prices(window_ts)
    meta["settle_method"] = "binance_klines_fallback"
    meta["binance_open"] = bo
    meta["binance_close"] = bc
    meta["open_used"] = bo
    meta["close_used"] = bc
    meta["missing"] = []
    d = 1 if bc >= bo else -1
    return d, meta


def resolve_window_direction(
    window_ts: int, feed: Optional[Any], *, dry_run: bool = False
) -> int:
    """
    Up if oracle close >= oracle open (first ticks at/after window start and window end).
    Falls back to Binance candles if RTDS data is missing.
    """
    d, _meta = resolve_window_direction_with_meta(window_ts, feed, dry_run=dry_run)
    return int(d)


def parse_gamma_tokens(slug: str) -> Tuple[str, str]:
    r = requests.get(GAMMA_EVENTS, params={"slug": slug}, timeout=30)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise ValueError(f"Gamma 无该 slug 对应事件: {slug}")
    markets = events[0].get("markets") or []
    if not markets:
        raise ValueError("事件下无市场(markets)")
    m0 = markets[0]
    outcomes = m0.get("outcomes")
    clobs = m0.get("clobTokenIds")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(clobs, str):
        clobs = json.loads(clobs)
    if len(outcomes) != 2 or len(clobs) != 2:
        raise ValueError(f"市场结构异常 outcomes={outcomes} clobs={clobs}")
    up_tid = down_tid = ""
    for o, tid in zip(outcomes, clobs):
        label = str(o).strip().lower()
        if label == "up":
            up_tid = str(tid)
        elif label == "down":
            down_tid = str(tid)
    if not up_tid or not down_tid:
        raise ValueError(f"无法映射涨/跌代币: {outcomes} {clobs}")
    return up_tid, down_tid


def token_price_from_delta(abs_window_pct: float) -> float:
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
    模型入场价：领先方向（价相对窗口开盘已走出来的那一侧）用 token_price_from_delta(|w|)；
    逆势单应对侧更便宜，用 1 - 该曲线值（与二元互补价一致），避免干跑/回测系统性高估逆势 payout。
    """
    d = token_price_from_delta(abs(w_pct))
    if direction == 1:
        if w_pct >= 0:
            return d
        return max(0.03, min(0.97, 1.0 - d))
    if w_pct <= 0:
        return d
    return max(0.03, min(0.97, 1.0 - d))


@dataclass
class BotState:
    bankroll: float
    principal: float
    trades: int = 0
    dry_run: bool = False
    tick_log: List[float] = field(default_factory=list)
    # 干跑：虚拟资金流水（写入 dry_run_bankroll.json 的 history）
    dry_history: List[Dict[str, Any]] = field(default_factory=list)
    dry_history_next_seq: int = 1


_SETTLEMENT_SENTINEL = object()


@dataclass(frozen=True)
class QueuedDrySettle:
    """方向单干跑：收盘后由结算队列线程判输赢、改虚拟资金并写 JSON。"""

    window_ts: int
    slug: str
    close_at: float
    settle_after: float
    direction: int
    entry: float
    bet: float
    min_bet: float
    window_open: float
    decision_score: float
    decision_confidence: float
    mode: str
    decide_px: float


@dataclass(frozen=True)
class QueuedLiveRedeemHint:
    """实盘方向单：收盘后仅提醒链上/Portfolio 赎回（本程序不代操作）。"""

    window_ts: int
    slug: str
    close_at: float
    hint_after_s: float


_settlement_q: Optional[queue.Queue[object]] = None
_settlement_worker: Optional[threading.Thread] = None
_settlement_worker_mu = threading.Lock()
_settlement_state: Optional[BotState] = None
_settlement_feed_cell: List[Optional[Any]] = [None]


def min_confidence_for_mode(mode: str) -> float:
    if mode == "safe":
        return 0.30
    if mode == "aggressive":
        return 0.20
    return 0.0


def compute_bet(mode: str, bankroll: float, principal: float, min_bet: float) -> float:
    """
    方向单名义（未应用 MAX_USD 封顶；封顶在 run_trade_cycle 内处理）。
    """
    if bankroll < min_bet:
        return 0.0
    if mode == "safe":
        return max(min_bet, min(bankroll, bankroll * 0.25))
    if mode == "degen":
        return max(min_bet, bankroll)
    if bankroll <= principal + 1e-9:
        return max(min_bet, bankroll)
    return max(min_bet, bankroll - principal)


def make_clob_client() -> Any:
    if ClobClient is None:
        raise RuntimeError("未安装 py-clob-client，请 pip install py-clob-client")
    load_dotenv()
    key = os.environ.get("POLY_PRIVATE_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 POLY_PRIVATE_KEY")
    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    host = os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
    sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    funder = os.environ.get("POLY_FUNDER_ADDRESS") or None
    client = ClobClient(host, chain_id=chain_id, key=key, creds=creds, signature_type=sig, funder=funder)
    return client


def orderbook_has_asks(client: Any, token_id: str) -> bool:
    try:
        book = client.get_order_book(token_id)
        asks = book.asks or []
        return len(asks) > 0
    except Exception:
        return True


def place_buy_fok(client: Any, token_id: str, usd: float) -> Any:
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=float(usd),
        side="BUY",
        price=0.0,
        order_type=OrderType.FOK,
    )
    signed = client.create_market_order(mo)
    return client.post_order(signed, OrderType.FOK)


def place_buy_gtc_095(client: Any, token_id: str, min_shares: int = MIN_SHARES_POLY) -> Any:
    sz = float(min_shares)
    oa = OrderArgs(token_id=token_id, price=GTC_LIMIT_PRICE, size=sz, side="BUY")
    signed = client.create_order(oa)
    return client.post_order(signed, OrderType.GTC)


def snipe_loop(
    window_open: float,
    window_close: float,
    mode: str,
    chainlink_feed: Optional[Any] = None,
    arb_hit: Optional[threading.Event] = None,
) -> Tuple[AnalysisResult, List[float]]:
    ss = _snipe_start_s()
    src = _snipe_price_source()
    print(
        f"[狙击] 提前秒数(snipe_start_s)={ss} 现价来源={src}；"
        f"{_rtds_snipe_status_suffix(chainlink_feed)}；"
        "oracle 现价：有 RTDS 最新价则用 Chainlink，否则 Binance ticker",
        flush=True,
    )
    min_conf = min_confidence_for_mode(mode)
    best: Optional[AnalysisResult] = None
    last_score: Optional[float] = None
    ticks: List[float] = []
    snipe_armed = False
    while True:
        t_left = window_close - now()
        if t_left < SNIPE_DEADLINE:
            break
        if arb_hit is not None and arb_hit.is_set():
            raise ArbitrageCycleDone
        if t_left > ss:
            time.sleep(max(0.15, min(5.0, t_left - ss)))
            continue
        if not snipe_armed:
            snipe_armed = True
            print(
                f"[狙击] 已进入狙击阶段 距收盘≈{t_left:.1f}s "
                f"（约每 {POLL:g}s 拉 K 线+analyse；模式最低置信度={min_conf:.2f}）",
                flush=True,
            )
        px = snipe_current_price(chainlink_feed)
        ticks.append(px)
        if arb_hit is not None and arb_hit.is_set():
            raise ArbitrageCycleDone
        candles: List[Candle] = []
        for attempt in range(5):
            try:
                candles = fetch_recent_candles_1m(60)
                break
            except (requests.RequestException, OSError) as e:
                if attempt >= 4:
                    raise
                wait = 1.0 + float(attempt)
                print(f"[狙击] 拉取 1m K 线失败（{e!s}），{wait:.1f}s 后重试 ({attempt + 1}/5)")
                time.sleep(wait)
        res = analyze(window_open, px, candles, tick_prices=ticks[-120:])
        if best is None or abs(res.score) > abs(best.score):
            best = res
        if last_score is not None and abs(res.score - last_score) >= _spike_jump():
            return res, ticks
        if res.confidence >= min_conf:
            return res, ticks
        last_score = res.score
        time.sleep(POLL)
    if best is None:
        px = snipe_current_price(chainlink_feed)
        ticks.append(px)
        candles2: List[Candle] = []
        for attempt in range(5):
            try:
                candles2 = fetch_recent_candles_1m(60)
                break
            except (requests.RequestException, OSError) as e:
                if attempt >= 4:
                    raise
                wait = 1.0 + float(attempt)
                print(f"[狙击] 拉取 1m K 线失败（{e!s}），{wait:.1f}s 后重试 ({attempt + 1}/5)")
                time.sleep(wait)
        best = analyze(window_open, px, candles2, tick_prices=ticks)
    # 因距收盘过近退出时：若从未达到「置信度≥模式最低」，不得用 best 强行下单（否则与 safe/aggressive 语义矛盾）。
    if best.confidence < min_conf:
        det = dict(best.details)
        det["skip_trade"] = True
        return (
            AnalysisResult(
                direction=best.direction,
                score=best.score,
                confidence=best.confidence,
                details=det,
            ),
            ticks,
        )
    return best, ticks


def run_trade_cycle(
    client: Optional[Any],
    state: BotState,
    mode: str,
    min_bet: float,
    dry_run: bool,
    window_ts: int,
    chainlink_feed: Optional[Any] = None,
) -> None:
    close_at = window_ts + WINDOW
    slug = window_slug(window_ts)
    up_tid, down_tid = parse_gamma_tokens(slug)
    poll_pre = _arbitrage_poll_interval_s()
    print(
        f"[周期] 开始 window_ts={window_ts} slug={slug} "
        f"dry_run={dry_run} client={'已连接' if client is not None else '无'} "
        f"套利_POLL_S={poll_pre:g} 套利日志={_enable_arbitrage_log()} 套利实盘开关={_enable_arbitrage_trade()}",
        flush=True,
    )
    if log_up_down_ask_spread(window_ts, up_tid, down_tid, client, dry_run, state):
        return
    try:
        window_open, open_how = window_open_oracle(window_ts, chainlink_feed)
    except Exception as e:
        print(
            f"[ERROR] 获取开盘价失败（多为 Binance REST/代理断连，已加重试仍失败）：{e}\n"
            "可设 BINANCE_HTTP_RETRIES、BINANCE_REST_BASE_FALLBACKS 或检查系统代理；本 5m 窗口跳过。",
            flush=True,
        )
        return
    print(
        f"窗口={window_ts} slug={slug} 开盘价={window_open:.2f}  来源=「{open_how}」",
        flush=True,
    )

    sleep_s = close_at - _snipe_start_s() - now()
    if sleep_s > 0:
        if sleep_s > 2.0:
            print(
                f"[调度] 窗口={window_ts} slug={slug} 距进入狙击尚早，"
                f"休眠约 {sleep_s:.0f}s（至距收盘 {_snipe_start_s()}s 内再轮询；"
                "非卡死，与结算队列并行）",
                flush=True,
            )
        time.sleep(sleep_s)

    poll = _arbitrage_poll_interval_s()
    log_a = _enable_arbitrage_log()
    trade_a = _enable_arbitrage_trade() and client is not None and not dry_run
    arb_hit_ev: Optional[threading.Event] = None
    stop_arb = threading.Event()
    arb_thread: Optional[threading.Thread] = None
    if poll > 0 and (log_a or trade_a):
        arb_hit_ev = threading.Event()

        def _arb_worker() -> None:
            n = 0
            print(
                f"[套利/后台] 线程已启动 window={window_ts} 间隔={poll:g}s "
                f"log={log_a} trade={trade_a}（trade=真 才下双边单）",
                flush=True,
            )
            while not stop_arb.is_set():
                try:
                    n += 1
                    if log_up_down_ask_spread(
                        window_ts, up_tid, down_tid, client, dry_run, state, silent=True
                    ):
                        arb_hit_ev.set()
                        print(
                            f"[套利/后台] 第{n}次探测触发成交或跳过方向单，线程结束",
                            flush=True,
                        )
                        return
                except Exception as e:
                    print(f"[套利] 后台轮询异常: {e}", flush=True)
                    traceback.print_exc()
                if stop_arb.wait(timeout=poll):
                    break
            print(
                f"[套利/后台] 线程结束 window={window_ts} 共完成探测≈{n}次（正常：狙击结束 stop）",
                flush=True,
            )

        arb_thread = threading.Thread(target=_arb_worker, name="arb-poll", daemon=True)
        arb_thread.start()
        print(
            f"[套利] 已启动后台线程每 {poll:g}s 探测双边卖一（仅本窗口狙击阶段；"
            f"狙击结束即停；下窗重来）。若仍无行：看 [套利/后台] 是否「盘口不全」或关 ARBITRAGE_POLL_SUMMARY",
            flush=True,
        )
    elif poll <= 0 and (log_a or _enable_arbitrage_trade()):
        print(
            "[套利] ARBITRAGE_POLL_S=0：本窗口仅在周期开头测一次双边卖一；"
            "狙击阶段无后台轮询。需要持续探测请设 ARBITRAGE_POLL_S=5 等",
            flush=True,
        )
    try:
        decision, ticks = snipe_loop(
            window_open,
            float(close_at),
            mode,
            chainlink_feed,
            arb_hit=arb_hit_ev,
        )
    except ArbitrageCycleDone:
        return
    finally:
        stop_arb.set()
        if arb_thread is not None:
            arb_thread.join(timeout=min(8.0, poll + 2.0) if poll > 0 else 2.0)
    if decision.details.get("skip_trade"):
        print(
            f"[狙击] 窗口末仍未达到最低置信度 "
            f"{min_confidence_for_mode(mode):.2f}（当前={decision.confidence:.2f}），跳过本周期",
            flush=True,
        )
        return

    with _BOT_STATE_LOCK:
        if _loss_streak_should_pause(state):
            print(
                "[风控] 连亏冷却：近期方向单结算输过多，本周期跳过",
                flush=True,
            )
            return

    px_decide = ticks[-1] if ticks else snipe_current_price(chainlink_feed)
    up_ask = get_best_ask(up_tid, client)
    down_ask = get_best_ask(down_tid, client)

    mx_sum = _direction_orderbook_max_sum()
    if mx_sum is not None:
        if not should_trade_by_orderbook_for_direction(up_ask, down_ask, max_sum=mx_sum):
            return

    only_lt = _direction_only_when_book_sum_lt()
    if only_lt is not None:
        if up_ask is None or down_ask is None:
            print("[过滤] DIRECTION_ONLY_WHEN_BOOK_SUM_LT：盘口不全 → 跳过方向单", flush=True)
            return
        s = float(up_ask) + float(down_ask)
        if s >= only_lt:
            print(
                f"[策略] 盘口合计={s:.4f} ≥ {only_lt}（未满足「仅低价差才方向」）→ 跳过方向单",
                flush=True,
            )
            return

    if _direction_strategy() == "imbalance":
        depth = _imbalance_depth()
        imbu = get_orderbook_imbalance(up_tid, client, depth)
        imbd = get_orderbook_imbalance(down_tid, client, depth)
        if imbu is None or imbd is None:
            print("[过滤] 失衡策略：无法读取深度盘口", flush=True)
            return
        th_imb = _imbalance_threshold()
        print(f"[imb] up={imbu:.3f} down={imbd:.3f} depth={depth} 阈={th_imb}", flush=True)
        d = decide_from_imbalance(imbu, imbd, th_imb)
        if d == 0:
            print("[过滤] 无明显单侧盘口优势", flush=True)
            return
        syn_conf = min(1.0, max(abs(imbu), abs(imbd)) / max(th_imb, 1e-6))
        decision = replace(
            decision,
            direction=int(d),
            score=float(d) * 10.0,
            confidence=float(syn_conf),
            details={
                **decision.details,
                "direction_strategy": "imbalance",
                "imb_up": float(imbu),
                "imb_down": float(imbd),
            },
        )
        print(
            f"[方向] DIRECTION_STRATEGY=imbalance → {'Up(1)' if d == 1 else 'Down(-1)'}",
            flush=True,
        )
    elif _direction_strategy() == "reversal":
        th = _reversal_min_abs_pct()
        d = decide_reversal_direction(window_open, px_decide, min_abs_pct=th)
        if d == 0:
            print(f"[过滤] 反转策略：|窗口偏离| ≤ {th}%%，不交易", flush=True)
            return
        w_pct_r = (px_decide - window_open) / window_open * 100.0
        syn_conf = min(1.0, abs(w_pct_r) / max(th, 1e-9))
        decision = replace(
            decision,
            direction=int(d),
            score=float(d) * 10.0,
            confidence=float(syn_conf),
            details={
                **decision.details,
                "direction_strategy": "reversal",
                "reversal_w_pct": w_pct_r,
            },
        )
        print(
            f"[方向] DIRECTION_STRATEGY=reversal → 方向={'Up(1)' if d == 1 else 'Down(-1)'} "
            f"w_pct={w_pct_r:.4f}%% 合成置信度={syn_conf:.3f}",
            flush=True,
        )
    else:
        min_dc = _min_decision_confidence()
        if min_dc > 0.0 and float(decision.confidence) < min_dc:
            print(
                f"[过滤] 置信度太低 {decision.confidence:.2f} < MIN_DECISION_CONFIDENCE={min_dc:.2f}",
                flush=True,
            )
            return

    token_up = decision.direction == 1
    token_id = up_tid if token_up else down_tid
    w_pct = (px_decide - window_open) / window_open * 100.0
    if _use_book_ask_for_entry():
        entry = entry_from_best_asks(int(decision.direction), up_ask, down_ask)
        if entry is None or entry > 0.97:
            print(f"[过滤] 卖一入场价不可用或过贵 entry={entry}", flush=True)
            return
    else:
        entry = directional_entry_from_window_pct(int(decision.direction), w_pct)

    edge_for_sizing: Optional[float] = None
    mwall = _min_seconds_before_close_for_trade()
    if mwall is not None:
        tl = float(close_at) - now()
        if tl < float(mwall):
            print(
                f"[过滤] 距收盘仅 {tl:.1f}s < MIN_SECONDS_BEFORE_CLOSE_FOR_TRADE={mwall:g}，不交易",
                flush=True,
            )
            return

    if _use_fair_prob_edge():
        fair = estimate_fair_prob(window_open, px_decide)
        ok_e, edgev = has_price_edge(
            int(decision.direction),
            float(entry),
            fair,
            _min_price_edge(),
        )
        if not ok_e:
            print(
                f"[过滤] 无概率优势 edge={edgev:.4f} fair={fair:.3f} entry={float(entry):.3f} "
                f"(MIN_PRICE_EDGE={_min_price_edge()})",
                flush=True,
            )
            return
        edge_for_sizing = float(edgev)
        print(
            f"[edge] fair_est={fair:.3f} entry={float(entry):.3f} edge={edgev:.4f}",
            flush=True,
        )

    cap_mx = _max_directional_usd()
    fix_usd = _fixed_directional_usd()
    with _BOT_STATE_LOCK:
        if state.bankroll < min_bet:
            print("资金低于最小下单额，跳过本周期")
            return

        if fix_usd is not None:
            bet = fix_usd
            if cap_mx is not None:
                bet = min(bet, cap_mx)
            bet = min(bet, state.bankroll)
            if bet < min_bet:
                print(
                    f"方向单固定名义不可用：min(固定={fix_usd:.4f}, MAX_USD, 资金)={bet:.4f} < MIN_BET {min_bet}"
                )
                return
            print(
                f"[下注] 固定名义(FIXED_DIRECTIONAL_USD)={fix_usd:.4f}，"
                f"实际={bet:.4f}（已按 MAX_USD / 资金封顶，与模式/Kelly 无关）"
            )
        elif _enable_kelly():
            bet = _kelly_directional_bet(
                state.bankroll,
                decision.confidence,
                min_bet,
                cap_mx,
            )
            if bet is None:
                return
        elif _use_edge_position_sizing() and edge_for_sizing is not None:
            bet = size_by_edge(state.bankroll, edge_for_sizing, cap_mx, min_bet)
            if bet < min_bet:
                print(
                    f"[下注] edge 仓位过小 {bet:.4f} < MIN_BET {min_bet}，跳过",
                    flush=True,
                )
                return
            print(
                f"[下注] edge 仓位 edge={edge_for_sizing:.4f} frac={_edge_sizing_bankroll_frac()} "
                f"scale={_edge_sizing_edge_scale()} → bet={bet:.4f}",
                flush=True,
            )
        else:
            raw_bet = compute_bet(mode, state.bankroll, state.principal, min_bet)
            if raw_bet <= 0:
                print("资金低于最小下单额，跳过本周期")
                return
            bet = raw_bet
            if cap_mx is not None:
                bet = min(bet, cap_mx)
            if bet < min_bet:
                print(
                    f"方向单经 MAX_USD 封顶后为 {bet:.4f} < 最小下单 {min_bet}；"
                    "请提高 MAX_USD、降低 MIN_BET 或增加资金"
                )
                return
            if cap_mx is not None and raw_bet > cap_mx:
                print(f"[下注] 方向单 MAX_USD={cap_mx}：原始 {raw_bet:.4f} -> 封顶后 {bet:.4f}")

    sig_cn = "涨(Up)" if token_up else "跌(Down)"
    print(
        f"信号={sig_cn} 得分={decision.score:.2f} "
        f"置信度={decision.confidence:.2f} 下注={bet:.2f} 参考入场价={entry:.3f}"
    )

    if not dry_run and client is not None:
        deadline = close_at
        placed = False
        while now() < deadline and not placed:
            try:
                if orderbook_has_asks(client, token_id):
                    place_buy_fok(client, token_id, bet)
                else:
                    need = GTC_LIMIT_PRICE * MIN_SHARES_POLY
                    with _BOT_STATE_LOCK:
                        br_ok = state.bankroll >= need
                    if br_ok:
                        place_buy_gtc_095(client, token_id)
                    else:
                        place_buy_fok(client, token_id, bet)
                placed = True
            except Exception as e:
                print(f"下单异常: {e}；{ORDER_RETRY} 秒后重试")
                time.sleep(ORDER_RETRY)
        if not placed:
            print("窗口结束前仍未成功下单")
            return

    with _BOT_STATE_LOCK:
        bankroll_before_bet = state.bankroll
        state.bankroll -= bet
        bankroll_after_bet = state.bankroll

    if dry_run:
        settle_after = float(os.environ.get("DRY_RUN_SETTLE_AFTER_S", "2"))
        job = QueuedDrySettle(
            window_ts=int(window_ts),
            slug=slug,
            close_at=float(close_at),
            settle_after=settle_after,
            direction=int(decision.direction),
            entry=float(entry),
            bet=float(bet),
            min_bet=float(min_bet),
            window_open=float(window_open),
            decision_score=float(decision.score),
            decision_confidence=float(decision.confidence),
            mode=str(mode),
            decide_px=float(px_decide),
        )
        enqueue_settlement(job, state, chainlink_feed)
        with _BOT_STATE_LOCK:
            state.trades += 1
            _append_dry_run_history(
                state,
                {
                    "kind": "directional_bet",
                    "trades": state.trades,
                    "bankroll": bankroll_after_bet,
                    "bankroll_before_bet": bankroll_before_bet,
                    "window_ts": int(window_ts),
                    "slug": slug,
                    "bet": float(bet),
                },
            )
            _save_dry_run_state(state)
        print(
            "[干跑] 已入队结算队列（单线程稍后判输赢、改虚拟资金并写 JSON，不阻塞主循环）",
            flush=True,
        )
        return

    hint_after = float(os.environ.get("LIVE_REDEEM_HINT_AFTER_S", "2"))
    enqueue_settlement(
        QueuedLiveRedeemHint(
            window_ts=int(window_ts),
            slug=slug,
            close_at=float(close_at),
            hint_after_s=hint_after,
        ),
        state,
        chainlink_feed,
    )
    with _BOT_STATE_LOCK:
        state.trades += 1
    print(
        "[实盘] 已入队收盘后赎回提醒（链上资金以 Portfolio / auto_claim.py 为准）",
        flush=True,
    )


def print_run_config(args: argparse.Namespace, starting: float, min_bet: float) -> None:
    """启动时打印关键配置（避免跑错环境）。"""
    arb_usd = _arbitrage_trade_usd()
    cap_dir = _max_directional_usd()
    cap_txt = f"{cap_dir:.4f}" if cap_dir is not None else "未设置"
    fix_d = _fixed_directional_usd()
    fix_txt = f"{fix_d:.4f}" if fix_d is not None else "未设置"
    log_ts_on = os.environ.get("LOG_TS_MS", "1").strip().lower() not in ("0", "false", "no", "off")
    lines = [
        "======== 机器人运行配置 ========",
        f"干跑(dry_run)={args.dry_run}  模式(mode)={args.mode}",
        f"起始资金(STARTING_BANKROLL)={starting}  最小下单(MIN_BET)={min_bet}",
        f"狙击现价来源(SNIPE_PRICE_SOURCE)={_snipe_price_source()}  狙击提前秒数(SNIPE_START)={_snipe_start_s()}",
        f"使用 Chainlink RTDS(USE_CHAINLINK_RTDS)={os.environ.get('USE_CHAINLINK_RTDS', '1')}；"
        "单独测 RTDS：`python chainlink_rtds.py`；开盘价回退诊断：RTDS_FALLBACK_DEBUG=1；"
        f"开盘 tick 最大 payload 滞后毫秒(RTDS_OPEN_MAX_PAYLOAD_LAG_MS)="
        f"{os.environ.get('RTDS_OPEN_MAX_PAYLOAD_LAG_MS', '12000')}（0/off=关闭；单位毫秒，默认≈12s）；"
        f"晚到仍用 Chainlink 开盘价(RTDS_OPEN_ACCEPT_LATE_TICK)={_rtds_open_accept_late_tick()}；"
        f"起点前回补最大提前毫秒(RTDS_OPEN_FALLBACK_MAX_MS)="
        f"{os.environ.get('RTDS_OPEN_FALLBACK_MAX_MS', '30000')}；"
        f"RTDS 看门狗「久未写入btc/usd」秒(RTDS_AUTO_RECONNECT_STALE_S)="
        f"{os.environ.get('RTDS_AUTO_RECONNECT_STALE_S', '120')}（0=关；不以 payload 墙钟滞后为准） "
        f"最小间隔(RTDS_AUTO_RECONNECT_MIN_INTERVAL_S)="
        f"{os.environ.get('RTDS_AUTO_RECONNECT_MIN_INTERVAL_S', '45')} "
        f"on_open 后宽限(RTDS_WATCHDOG_GRACE_S)={os.environ.get('RTDS_WATCHDOG_GRACE_S', '40')}",
        f"套利仅日志(ENABLE_ARBITRAGE_LOG)={_enable_arbitrage_log()}  套利合计告警阈值(ARBITRAGE_SUM_ALERT)={_arbitrage_sum_alert():.4f}",
        f"套利实盘(ENABLE_ARBITRAGE_TRADE)={_enable_arbitrage_trade()}  "
        f"套利双边合计美元(解析后)={arb_usd:.4f}",
        f"套利轮询间隔秒(ARBITRAGE_POLL_S)={_arbitrage_poll_interval_s():g}（0=仅周期开头测一次）",
        f"方向单上限(MAX_USD)={cap_txt}",
        f"固定方向单名义(FIXED_DIRECTIONAL_USD)={fix_txt}",
        f"日志毫秒时间戳(LOG_TS_MS)={'开' if log_ts_on else '关'}",
        f"Kelly 方向单(ENABLE_KELLY)={_enable_kelly()}  Kelly 乘子(KELLY_SCALE)={_kelly_scale():.4f}",
        f"实盘收盘赎回提醒延迟秒(LIVE_REDEEM_HINT_AFTER_S)="
        f"{float(os.environ.get('LIVE_REDEEM_HINT_AFTER_S', '2')):g}（结算队列打印）",
        f"Python logging: LOG_LEVEL={os.environ.get('LOG_LEVEL', 'INFO')} 可选 LOG_FILE=路径；"
        f"WEBSOCKET_LOG=1 才打印 websocket-client 断线/重连（默认静默）",
        f"训练 JSONL(TRADE_TRAIN_JSONL)="
        f"{_trade_train_jsonl_path() or '未设'}（每笔结算追加一行，含 settle_meta）",
        f"套利后台简要行(ARBITRAGE_POLL_SUMMARY)="
        f"{'关' if os.environ.get('ARBITRAGE_POLL_SUMMARY', '1').strip().lower() in ('0', 'false', 'no', 'off') else '开'}"
        f"（关时仍每轮一条短心跳）；周期内日志前缀：[周期] [套利/后台] [狙击]",
        f"方向单盘口闸 DIRECTION_ORDERBOOK_MAX_SUM={os.environ.get('DIRECTION_ORDERBOOK_MAX_SUM', '') or '未设(不关)'}",
        f"仅低价差才方向 DIRECTION_ONLY_WHEN_BOOK_SUM_LT={os.environ.get('DIRECTION_ONLY_WHEN_BOOK_SUM_LT', '') or '未设(不关)'}",
        f"方向逻辑 DIRECTION_STRATEGY={_direction_strategy()}  REVERSAL_MIN_ABS_PCT={_reversal_min_abs_pct()}  "
        f"失衡深度 ORDERBOOK_IMBALANCE_DEPTH={_imbalance_depth()}  IMBALANCE_THRESHOLD={_imbalance_threshold()}",
        f"概率优势 USE_FAIR_PROB_EDGE={_use_fair_prob_edge()}  MIN_PRICE_EDGE={_min_price_edge()}  "
        f"FAIR_PROB_SIGMOID_SCALE={os.environ.get('FAIR_PROB_SIGMOID_SCALE', '50')}",
        f"edge 仓位 USE_EDGE_POSITION_SIZING={_use_edge_position_sizing()}  "
        f"EDGE_SIZING_BANKROLL_FRAC={_edge_sizing_bankroll_frac()}  EDGE_SIZING_EDGE_SCALE={_edge_sizing_edge_scale()}",
        f"收盘前时间闸 MIN_SECONDS_BEFORE_CLOSE_FOR_TRADE="
        f"{_min_seconds_before_close_for_trade() or '未设'}",
        f"连亏冷却 LOSS_STREAK_COOLDOWN={_loss_streak_cooldown_enabled()}  "
        f"LOSS_STREAK_MIN_TRADES={os.environ.get('LOSS_STREAK_MIN_TRADES', '6')}  "
        f"WINDOW={os.environ.get('LOSS_STREAK_WINDOW', '5')}  MAX_LOSSES={os.environ.get('LOSS_STREAK_MAX_LOSSES', '4')}",
        f"卖一作入场 USE_BOOK_ASK_FOR_ENTRY={_use_book_ask_for_entry()}  "
        f"TA最低置信 MIN_DECISION_CONFIDENCE="
        f"{_min_decision_confidence():g}（0=不启用）  SPIKE_JUMP(尖峰)={_spike_jump()}",
    ]
    if args.dry_run:
        dr_bs = os.environ.get("DRY_RUN_BINANCE_SETTLE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        lines.append(
            "干跑结算：收盘后等待秒(DRY_RUN_SETTLE_AFTER_S)="
            f"{float(os.environ.get('DRY_RUN_SETTLE_AFTER_S', '2')):g}；"
            f"Chainlink 收盘等待上限(DRY_RUN_CHAINLINK_CLOSE_WAIT_S)="
            f"{float(os.environ.get('DRY_RUN_CHAINLINK_CLOSE_WAIT_S', '90')):g}；"
            f"仅用 Binance 结算(DRY_RUN_BINANCE_SETTLE)={'是' if dr_bs else '否'}；"
            "方向单干跑结算=单消费者队列线程（不阻塞主循环）"
        )
        lines.append(
            f"干跑资金流水(JSON history)最多保留条数(DRY_RUN_HISTORY_MAX)={_dry_run_history_max()}"
        )
    lines.append("================================")
    print("\n".join(lines))


def _dry_run_state_path() -> str:
    return os.environ.get(
        "DRY_RUN_BANKROLL_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dry_run_bankroll.json"),
    )


def _dry_run_history_max() -> int:
    """history 数组最多保留条数（防 JSON 无限膨胀）；默认 2000。"""
    raw = os.environ.get("DRY_RUN_HISTORY_MAX", "2000").strip()
    try:
        v = int(raw)
    except ValueError:
        return 2000
    return max(50, min(v, 50_000))


def _load_dry_run_state(
    default_bankroll: float,
    default_principal: float,
    default_trades: int = 0,
) -> Tuple[float, float, int, List[Dict[str, Any]], int]:
    """干跑：从 JSON 恢复虚拟资金、笔数与资金流水；无文件或损坏则用默认值。"""
    path = _dry_run_state_path()
    if not os.path.isfile(path):
        return default_bankroll, default_principal, default_trades, [], 1
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        b = float(data.get("bankroll", default_bankroll))
        p = float(data.get("principal", default_principal))
        t = int(data.get("trades", default_trades))
        if b < 0 or p < 0 or t < 0:
            return default_bankroll, default_principal, default_trades, [], 1
        raw_hist = data.get("history")
        clean: List[Dict[str, Any]] = []
        if isinstance(raw_hist, list):
            for item in raw_hist:
                if not isinstance(item, dict):
                    continue
                try:
                    sq = int(item.get("seq", 0))
                except (TypeError, ValueError):
                    continue
                if sq <= 0:
                    continue
                clean.append(item)
        cap = _dry_run_history_max()
        if len(clean) > cap:
            clean = clean[-cap:]
        next_seq = 1
        for item in clean:
            try:
                next_seq = max(next_seq, int(item.get("seq", 0)) + 1)
            except (TypeError, ValueError):
                pass
        return b, p, t, clean, next_seq
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default_bankroll, default_principal, default_trades, [], 1


def _dry_run_history_kind_cn(kind: str) -> str:
    return {
        "directional_bet": "方向单·扣下注",
        "directional_settle": "方向单·结算后",
    }.get(kind, kind)


def _append_dry_run_history(state: BotState, rec: Dict[str, Any]) -> None:
    """追加一条虚拟资金流水（须已持有 _BOT_STATE_LOCK 或仅单线程写 state）。"""
    if not state.dry_run:
        return
    n = state.dry_history_next_seq
    state.dry_history_next_seq = n + 1
    row: Dict[str, Any] = {"seq": n, "ts_unix": now(), **rec}
    for _k in ("bankroll", "bankroll_before_bet", "post_bet_bankroll", "settle_payout"):
        if _k in row and isinstance(row[_k], (int, float)):
            row[_k] = round(float(row[_k]), 4)
    state.dry_history.append(row)
    cap = _dry_run_history_max()
    if len(state.dry_history) > cap:
        state.dry_history = state.dry_history[-cap:]


def _print_dry_run_history_table(state: BotState, last_n: int = 14) -> None:
    """控制台打印最近流水（完整表见 JSON history）。"""
    if not state.dry_run or not state.dry_history:
        return
    rows = state.dry_history[-last_n:]
    print(
        f"[干跑流水] 共 {len(state.dry_history)} 条，最近 {len(rows)} 条（"
        f"完整见 {os.path.basename(_dry_run_state_path())} → history）",
        flush=True,
    )
    hdr = f"{'seq':>4}  {'trades':>6}  {'bankroll':>10}  {'说明':<18}  备注"
    print(hdr, flush=True)
    print("  " + "-" * (len(hdr) + 6), flush=True)
    for r in rows:
        seq = r.get("seq", "")
        tr = r.get("trades", "")
        br = r.get("bankroll", "")
        k = _dry_run_history_kind_cn(str(r.get("kind", "")))
        extra = ""
        if r.get("kind") == "directional_bet":
            extra = (
                f"bet={r.get('bet')} 扣前={r.get('bankroll_before_bet', '?')} "
                f"w={r.get('window_ts')}"
            )
        elif r.get("kind") == "directional_settle":
            wn = "赢" if r.get("win") else "输"
            po = r.get("settle_payout", "?")
            pbb = r.get("post_bet_bankroll", "?")
            extra = (
                f"{wn} payout={po} 结算前={pbb} bust={bool(r.get('bust_reset'))} "
                f"w={r.get('window_ts')}"
            )
        print(f"{seq!s:>4}  {tr!s:>6}  {br!s:>10}  {k:<18}  {extra}", flush=True)


def _save_dry_run_state(state: BotState) -> None:
    """干跑：写入虚拟资金与流水（下注后、结算后均会调用）。"""
    path = _dry_run_state_path()
    try:
        payload: Dict[str, Any] = {
            "bankroll": round(state.bankroll, 4),
            "principal": round(state.principal, 4),
            "trades": state.trades,
        }
        if state.dry_run:
            payload["history"] = state.dry_history
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(
            f"[干跑存档] 已保存 bankroll={state.bankroll:.4f} "
            f"principal={state.principal:.4f} trades={state.trades} → {path}"
        )
    except OSError as e:
        print(f"[干跑存档] 写入失败: {e}")


_TRAIN_LOG_LOCK = threading.Lock()


def _trade_train_jsonl_path() -> str:
    """设 TRADE_TRAIN_JSONL=路径 时每笔方向单结算后追加一行 JSON（训练用）。"""
    return os.environ.get("TRADE_TRAIN_JSONL", "").strip()


def _append_trade_train_record(rec: Dict[str, Any]) -> None:
    path = _trade_train_jsonl_path()
    if not path:
        return
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    try:
        with _TRAIN_LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        log.error("训练日志写入失败 %s: %s", path, e)


def _apply_queued_dry_settle(job: QueuedDrySettle, state: BotState) -> None:
    wait_s = max(0.1, job.close_at - now() + job.settle_after)
    print(
        f"[干跑/队列] slug={job.slug} 约 {wait_s:.1f}s 后判定输赢并写档…",
        flush=True,
    )
    time.sleep(wait_s)
    feed = _settlement_feed_cell[0]
    last_err: Optional[BaseException] = None
    actual = 0
    settle_meta: Dict[str, Any] = {}
    for attempt in range(3):
        try:
            actual, settle_meta = resolve_window_direction_with_meta(
                job.window_ts, feed, dry_run=True
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(
                f"[干跑/队列] 结算判定失败 ({attempt + 1}/3): {e}",
                flush=True,
            )
            time.sleep(2.0 * (attempt + 1))
    if last_err is not None:
        print(
            f"[干跑/队列] 结算多次失败，按输处理（未发放赢钱）: {last_err}",
            flush=True,
        )
        actual = -int(job.direction)
        settle_meta = {
            "settle_method": "error_fallback_loss",
            "error": repr(last_err),
            "window_ts": int(job.window_ts),
        }

    win = actual == job.direction
    bust_reset = False
    with _BOT_STATE_LOCK:
        post_bet_bankroll = state.bankroll
        settle_payout = 0.0
        if win:
            safe_entry = max(float(job.entry), 1e-9)
            shares = job.bet / safe_entry
            settle_payout = float(shares * 1.0)
            state.bankroll += settle_payout
        if state.bankroll < job.min_bet:
            state.bankroll = state.principal
            bust_reset = True
        _append_dry_run_history(
            state,
            {
                "kind": "directional_settle",
                "trades": state.trades,
                "bankroll": state.bankroll,
                "post_bet_bankroll": post_bet_bankroll,
                "settle_payout": settle_payout,
                "window_ts": int(job.window_ts),
                "slug": job.slug,
                "win": bool(win),
                "actual_outcome": int(actual),
                "bet_direction": int(job.direction),
                "bust_reset": bust_reset,
                "bet": float(job.bet),
                "entry_model": float(job.entry),
            },
        )
        _save_dry_run_state(state)
    ou = settle_meta.get("open_used") or settle_meta.get("open_rtds")
    cu = settle_meta.get("close_used") or settle_meta.get("close_rtds")
    bo = settle_meta.get("binance_open")
    bc = settle_meta.get("binance_close")
    dir_cn = lambda d: "涨(1)" if d == 1 else "跌(-1)"
    print(
        f"[干跑/队列] 窗口={job.window_ts} 结算={'赢' if win else '输'} 资金余额={state.bankroll:.4f} "
        f"settle={settle_meta.get('settle_method')} "
        f"结果链={dir_cn(actual)} 下注方向={dir_cn(job.direction)} "
        f"chainlink_open={ou} chainlink_close={cu} "
        f"binance_open={bo} binance_close={bc}",
        flush=True,
    )
    if state.dry_history:
        lh = state.dry_history[-1]
        print(
            f"[干跑流水] 本条 seq={lh.get('seq')} kind={lh.get('kind')} "
            f"trades={lh.get('trades')} bankroll={lh.get('bankroll')}",
            flush=True,
        )
    _append_trade_train_record(
        {
            "event": "directional_settle",
            "ts_unix": now(),
            "window_ts": job.window_ts,
            "slug": job.slug,
            "mode": job.mode,
            "bet_usd": job.bet,
            "entry_model": job.entry,
            "direction_bet": job.direction,
            "decision_score": job.decision_score,
            "decision_confidence": job.decision_confidence,
            "window_open_at_bet": job.window_open,
            "decision_oracle_px": job.decide_px,
            "actual_outcome": actual,
            "win": win,
            "settle_meta": settle_meta,
        }
    )
    if bust_reset:
        print(
            f"干跑：资金已重置为起始本金 {state.principal}",
            flush=True,
        )


def _apply_queued_live_redeem_hint(job: QueuedLiveRedeemHint) -> None:
    wait_s = max(0.1, job.close_at - now() + job.hint_after_s)
    time.sleep(wait_s)
    print(
        f"[实盘/队列] 窗口={job.window_ts} slug={job.slug}："
        "建议在 Portfolio 完成 USDC/份额赎回，或运行: python auto_claim.py",
        flush=True,
    )


def _settlement_consumer_loop() -> None:
    assert _settlement_q is not None
    assert _settlement_state is not None
    st = _settlement_state
    while True:
        item = _settlement_q.get()
        try:
            if item is _SETTLEMENT_SENTINEL:
                return
            if isinstance(item, QueuedDrySettle):
                _apply_queued_dry_settle(item, st)
            elif isinstance(item, QueuedLiveRedeemHint):
                _apply_queued_live_redeem_hint(item)
            else:
                print(f"[结算队列] 未知任务: {type(item)!r}", flush=True)
        except Exception as e:
            print(f"[结算队列] 处理异常: {e}", flush=True)
            traceback.print_exc()


def ensure_settlement_worker(state: BotState, feed: Optional[Any]) -> None:
    """懒启动单消费者线程；feed 指针每次入队前更新。"""
    global _settlement_q, _settlement_worker, _settlement_state
    with _settlement_worker_mu:
        _settlement_feed_cell[0] = feed
        _settlement_state = state
        if _settlement_worker is not None:
            return
        _settlement_q = queue.Queue()
        th = threading.Thread(
            target=_settlement_consumer_loop,
            name="settlement-queue",
            daemon=False,
        )
        _settlement_worker = th
        th.start()


def enqueue_settlement(item: object, state: BotState, feed: Optional[Any]) -> None:
    ensure_settlement_worker(state, feed)
    assert _settlement_q is not None
    _settlement_q.put(item)


def shutdown_settlement_worker(timeout: float = 240.0) -> None:
    """退出前送入哨兵并 join，避免干跑结算丢写。"""
    global _settlement_q, _settlement_worker
    with _settlement_worker_mu:
        q = _settlement_q
        w = _settlement_worker
    if q is None or w is None:
        return
    q.put(_SETTLEMENT_SENTINEL)
    w.join(timeout=timeout)


def main() -> None:
    load_dotenv()
    setup_logging()
    _ensure_utf8_stdio()
    _install_log_timestamp_print()
    p = argparse.ArgumentParser(description="Polymarket BTC 5 分钟 Up/Down 机器人")
    p.add_argument(
        "--mode",
        choices=("safe", "aggressive", "degen"),
        default=os.environ.get("BOT_MODE", "safe"),
        help="策略模式：safe / aggressive / degen（可与环境变量 BOT_MODE 一致）",
    )
    p.add_argument("--dry-run", action="store_true", help="干跑：模拟流程，不下真实单")
    p.add_argument("--once", action="store_true", help="只跑一个交易周期后退出")
    p.add_argument("--max-trades", type=int, default=0, help="最多完成几笔后退出，0 表示不限制")
    args = p.parse_args()

    starting = float(os.environ.get("STARTING_BANKROLL", "1.0"))
    min_bet = float(os.environ.get("MIN_BET", "1.0"))
    if args.dry_run:
        br, pr, tr, hist, hseq = _load_dry_run_state(starting, starting, 0)
        state = BotState(
            bankroll=br,
            principal=pr,
            trades=tr,
            dry_run=True,
            dry_history=hist,
            dry_history_next_seq=hseq,
        )
        print(
            f"[干跑存档] 从文件继续：bankroll={br:.4f} principal={pr:.4f} trades={tr} "
            f"流水条数={len(hist)} "
            f"（若无 {os.path.basename(_dry_run_state_path())} 则用 STARTING_BANKROLL={starting}）"
        )
        if tr > 0 and len(hist) == 0:
            print(
                "[干跑流水] 存档无 history（旧版 JSON 或手动删过）；自本局下注起会重新累积流水。",
                flush=True,
            )
        _print_dry_run_history_table(state)
    else:
        state = BotState(bankroll=starting, principal=starting, dry_run=False)
    print_run_config(args, starting, min_bet)
    bankroll_warn = state.bankroll if args.dry_run else starting
    if bankroll_warn < min_bet:
        who = "当前干跑虚拟资金" if args.dry_run else "起始资金"
        print(
            f"[提示] {who} {bankroll_warn} < 最小下单 {min_bet}："
            "方向单/套利可能被跳过；请充值、调低 MIN_BET 或删除干跑存档 JSON 用 STARTING_BANKROLL 重来"
        )
    client = None
    if not args.dry_run:
        try:
            client = make_clob_client()
        except Exception as e:
            print(f"初始化 CLOB 客户端失败: {e}", file=sys.stderr)
            sys.exit(1)

    chainlink_feed: Optional[Any] = None
    use_rtds = os.environ.get("USE_CHAINLINK_RTDS", "1").lower() not in ("0", "false", "no", "off")
    if use_rtds and ChainlinkBtcUsdRtds is not None:
        try:
            dbg = os.environ.get("RTDS_DEBUG")
            chainlink_feed = ChainlinkBtcUsdRtds(
                on_status=(lambda m: print(f"RTDS 状态: {m}")) if dbg else None,
            )
            chainlink_feed.start()
            time.sleep(float(os.environ.get("RTDS_WARMUP_S", "1.5")))
            buf_wait = float(os.environ.get("RTDS_BUFFER_WAIT_S", "12"))
            got_first = True
            if buf_wait > 0:
                got_first = chainlink_feed.wait_for_ticks(1, timeout_s=buf_wait)
            n, mn, mx, lp = chainlink_feed.buffer_stats()
            if n > 0:
                print(
                    f"[RTDS] 启动自检：tick={n} 最新oracle≈{lp:.2f} "
                    f"时间戳ms min={mn} max={mx}"
                    f"{'；首包已就绪' if got_first else f'；首包等待 {buf_wait:g}s 超时（现价/开盘可能仍回退 Binance）'}",
                    flush=True,
                )
            else:
                print(
                    f"[RTDS] 启动自检：tick=0（{buf_wait:g}s 内无解析到 btc/usd；"
                    "开盘/狙击现价将多用 Binance；可检查网络、RTDS_DEBUG=1、或调大 RTDS_BUFFER_WAIT_S）",
                    flush=True,
                )
            wh = getattr(chainlink_feed, "ws_health_line", None)
            if callable(wh):
                print(f"[RTDS] {wh()}", flush=True)
        except Exception as e:
            print(f"RTDS 已禁用（{e}）；开盘/结算仅使用 Binance")
            chainlink_feed = None
    elif not use_rtds:
        print("USE_CHAINLINK_RTDS=0：开盘与结算仅使用 Binance")

    max_trades = int(args.max_trades or 0)

    while True:
        wts = current_window_ts()
        close_at = wts + WINDOW
        t_left = close_at - now()
        if t_left <= 0:
            time.sleep(0.25)
            continue
        if t_left < _snipe_start_s():
            sleep_s = t_left + 0.5
            if sleep_s > 2.0:
                print(
                    f"[主循环] 距下一狙击窗口约 {sleep_s:.0f}s，休眠中（进程未卡死；"
                    "连续跑时此处可能静默数分钟）",
                    flush=True,
                )
            else:
                print(
                    f"[主循环] 本 5m 窗已较晚（距收盘 {t_left:.1f}s < 狙击提前 {_snipe_start_s()}s），"
                    f"短眠 {sleep_s:.1f}s 对齐下一窗（避免 silent sleep）",
                    flush=True,
                )
            time.sleep(sleep_s)
            continue

        try:
            run_trade_cycle(client, state, args.mode, min_bet, args.dry_run, wts, chainlink_feed)
        except Exception as e:
            print(f"[主循环] run_trade_cycle 异常（已记录并继续）: {e}", flush=True)
            traceback.print_exc()
            time.sleep(5.0)

        if args.once:
            break
        if max_trades and state.trades >= max_trades:
            break
        close_this = wts + WINDOW
        if now() < close_this:
            sleep_s = max(0.5, close_this - now() + 0.5)
            if sleep_s > 2.0:
                print(
                    f"[主循环] 本 5m 窗口尚未结束，休眠约 {sleep_s:.0f}s 再进入下一周期调度",
                    flush=True,
                )
            time.sleep(sleep_s)

    print("[主循环] 退出：等待结算队列线程收尾…", flush=True)
    shutdown_settlement_worker()


if __name__ == "__main__":
    main()
