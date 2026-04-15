"""
对比回测预测 vs Polymarket 实际结果（使用真实结算数据）。
关键：Polymarket 用 Chainlink BTC/USD 结算，Binance 可能不同！
"""

import json
from datetime import datetime, timezone
from openpyxl import Workbook

from compare_runs import simulate, key, SIZING_MODES, THRESHOLDS
from backtest import fetch_klines_range_hours


def main():
    # 1. 加载 Polymarket 实际结果（只取已结算的）
    with open("polymarket_outcomes.json", "r") as f:
        poly_data = json.load(f)

    settled = {int(k): v for k, v in poly_data.items() if v.get("closed", False)}
    print(f"已结算窗口数: {len(settled)}")

    # 实际统计
    up_count = sum(1 for v in settled.values() if v.get("resolution") == "Up")
    down_count = sum(1 for v in settled.values() if v.get("resolution") == "Down")
    print(f"Polymarket 实际: Up={up_count}, Down={down_count}, Up比例={up_count/len(settled)*100:.0f}%")

    # 2. 获取回测数据
    rows = fetch_klines_range_hours(48)
    print(f"K线数量: {len(rows)}")

    states, _ = simulate(rows, min_bet=1.0, initial=100.0, verbose=False)

    # 3. 逐笔对比（使用 flat_0.1 配置）
    best_key = key("flat", 0.1)
    trades = states[best_key].trade_log

    # 对比
    comparison = []
    matches = 0
    total = 0

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

        dt = datetime.fromtimestamp(window_ts, tz=timezone.utc)

        # 计算盈亏（与 compare_runs.py simulate() 逻辑一致）
        shares = trade["bet"] / trade["entry"] if trade["entry"] > 0 else 0.0
        pnl = (shares - trade["bet"]) if trade["win"] else (-trade["bet"])

        comparison.append({
            "序号": len(comparison) + 1,
            "窗口时间(UTC)": dt.strftime("%m-%d %H:%M"),
            "Polymarket结果": poly_result,
            "回测预测": pred_str,
            "预测方向": "涨" if pred_up else "跌",
            "是否正确": "正确" if correct else "错误",
            "置信度": round(trade["conf"], 2),
            "得分": round(trade["score"], 1),
            "入场价": round(trade["entry"], 4),
            "下注金额": round(trade["bet"], 4),
            "盈亏": round(pnl, 4),
            "胜负": "赢" if trade["win"] else "输",
        })

    # 4. 打印对比
    print("\n" + "=" * 80)
    print("回测预测 vs Polymarket 真实结算 对比（flat_0.1 配置）")
    print("=" * 80)
    print(f"对比窗口数: {total}")
    print(f"预测正确数: {matches}")
    print(f"预测准确率: {matches/total*100:.1f}%" if total else "N/A")
    print("-" * 80)

    print(f"{'序号':>4} | {'时间(UTC)':>12} | {'Poly结果':^8} | {'回测预测':^8} | {'正确':^6} | {'置信度':^6} | {'入场价':>8} | {'下注':>8} | {'盈亏':>8}")
    print("-" * 90)

    total_pnl = 0.0
    for row in comparison:
        total_pnl += row["盈亏"]
        correct_mark = "O" if row["是否正确"] == "正确" else "X"
        pnl_s = f"{row['盈亏']:+.4f}"
        print(f"{row['序号']:>4}. | {row['窗口时间(UTC)']:>12} | {row['Polymarket结果']:^8} | {row['回测预测']:^8} | {correct_mark:^6} | {row['置信度']:>6.2f} | {row['入场价']:>8.4f} | {row['下注金额']:>8.4f} | {pnl_s:>8}")

    print("-" * 90)
    print(f"总盈亏: {total_pnl:+.4f}   终资金: {100.0 + total_pnl:.4f}")

    # 5. 按预测方向分组统计
    pred_up_list = [r for r in comparison if r["预测方向"] == "涨"]
    pred_down_list = [r for r in comparison if r["预测方向"] == "跌"]

    up_correct = sum(1 for r in pred_up_list if r["是否正确"] == "正确")
    down_correct = sum(1 for r in pred_down_list if r["是否正确"] == "正确")

    print(f"\n按预测方向统计:")
    print(f"  预测涨: {len(pred_up_list)} 次, 正确 {up_correct} 次, 准确率 {up_correct/len(pred_up_list)*100:.0f}%" if pred_up_list else "  预测涨: 0 次")
    print(f"  预测跌: {len(pred_down_list)} 次, 正确 {down_correct} 次, 准确率 {down_correct/len(pred_down_list)*100:.0f}%" if pred_down_list else "  预测跌: 0 次")

    print(f"\n实际市场分布: Up={up_count} ({up_count/len(settled)*100:.0f}%), Down={down_count} ({down_count/len(settled)*100:.0f}%)")
    print(f"回测预测分布: Up={len(pred_up_list)} ({len(pred_up_list)/len(comparison)*100:.0f}%), Down={len(pred_down_list)} ({len(pred_down_list)/len(comparison)*100:.0f}%)")

    # 6. 导出 Excel（中文表头）
    wb = Workbook()
    ws = wb.active
    ws.title = "预测对比"

    # 表头
    headers = ["序号", "窗口时间(UTC)", "Polymarket结果", "回测预测", "预测方向", "是否正确",
               "置信度", "得分", "入场价", "下注金额", "盈亏", "胜负"]
    ws.append(headers)

    # 数据
    total_pnl = 0.0
    for row in comparison:
        total_pnl += row["盈亏"]
        ws.append([
            row["序号"],
            row["窗口时间(UTC)"],
            row["Polymarket结果"],
            row["回测预测"],
            row["预测方向"],
            row["是否正确"],
            row["置信度"],
            row["得分"],
            row["入场价"],
            row["下注金额"],
            row["盈亏"],
            row["胜负"],
        ])

    # 统计表
    ws.append([])
    ws.append(["统计汇总"])
    ws.append(["对比窗口总数", total])
    ws.append(["预测正确数", matches])
    ws.append(["预测准确率", f"{matches/total*100:.1f}%" if total else "N/A"])
    ws.append(["总盈亏", f"{total_pnl:+.4f}"])
    ws.append(["终资金（flat_0.1）", f"{100.0 + total_pnl:.4f}"])
    ws.append([])
    ws.append(["预测涨次数", len(pred_up_list)])
    ws.append(["预测涨准确率", f"{up_correct/len(pred_up_list)*100:.0f}%" if pred_up_list else "N/A"])
    ws.append(["预测跌次数", len(pred_down_list)])
    ws.append(["预测跌准确率", f"{down_correct/len(pred_down_list)*100:.0f}%" if pred_down_list else "N/A"])
    ws.append([])
    ws.append(["Polymarket实际Up", up_count, f"({up_count/len(settled)*100:.0f}%)"])
    ws.append(["Polymarket实际Down", down_count, f"({down_count/len(settled)*100:.0f}%)"])

    wb.save("prediction_vs_reality.xlsx")
    print(f"\n详细对比已保存到 prediction_vs_reality.xlsx")

    # 7. 保存 JSON
    with open("comparison_detail.json", "w", encoding="utf-8") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 80)
    print("结论:")
    print("=" * 80)
    if total > 0:
        accuracy = matches / total * 100
        total_pnl_final = sum(r["盈亏"] for r in comparison)
        final_bankroll = 100.0 + total_pnl_final
        if accuracy < 40:
            print(f"  回测准确率 {accuracy:.1f}% 低于随机（50%），策略需要改进！")
        elif accuracy < 55:
            print(f"  回测准确率 {accuracy:.1f}% 接近随机，略低于预期。")
        else:
            print(f"  回测准确率 {accuracy:.1f}% 表现良好！")
        print(f"  总盈亏: {total_pnl_final:+.4f}   终资金: {final_bankroll:.4f} ({final_bankroll/100:.2%})")


if __name__ == "__main__":
    main()
