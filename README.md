# PolymarketBot

面向 [Polymarket](https://polymarket.com) **BTC 5 分钟 Up/Down** 市场的自动化脚本：用 Binance 1m K 线与自研加权指标做方向判断，在窗口末段狙击下单；可选接入 Polymarket **Chainlink RTDS**（`btc/usd`）作为与「Price to beat」对齐的开盘价/结算参考。**不构成投资建议；实盘有本金损失与合规风险，请自行评估。**

**完整交易与系统逻辑（审查用、中文单一权威）**：请阅读根目录 **`TRADING_AND_SYSTEM_LOGIC.md`**。原英文 `PolymarketBot.md` 已废止并删除，内容已并入该文件附录 E（并按当前代码校正差异）。

## 环境

- Python 3.10+（建议）
- 依赖见 `requirements.txt`

```bash
pip install -r requirements.txt
```

Playwright 仅用于 `auto_claim.py` 辅助网页赎回：

```bash
playwright install chromium
```

## 快速开始

1. 复制并编辑 `.env`（勿提交私钥与 API 密钥到公开仓库）。
2. **实盘**前用 `setup_creds.py` 从 `POLY_PRIVATE_KEY` 推导 CLOB API 凭证并写入 `.env`。
3. **干跑**（不下真实单、虚拟资金写入 `dry_run_bankroll.json`，路径可用 `DRY_RUN_BANKROLL_FILE` 覆盖）：

```bash
python bot.py --dry-run
```

4. **实盘**（需完整 Polymarket CLOB 环境变量）：

```bash
python bot.py
```

### 命令行参数（`bot.py`）

| 参数 | 说明 |
|------|------|
| `--mode safe\|aggressive\|degen` | 策略模式，默认可读 `BOT_MODE` |
| `--dry-run` | 干跑：不提交真实订单 |
| `--once` | 只跑一个 5m 周期后退出 |
| `--max-trades N` | 完成 N 笔后退出，0 为不限制 |

## 仓库内脚本

| 文件 | 作用 |
|------|------|
| `bot.py` | 主程序：调度、Gamma 市场、CLOB 下单、干跑结算队列、日志与训练 JSONL |
| `strategy.py` | 多指标合成打分（正偏 Up、负偏 Down） |
| `backtest.py` | Binance 历史 1m K 线拉取（供回测与结算对照） |
| `compare_runs.py` | 网格回测（多置信阈值 × 仓位模式），导出 Excel |
| `chainlink_rtds.py` | RTDS WebSocket：`ChainlinkBtcUsdRtds`；可单独运行自检 |
| `setup_creds.py` | 一次性生成 `POLY_API_*` |
| `auto_claim.py` | Playwright 定时打开 Portfolio，尝试点击 Redeem（需登录态 storage） |

### 自检与工具命令示例

```bash
python chainlink_rtds.py          # RTDS 连接与 buffer / ws 健康摘要
python backtest.py                # 若模块内定义了入口则按该文件说明使用
python compare_runs.py --hours 72 --output results.xlsx
python setup_creds.py
python auto_claim.py --headed     # 有头模式便于首次登录并保存 storage
```

## 核心环境变量（节选）

**Polymarket / CLOB（实盘）**

- `POLY_PRIVATE_KEY`：钱包私钥
- `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE`：CLOB API（`setup_creds.py` 生成）
- `POLY_CLOB_HOST`：默认 `https://clob.polymarket.com`
- `POLY_CHAIN_ID`：默认 `137`
- `POLY_SIGNATURE_TYPE`：与账户类型绑定（0/1/2，见官方文档）
- `POLY_FUNDER_ADDRESS`：部分账户类型需要

**资金与下单**

- `STARTING_BANKROLL`：起始虚拟/逻辑资金
- `MIN_BET`：最小名义（过小可能导致方向单被跳过）
- `MAX_USD` / `FIXED_DIRECTIONAL_USD`：方向单上限或固定名义
- `ENABLE_KELLY` / `KELLY_SCALE` / `KELLY_MODE`：Kelly 相关

**时间与价格源**

- `SNIPE_START`：距收盘多少秒进入狙击（有上下限夹紧）
- `SNIPE_PRICE_SOURCE`：`oracle` 或 `binance`
- `USE_CHAINLINK_RTDS`：设为 `0/false/off` 则全程多用 Binance
- `RTDS_WARMUP_S` / `RTDS_BUFFER_WAIT_S`：启动等待首包
- `RTDS_AUTO_RECONNECT_STALE_S`：看门狗仅在 **久未成功解析并写入 btc/usd**（墙钟）超过该秒数时重连；**不以** payload 时间戳落后墙钟为准（Chainlink 的 payload 时间常天然晚于墙钟，否则会误报、频繁断连）。默认 `120`；`0`/`off`/`false`/`none` 关闭。`RTDS_AUTO_RECONNECT_MIN_INTERVAL_S`（默认 `45`）、`RTDS_WATCHDOG_GRACE_S`（默认 `40`）同上
- `RTDS_OPEN_MAX_PAYLOAD_LAG_MS`：**毫秒**（默认 `12000`≈12s）。若「最早 ≥ 窗口起点」的 tick 的 **payload 时间戳** 比窗起点晚超过该值，则不用作开盘价（避免中途连上 WS 时用太晚的 tick 冒充边界价）。`0`/`off`/`false`/`none` 关闭检查。
- `RTDS_OPEN_ACCEPT_LATE_TICK=1`：仍过晚、且边界前无 tick 可回补时，**强制采用**晚到的首条 ≥ 边界的 Chainlink 价作开盘（与页面秒级对齐可能仍有偏差，但减少纯 Binance 混源）。
- `RTDS_OPEN_FALLBACK_MAX_MS`：无 ≥ 起点的 tick 时，允许用「起点前最近一条」oracle 的最大提前量（毫秒）；默认 **`30000`（30s）**。过大（如旧默认 180s）会误用窗口开始前很久的旧价，与页面目标价偏差大；链上极稀疏时再自行调大。
- `CHAINLINK_OPEN_WAIT_S` / `CHAINLINK_CLOSE_WAIT_S`：窗口边界附近等待 oracle 点
- 干跑：`DRY_RUN_SETTLE_AFTER_S`、`DRY_RUN_CHAINLINK_CLOSE_WAIT_S`、`DRY_RUN_BINANCE_SETTLE`（强制 Binance 结算）

**方向单「少送钱」可选闸（默认全关，与旧行为一致）**

- `DIRECTION_ORDERBOOK_MAX_SUM`：例如 `1.05`，双边卖一合计 **>** 该值则**不下**方向单（防盘口已贵仍赌方向）。
- `DIRECTION_ONLY_WHEN_BOOK_SUM_LT`：例如 `0.99`，仅当合计 **<** 该值才允许方向单（偏松盘才参与；与套利逻辑不同，此处不自动下单）。
- `DIRECTION_STRATEGY`：`ta`（默认，走 `strategy.analyze`）或 `reversal`（涨相对开盘则押 Down，跌则押 Up；`|偏离%%|` 见 `REVERSAL_MIN_ABS_PCT`，默认 `0.08`）。
- `USE_BOOK_ASK_FOR_ENTRY`：`1` / `true` / `on` 时用 **真实 best ask** 作干跑/记账 `entry`，并拒绝 `entry>0.97` 或缺失。
- `MIN_DECISION_CONFIDENCE`：仅在 **`DIRECTION_STRATEGY=ta`** 时生效，低于则跳过（如 `0.2`）。
- `SPIKE_JUMP`：尖峰触发阈值，环境变量覆盖默认 `1.5`；设为 **`999`** 等可实质关闭尖峰提前下单。
- 提前进场：调大 **`SNIPE_START`**（上限见代码夹紧，如 `45`）。

**第二阶段：盘口 + 错价（默认全关）**

- `DIRECTION_STRATEGY=imbalance`：用 **`get_orderbook_imbalance`**（前 N 档 bid/ask 量）定方向；`ORDERBOOK_IMBALANCE_DEPTH`（默认 3）、`IMBALANCE_THRESHOLD`（默认 0.25）；**双侧同时过阈不下单**。
- `USE_FAIR_PROB_EDGE=1`：用 **`estimate_fair_prob`**（sigmoid，`FAIR_PROB_SIGMOID_SCALE` 默认 50）与当前 **`entry`** 比 **`MIN_PRICE_EDGE`**（默认 0.03）；不够则跳过；通过时记下 edge 供下项。
- `USE_EDGE_POSITION_SIZING=1`：在**未**用 `FIXED_DIRECTIONAL_USD` / **未** `ENABLE_KELLY` 时，用 **`size_by_edge`**（`EDGE_SIZING_BANKROLL_FRAC` 默认 0.02、`EDGE_SIZING_EDGE_SCALE` 默认 10）按 edge 缩放名义；**需**前面 `USE_FAIR_PROB_EDGE` 已通过才生效。
- `MIN_SECONDS_BEFORE_CLOSE_FOR_TRADE`：例如 `8`，距收盘不足该秒数则不做方向单。
- `LOSS_STREAK_COOLDOWN=1`：**仅干跑**，且 `trades ≥ LOSS_STREAK_MIN_TRADES`（默认 6）时，若最近 `LOSS_STREAK_WINDOW`（默认 5）条结算里输 ≥ `LOSS_STREAK_MAX_LOSSES`（默认 4）则跳过本周期。

**套利探测（可选）**

- `ENABLE_ARBITRAGE_LOG`：仅打印双边卖一与价差，不下单
- `ENABLE_ARBITRAGE_TRADE`：套利实盘（高风险，需理解机制）
- `ARBITRAGE_SUM_ALERT` / `ARBITRAGE_POLL_S` / `ARBITRAGE_POLL_SUMMARY`

**日志与排障**

- `LOG_LEVEL` / `LOG_FILE`：标准 `logging`
- `LOG_TS_MS`：是否为 `print` 打墙钟毫秒前缀
- `WEBSOCKET_LOG=1`：显示 `websocket-client` 断线重连（默认静默以免误判「卡死」）
- `TRADE_TRAIN_JSONL`：每笔结算追加一行 JSONL（含 `settle_meta`）

**Binance REST（网络受限时）**

- `BINANCE_REST_BASE`、`BINANCE_REST_BASE_FALLBACKS`：见 `backtest.py` 文档字符串
- `BINANCE_HTTP_RETRIES`（默认 5）、`BINANCE_HTTP_RETRY_BACKOFF_S`（默认 1.2）：同一 REST 根遇断线/超时重试，减轻代理与 WinError 10054

**赎回辅助**

- `POLYMARKET_STORAGE_STATE`：`auto_claim.py` 使用的 Playwright storage JSON 路径
- `POLYMARKET_CLAIM_URL`：默认 Portfolio 页

完整启动摘要会在运行开始时通过 `print_run_config` 打印，建议每次对照。

## 干跑盈亏（模型说明）

- 下注时已从虚拟 `bankroll` 扣除名义 `bet`。
- **猜错**：不再加回（即输掉该笔名义）。
- **猜对**：按模型入场价 `entry` 计份额 `bet/entry`，干跑简化为 `+ (bet/entry) * 1`（与真实成交滑点、手续费、Polymarket 规则可能不一致，仅用于策略联调）。

结算在**单消费者后台线程**中排队执行，避免长时间 `sleep` 阻塞主循环；`bankroll` 等状态与主线程并发写时用锁保护。

Binance 回退结算价：使用窗口内 **恰好 5 根** 1m K 的首 open 与**第 5 根** close（不再多取窗后一分钟），以免与 Polymarket 窗尾判定反向。

**虚拟资金流水**：`dry_run_bankroll.json` 的 `history` 按 `seq` 记录每笔「方向单·扣下注」与「方向单·结算后」。下注行含 `bankroll_before_bet` / `bankroll`（扣后）；结算行含 `post_bet_bankroll`（与下注后一致）、`settle_payout`（赢局加账额，输为 0）、最终 `bankroll`。**输局两行 `bankroll` 可相同**：钱已在下注时扣过，结算不再扣。`DRY_RUN_HISTORY_MAX`（默认 2000）限制条数。

## 常见问题

- **终端像卡住**：主循环长休眠、`run_trade_cycle` 在开盘价之后会**睡到临近狙击**（最长可达约一个 5m 窗口减去 `SNIPE_START`），与结算队列并行，属正常；长休眠前会打印 `[主循环]` / `[调度]`。若仍刷屏，检查 `WEBSOCKET_LOG` 等。
- **WinError 10054 / 代理**：检查系统代理；可调大 `BINANCE_HTTP_RETRIES` 或配置 `BINANCE_REST_BASE_*` 备用根。
- **结算提示数据不完整**：查看日志中的 `settle_meta` 与 `TRADE_TRAIN_JSONL` 行；可适当增大 `DRY_RUN_CHAINLINK_CLOSE_WAIT_S`，并理解「payload 时间戳早于收盘边界」与「等待时长」不是同一回事。

## 许可与责任

代码按原仓库许可（若未声明则默认「仅学习研究」）。使用本软件产生的任何交易损失、账户封禁或法律后果由使用者自行承担。
