"""
每日交易日志（trading_journal.csv）：
每窗口一条记录，完整记录狙击决策、跳过原因、结算结果。
一行一个窗口，便于按天抽查。
"""
import csv, os, threading
from datetime import datetime, timezone
from typing import List, Optional

# 模块级写锁（防止多线程并发写文件损坏）
_journal_lock = threading.Lock()


def _journal_path() -> str:
    return os.environ.get(
        "TRADING_JOURNAL_CSV",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_journal.csv"),
    )


def _ts_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── 字段常量 ────────────────────────────────────────────────────────────────
_JOURNAL_HEADER = [
    "window_ts",
    "window_time",
    "mode",
    # ── 狙击决策 ──────────────────────────────────────────────────────────
    "score",
    "confidence",
    "direction",          # 1=Up, -1=Down, 0=无方向
    "direction_cn",
    "skip_reason",        # 空=已下单，其他=跳过原因
    # ── 盘口信息 ──────────────────────────────────────────────────────────
    "up_ask",
    "down_ask",
    "book_sum",
    "window_open",
    "open_source",
    # ── 下单信息 ──────────────────────────────────────────────────────────
    "bet",
    "entry",
    "decided",            # True/False（是否实际下单）
    # ── 结算（结算后填充，由 write_journal_settled 更新）────────────────
    "actual",             # 1=Up win, -1=Down win, 空=未结算
    "win",                # True/False，空=未结算
    "settle_payout",
    "bankroll_before",
    "bankroll_after",
    "cum_pnl",
    "settle_method",
]


def _journal_has_window(path: str, window_ts: int) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row["window_ts"]) == window_ts:
                    return True
    except Exception:
        pass
    return False


def write_journal_open(
    window_ts: int,
    mode: str,
    score: float,
    confidence: float,
    direction: int,
    skip_reason: str,
    up_ask: Optional[float],
    down_ask: Optional[float],
    window_open: float,
    open_source: str,
    bet: float,
    entry: float,
    decided: bool,
) -> None:
    """
    窗口开始时写入（或覆盖）一条记录。
    - decided=True  → 实际下单
    - skip_reason 非空 → 跳过（注明原因）
    - 结算字段留空，待后续 update_journal_settled 填充
    """
    path = _journal_path()
    row = {
        "window_ts": window_ts,
        "window_time": _ts_str(window_ts),
        "mode": mode,
        "score": f"{score:.2f}",
        "confidence": f"{confidence:.2f}",
        "direction": direction,
        "direction_cn": "涨" if direction == 1 else ("跌" if direction == -1 else "无"),
        "skip_reason": skip_reason,
        "up_ask": f"{up_ask:.4f}" if up_ask is not None else "",
        "down_ask": f"{down_ask:.4f}" if down_ask is not None else "",
        "book_sum": f"{(up_ask or 0) + (down_ask or 0):.4f}" if up_ask is not None and down_ask is not None else "",
        "window_open": f"{window_open:.4f}",
        "open_source": open_source,
        "bet": f"{bet:.4f}",
        "entry": f"{entry:.4f}",
        "decided": "是" if decided else "否",
        "actual": "",
        "win": "",
        "settle_payout": "",
        "bankroll_before": "",
        "bankroll_after": "",
        "cum_pnl": "",
        "settle_method": "",
    }

    with _journal_lock:
        file_exists = os.path.exists(path) and os.path.getsize(path) > 0
        needs_header = not file_exists

        # 找到该 window_ts 是否已有行（覆盖更新）
        rows: List[dict] = []
        if file_exists:
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for r in reader:
                        if int(r["window_ts"]) == window_ts:
                            rows.append(row)  # 替换
                        else:
                            rows.append(r)
            except Exception:
                needs_header = True
                rows = []
        else:
            rows = []

        if not rows:
            rows.append(row)

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_JOURNAL_HEADER, extrasaction="ignore")
            if needs_header:
                writer.writeheader()
            writer.writerows(rows)


def update_journal_settled(
    window_ts: int,
    actual: int,
    win: bool,
    settle_payout: float,
    bankroll_before: float,
    bankroll_after: float,
    cum_pnl: float,
    settle_method: str,
) -> None:
    """结算后更新对应 window_ts 的结算字段。"""
    path = _journal_path()
    if not os.path.exists(path):
        return

    with _journal_lock:
        rows: List[dict] = []
        updated = False
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if int(r["window_ts"]) == window_ts:
                        r["actual"] = "涨" if actual == 1 else "跌"
                        r["win"] = "赢" if win else "输"
                        r["settle_payout"] = f"{settle_payout:.4f}"
                        r["bankroll_before"] = f"{bankroll_before:.4f}"
                        r["bankroll_after"] = f"{bankroll_after:.4f}"
                        r["cum_pnl"] = f"{cum_pnl:.4f}"
                        r["settle_method"] = settle_method
                        updated = True
                    rows.append(r)
        except Exception:
            return

        if not updated:
            return

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_JOURNAL_HEADER, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
