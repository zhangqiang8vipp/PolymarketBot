"""测试窗口开盘价的最佳数据源"""
import os, sys, time, requests
from datetime import datetime, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from backtest import fetch_klines_1m, fetch_klines_1m_ts

CLOB_HOST = os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com")

def ts_str(s: int) -> str:
    return datetime.fromtimestamp(s, tz=timezone.utc).strftime("%H:%M:%S UTC")

def clob_prices_history(token_id: str, start_ts: int, end_ts: int):
    url = f"{CLOB_HOST}/prices-history"
    params = {"market": token_id, "startTs": start_ts, "endTs": end_ts, "interval": "1m"}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            d = r.json()
            return d.get("history", [])
    except Exception as e:
        pass
    return None

def clob_get_markets(query: str = "btc"):
    url = f"{CLOB_HOST}/markets"
    params = {"markets": query} if query else {}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def main():
    # 当前 UTC 时间
    now = int(time.time())
    # 窗口 1776241200 = 08:20:00 UTC（用户确认 Price to Beat = 73882.70）
    w = 1776241200

    print(f"=== 窗口 {ts_str(w)} UTC (用户确认 Price to Beat: $73,882.70) ===")
    print()

    # 1. Binance 1m K 线
    print("【1】Binance 1m K 线")
    klines = fetch_klines_1m("BTCUSDT", start_ms=w * 1000, limit=6)
    for k in klines:
        ts = k[0] // 1000
        o, h, l, c = k[1], k[2], k[3], k[4]
        print(f"  {ts_str(ts)}  open={o:.2f}  high={h:.2f}  low={l:.2f}  close={c:.2f}")

    # 2. Binance 首根 close vs Polymarket
    if klines:
        bn_open = float(klines[0][1])
        bn_close = float(klines[-1][4])
        print(f"\n  Binance 第1根 open = {bn_open:.2f}")
        print(f"  Binance 第5根 close = {bn_close:.2f}")
        print(f"  Polymarket Price to Beat = 73882.70")
        print(f"  Binance open 与 Poly 的偏差: {((bn_open - 73882.70) / 73882.70 * 100):+.4f}%")
        print(f"  Binance close vs open 的涨跌: {'Up' if bn_close >= bn_open else 'Down'} (delta={((bn_close-bn_open)/bn_open*100):+.4f}%)")

    # 3. CLOB /prices-history
    print("\n【2】CLOB /prices-history")
    markets = clob_get_markets("btc")
    if not markets:
        print("  无法访问 CLOB API（网络超时）")
    else:
        print(f"  找到市场数: {len(markets) if isinstance(markets, list) else 'dict'}")
        btc = [m for m in (markets if isinstance(markets, list) else []) if "btc" in m.get("question","").lower()]
        if btc:
            m = btc[0]
            up_tok = next((t["token_id"] for t in m.get("tokens",[]) if t.get("outcome","").lower() in ("up","yes")), None)
            down_tok= next((t["token_id"] for t in m.get("tokens",[]) if t.get("outcome","").lower() not in ("up","yes")), None)
            print(f"  市场: {m.get('question','')[:60]}")
            print(f"  up_token={up_tok and up_tok[:20]}  down_token={down_tok and down_tok[:20]}")

            w_end = w + 300
            for tok_id, side in [(up_tok, "UP"), (down_tok, "DOWN")]:
                if not tok_id:
                    continue
                history = clob_prices_history(tok_id, w, w_end)
                if history:
                    print(f"  {side}: {len(history)} 笔成交")
                    for h in history[:3]:
                        print(f"    t={ts_str(h['t'])}  p={h['p']}")
                else:
                    print(f"  {side}: 无成交或网络问题")
        else:
            print("  无 BTC 市场")

    # 4. 测试更早的窗口（让 bot 还在跑时看最近已结算窗口的开盘价）
    print("\n【3】检查更早窗口（已结算可对比）")
    # 当前窗口 T-300, T-600
    for w_past in [w - 300, w - 600]:
        print(f"\n  窗口 {ts_str(w_past)}:")
        klines2 = fetch_klines_1m("BTCUSDT", start_ms=w_past * 1000, limit=6)
        if klines2:
            print(f"    Binance open={klines2[0][1]:.2f}")
        # 尝试 CLOB
        markets2 = clob_get_markets("btc")
        if markets2 and isinstance(markets2, list):
            btc2 = [m for m in markets2 if "btc" in m.get("question","").lower()]
            if btc2:
                m2 = btc2[0]
                up_tok2 = next((t["token_id"] for t in m2.get("tokens",[]) if t.get("outcome","").lower() in ("up","yes")), None)
                if up_tok2:
                    hist = clob_prices_history(up_tok2, w_past, w_past + 300)
                    if hist:
                        print(f"    CLOB UP 成交: {hist[0]['p']} @ {ts_str(hist[0]['t'])}")

if __name__ == "__main__":
    main()