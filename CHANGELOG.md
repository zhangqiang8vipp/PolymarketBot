# CHANGELOG - PolymarketBot 策略演化记录

> 所有策略改动必须记录在此，确保回测与实盘逻辑可追溯、可对照。

---

## [Unreleased] v2.0 - 统一回测与实盘逻辑（裸奔版）

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
