# PolymarketBot

Polymarket BTC 5 分钟 Up/Down 自动化交易机器人。

> **重要提醒**：本项目仅供学习研究使用。实盘交易涉及真实资金损失风险，请充分在干跑模式测试后再考虑实盘。所有交易决策由算法自动做出，市场波动可能导致资金损失。

---

## 项目概述

### 这是什么？

PolymarketBot 是一个针对 [Polymarket](https://polymarket.com) 平台上 **BTC 5 分钟 Up/Down 预测市场**的自动化交易系统。

**核心功能：**

- 自动获取 Binance BTCUSDT 1 分钟 K 线数据（历史回退用 Coinbase 兜底）
- 基于多指标加权技术分析（TA）预测价格走向
- 在 5 分钟窗口的最后阶段（狙击阶段）自动下单
- 可选接入 Polymarket Chainlink RTDS WebSocket 获取实时价格
- 支持干跑（模拟交易）和实盘交易两种模式
- 干跑完成后自动写入 `bot_trades.xlsx` 记录每笔交易

### 工作原理

```
┌─────────────────────────────────────────────────────────────────┐
│                        5 分钟窗口周期                            │
├─────────────────────────────────────────────────────────────────┤
│  窗口开始（0s）    │    狙击开始（距收盘 SNIPE_START 秒）  │  窗口结束（300s）  │
│                    │         ↓                │                  │
│   获取窗口开盘价    │   每 POLL 秒轮询分析一次    │   结算判定输赢    │
│   (RTDS/Binance)   │   等待信号出现            │   自动赎回        │
└─────────────────────────────────────────────────────────────────┘
```

**Polymarket 市场机制：**

- 每 5 分钟产生一个新市场（slug 格式：`btc-updown-5m-{unix_timestamp}`）
- 预测窗口结束时 BTC 价格相对开盘价「涨」买 Up，「跌」买 Down
- 每份额 $1，猜对获得 $1，猜错损失下注金额
- 结算价格基于 Chainlink BTC/USD 数据源

---

## 目录结构

```
PolymarketBot/
├── bot.py                 # 主程序：交易循环、订单执行、干跑/实盘
├── strategy.py            # 技术分析引擎：多指标合成打分
├── backtest.py            # Binance K 线数据拉取（含 Coinbase 兜底）
├── compare_runs.py        # 网格回测：多参数组合测试
├── compare_results.py     # 回测 vs 实际结果对比
├── backtest_vs_polymarket.py  # 回测预测 vs Polymarket 实际对比
├── fetch_poly_results.py  # 抓取 Polymarket 实际结算数据
├── chainlink_rtds.py     # Polymarket RTDS WebSocket 连接
├── trading_logic.py       # 共享交易逻辑：仓位计算、入场价
├── setup_creds.py        # CLOB API 凭证生成工具
├── auto_claim.py         # Playwright 自动赎回脚本
├── TRADING_AND_SYSTEM_LOGIC.md  # 完整技术文档
├── COMMANDS.md           # 详细命令使用说明
├── CHANGELOG.md          # 策略演化记录
├── README.md             # 本文档
└── .env                  # 环境变量配置（需自行创建）
```

---

## 快速开始

### 1. 环境准备

```bash
# 克隆仓库
git clone https://github.com/zhangqiang8vipp/PolymarketBot.git
cd PolymarketBot

# 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
.\venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置文件

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，配置必要的参数
notepad .env
```

**基础配置示例（干跑）：**

```env
# 资金配置
STARTING_BANKROLL=100.0
MIN_BET=1.0

# 日志配置
LOG_LEVEL=INFO
LOG_TS_MS=1
```

### 3. RTDS 自检（推荐）

```bash
python chainlink_rtds.py
```

成功输出示例：

```
RTDS 自检：连接 wss 并订阅 crypto_prices_chainlink / btc/usd …
  [状态] 已连接
  [结果] 可用：tick=10 最新价=97543.21
  [WS] WS 正常；btc/usd 缓冲约 10s 内有更新
```

### 4. 运行回测

```bash
# 默认拉取 48 小时 K 线进行回测
python compare_runs.py

# 自定义参数
python compare_runs.py --hours 72 --output results_72h.xlsx --min-bet 5 --initial 1000
```

> **重要：Polymarket 结算延迟**
> - 窗口结束后约 **5-10 分钟** 才会结算
> - 因此 48 小时的窗口，最多只有约 **400-500 个已结算**（其余窗口还在等待结算）
> - 运行 `backtest_vs_polymarket.py` 前需先运行 `fetch_poly_results.py` 抓取足够数据
> - 初始资金默认 **$20**（可通过 `--initial 20` 指定）

### 5. 干跑测试

```bash
# 基础干跑（使用默认 safe 模式）
python bot.py --dry-run

# 仅运行一个周期
python bot.py --dry-run --once

# 限制交易次数
python bot.py --dry-run --max-trades 50

# 使用特定模式
python bot.py --dry-run --mode aggressive
```

### 6. 回测预测 vs Polymarket 实际对比

```bash
# 1. 先抓取 Polymarket 实际结算数据（需要几分钟）
python fetch_poly_results.py

# 2. 运行对比分析（初始资金 $20）
python backtest_vs_polymarket.py --initial 20 --hours 48
```

### 6. 实盘交易（谨慎！）

```bash
# 实盘前请确保：
# 1. 已充分在干跑模式测试
# 2. 配置了完整的 CLOB API 凭证
# 3. 钱包中有足够的 USDC

python bot.py --mode safe
```

---

## 核心组件详解

### 1. bot.py — 主交易引擎

**职责：**

- 管理主交易循环
- 从 Gamma API 获取市场信息
- 调用技术分析策略
- 执行订单（下单/撤销）
- 处理干跑和实盘结算
- 管理资金和交易记录

**核心流程：**

```
主循环 → 获取窗口信息 → 开盘价获取 → 套利检测
       → 狙击阶段轮询 → 信号分析 → 过滤检查
       → 计算下注金额 → 执行订单 → 入队结算
```

**关键环境变量：**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `STARTING_BANKROLL` | 起始资金 | 50.0 |
| `MIN_BET` | 最小下注 | 0.5 |
| `SNIPE_START` | 狙击提前秒数 | 20（.env默认60） |
| `DIRECTION_STRATEGY` | 方向策略 | ta |
| `BOT_MODE` | 策略模式 | degen |
| `MIN_DECISION_CONFIDENCE` | TA 最低置信度 | 0.30（.env默认0.1） |
| `MIN_ABS_SCORE` | 最低得分绝对值 | 2.0（.env默认1.0） |
| `BOT_TRADES_XLSX` | 干跑 Excel 记录路径 | bot_trades.xlsx |

### 2. strategy.py — 技术分析引擎

**分析指标：**

| 指标 | 权重范围 | 说明 |
|------|---------|------|
| 微动量（Micro Momentum） | -2 ~ +2 | 最近 1 分钟价格变动方向 |
| 加速度（Acceleration） | -1.5 ~ +1.5 | 价格变动速率变化 |
| EMA 交叉（EMA Cross） | -1 ~ +1 | 9 期 vs 21 期 EMA |
| RSI 信号（RSI Weight） | -2 ~ +2 | RSI 超买超卖区域 |
| 成交量突增（Volume Surge） | -1 ~ +1 | 成交量异常放大 |
| 趋势强度（Trend Strength） | -2 ~ +2 | 最近 10 根 K 线方向一致性 |
| Tick 趋势（Tick Trend） | -2 ~ +2 | 实盘 2s 采样数据趋势 |

**输出：**

```python
@dataclass
class AnalysisResult:
    direction: int      # 1=Up, -1=Down
    score: float        # 综合得分（范围约 -13 ~ +13）
    confidence: float   # 置信度（0 ~ 1）
    details: dict      # 各指标详细分项
```

**得分计算：**

- 所有子信号加权求和
- score > 0 → 预测 Up
- score < 0 → 预测 Down
- |score| 越大 → 置信度越高

### 3. backtest.py — 数据获取

**功能：**

- 从 Binance REST API 获取 1 分钟 K 线
- 支持自动重试和多备用地址
- Binance 不可用时自动切换 Coinbase
- 处理 K 线分页和大时间范围

**主要函数：**

```python
# 获取指定时间范围的 K 线
rows = fetch_klines_range_hours(hours=48)

# 获取指定时间戳范围的 K 线
rows = fetch_klines_1m_ts(symbol="BTCUSDT", start_ms=ts1, end_ms=ts2)

# 获取最近 N 根 K 线
candles = fetch_klines_1m(symbol="BTCUSDT", limit=60)

# 获取当前 BTC 价格
price = fetch_btc_spot_price_usdt()
```

### 4. compare_runs.py — 网格回测

**功能：**

- 多置信度阈值 × 多仓位模式组合测试
- 模拟真实交易流程
- 输出详细 Excel 报告

**参数网格：**

- 置信度阈值：0.0, 0.1, 0.2, ..., 0.8
- 仓位模式：flat（10%初始资金）、safe（25%当前资金）、aggressive（仅利润）

**仓位计算模式：**

| 模式 | 计算方式 | 风险等级 |
|------|---------|---------|
| flat | bet = min(bankroll, initial × 10%) | 低 |
| safe | bet = min(bankroll, bankroll × 25%) | 中 |
| aggressive | bet = min(bankroll, bankroll - principal) | 高 |

### 5. chainlink_rtds.py — 实时数据源

**功能：**

- 连接 Polymarket RTDS WebSocket
- 订阅 Chainlink BTC/USD 价格流
- 缓冲历史价格数据
- 自动重连机制

**连接地址：**

```
wss://ws-live-data.polymarket.com
```

**订阅主题：**

```json
{
  "action": "subscribe",
  "subscriptions": [{
    "topic": "crypto_prices_chainlink",
    "type": "*",
    "filters": "{\"symbol\":\"btc/usd\"}"
  }]
}
```

### 6. compare_results.py — 策略验证

**功能：**

- 将回测预测与 Polymarket 实际结算对比
- 计算预测准确率
- 按预测方向分组分析
- 输出详细 Excel 报告

**使用流程：**

```bash
# 1. 抓取 Polymarket 实际结果
python fetch_poly_results.py

# 2. 运行对比分析
python compare_results.py
```

### 7. setup_creds.py — 凭证生成

**功能：**

- 从钱包私钥推导 CLOB API 凭证
- 一次性工具，无需重复运行

**使用：**

```bash
python setup_creds.py
```

输出：

```
CLOB API 凭证（请添加到 .env 文件）：
POLY_API_KEY=your_key
POLY_API_SECRET=your_secret
POLY_API_PASSPHRASE=your_passphrase
POLY_FUNDER_ADDRESS=your_address
```

### 8. auto_claim.py — 自动赎回

**功能：**

- 使用 Playwright 浏览器自动化
- 定时检查 Portfolio 页面
- 自动点击 Redeem/Claim 按钮

**依赖：**

```bash
pip install playwright
playwright install chromium
```

**使用：**

```bash
# 有头模式（首次登录保存状态）
python auto_claim.py --headed

# 无头模式（后台运行）
python auto_claim.py
```

---

## 策略模式

### safe（保守模式）

- 最低置信度：0.45
- 仓位：最多 25% 当前资金
- 适合：资金较少、风险厌恶型用户

### aggressive（激进模式）

- 最低置信度：0.35
- 仓位：当前资金减去本金（拿利润冒险）
- 适合：资金充足、追求高收益

### degen（极限模式）

- 最低置信度：0.0（无限制）
- 仓位：全部资金
- 适合：资金充足、高风险偏好

---

## 交易逻辑详解

### 窗口周期

```
┌────────────────────────────────────────────────────────────────┐
│                      5 分钟窗口 (300 秒)                        │
├──────────────┬───────────────────────────────┬─────────────────┤
│   开盘期     │        等待期（休眠）         │    狙击期       │
│   0-5s      │        5s - 290s             │   290s-295s    │
├──────────────┼───────────────────────────────┼─────────────────┤
│ 获取开盘价   │   监控无活动                  │  每 2s 分析一次  │
│ RTDS/BN     │   定期套利检测                │  等待信号触发    │
└──────────────┴───────────────────────────────┴─────────────────┘
```

### 狙击阶段

狙击开始后，机器人每 2 秒执行一次分析：

```python
while True:
    # 1. 获取当前价格
    current_price = snipe_current_price()
    
    # 2. 获取最近 K 线
    candles = fetch_recent_candles_1m(limit=60)
    
    # 3. 技术分析
    result = analyze(candles, tick_prices=ticks)
    
    # 4. 检查退出条件
    if t_left < SNIPE_DEADLINE:
        break
    
    # 5. 检查尖峰（score 突变）
    if abs(result.score - last_score) >= SPIKE_JUMP:
        return result  # 立即发射
    
    # 6. 检查置信度
    if result.confidence >= min_confidence:
        return result
    
    sleep(2)
```

### 信号过滤

机器人执行多层级信号过滤：

1. **模式置信度**：低于模式要求的最低置信度跳过（degen 为 0）
2. **得分阈值**：`|score|` 低于 `MIN_ABS_SCORE` 跳过（默认 2.0，干跑测试可调低）
3. **盘口检查**：实盘时 Up ask + Down ask 超过阈值跳过（**干跑跳过此检查**）
4. **价格优势**：无足够概率优势跳过
5. **连亏冷却**：连续亏损后暂停交易

### 盈亏计算

```
Polymarket 机制：
- 下注 $bet 买入份额 = $bet / entry_price
- 猜对：获得 $bet / entry_price（每份额 $1）
- 猜错：损失 $bet

干跑模拟：
- 赢：bankroll += bet / entry
- 输：bankroll -= bet
```

---

## 环境变量配置

### 资金与风险

| 变量 | 说明 | 推荐值 |
|------|------|--------|
| `STARTING_BANKROLL` | 起始资金 | 100-1000 |
| `MIN_BET` | 最小下注 | 1-10 |
| `MAX_USD` | 单笔上限 | 50-100 |
| `BOT_MODE` | 策略模式 | safe |

### 狙击参数

| 变量 | 说明 | 推荐值 |
|------|------|--------|
| `SNIPE_START` | 狙击提前秒数（须≥20s保证K线获取时间） | 60 |
| `SNIPE_PRICE_SOURCE` | 价格来源 | oracle |
| `SPIKE_JUMP` | 尖峰阈值 | 1.5 |

### 信号过滤

| 变量 | 说明 | 推荐值 |
|------|------|--------|
| `MIN_DECISION_CONFIDENCE` | 最低置信度 | 0.30（干跑测试可设0.1） |
| `MIN_ABS_SCORE` | 最低得分 | 2.0（干跑测试可设1.0） |
| `DIRECTION_ORDERBOOK_MAX_SUM` | 盘口上限（仅实盘生效） | 1.05 |

### RTDS 配置

| 变量 | 说明 | 推荐值 |
|------|------|--------|
| `USE_CHAINLINK_RTDS` | 使用 RTDS | 1 |
| `RTDS_AUTO_RECONNECT_STALE_S` | 重连阈值 | 300 |
| `RTDS_BUFFER_WAIT_S` | 缓冲等待 | 12 |

完整环境变量说明见 [COMMANDS.md](./COMMANDS.md)。

---

## 输出文件

### 干跑存档

```json
{
  "bankroll": 150.25,
  "principal": 100.0,
  "trades": 25,
  "history": [
    {
      "seq": 1,
      "ts_unix": 1734567890.123,
      "kind": "directional_bet",
      "bet": 10.0,
      "bankroll_before_bet": 100.0,
      "bankroll": 90.0,
      "window_ts": 1734567600
    },
    {
      "seq": 2,
      "ts_unix": 1734567902.456,
      "kind": "directional_settle",
      "win": true,
      "settle_payout": 20.5,
      "bankroll": 110.5,
      "window_ts": 1734567600
    }
  ]
}
```

### 训练数据（JSONL）

```json
{"event":"directional_settle","ts_unix":1734567890.123,"window_ts":1734567600,
 "mode":"safe","bet_usd":10.0,"entry_model":0.52,"direction_bet":1,
 "decision_score":3.5,"decision_confidence":0.5,"win":true,"settle_payout":19.23,
 "settle_meta":{"settle_method":"binance_klines_only","binance_open":97000.0,"binance_close":97200.0}}
```

### 干跑交易记录（Excel）

每次结算后自动追加一行到 `bot_trades.xlsx`（路径可通过 `BOT_TRADES_XLSX` 环境变量自定义）：

| 列 | 说明 |
|---|---|
| `window_ts` | 窗口 Unix 时间戳 |
| `窗口时间` | 窗口开始时间（北京时间） |
| `slug` | 市场 slug |
| `mode` | 策略模式 |
| `direction` | 1=Up, -1=Down |
| `bet` | 下注金额 |
| `entry` | 入场模型价 |
| `actual` | 实际结算方向 |
| `win` | 是否赢 |
| `settle_payout` | 结算收益 |
| `post_bet_bankroll` | 下注后余额 |
| `post_settle_bankroll` | 结算后余额 |
| `settle_method` | 结算方式（rtds_chainlink / binance_klines_only 等） |
| `score` | TA 综合得分 |
| `confidence` | TA 置信度 |
| `pnl` | **累计盈亏** = post_settle_bankroll - 会话起始余额 |
| `起始余额` | 会话开始时的余额 |

同 `window_ts` 防重。`pnl` 为累计值，每行反映当前余额相对于会话开始时的盈亏。

### 回测结果（Excel）

- **Summary**：所有配置汇总表
- **Best Config Trades**：最佳配置交易明细
- **Bankroll Curves**：各配置资金曲线

---

## 故障排查

### RTDS 连接失败

```bash
# 检查网络
curl -I https://api.binance.com

# 测试 WebSocket
wscat -c wss://ws-live-data.polymarket.com

# 开启调试
RTDS_DEBUG=1 python chainlink_rtds.py
```

### 回测 K 线获取失败

```bash
# 检查 Binance API
curl "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"

# 使用备用 API
export BINANCE_REST_BASE=https://api-gcp.binance.com/api/v3
```

### 干跑存档损坏

```bash
# 删除存档重新开始
rm dry_run_bankroll.json
python bot.py --dry-run
```

### 终端显示卡住

这是正常现象，主循环在等待下一个狙击窗口。可以查看日志或开启 `LOG_TS_MS=1` 确认程序在运行。

---

## 性能优化建议

### 1. 网络优化

- 使用低延迟网络连接
- 配置多个 Binance API 备用地址
- 考虑使用 VPN 改善国际连接

### 2. 参数调优

- 从回测结果中选择最优配置
- 使用 `compare_results.py` 验证预测准确率
- 逐步调整参数并记录效果

### 3. 风险管理

- 设置合理的 `MAX_USD` 限制
- 启用 `LOSS_STREAK_COOLDOWN` 防止连续亏损
- 定期检查和提取利润

---

## 许可与免责

本项目代码按原仓库许可发布。使用本软件产生的任何交易损失、账户封禁或法律后果由使用者自行承担。

**重要提醒：**

- 预测市场本身具有不确定性
- 历史回测结果不代表未来表现
- 实盘交易前请充分测试
- 请遵守当地法律法规

---

## 相关文档

- [TRADING_AND_SYSTEM_LOGIC.md](./TRADING_AND_SYSTEM_LOGIC.md) — 完整交易与系统逻辑文档
- [COMMANDS.md](./COMMANDS.md) — 详细命令使用说明
