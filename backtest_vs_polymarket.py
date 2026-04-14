"""
对比回测预测 vs Polymarket 实际结果
关键：回测用 Binance K 线判断（窗口首根 open vs 末根 close），
但 Polymarket 用 Chainlink BTC/USD 结算，可能存在差异！
"""
import argparse
import json
from datetime import datetime, timezone
from openpyxl import Workbook

from compare_runs import simulate, key, SIZING_MODES, THRESHOLDS
from backtest import fetch_klines_range_hours


def main():
    parser = argparse.ArgumentParser(description="回测预测 vs Polymarket 实际结算对比")
    parser.add_argument("--hours", type=int, default=48, help="回测时间范围（小时）")
    parser.add_argument("--initial", type=float, default=20.0, help="初始资金（默认20）")
    parser.add_argument("--min-bet", type=float, default=1.0, help="最小下注（默认1）")
    args = parser.parse_args()

    print("=" * 80)
    print("回测预测 vs Polymarket 实际结算 对比分析")
    print(f"初始资金: ${args.initial:.2f} | 时间范围: {args.hours}小时")
    print("=" * 80)
    print()

    # 1. 加载 Polymarket 实际结果（只取已结算的）
    try:
        with open("polymarket_outcomes.json", "r") as f:
            poly_data = json.load(f)
    except FileNotFoundError:
        print("错误: polymarket_outcomes.json 不存在")
        print("请先运行: python fetch_poly_results.py")
        return

    settled = {int(k): v for k, v in poly_data.items() if v.get("closed", False)}
    print(f"Polymarket 已结算窗口数: {len(settled)}")

    # 统计 Polymarket 实际分布
    up_count = sum(1 for v in settled.values() if v.get("resolution") == "Up")
    down_count = sum(1 for v in settled.values() if v.get("resolution") == "Down")
    print(f"Polymarket 实际: Up={up_count}, Down={down_count}, Up比例={up_count/len(settled)*100:.1f}%")
    print()

    # 2. 获取回测数据
    print("正在拉取 48 小时 Binance K 线数据...")
    rows = fetch_klines_range_hours(48)
    print(f"K线数量: {len(rows)}")
    print()

    print("正在运行回测模拟...")
    states, _ = simulate(rows, min_bet=args.min_bet, initial=args.initial, verbose=False)
    print()

    # 3. 分析所有配置
    print("=" * 80)
    print("各配置预测准确率对比")
    print("=" * 80)
    print()

    results_summary = []

    for mode in SIZING_MODES:
        for th in THRESHOLDS:
            k = key(mode, th)
            trades = states[k].trade_log

            if len(trades) == 0:
                continue

            matches = 0
            total = 0
            pred_up_correct = 0
            pred_up_total = 0
            pred_down_correct = 0
            pred_down_total = 0

            for trade in trades:
                window_ts = trade["window_ts"]

                # Polymarket 真实结果
                poly = settled.get(window_ts)
                if not poly:
                    continue

                poly_result = poly.get("resolution", "")  # "Up" or "Down"

                # 回测预测
                pred_up = trade["direction"] == 1
                pred_str = "Up" if pred_up else "Down"

                # 是否正确
                correct = (pred_str.lower() == poly_result.lower())
                if correct:
                    matches += 1
                total += 1

                if pred_up:
                    pred_up_total += 1
                    if correct:
                        pred_up_correct += 1
                else:
                    pred_down_total += 1
                    if correct:
                        pred_down_correct += 1

            if total > 0:
                accuracy = matches / total * 100
                up_acc = pred_up_correct / pred_up_total * 100 if pred_up_total > 0 else 0
                down_acc = pred_down_correct / pred_down_total * 100 if pred_down_total > 0 else 0

                results_summary.append({
                    "config": k,
                    "trades": total,
                    "accuracy": accuracy,
                    "up_trades": pred_up_total,
                    "up_accuracy": up_acc,
                    "down_trades": pred_down_total,
                    "down_accuracy": down_acc,
                })

    # 按准确率排序打印
    results_summary.sort(key=lambda x: x["accuracy"], reverse=True)

    print(f"{'配置':<15} | {'交易数':>6} | {'准确率':>8} | {'预测涨':>6} | {'涨准确':>8} | {'预测跌':>6} | {'跌准确':>8}")
    print("-" * 90)

    for r in results_summary[:15]:  # 只打印前 15 个
        print(f"{r['config']:<15} | {r['trades']:>6} | {r['accuracy']:>7.1f}% | {r['up_trades']:>6} | {r['up_accuracy']:>7.1f}% | {r['down_trades']:>6} | {r['down_accuracy']:>7.1f}%")

    print()
    print("=" * 80)

    # 4. 详细对比最佳配置
    if results_summary:
        best = results_summary[0]
        print(f"最佳配置: {best['config']} - 准确率 {best['accuracy']:.1f}%")
        print()

        # 打印详细交易记录
        best_key = key(best['config'].split('_')[0], float(best['config'].split('_')[1]))
        trades = states[best_key].trade_log

        print("详细交易对比（前 30 笔）:")
        print("-" * 100)
        print(f"{'#':>3} | {'窗口时间':>18} | {'Polymarket':^10} | {'回测预测':^10} | {'正确':^6} | {'置信':>6} | {'得分':>6}")
        print("-" * 100)

        detail_rows = []
        for i, trade in enumerate(trades[:30]):
            window_ts = trade["window_ts"]
            poly = settled.get(window_ts)
            if not poly:
                continue

            poly_result = poly.get("resolution", "")
            pred_up = trade["direction"] == 1
            pred_str = "Up" if pred_up else "Down"
            correct = "YES" if pred_str.lower() == poly_result.lower() else "NO"

            dt = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%m-%d %H:%M")

            print(f"{i+1:>3} | {dt:>18} | {poly_result:^10} | {pred_str:^10} | {correct:^6} | {trade['conf']:>6.2f} | {trade['score']:>6.1f}")

            detail_rows.append({
                "序号": i + 1,
                "窗口时间": dt,
                "Polymarket结果": poly_result,
                "回测预测": pred_str,
                "是否正确": correct,
                "置信度": round(trade["conf"], 2),
                "得分": round(trade["score"], 1),
            })

        print("-" * 100)

        # 5. 导出 Excel
        wb = Workbook()
        ws = wb.active
        ws.title = "准确率汇总"

        # 汇总表
        headers = ["配置", "交易数", "准确率%", "预测涨次数", "涨准确率%", "预测跌次数", "跌准确率%"]
        ws.append(headers)

        for r in results_summary:
            ws.append([
                r["config"],
                r["trades"],
                round(r["accuracy"], 1),
                r["up_trades"],
                round(r["up_accuracy"], 1),
                r["down_trades"],
                round(r["down_accuracy"], 1),
            ])

        # 详细记录
        ws2 = wb.create_sheet("详细交易")
        ws2.append(["序号", "窗口时间", "Polymarket结果", "回测预测", "是否正确", "置信度", "得分"])
        for row in detail_rows:
            ws2.append([
                row["序号"],
                row["窗口时间"],
                row["Polymarket结果"],
                row["回测预测"],
                row["是否正确"],
                row["置信度"],
                row["得分"],
            ])

        wb.save("backtest_vs_polymarket.xlsx")
        print()
        print(f"详细对比已保存到 backtest_vs_polymarket.xlsx")

    # 6. 结论
    print()
    print("=" * 80)
    print("结论:")
    print("=" * 80)

    if results_summary:
        best = results_summary[0]
        accuracy = best["accuracy"]

        if accuracy < 40:
            print(f"  回测准确率 {accuracy:.1f}% 低于随机（50%），策略需要改进！")
        elif accuracy < 48:
            print(f"  回测准确率 {accuracy:.1f}% 略低于随机，可能受 Polymarket vs Binance 差异影响")
        elif accuracy < 52:
            print(f"  回测准确率 {accuracy:.1f}% 接近随机（50%）")
        else:
            print(f"  回测准确率 {accuracy:.1f}% 表现良好！")

        print()
        print("  注意: Polymarket 使用 Chainlink BTC/USD 结算，Binance 可能略有差异")
        print("  建议对比 Binance vs Polymarket 结算的匹配率来评估数据源差异")


if __name__ == "__main__":
    main()
