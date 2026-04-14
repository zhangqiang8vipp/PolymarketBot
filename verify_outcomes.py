"""验证 Polymarket 实际结算结果"""
import requests
import json
from datetime import datetime, timezone

# 抽查的时间戳
test_timestamps = [
    1776197700,  # 第一个窗口
    1776198000,
    1776198300,
    1776198600,  # 用户抽查的
    1776198900,
    1776199200,
]

def get_binance_direction(window_ts):
    """获取 Binance K 线判定的涨跌"""
    start_ms = window_ts * 1000
    end_ms = (window_ts + 300) * 1000
    
    url = 'https://api.binance.com/api/v3/klines'
    params = {
        'symbol': 'BTCUSDT',
        'interval': '1m',
        'startTime': start_ms,
        'endTime': end_ms,
        'limit': 10
    }
    
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        if data and len(data) >= 5:
            window_open = float(data[0][1])
            window_close = float(data[4][4])
            direction = 'Up' if window_close >= window_open else 'Down'
            return direction, window_open, window_close
    except Exception as e:
        print(f"    Binance 错误: {e}")
    return None, None, None

def get_polymarket_resolution(slug):
    """获取 Polymarket 实际结算结果"""
    url = 'https://gamma-api.polymarket.com/events'
    params = {'slug': slug}
    
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                market = data[0]
                closed = market.get('closed', False)
                
                # 尝试获取 resolution
                resolution = None
                if market.get('markets'):
                    m = market['markets'][0]
                    resolution = m.get('resolution')
                    
                    # 如果没有 resolution，尝试从 outcomePrices 推断
                    if not resolution:
                        try:
                            prices = json.loads(m.get('outcomePrices', '[]'))
                            if len(prices) >= 2:
                                # 结算后，概率为 1 的那个是赢的
                                resolution = 'Up' if float(prices[0]) >= float(prices[1]) else 'Down'
                        except:
                            pass
                
                return closed, resolution
    except Exception as e:
        print(f"    Polymarket 错误: {e}")
    return None, None

def main():
    print("=" * 80)
    print(" Polymarket 实际结算 vs Binance K线 验证")
    print("=" * 80)
    print()
    print(f"{'时间戳':>12} | {'UTC时间':>20} | {'Polymarket':^12} | {'Binance':^12} | {'对比':^8}")
    print("-" * 80)
    
    matches = 0
    total = 0
    
    for ts in test_timestamps:
        slug = f'btc-updown-5m-{ts}'
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        # 获取 Polymarket 结果
        closed, poly_res = get_polymarket_resolution(slug)
        
        # 获取 Binance 判定
        binance_res, open_px, close_px = get_binance_direction(ts)
        
        # 对比
        if poly_res and binance_res:
            match = "OK" if poly_res == binance_res else "MISMATCH"
            if poly_res == binance_res:
                matches += 1
            total += 1
        else:
            match = "?"
        
        poly_str = poly_res or "?"
        binance_str = binance_res or "?"
        
        print(f"{ts:>12} | {dt:>20} | {poly_str:^12} | {binance_str:^12} | {match:^8}")
        
        if open_px and close_px:
            print(f"            Binance: {open_px:.2f} -> {close_px:.2f}")
    
    print("-" * 80)
    
    if total > 0:
        accuracy = matches / total * 100
        print(f"匹配率: {matches}/{total} ({accuracy:.0f}%)")
        print()
        if accuracy < 50:
            print("WARNING: Polymarket 结算与 Binance K线不匹配!")
            print("    这可能是因为:")
            print("    1. Polymarket 使用 Chainlink BTC/USD 结算，与 Binance 价格不同")
            print("    2. fetch_poly_results.py 抓取逻辑有误")
            print("    3. 结算时间点不同（Binance vs Polymarket）")
    else:
        print("无法获取足够数据进行对比")

if __name__ == '__main__':
    main()
