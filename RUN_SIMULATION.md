# 模拟盘运行指南

## 快速启动

```powershell
cd e:\ProjectMyNew\PolymarketBot\PolymarketBot
python bot.py --dry-run
```

---

## 持续运行方案

### 方式一：PM2 管理（推荐）

PM2 支持崩溃自动重启、开机自启、日志管理。

```powershell
# 安装 PM2（首次执行）
npm install -g pm2

# 启动机器人
pm2 start python --interpreter python --name polymarket-bot -- "bot.py --dry-run"

# 设置开机自启（按提示复制输出的命令并执行）
pm2 startup
pm2 save

# 查看状态
pm2 list

# 查看实时日志
pm2 logs polymarket-bot

# 重启
pm2 restart polymarket-bot

# 停止
pm2 stop polymarket-bot
```

---

### 方式二：Windows 任务计划程序

创建启动脚本 `e:\ProjectMyNew\PolymarketBot\PolymarketBot\run_bot.bat`：

```bat
@echo off
cd /d e:\ProjectMyNew\PolymarketBot\PolymarketBot
python bot.py --dry-run
```

添加任务计划程序：
1. 打开「任务计划程序」
2. 创建基本任务 → 命名如 `PolymarketBot`
3. 触发器选「计算机启动时」
4. 操作选「启动程序」，程序填 `run_bot.bat` 完整路径
5. 完成

---

### 方式三：PowerShell 后台运行

```powershell
# 启动（隐藏窗口）
Start-Process -FilePath python -ArgumentList "bot.py --dry-run" -WorkingDirectory "e:\ProjectMyNew\PolymarketBot\PolymarketBot" -WindowStyle Hidden

# 查看是否在运行
Get-Process python | Where-Object { $_.CommandLine -like "*bot.py*" }

# 停止
Get-Process python | Where-Object { $_.CommandLine -like "*bot.py*" } | Stop-Process
```

---

### 方式四：nohup 后台运行（Git Bash / WSL）

```bash
cd e:/ProjectMyNew/PolymarketBot/PolymarketBot
nohup python bot.py --dry-run > bot_dry_run.log 2>&1 &

# 查看日志
tail -f bot_dry_run.log

# 停止
pkill -f "bot.py --dry-run"
```

---

## 当前配置（.env）

| 参数 | 值 | 说明 |
|------|-----|------|
| `STARTING_BANKROLL` | 50.0 | 起始虚拟资金 |
| `MIN_BET` | 0.5 | 最小下单金额 |
| `BOT_MODE` | safe | 策略模式 |
| `DRY_RUN_BINANCE_SETTLE` | 1 | 干跑用 Binance 结算 |
| `SNIPE_PRICE_SOURCE` | oracle | 狙击价格来源 |
| `SNIPE_START` | 60 | 距收盘秒数开始狙击 |
| `MIN_ABS_SCORE` | 3.0 | 最低 \|score\| 过滤 |
| `MIN_DECISION_CONFIDENCE` | 0.3 | 最低置信度过滤 |

---

## 常用命令

```powershell
# 只跑一个窗口后退出
python bot.py --dry-run --once

# 最多 N 笔后退出
python bot.py --dry-run --max-trades 10

# aggressive 模式
python bot.py --dry-run --mode aggressive
```
