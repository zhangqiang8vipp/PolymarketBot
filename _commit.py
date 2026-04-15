"""快速提交所有修复"""
import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

msg = """fix: window_open_oracle 概率归一化 + 开盘价 Gamma bid-ask 中点优先

1. _gamma_window_open_px: 优先用 bestBid/bestAsk 中点(直接是概率),
   次选 lastTradePrice, 窗口刚开无成交时 bid-ask 中点仍然可用
2. RTDS/Binance 路径: 归一化公式修复 prob = 1 - ref/px
3. w_pct: 统一在 BTC/USD 单位域计算 (px_decide/window_open_btc - 1)*100%
4. decide_reversal_direction: 参数改为 window_open_btc_price
5. Binance kline read timeout 5s -> 15s
6. datetime.utcfromtimestamp -> timezone-aware datetime
7. 新增每日交易日志 trading_journal.csv
8. 狙击提前退出: 连续4次置信不足则窗口末段主动跳过
9. _skip_and_journal: 所有跳过路径统一记录 journal
"""

subprocess.run(["git", "add", "bot.py", "backtest.py", "trading_journal.py", "batch_run.py"], check=True)
subprocess.run(["git", "commit", "-m", msg], check=True)
print("Done!")
