"""
快速抓取 Polymarket BTC 5m 结果进行验证（逐个获取）。
"""

import json
import time
from datetime import datetime, timezone
import requests

def fetch_single(slug: str) -> dict | None:
    """获取单个市场。"""
    url = "https://gamma-api.polymarket.com/events"
    params = {"slug": slug}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
    except:
        pass
    return None

def main():
    base_ts = 1776197700  # 第一个窗口
    test_count = 30       # 只获取 30 个进行验证
    
    print(f"抓取 {test_count} 个窗口的实际结果...")
    print(f"时间: {datetime.fromtimestamp(base_ts, tz=timezone.utc)}")
    print("-" * 60)
    
    outcomes = {}
    
    for i in range(test_count):
        ts = base_ts + i * 300
        slug = f"btc-updown-5m-{ts}"
        
        market = fetch_single(slug)
        
        if market:
            closed = market.get("closed", False)
            resolution = None
            
            if market.get("markets"):
                m = market["markets"][0]
                resolution = m.get("resolution", "")
                
                # 从 outcome prices 推断
                if not resolution:
                    try:
                        prices = json.loads(m.get("outcomePrices", "[]"))
                        if len(prices) >= 2:
                            resolution = "Up" if float(prices[0]) < float(prices[1]) else "Down"
                    except:
                        pass
            
            outcomes[ts] = {"closed": closed, "resolution": resolution}
            
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
            res = resolution or "未结算"
            print(f"{i+1:2d}. {dt} | {res}")
        else:
            outcomes[ts] = {"error": True}
            print(f"{i+1:2d}. 错误")
        
        time.sleep(0.05)  # 避免过快
    
    print("-" * 60)
    
    up = sum(1 for v in outcomes.values() if v.get("resolution") == "Up")
    down = sum(1 for v in outcomes.values() if v.get("resolution") == "Down")
    total = len(outcomes)
    
    print(f"Up: {up} ({up/total*100:.0f}%) | Down: {down} ({down/total*100:.0f}%)")
    
    # 保存
    with open("polymarket_outcomes.json", "w") as f:
        json.dump({str(k): v for k, v in outcomes.items()}, f, indent=2)
    
    print(f"已保存到 polymarket_outcomes.json")

if __name__ == "__main__":
    main()
