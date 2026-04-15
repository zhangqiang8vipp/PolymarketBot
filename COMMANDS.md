# PolymarketBot 命令详解

本文档详细说明 PolymarketBot 项目中所有可用的命令、脚本及其使用方法。

---

## 目录

1. [核心交易机器人](#1-核心交易机器人)
2. [回测与分析](#2-回测与分析)
3. [数据获取](#3-数据获取)
4. [RTDS 数据源](#4-rtds-数据源)
5. [凭证与配置](#5-凭证与配置)
6. [环境变量速查](#6-环境变量速查)

---

## 1. 核心交易机器人

### 1.1 启动机器人（干跑模式）

干跑模式：不下真实订单，用于测试策略和积累数据。

```bash
# 基本干跑（使用默认配置）
python bot.py --dry-run

# 干跑 + 仅执行一个交易周期后退出
python bot.py --dry-run --once

# 干跑 + 限制最大交易次数
python bot.py --dry-run --max-trades 10

# 指定策略模式（safe/aggressive/degen）
python bot.py --dry-run --mode safe
```

**参数说明：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--dry-run` | 干跑模式，不下真实单 | 必填 |
| `--once` | 只跑一个周期后退出 | False |
| `--max-trades N` | 最多完成 N 笔交易后退出 | 0（不限制） |
| `--mode MODE` | 策略模式 | safe |

**策略模式说明：**

| 模式 | 说明 | 最低置信度 |
|------|------|-----------|
| `safe` | 保守模式 | 0.45 |
| `aggressive` | 激进模式 | 0.35 |
| `degen` | 极限模式 | 0.0（不设限） |

### 1.2 启动机器人（实盘模式）

**⚠️ 警告：实盘模式会使用真实资金！请确保已完成以下准备：**

1. 配置 Polymarket CLOB API 凭证（见 `setup_creds.py`）
2. 钱包中有足够的 USDC
3. 已充分在干跑模式测试

```bash
# 实盘交易（必须先配置环境变量）
python bot.py --mode safe

# 实盘 + 仅执行一个周期
python bot.py --once --mode safe
```

---

## 2. 回测与分析

### 2.1 网格回测（compare_runs.py）

多组置信阈值 × 仓位模式的网格回测，评估不同参数组合的表现。

```bash
# 基本回测（拉取最近 48 小时 K 线）
python compare_runs.py

# 指定回测时间范围（小时）
python compare_runs.py --hours 72

# 指定输出文件名
python compare_runs.py --output my_results.xlsx

# 指定初始资金
python compare_runs.py --initial 1000

# 指定最小下注
python compare_runs.py --min-bet 5

# 打印详细统计信息
python compare_runs.py --verbose
```

**完整参数：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--hours HOURS` | 拉取最近多少小时的 K 线 | 48 |
| `--output PATH` | 输出 Excel 路径 | results_fixed.xlsx |
| `--min-bet AMOUNT` | 最小下注金额 | 1.0 |
| `--initial AMOUNT` | 初始资金 | 100.0 |
| `--verbose` | 打印详细统计 | False |

**输出 Excel 包含以下 sheets：**

- `Summary`：所有配置组合的汇总对比
- `Best Config Trades`：最佳配置的所有交易明细
- `Bankroll Curves`：各配置的资金曲线

### 2.2 预测 vs 实际对比（compare_results.py）

将回测预测与 Polymarket 实际结算结果进行对比分析。

```bash
# 运行对比分析
python compare_results.py
```

**前置条件：**

1. `polymarket_outcomes.json` - 已抓取的 Polymarket 实际结算数据
2. 运行过 `compare_runs.py` 生成回测结果

**输出文件：**

- `prediction_vs_reality.xlsx` - 预测准确率分析 Excel
- `comparison_detail.json` - 详细对比数据 JSON

---

## 3. 数据获取

### 3.1 抓取 Polymarket 实际结果（fetch_poly_results.py）

快速抓取 Polymarket BTC 5m 市场的实际结算结果，用于验证策略准确性。

```bash
# 运行抓取脚本
python fetch_poly_results.py
```

**功能说明：**

- 从 Polymarket API 获取市场结算信息
- 支持批量抓取多个窗口
- 输出 `polymarket_outcomes.json` 文件

**输出文件格式：**

```json
{
  "1776197700": {
    "closed": true,
    "resolution": "Up"
  },
  "1776198000": {
    "closed": true,
    "resolution": "Down"
  }
}
```

---

## 4. RTDS 数据源

### 4.1 RTDS 自检脚本（chainlink_rtds.py）

测试 Polymarket RTDS WebSocket 连接是否正常，获取 Chainlink BTC/USD 价格数据。

```bash
# 运行自检（约 15 秒超时）
python chainlink_rtds.py
```

**自检内容：**

1. 连接 `wss://ws-live-data.polymarket.com`
2. 订阅 `crypto_prices_chainlink` 主题
3. 等待接收 btc/usd 价格 tick
4. 打印连接状态和最新价格

**成功输出示例：**

```
RTDS 自检：连接 wss 并订阅 crypto_prices_chainlink / btc/usd …
  [状态] 已连接
  [状态] 解析到 btc/usd tick
  [结果] 可用：tick=10 时间戳ms范围=[1734567890000, 1734567950000] 最新价=97543.21
  [WS] WS 正常；btc/usd 缓冲约 10s 内有更新 → 可认为 WS+解析链路可用
  [WS] json={"last_frame_rx_s_ago": 2.5, "last_btc_tick_rx_s_ago": 2.5, "last_pong_rx_s_ago": null}
```

**常见失败原因：**

- 网络问题（防火墙、代理）
- WebSocket 端口被阻断
- Polymarket RTDS 服务不可用

---

## 5. 凭证与配置

### 5.1 CLOB API 凭证生成（setup_creds.py）

从钱包私钥推导 CLOB API 凭证（仅需运行一次）。

```bash
# 基本用法（交互式输入私钥）
python setup_creds.py

# 通过环境变量指定私钥
POLY_PRIVATE_KEY=your_private_key python setup_creds.py
```

**输出内容：**

```
CLOB API 凭证（请添加到 .env 文件）：
POLY_API_KEY=your_api_key
POLY_API_SECRET=your_api_secret
POLY_API_PASSPHRASE=your_passphrase
POLY_FUNDER_ADDRESS=your_funder_address
```

**⚠️ 安全警告：**

- 私钥会显示在终端，请确保周围无人
- 建议使用专用测试钱包，不要使用主钱包

---

## 6. 环境变量速查

### 6.1 资金与下注

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `STARTING_BANKROLL` | 起始资金（干跑/实盘） | 1.0 |
| `MIN_BET` | 最小下注金额 | 1.0 |
| `MAX_USD` | 方向单名义上限 | 不设限 |
| `FIXED_DIRECTIONAL_USD` | 固定方向单金额 | 不设限 |
| `BOT_MODE` | 默认策略模式 | safe |

### 6.2 狙击与交易

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `SNIPE_START` | 距收盘多少秒开始狙击 | 20（.env默认60） |
| `SNIPE_PRICE_SOURCE` | 狙击价格来源 | oracle |
| `SPIKE_JUMP` | 尖峰阈值 | 1.5 |
| `USE_BOOK_ASK_FOR_ENTRY` | 用盘口卖一作入场价 | 0 |

### 6.3 方向策略

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `DIRECTION_STRATEGY` | 方向策略 | ta |
| `MIN_DECISION_CONFIDENCE` | 最低置信度阈值 | 0.30（.env默认0.1） |
| `MIN_ABS_SCORE` | 最低得分绝对值 | 2.0（.env默认1.0） |
| `DIRECTION_ORDERBOOK_MAX_SUM` | 盘口合计上限（仅实盘） | 1.05 |
| `DIRECTION_ONLY_WHEN_BOOK_SUM_LT` | 仅低价差时交易 | 不设限 |
| `REVERSAL_MIN_ABS_PCT` | 反转最小偏离百分比 | 0.08 |
| `ORDERBOOK_IMBALANCE_DEPTH` | 失衡检测档位数 | 3 |
| `IMBALANCE_THRESHOLD` | 失衡阈值 | 0.25 |

**方向策略选项：**

- `ta`（默认）：技术分析信号
- `reversal`：均值回归反转
- `imbalance`：盘口失衡

### 6.4 概率与仓位

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `USE_FAIR_PROB_EDGE` | 使用合理概率优势 | 0 |
| `MIN_PRICE_EDGE` | 最小概率优势 | 0.03 |
| `USE_EDGE_POSITION_SIZING` | Edge 仓位计算 | 0 |
| `EDGE_SIZING_BANKROLL_FRAC` | Bankroll 比例 | 0.02 |
| `EDGE_SIZING_EDGE_SCALE` | Edge 缩放系数 | 10 |
| `ENABLE_KELLY` | Kelly 下注 | 0 |
| `KELLY_SCALE` | Kelly 乘子 | 0.25 |

### 6.5 套利

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `ENABLE_ARBITRAGE_LOG` | 套利日志 | 0 |
| `ENABLE_ARBITRAGE_TRADE` | 套利实盘交易 | 0 |
| `ARBITRAGE_SUM_ALERT` | 套利告警阈值 | 0.99 |
| `ARBITRAGE_POLL_S` | 套利轮询间隔秒 | 0 |

### 6.6 RTDS 与数据源

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `USE_CHAINLINK_RTDS` | 使用 RTDS | 1 |
| `POLY_RTDS_WS` | RTDS WebSocket URL | wss://ws-live-data.polymarket.com |
| `RTDS_WARMUP_S` | RTDS 预热秒数 | 1.5 |
| `RTDS_BUFFER_WAIT_S` | RTDS 缓冲等待秒数 | 12 |
| `RTDS_AUTO_RECONNECT_STALE_S` | RTDS 自动重连阈值 | 300 |
| `RTDS_OPEN_MAX_PAYLOAD_LAG_MS` | 开盘 tick 最大滞后 | 12000 |
| `RTDS_OPEN_ACCEPT_LATE_TICK` | 接受晚到 tick | 0 |

### 6.7 结算

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `DRY_RUN_BINANCE_SETTLE` | 干跑用 Binance 结算 | 1 |
| `DRY_RUN_SETTLE_AFTER_S` | 干跑结算等待秒数 | 2 |
| `LIVE_REDEEM_HINT_AFTER_S` | 实盘赎回提醒延迟 | 2 |

### 6.8 日志与调试

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `LOG_LEVEL` | 日志级别 | INFO |
| `LOG_FILE` | 日志文件路径 | 不设置 |
| `LOG_TS_MS` | 打印毫秒时间戳 | 1 |
| `WEBSOCKET_LOG` | WebSocket 日志 | 0 |
| `RTDS_DEBUG` | RTDS 调试信息 | 0 |
| `RTDS_FALLBACK_DEBUG` | RTDS 回退诊断 | 0 |

### 6.9 Binance 配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `BINANCE_REST_BASE` | Binance REST API | https://api.binance.com/api/v3 |
| `BINANCE_REST_BASE_FALLBACKS` | 备用 Binance API | 不设置 |
| `BINANCE_HTTP_RETRIES` | HTTP 重试次数 | 5 |
| `BTC_KLINE_NO_COINBASE_FALLBACK` | Coinbase 回退 | 0 |

### 6.10 训练与存档

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `TRADE_TRAIN_JSONL` | 训练数据输出路径 | 不设置 |
| `DRY_RUN_BANKROLL_FILE` | 干跑存档路径 | dry_run_bankroll.json |
| `DRY_RUN_HISTORY_MAX` | 流水最大条数 | 2000 |
| `BOT_TRADES_XLSX` | 干跑 Excel 交易记录路径 | bot_trades.xlsx |

---

## 快速开始

### 首次运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制环境变量模板
cp .env.example .env

# 3. 编辑 .env 配置必要参数
# 至少配置 STARTING_BANKROLL 和 MIN_BET

# 4. 运行 RTDS 自检
python chainlink_rtds.py

# 5. 运行网格回测
python compare_runs.py --hours 48

# 6. 干跑测试
python bot.py --dry-run --mode safe

# 7. 确认策略有效后，实盘交易
python bot.py --mode safe
```

### 常用命令组合

```bash
# 1. 每日监控命令
python bot.py --dry-run --mode safe --max-trades 50

# 2. 批量回测（不同时间范围）
python compare_runs.py --hours 24 --output results_24h.xlsx
python compare_runs.py --hours 72 --output results_72h.xlsx
python compare_runs.py --hours 168 --output results_week.xlsx

# 3. 预测准确性验证
python fetch_poly_results.py  # 先抓取实际结果
python compare_results.py     # 再对比分析

# 4. RTDS 连接诊断
RTDS_DEBUG=1 python chainlink_rtds.py
RTDS_FALLBACK_DEBUG=1 python bot.py --dry-run
```

---

## 故障排查

### 问题：RTDS 连接失败

```bash
# 检查网络
curl -I https://api.binance.com

# 测试 WebSocket
wscat -c wss://ws-live-data.polymarket.com

# 查看详细错误
RTDS_DEBUG=1 python chainlink_rtds.py
```

### 问题：回测拉不到 K 线

```bash
# 测试 Binance API
curl "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"

# 使用备用 API
export BINANCE_REST_BASE=https://api-gcp.binance.com/api/v3
python compare_runs.py
```

### 问题：干跑存档损坏

```bash
# 删除存档重新开始
rm dry_run_bankroll.json
python bot.py --dry-run
```

---

## 注意事项

1. **实盘风险**：实盘模式使用真实资金，请务必先在干跑模式充分测试
2. **网络要求**：需要稳定连接 Polymarket CLOB 和 Binance API
3. **资金管理**：建议设置 `MAX_USD` 限制单笔最大下注
4. **监控日志**：生产环境建议开启 `LOG_FILE` 记录完整日志
5. **时区**：所有时间使用 UTC，请确保系统时间准确
