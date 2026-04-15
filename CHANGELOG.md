# CHANGELOG - PolymarketBot 策略演化记录

> 所有策略改动必须记录在此，确保回测与实盘逻辑可追溯、可对照。

---

## [Unreleased] v2.1 - 狙击K线获取、结算等待、Excel写入

**目标**：修复 `--once` 模式下狙击 K 线无法获取、结算未完成就退出的问题；新增干跑 Excel 交易记录。

### 改动详情（已完成）

#### 1. K 线获取重构 ✅

**Before**：狙击循环内仅在首次迭代且 `t_left >= 15s` 时尝试获取 K 线；窗口较旧时 Binance 历史请求返回空；不重试。

**After**：
- 每次狙击循环迭代都尝试获取 K 线（无 `t_left` 限制），成功一次即止
- Binance 历史请求返回空（窗口距今 >~2 分钟，Binance 不保留）时，回退：拉最近 N 根 K 线，过滤掉窗口内的，只取窗口前的
- 新增 `fetch_history_candles_before_window(window_start_ms, lookback=120)` 函数

#### 2. --once 干跑等待结算 ✅

**Before**：`QueuedDrySettle` 无等待机制，`_settlement_done_evt` 为 None 时直接退出，结算线程未完成就被中断。

**After**：
- `QueuedDrySettle` 新增 `settle_done: Optional[threading.Event]` 字段
- 结算线程在调用 `resolve_window_direction_with_meta` **之前** `set()` 该事件
- 主线程 `--once` 模式：`evt.wait(timeout=WINDOW+120)` 等待结算完成后再退出
- 修复 `_apply_queued_dry_settle` 中 `wait_s` 在 `else` 分支外引用的 UnboundLocalError

#### 3. Excel 交易记录 ✅

**Before**：`compare_runs.py` 有 Excel 输出，但 `bot.py` 实盘/干跑无 Excel 记录。

**After**：
- 每笔结算后写入 `bot_trades.xlsx`（`BOT_TRADES_XLSX` 环境变量可自定义路径）
- `pnl` = `post_settle_bankroll - 会话初始余额`（累计盈亏，不是单笔）
- 新建文件用 `Workbook()`，`load_workbook()` 仅用于追加已有文件
- 防重：同一 `window_ts` 跳过写入

#### 4. 干跑跳过盘口检查 ✅

**Before**：干跑模式下仍执行 `DIRECTION_ORDERBOOK_MAX_SUM` 等盘口过滤，导致大量"盘口不全"/"盘口过贵"跳过。

**After**：`dry_run=True` 时跳过整个盘口检查块（`mx_sum` / `only_lt`）；干跑无真实持仓，不需要流动性闸值。

#### 5. 狙击置信触发修复 ✅

**Before**：`res.confidence >= min_conf` 在 `kline_fetch_done` 为 False 时也触发 `return`，导致 K 线未就绪就退出。

**After**：置信触发条件改为 `kline_fetch_done and res.confidence >= min_conf`，K 线未就绪时继续循环等待。

#### 6. SNIPE_START 默认值调整 ✅

- 代码默认从 `10s` 改为 `20s`（`.env` 默认 `60s`）
- 理由：需要 ≥20s 才能保证 Binance K 线有时间获取并分析

#### 7. .env 干跑测试参数 ✅

新增（干跑测试用）：
- `SNIPE_START=60` — 给 K 线足够获取时间
- `MIN_ABS_SCORE=1.0` — 信号得分阈值（原默认 2.0 偏高）
- `MIN_DECISION_CONFIDENCE=0.1` — TA 置信度阈值（原默认 0.30 偏高）

---

## [History] v2.0 - 统一回测与实盘逻辑（裸奔版）

**目标**：以回测为基准，让 bot.py 的交易逻辑与 compare_runs.py 完全一致，消除两套逻辑不同导致的胜率差异无法定位问题。

### 改动原则

- 回测是白盒、bot 是黑盒；任何实盘逻辑，回测必须能模拟
- 实盘可以比回测多过滤，但方向决策、仓位计算、入场价逻辑必须完全一致
- 每次改动都记录在这里

### 改动详情（已完成）

#### 1. 仓位计算统一 ✅

**Before**：
- `compare_runs.py`：自建 `sizing_bet()` + `bet_flat/bet_safe/bet_aggressive`
- `bot.py`：`compute_bet()` flat/safe/aggressive + Kelly + edge sizing

**After**：
- 两边统一引用 `trading_logic.compute_bet(mode, bankroll, principal, min_bet)`
- 新增 `trading_logic.py` 模块，`compute_bet` 支持 flat/safe/aggressive/degen 四种模式
- 回测删除自建仓位函数，改为 import 引用

#### 2. 入场价估算统一 ✅

**Before**：
- `compare_runs.py`：`estimate_fair_prob` + `entry_price_from_fair_prob`（自建公式）
- `bot.py`：`directional_entry_from_window_pct` + `entry_from_best_asks`

**After**：
- 统一用 `trading_logic.token_price_from_delta` + `directional_entry_from_window_pct`
- 回测用 `trading_logic.estimate_entry_for_backtest(direction, window_open, decision_px)`
- 实盘用 `directional_entry_from_window_pct` + 真实盘口
- **明确差距**：`estimate_entry_for_backtest` 是对真实盘口的估算，实盘入场价用真实盘口，两者差距单独记录

#### 3. 新增共享模块 `trading_logic.py` ✅

包含：
- `token_price_from_delta()` — 窗口偏离 → token 价格映射
- `directional_entry_from_window_pct()` — 基于偏离方向估算入场价
- `estimate_entry_for_backtest()` — 回测专用入场价估算
- `compute_bet()` — 统一仓位计算
- `size_by_edge()` — edge sizing（回测默认关闭）

#### 4. 回测模块重构 ✅

- 删除 `bet_flat/bet_safe/bet_aggressive/sizing_bet/estimate_fair_prob/entry_price_from_fair_prob`
- 统一引用 `trading_logic`
- trade_log 字段调整：`fair_prob` → `w_pct`（窗口偏离百分比）

---

## [History] v1.x - 早期版本（分隔期）

> 早期版本，回测和 bot 独立开发，逻辑分歧较大。

### v1.0 - 初始版本
- 回测独立实现，自建仓位公式、入场价公式
- Bot 独立实现，方向可被 imbalance/reversal 覆盖，Kelly + edge sizing
- 两者 TA 分析共享 `strategy.analyze()`
- 已知问题：回测 63% vs 实盘 23%，差距无法定位

---

## 版本对照表

| 版本 | 方向决策 | 仓位计算 | 入场价 | 过滤链 |
|------|---------|---------|--------|-------|
| v1.0 | 分歧（bot 有覆盖）| 分歧（各写各的）| 分歧 | 分歧 |
| v2.0 | 统一（都用 analyze）| 统一（compute_bet）| 统一估算（盘口差距记录）| 对齐可对齐的 |

---

## 后续计划

- [ ] v2.0 干跑验证（dry run），确认回测胜率 ≈ 干跑胜率
- [ ] 逐个加回 bot 特有逻辑（Kelly、edge sizing、方向覆盖），每次单独验证
- [ ] 回测加入 Binance tick 模拟，改善 TA 信号质量评估
