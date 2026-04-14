"""
快速抓取 Polymarket BTC 5m 结果进行验证（逐个获取）。

用法：
    python fetch_poly_results.py              # 抓取过去48小时内已结算的数据
    python fetch_poly_results.py --hours 72   # 抓取过去72小时
"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta
import requests

OUTPUT_FILE = "polymarket_outcomes.json"


def get_resolution_from_prices(prices):
    """
    从 outcomePrices 推断结算结果。
    Polymarket 结算后，赢家的概率变为 1，输家变为 0。
    所以 prices[0] >= prices[1] 时 Up 赢。
    """
    if len(prices) >= 2:
        if prices[0] >= prices[1]:
            return "Up"
        else:
            return "Down"
    return None


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


def load_existing():
    """加载已存在的文件，如果不存在返回空字典。"""
    try:
        with open(OUTPUT_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_immediate(outcomes: dict, ts: int, result: dict):
    """每抓一个立即写入文件。"""
    outcomes[str(ts)] = result
    with open(OUTPUT_FILE, "w") as f:
        json.dump(outcomes, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="抓取 Polymarket BTC 5m 结算结果")
    parser.add_argument("--hours", type=int, default=48,
                       help="抓取过去多少小时的数据（默认48小时）")
    args = parser.parse_args()

    # 清空文件
    with open(OUTPUT_FILE, "w") as f:
        json.dump({}, f)
    print(f"[初始化] 已清空 {OUTPUT_FILE}")

    # UTC 时区
    utc_tz = timezone.utc

    # 计算时间范围
    now_ts = int(time.time())
    start_ts = now_ts - args.hours * 3600

    # 找到第一个窗口起点（5分钟的整数倍）
    first_window = (start_ts // 300) * 300

    # 计算窗口数量
    num_windows = (now_ts - first_window) // 300

    print(f"抓取过去 {args.hours} 小时的数据...")
    print(f"时间范围（UTC）:")
    print(f"  开始: {datetime.fromtimestamp(start_ts, tz=utc_tz).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  结束: {datetime.fromtimestamp(now_ts, tz=utc_tz).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"窗口数量: {num_windows}")
    print("-" * 60)

    outcomes = {}
    closed_count = 0
    not_closed_count = 0
    error_count = 0

    for i in range(num_windows):
        ts = first_window + i * 300
        slug = f"btc-updown-5m-{ts}"

        market = fetch_single(slug)

        if market:
            closed = market.get("closed", False)
            resolution = None

            if market.get("markets"):
                m = market["markets"][0]

                # 优先使用 API 返回的 resolution 字段
                resolution = m.get("resolution", "")

                # 如果没有 resolution 字段，从 outcomePrices 推断
                if not resolution:
                    try:
                        prices = json.loads(m.get("outcomePrices", "[]"))
                        if len(prices) >= 2:
                            prices = [float(p) for p in prices]
                            resolution = get_resolution_from_prices(prices)
                    except:
                        pass

            result = {"closed": closed, "resolution": resolution}
            outcomes[str(ts)] = result

            if closed:
                closed_count += 1
                dt = datetime.fromtimestamp(ts, tz=utc_tz).strftime("%m-%d %H:%M")
                print(f"  [已结算] {dt} (ts={ts}) -> {resolution}")
            else:
                not_closed_count += 1
        else:
            result = {"error": True}
            outcomes[str(ts)] = result
            error_count += 1

        # 每抓一个立即写入文件
        save_immediate(outcomes, ts, result)

        # 每 100 个窗口打印进度
        if (i + 1) % 100 == 0:
            progress = (i + 1) / num_windows * 100
            print(f"  进度: {i+1}/{num_windows} ({progress:.1f}%) - 已结算: {closed_count}, 未结算: {not_closed_count}")

        time.sleep(0.02)  # 避免过快请求

    print("-" * 60)
    print(f"抓取完成:")
    print(f"  总窗口数: {len(outcomes)}")
    print(f"  已结算: {closed_count}")
    print(f"  未结算: {not_closed_count}")
    print(f"  错误: {error_count}")

    # 统计涨跌分布（只统计已结算的）
    settled = {k: v for k, v in outcomes.items() if v.get("closed")}
    up = sum(1 for v in settled.values() if v.get("resolution") == "Up")
    down = sum(1 for v in settled.values() if v.get("resolution") == "Down")

    if settled:
        print(f"  已结算分布: Up={up} ({up/len(settled)*100:.1f}%), Down={down} ({down/len(settled)*100:.1f}%)")

    # 最终保存
    with open(OUTPUT_FILE, "w") as f:
        json.dump(outcomes, f, indent=2)

    print(f"已保存到 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
