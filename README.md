# Polymarket BTC Bot

 Polymarket 5分钟 BTC 涨跌预测交易机器人

## 项目结构

```
PolymarketBot/
├── core/                    # 核心代码
│   ├── __init__.py         # 模块初始化
│   ├── __main__.py         # 入口点 (python -m core.bot)
│   ├── bot.py              # 主程序入口
│   ├── chainlink_rtds.py   # RTDS WebSocket 数据源
│   ├── strategy.py         # 交易策略 (TA 分析)
│   ├── trading_logic.py    # 交易逻辑 (仓位计算)
│   ├── trading_journal.py  # 交易日志
│   └── backtest.py         # Binance K 线获取
├── scripts/                # 工具脚本
│   ├── backtest.py         # 回测分析
│   ├── compare_results.py  # 结果对比
│   ├── compare_runs.py     # 多配置对比
│   ├── fetch_poly_results.py # 抓取 Polymarket 结果
│   ├── verify_outcomes.py   # 验证结果
│   ├── rtds_debug.py       # RTDS 调试
│   ├── rtds_realtime_check.py # RTDS 自检
│   ├── rtds_window_now.py  # 查看当前窗口
│   ├── rtds_wait_boundary.py # 等待窗口边界
│   ├── open_price_test.py  # 开盘价测试
│   ├── backtest_vs_polymarket.py # 回测对比
│   ├── setup_creds.py     # 配置凭证
│   └── auto_claim.py      # 自动赎回
├── docs/                   # 文档
│   ├── README.md           # 文档索引
│   ├── CHANGELOG.md        # 版本变更记录
│   ├── COMMANDS.md         # 命令详解
│   ├── TRADING_AND_SYSTEM_LOGIC.md # 交易与系统逻辑
│   └── RUN_SIMULATION.md   # 模拟盘运行指南
├── data/                   # 数据目录
│   └── README.md
├── .env                    # 环境变量（需创建）
├── .gitignore
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件：

```bash
# Polymarket API
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret

# 可选配置
USE_CHAINLINK_RTDS=1           # 使用 RTDS 数据源
SNIPE_PRICE_SOURCE=oracle      # 狙击价格来源 (oracle/binance)
DRY_RUN=1                      # 干跑模式
```

### 3. 运行

```bash
# 方式一：使用 -m 模块运行（推荐）
python -m core.bot --dry-run
python -m core.bot --dry-run --once

# 方式二：直接运行入口点
python core/__main__.py --dry-run

# 重置所有历史数据
python -m core.bot --reset-history
```

## 核心功能

| 功能 | 说明 |
|------|------|
| **RTDS 实时数据** | 通过 Chainlink RTDS WebSocket 获取 BTC/USD 实时价格 |
| **窗口追踪** | 5分钟窗口开盘价/收盘价追踪，与 Demo 一致 |
| **智能狙击** | 在窗口关闭前自动分析并下单 |
| **套利检测** | 检测双边价差机会 |
| **交易日志** | 记录每笔交易详情到 CSV/Excel |

## 命令行参数

```bash
python -m core.bot [选项]

选项:
  --dry-run           干跑模式：模拟流程，不下真实单
  --once              只跑一个交易周期后退出
  --max-trades N      最多完成 N 笔后退出，0 表示不限制
  --reset-history     重置所有历史数据
  --mode MODE         策略模式：safe / aggressive / degen
```

## 文档

- [命令详解](docs/COMMANDS.md) - 所有命令和参数说明
- [交易逻辑](docs/TRADING_AND_SYSTEM_LOGIC.md) - 核心交易逻辑详解
- [版本记录](docs/CHANGELOG.md) - 版本变更历史
- [运行指南](docs/RUN_SIMULATION.md) - 持续运行方案

## 环境变量

详见 [COMMANDS.md](docs/COMMANDS.md#6-环境变量速查)

## 版本

当前版本：**v2.2**

主要更新：
- 窗口动量信号 `window_momentum`
- RTDS Tick 开盘价
- 狙击期间持续套利监控
