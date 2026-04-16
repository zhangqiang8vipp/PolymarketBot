# PolymarketBot — 交易与系统逻辑全量说明（代码对照版）

> **本文件为仓库内交易/架构说明的唯一权威正文**（中文）。快速上手与环境变量节选仍以 `README.md` 为准；**凡与代码冲突之处一律以代码与本文件技术章节为准**。  
> 覆盖：`bot.py`、`strategy.py`、`backtest.py`、`compare_runs.py`、`chainlink_rtds.py`、`setup_creds.py`；另附 `auto_claim.py` 行为摘要。

---

## 1. 代码与职责一览

| 文件 | 职责 |
|------|------|
| `bot.py` | 主进程：主循环调度、`run_trade_cycle`（Gamma、开盘价、套利、狙击、下注、干跑/实盘队列）、Binance K 线开盘/结算边价、CLOB 下单、环境变量解析、日志钩子 |
| `strategy.py` | `analyze()`：复合加权 TA；`Candle` / `AnalysisResult` 数据结构 |
| `backtest.py` | Binance REST：`binance_get`、`fetch_klines_1m`、`fetch_klines_1m_ts`、`fetch_klines_range_hours` |
| `compare_runs.py` | 离线网格：多阈值 × 多 sizing 模式，拉历史 K 线模拟，导出 Excel（**不**跑 RTDS） |
| `chainlink_rtds.py` | Polymarket RTDS WebSocket：`ChainlinkBtcUsdRtds`，缓冲 `(payload_timestamp_ms, value)`，开盘价/收盘价查询与看门狗重连 |
| `setup_creds.py` | 一次性从私钥推导 CLOB API 三字段并打印（见附录 D） |
| `auto_claim.py` | Playwright 周期性打开 Portfolio 尝试点击 Redeem/Claim/Collect（独立脚本，不参与 `bot.py` 交易决策） |

---

## 2. 时间与窗口模型

### 2.1 常量（`bot.py`）

- **`WINDOW = 300`**：每个市场为 **5 分钟**（300 秒）窗口。
- **`SNIPE_DEADLINE = 5`**：`snipe_loop` 在距收盘 **小于 5 秒** 时退出狙击循环（不再做轮询）。
- **`POLL = 0.75`**：狙击阶段内两次 `analyze` 之间的 `sleep` 秒数（新版缩短以更快响应信号）。
- **`SPIKE_JUMP = 1.5`**：相邻两次 `analyze` 的 `|score|` 差 ≥ 1.5 视为「尖峰」，**立即**返回当前 `res`（见 §6）。
- **`SNIPE_START`**：距收盘秒数开始进入狙击轮询；代码默认 **20s**，`.env` 默认 **60s**（须 ≥20s 才能保证 Binance K 线有时间获取）。
- **`MIN_SHARES_POLY = 5`**、`**GTC_LIMIT_PRICE = 0.95**`：无卖单时 GTC 限价买参数（见 §10）。

### 2.2 当前窗口起点 Unix 秒

```text
current_window_ts(t=None):
  tt = int(t or time.time())
  return tt - (tt % WINDOW)
```

即对齐到 **5 分钟整点**（UTC 墙钟与系统时区一致，由本机 `time.time()` 决定）。

### 2.3 Slug 与 Gamma

- **`window_slug(window_ts)`** → `f"btc-updown-5m-{window_ts}"`。
- **`parse_gamma(slug)`**：`GET https://gamma-api.polymarket.com/events?slug=...`，取 `markets[0]` 的 `outcomes` 与 `clobTokenIds`（可能为 JSON 字符串，会 `json.loads`），按 label `up` / `down` 映射出 **两个 token id**。

---

## 3. 主循环（`main` → `while True`）

逻辑顺序（**影响「为什么整段没日志」**）：

1. **`wts = current_window_ts()`**，**`close_at = wts + WINDOW`**，**`t_left = close_at - now()`**。
2. 若 **`t_left <= 0`**：`sleep(0.25)`，`continue`（刚跨窗边界时的短自旋）。
3. 若 **`t_left < _snipe_start_s()`**：打印 **`[休眠] 距狙击窗口 Xs，未进入本窗`**，休眠后 **`continue`**。若进入较晚，本窗不调用 `run_trade_cycle`。
4. 调用 **`run_trade_cycle(...)`**，外层 **`try/except`**：异常打印栈并 `sleep(5)` 继续。
5. **`--once`** 或 **`max_trades`** 达标则退出循环。
6. 若 **`now() < close_this`**：打印 **`[休眠] 窗口未结束，Xs 后下一窗`**，再休眠。

### 3.1 启动阶段（`main` 内、`while` 之前）

- **`load_dotenv()`**、**`setup_logging()`**、**`_ensure_utf8_stdio()`**、**`_install_log_timestamp_print()`**（`LOG_TS_MS` 默认开，给所有 `print` 加墙钟前缀）。
- **干跑**：**`_load_dry_run_state`** 读 `DRY_RUN_BANKROLL_FILE`（默认项目目录下 `dry_run_bankroll.json`），恢复 `bankroll`、`principal`、`trades`、`history`、`dry_history_next_seq`。
- **非干跑**：**`make_clob_client()`** 失败则 **`sys.exit(1)`**。
- **RTDS**：`USE_CHAINLINK_RTDS` 非关则尝试 **`ChainlinkBtcUsdRtds`** → **`start()`** → **`RTDS_WARMUP_S`（默认 1.5s）** → **`wait_for_ticks(1, RTDS_BUFFER_WAIT_S)`** → 打印 `buffer_stats` 与 **`ws_health_line()`**。

---

## 4. 单笔周期：`run_trade_cycle` 完整顺序

以下按 **实际执行顺序** 列出（`window_ts` 即本窗 `wts`）。

1. **`close_at = window_ts + WINDOW`**，**`slug = window_slug(window_ts)`**。
2. **`parse_gamma_tokens(slug)`** → `up_tid`, `down_tid`。
3. **打印 `[======== 窗口 {wts} ========]` 块**（含 slug、dry_run、client、狙击提前秒、模式最低置信、套利开关；多行 `print`）。
4. **`log_up_down_ask_spread(..., silent=False)`**（周期 **第一次** 套利检测）。若返回 **`True`**（实盘两腿 FOK 成功且扣款），**整个 `run_trade_cycle` 直接 `return`**：本窗 **不再** 取 Chainlink 开盘价、不再狙击、不下方向单。
5. **`window_open_oracle(window_ts, chainlink_feed)`**（§5）。若抛异常：打印 **`[跳过] 开盘价获取失败`**，`return`。
6. 打印 **`开盘=X | 来源=XXX`**。
7. **`sleep_s = close_at - _snipe_start_s() - now()`**；若 `> 2.0s` 打印 **`距狙击还有 Xs，休眠中…`**，然后 `time.sleep(sleep_s)`。
8. **套利后台线程**（§9）：若 **`ARBITRAGE_POLL_S > 0`** 且 **(ENABLE_ARBITRAGE_LOG 或 trade)**，启动 `_arb_worker`；若 POLL=0 且开了日志/实盘开关，仅打印一行说明。
9. **`snipe_loop(..., arb_hit=arb_hit_ev)`**（§6）。若抛出 **`ArbitrageCycleDone`**：**`return`**。`finally`**：**`stop_arb.set()`**，若 `arb_thread` 存在则 **`join(timeout=…)`**。
10. 若 **`decision.details.get("skip_trade")`**：打印 **`→ 跳过：狙击末置信 X < Y`**，`return`。
11. **`loss_streak_should_pause`**：若真，打印 **`→ 跳过：连亏冷却`**，`return`。
12. **盘口检查**：`mx_sum` / `only_lt` 过滤；打印 **`→ 跳过：盘口不全/合计过贵`**，`return`。
13. **方向策略**（imbalance / reversal / TA）：打印方向决策行。
14. **信号过滤**：MIN_ABS_SCORE / MIN_DECISION_CONFIDENCE；打印 **`→ 跳过：|score|/置信度过弱`**，`return`。
15. **入场价**：`USE_BOOK_ASK` 过滤；**概率优势**过滤；**距收盘时间**过滤；打印 **`→ 跳过`**，`return`。
16. **计算 bet**：kelly / edge / 模式（safe/aggressive/degen）；打印 **`┌─── 交易信号 ──────────────────────────────`** 卡片块（含方向、得分、置信度、入场、金额、BTC现价、偏离）。
17. **实盘且 `client` 非空**：在 **`now() < close_at`** 内循环下单；失败重试；超时打印 **`→ 下单超时，本窗结束`**，`return`。
18. **`with _BOT_STATE_LOCK:`**：**`state.bankroll -= bet`**。
19. **干跑**：构造 **`QueuedDrySettle`** → **`enqueue_settlement`** → 打印 **`✓ 已下注 $X → 涨/跌，结算队列约 Xs 后判输赢`** + 余额变化 → `return`。
20. **实盘**：**`QueuedLiveRedeemHint`** 入队、`trades+=1`、打印 **`✓ 已下注 $X → 涨/跌，实盘请手动赎回`** → `return`。

---

## 5. 开盘价：`window_open_oracle` + BTC/USD 参考价

目标：**Price to beat**（与 Polymarket 页面对齐时优先 RTDS Chainlink）。

### 5.0 BTC/USD 窗口开盘参考价（`window_open_btc_price`）

用于计算窗口内价格偏离百分比（`w_pct`），**必须使用窗口开始时的 BTC/USD 价格**。

**优先级**：
1. **RTDS tick**：`window_tracker.open_price`（`current_window == window_ts` 时）
2. **Binance 回退**：`fetch_btc_price()`

打印区分来源：`[窗口开盘价] RTDS tick=$XXXXX.XX` 或 `[窗口开盘价] Binance=$XXXXX.XX (RTDS未就绪)`

### 5.1 Polymarket 概率开盘（`window_open_oracle`）

1. **`feed is None`**：**`fetch_window_open_price_binance(window_ts)`**  
   - **`fetch_klines_1m(BTCUSDT, start_ms=window_ts*1000, end_ms=None, limit=1)`** 的第一根 **`open`**；不足则 **`RuntimeError`**。
2. **`feed` 非空**：**`_chainlink_window_open_px(feed, window_ts)`**（§5.1）。若得到 **`px`**：返回 **`(px, "Polymarket RTDS — " + how)`**。
3. 否则打印长说明（RTDS 与「自检有 tick」不是一回事），可选 **`RTDS_FALLBACK_DEBUG`** 调 **`diagnose_rtds_open_buffer`**。
4. 回退：**`fetch_window_open_price_binance`**，说明串里带 RTDS 失败原因。

### 5.1 `_chainlink_window_open_px`（细节）

- **`lag = RTDS_OPEN_MAX_PAYLOAD_LAG_MS`**：**单位毫秒**（默认 **12000 ≈ 12 秒**）。若为 `0/off/false/none` 则 **`None`** 表示 **不做**「最早 tick 相对窗起点过晚」的拒绝。日志里「已晚 xxxs」与 `12000` 并列时，`12000` 指 **ms** 不是秒。
- **`feed.first_price_at_or_after(window_ts, max_payload_lag_ms=lag)`**：见 `chainlink_rtds.py` — 取 **payload 时间戳 ≥ 窗起点** 的 **最早** 一条；若 `lag` 非空且 **(ts_ms - boundary_ms) > lag**，返回 **`None`**（避免用「窗中途才出现」的样本当初始价）。
- 若 **`earliest_tick_at_or_after`** 显示最早 ≥ 边界的 tick 已晚于 `lag`：可设 **`skipped_wait_for_late`**，跳过 **`CHAINLINK_OPEN_WAIT_S`** 的阻塞等待，直接尝试 **边界前回补**。
- 否则 **`feed.wait_first_price_at_or_after(..., timeout_s=CHAINLINK_OPEN_WAIT_S, max_payload_lag_ms=lag)`**，超时则继续。
- **`feed.open_price_before_boundary_fallback(window_ts)`**：在 **`RTDS_OPEN_FALLBACK_MAX_MS`**（默认 30000 **ms**）内取 **边界前最近** tick；若缓冲里 **所有** tick 的 payload 时间都在窗起点**之后**（中途才连上 WS），则 **无可回补**，只能回退 Binance。
- **`RTDS_OPEN_ACCEPT_LATE_TICK=1`**：在上述「过晚且无边界前 tick」时，**仍采用** `earliest_tick_at_or_after` 的价格作开盘（Chainlink 源，但与页面严格「目标价」可能差数十刀量级）；不设则回退 Binance。

---

## 6. 狙击：`snipe_loop`

输入：**`window_open`**（oracle 开盘价）、**`window_close`**（= `close_at` 浮点）、**`mode`**、**`chainlink_feed`**、**`arb_hit`**、**`window_open_btc_price`**、**`up_tid`**、**`down_tid`**、**`client`**、**`dry_run`**、**`state`**。

1. 打印狙击参数：狙击提前秒、现价来源、RTDS 状态、模式最低置信度。
2. **`min_conf = min_confidence_for_mode(mode)`**：safe **0.45**，aggressive **0.35**，degen **0.0**。
3. **`while True`**（约每2秒一轮）：
   - **`t_left = window_close - now()`**。
   - 若 **`t_left <= 0`**：**`break`** 出循环。
   - 若 **`arb_hit` 已 set**：抛 **`ArbitrageCycleDone`**。
   - **套利持续监控**：狙击循环期间每轮检查套利（`log_up_down_ask_spread(..., silent=True)`），命中则抛出 `ArbitrageCycleDone`。
   - **K 线获取（每次迭代都尝试，不限 `t_left`）**：调用 **`fetch_history_candles_before_window(window_start_ms, lookback=120)`** — 直接请求窗口前历史数据；若 Binance 返回空（窗口距今 >~2 分钟），回退：拉最近 120 根过滤掉窗口内的。
   - 第一次进入狙击：打印 **`[狙击] → 狙击开始，距收盘 Xs，开始分析（K线已获取/K线获取中）`**。
   - **`px = snipe_current_price`** → **`ticks.append(px)`**。
   - 再检查 **`arb_hit`**。
   - **`res = analyze(candles, tick_prices=ticks[-120:], window_open_price=window_open_btc_price)`**（K 线未就绪时返回 `_tick_only_decision`）。
   - 更新 **`best`**：若 `best is None` 或 **`abs(res.score) > abs(best.score)`** 则 **`best = res`**。
   - **尖峰**：若 **`last_score` 非空** 且 **`abs(res.score - last_score) >= SPIKE_JUMP`**：打印 **`[尖峰] Δ=X.X，提前发射`**，**立即 `return res, ticks`**（**不** 检查 `min_conf`）。
   - **置信度达标**：若 **`kline_fetch_done and res.confidence >= min_conf`**：**`return res, ticks`**（K 线未就绪时继续循环，不提前返回）。
   - **`last_score = res.score`**，**`sleep(POLL)`**。

4. **退出 `while` 后**（因 `t_left <= 0`）：
   - 若 **`best is None`**：再取一次价与 K 线，**`best = analyze(...)`**。
   - 若 **`best.confidence < min_conf`** 或 **`best.details.get("skip_trade")`**：返回该 **`AnalysisResult`**（**`run_trade_cycle` 会跳过下单**）。
   - 否则 **`return best, ticks`**。

### 6.1 `snipe_current_price`

- **`SNIPE_PRICE_SOURCE=binance`**：**`fetch_btc_price()`**（Binance `ticker/price`）。  
- **否则**：若 feed 存在且 **`latest_price()`** 非空则用之；否则 **`fetch_btc_price()`**。

---

## 7. 信号与入场价模型

### 7.1 `strategy.analyze(candles, tick_prices, window_open_price)`

> **v2.2 更新**：新版 `analyze()` 新增 `window_open_price` 参数，支持窗口动量信号。

**签名**：`analyze(candles, tick_prices=None, window_open_price=None)`
- `candles`: 决策点之前的 1m K 线（最老在前，**不含窗口期内 K 线**）
- `tick_prices`: 窗口内的实时 tick 价格列表（首价格为窗口起点附近）
- `window_open_price`: 窗口开始时的 BTC/USD 价格（来自 RTDS tick，最准确）

**TA 信号**：

| 子信号 | 描述 | 权重范围 |
|--------|------|---------|
| `_window_momentum` | 窗口内 BTC/USD 实时变动（最重要）| ±4 |
| `_micro_momentum` | 最近 1 分钟涨跌 | ±2 |
| `_acceleration` | 动量加速/减速 | ±1.5 |
| `_ema_cross` | EMA(9) vs EMA(21) | ±1 |
| `_rsi_weight` | RSI 超买超卖 | ±2 |
| `_volume_surge` | 成交量突增 | ±1 |
| `_trend_strength` | 最近 10 根方向一致性 | ±2 |
| `_tick_trend` | tick 价格趋势（仅实盘）| ±2 |

**`_window_momentum` 逻辑**：
- 当窗口内 BTC 价格变动超过 $50 时触发
- $50 → ±2，$100 → ±4，$200+ → ±4（饱和）
- 直接反映窗口内实际价格变化，应主导决策

**汇总**：
- `score = 上述八项之和`（范围约 -17 ~ +17）
- `direction = 1 if score >= 0 else -1`
- `confidence = min(abs(score) / 8.5, 1.0)`（归一化到 0~1）

**旧版 `_window_delta_weight` 已移除**：旧版用窗口内已发生的 `window_pct` 算分，造成循环论证。

### 7.2 `token_price_from_delta(abs_window_pct)`（`bot.py`）

分段映射 **`d = abs_window_pct`** 到 **[0.50, 0.97]** 的模型价（**不代表**真实订单簿成交价）：

- `< 0.005` → 0.50  
- `< 0.02` → 0.50 + 0.05 * (d-0.005)/(0.015)  
- `< 0.05` → 0.55 + 0.10 * (d-0.02)/0.03  
- `< 0.10` → 0.65 + 0.15 * (d-0.05)/0.05  
- `< 0.15` → 0.80 + 0.12 * (d-0.10)/0.05  
- 否则 → **`min(0.97, 0.92 + 0.05 * min(1, (d-0.15)/0.05))`**

### 7.3 `directional_entry_from_window_pct(direction, w_pct)`（`bot.py`）

- **`d = token_price_from_delta(abs(w_pct))`**。  
- **买 Up（direction==1）**：`w_pct >= 0` 时返回 **`d`**；否则 **`max(0.03, min(0.97, 1-d))`**（逆势更便宜）。  
- **买 Down（direction==-1）**：`w_pct <= 0` 时返回 **`d`**；否则 **`max(0.03, min(0.97, 1-d))`**。

---

## 8. 方向单名义 `bet`（`run_trade_cycle` 内 `_BOT_STATE_LOCK` 块）

检查：**`state.bankroll < min_bet`** → 跳过。

分支优先级：

1. **`FIXED_DIRECTIONAL_USD` 为正**：**`bet = fix_usd`** → 若有 **`MAX_USD`** 则 **`min`** → **`min(bet, bankroll)`** → 若 `< min_bet` 则跳过。  
2. **`ENABLE_KELLY == "1"`**（**严格字符串**，非 `true`）：**`_kelly_directional_bet(bankroll, decision.confidence, min_bet, cap_mx)`**；可返回 **`None`** → 跳过。  
3. **否则**：**`raw_bet = compute_bet(mode, bankroll, principal, min_bet)`**；`<=0` 跳过；**`bet = raw_bet`**；若有 **`MAX_USD`** 则封顶；`< min_bet` 则跳过。

### 8.1 `compute_bet(mode, bankroll, principal, min_bet)`

- **`bankroll < min_bet`** → 0。  
- **safe**：**`max(min_bet, min(bankroll, bankroll*0.25))`**。  
- **degen**：**`max(min_bet, bankroll)`**（全仓名义，仍可能被 `MAX_USD` 截断）。  
- **aggressive**：若 **`bankroll <= principal`**：**`max(min_bet, bankroll)`**；否则 **`max(min_bet, bankroll - principal)`**。

### 8.2 `_kelly_directional_bet`

- **`p = clip(confidence, 0, 1)`**，**`ks = KELLY_SCALE`**（默认 0.25，范围 [0.001, 1]）。  
- **`KELLY_MODE=binary`**：**`f_eff = ks * max(0, 2p-1)`**；否则 **`f_eff = ks * p`**。  
- **`calc = bankroll * f_eff`**，**`bet = min(calc, bankroll)`**，再 **`min(MAX_USD)`** 若设置。  
- **`< min_bet`** → **`None`**。

---

## 9. 套利逻辑（`log_up_down_ask_spread` + 后台线程）

### 9.1 是否启用

- **`log_ok`**：`ENABLE_ARBITRAGE_LOG` ∈ {`1`,`true`,`yes`,`on`}。  
- **`trade_ok`**：`ENABLE_ARBITRAGE_TRADE` 同上 **且** **`client is not None`** **且** **`not dry_run`**。  
- 若 **`not log_ok and not trade_ok`**：**直接 `return False`**（不调盘口）。

### 9.2 盘口

- **`get_best_ask(token_id, client)`**：有 client 则 **`client.get_order_book`** 取 **`asks[0].price`**；异常则回退 **`GET {POLY_CLOB_HOST}/book?token_id=`** 解析 JSON **`asks[0]["price"]`**。  
- 任一侧 **`None`**：非 silent 打 `[套利日志]`；**silent** 打 **`[套利/后台] 盘口不全...`**；**`return False`**。

### 9.3 合计与阈值

- **`total = up_ask + down_ask`**，**`edge = 1 - total`**。  
- **silent 且 `log_ok`**：若 **`ARBITRAGE_POLL_SUMMARY` 未关**，打印完整摘要行；若关，仍打印短「心跳」行（含合计）。  
- **非 silent 且 `log_ok`**：打印 `[套利日志]`。  
- 若 **`total >= ARBITRAGE_SUM_ALERT`**（默认 0.99）：**`return False`**。  
- 否则 **`log_ok`** 时打印 **`[套利告警]`**。

### 9.4 实盘下单条件

- 若 **`not trade_ok`**：干跑且开关开时打印「干跑跳过」；无 client 打印「无客户端」；**`return False`**。  
- **`bet = _arbitrage_trade_usd()`**：顺序读 **`ARBITRAGE_TRADE_USD`**、**`MAX_USD`**，否则默认 **10**，下限 **0.01**。  
- **`state.bankroll < bet`**：跳过。  
- **`execute_arbitrage_trade`**：两腿各 **`bet/2`** 的 **FOK 市价买**；**仅当两腿都成功** 返回 True，此时 **`state.bankroll -= bet`**，**`trades += 1`**，函数返回 **`True`**。

### 9.5 与方向单关系

- 周期 **开头** 一次 **`log_up_down_ask_spread(silent=False)`**；若返回 True，**本窗不再方向单**。  
- 狙击阶段：**`ARBITRAGE_POLL_S > 0`** 且 **(log 或 trade)** 时后台线程重复探测；**`snipe_loop` 内**在多个点检查 **`arb_hit`**，触发则 **`ArbitrageCycleDone`** 结束本窗方向逻辑。

---

## 10. 实盘方向单下单（`place_buy_*`）

在 **`now() < close_at`** 内循环：

- 若 **`orderbook_has_asks(client, token_id)`** 为真：**`place_buy_fok(client, token_id, bet)`**（美元名义 FOK）。  
- 否则：若 **`bankroll >= GTC_LIMIT_PRICE * MIN_SHARES_POLY`**：**`place_buy_gtc_095`****；否则仍 **`place_buy_fok`**。  
- 异常：打印，`sleep(ORDER_RETRY)` 重试。  
- **`orderbook_has_asks`**：异常时返回 **`True`**（**倾向于走 FOK**）。

**注意**：实盘在 **成功 `placed`** 之后才进入 **`bankroll -= bet`**；与干跑路径一致在扣款后入队/记账。

---

## 11. 结算与 Binance 边价

### 11.1 `_binance_window_edge_prices(window_ts)`

- **`n = max(1, WINDOW//60)`** → 5 分钟窗为 **5 根 1m**。  
- **`fetch_klines_1m(start_ms=window_ts*1000, end_ms=None, limit=n)`**。  
- 返回 **`(k[0].open, k[n-1].close)`**；不足 **`n`** 根则 **`RuntimeError`**。  
- **语义**：与「窗内第一根 open、窗内最后一根 close」对齐，**避免**旧实现多取一根窗后 K 的 bug。

### 11.2 `resolve_window_direction_with_meta(window_ts, feed, dry_run=...)`

1. **干跑且 `DRY_RUN_BINANCE_SETTLE` 为真**：仅用 **`_binance_window_edge_prices`**，`close >= open` → **Up(1)**。  
2. **`feed` 非空**：  
   - **`open_px, open_how = _chainlink_window_open_px`**（与开盘同源逻辑）。  
   - **`close_px = feed.first_price_at_or_after(close_boundary_s)`**，`close_boundary_s = window_ts + WINDOW`；若无则 **`wait_first_price_at_or_after`**，超时 **`close_px=None`**。干跑时等待上限还会与 **`DRY_RUN_CHAINLINK_CLOSE_WAIT_S`**（默认 90）取 **`min(CHAINLINK_CLOSE_WAIT_S, cap)`** 等（见代码注释）。  
   - 若 **open 与 close 均非空**：**`settle_method=rtds_chainlink`**，**`1 if close >= open else -1`**。  
   - 否则：填充 **`missing`**、诊断、`buffer_stats`、**`ws_health`**，打印长文，取 **Binance** **`bo_fb, bc_fb`**，**`open_used/close_used` 均为 Binance**，返回 Binance 判定方向。  
3. **`feed` 为空**：直接 Binance fallback，同上。

### 11.3 干跑队列结算 `_apply_queued_dry_settle`

1. **`wait_s = max(0.1, job.close_at - now() + job.settle_after)`**，默认 **`DRY_RUN_SETTLE_AFTER_S=2`**。
2. **`resolve_window_direction_with_meta`** 最多 3 次，失败间隔 **`2*(attempt+1)`** 秒。
3. 若 3 次全部失败（包括 RTDS 开盘、Binance 收盘都失败），则**强制直接调用 `_binance_window_edge_prices`** 再次尝试（绕过 RTDS 路径）。
4. 若强制 Binance 也失败：**`actual = -job.direction`**，**`settle_method=error_fallback_loss`**（按输处理，不增发赢钱）。**注意**：修复了旧版「Binance 失败后仍用上次残留数据」的 bug。
5. **`win = (actual == job.direction)`**。
6. 赢：**`shares = bet / max(entry, 1e-9)`**，**`settle_payout = shares * 1.0`**（模型按每股 1 USD 赎回），**`bankroll += settle_payout`**。
7. 若 **`bankroll < min_bet`**：**`bankroll = principal`**（**bust 重置**）。
8. 写 **`directional_settle`** history、**`_save_dry_run_state`**、打印、`TRADE_TRAIN_JSONL` 若设则追加。

---

## 12. `chainlink_rtds.py` 行为摘要

- **WebSocket**：`POLY_RTDS_WS` 默认 **`wss://ws-live-data.polymarket.com`**；订阅 **`crypto_prices_chainlink`**，filters **`btc/usd`**；也接受 **`crypto_prices`** 主题的快照数组。  
- **缓冲**：**`(ts_ms, value)`** 列表，**`KEEP_HISTORY_MS = 2h`** 修剪。  
- **`first_price_at_or_after(boundary_unix_s, max_payload_lag_ms=...)`**：最早 **`ts_ms >= boundary*1000`**；若 `max_payload_lag_ms` 非空且最早 tick 相对边界过晚则 **返回 None**（**开盘**用，**收盘**调用不传 lag）。  
- **`wait_first_price_at_or_after`**：轮询直至有值或 **`TimeoutError`**。  
- **`latest_price()`**：缓冲内 **时间戳最大** 的一条的 value。  
- **看门狗**：仅当 **「距上次成功写入 btc/usd 的墙钟秒数」> `RTDS_AUTO_RECONNECT_STALE_S`** 且过 **`RTDS_WATCHDOG_GRACE_S`** 与最小重连间隔时 **`_force_reconnect`**（清空缓冲、关闭 WS）。**不**用 payload 相对墙钟滞后触发。

---

## 13. `compare_runs.py` 与实盘的差异（修复后版本）

> ⚠️ **2024 年重大修复**：旧版 `compare_runs.py` 有严重的循环论证 bug（见下方「旧版 Bug」说明）。

### 13.1 旧版 Bug（已修复）

旧版 `analyze(window_open, current_price, candles, tick_prices)` 传入窗口内的价格变动来算分：

```
窗口内已涨 → w_score → direction=Up
窗口结束时涨了 → outcome=Up
outcome == direction → "100% 胜率"
```

这相当于用「同一事实」证明自己，永远不会错。新版已彻底移除此逻辑。

### 13.2 新版回测逻辑

| 项目 | 说明 |
|------|------|
| **数据** | Binance 历史 1m（`fetch_klines_range_hours`），无 RTDS/尖峰/狙击循环 |
| **TA 信号** | `analyze(candles)` 只接收决策点之前的 K 线（不含窗口期内），用纯历史指标打分 |
| **决策时刻** | 窗口开始前取 `MIN_CANDLES_FOR_TA=60` 根历史 K 线做分析 |
| **入场价** | 用 `estimate_fair_prob` + `entry_price_from_fair_prob`（基于 TA confidence），不用窗口内变动 |
| **结果判断** | `outcome` 来自窗口结束时的收盘价（与预测无关的独立事件）|
| **置信过滤** | 网格阈值 0.0..0.8 与 `res.confidence` 比较 |
| **盈亏计算** | `shares = bet / entry`，赢了 `bankroll += shares`，输了 `bankroll -= bet` |
| **sizing** | flat / safe / aggressive 三种 |

---

## 14. `backtest.binance_get` 重试

- 多 **`BINANCE_REST_BASE`** + **`BINANCE_REST_BASE_FALLBACKS`**。  
- 同一根上 **`BINANCE_HTTP_RETRIES`** 次，**`ConnectionError`/`Timeout`/`OSError`** 退避 **`BINANCE_HTTP_RETRY_BACKOFF_S`**。  
- 可重试 HTTP 码：451、403、404、429、≥500；**451** 在无更多 fallback 时抛明确 **`RuntimeError`**。

---

## 15. 环境变量速查表（与交易强相关）

| 变量 | 作用摘要 |
|------|-----------|
| `BOT_MODE` / `--mode` | safe \| aggressive \| degen |
| `STARTING_BANKROLL` | 初始资金；干跑可与 JSON 覆盖 |
| `MIN_BET` | 最小名义；方向单与套利资金检查 |
| `MAX_USD` | 方向单封顶；套利金额备选 |
| `FIXED_DIRECTIONAL_USD` | 固定方向单名义（优先于模式/Kelly） |
| `ENABLE_KELLY` | **仅 `"1"`** 启用 Kelly |
| `KELLY_SCALE` / `KELLY_MODE` | Kelly 参数 |
| `SNIPE_START` | 狙击提前秒数，夹紧到 [6, 295]（须≥20s保证K线获取） |
| `SNIPE_PRICE_SOURCE` | oracle \| binance |
| `USE_CHAINLINK_RTDS` | 关则全程无 RTDS |
| `POLY_*` / `POLY_CLOB_HOST` | 实盘 CLOB 与签名 |
| `ENABLE_ARBITRAGE_LOG` / `ENABLE_ARBITRAGE_TRADE` | 1/true/on |
| `ARBITRAGE_SUM_ALERT` | 卖一合计低于此才告警/可下单 |
| `ARBITRAGE_POLL_S` | 狙击期后台轮询间隔；0=仅周期头 |
| `ARBITRAGE_POLL_SUMMARY` | 关时后台仍打短心跳 |
| `ARBITRAGE_TRADE_USD` | 套利双边总美元 |
| `DRY_RUN_BANKROLL_FILE` | 干跑 JSON 路径 |
| `DRY_RUN_SETTLE_AFTER_S` / `DRY_RUN_BINANCE_SETTLE` / `DRY_RUN_CHAINLINK_CLOSE_WAIT_S` | 干跑结算 |
| `DRY_RUN_HISTORY_MAX` | history 最大条数 |
| `TRADE_TRAIN_JSONL` | 每笔结算追加 JSONL |
| `LIVE_REDEEM_HINT_AFTER_S` | 实盘收盘后队列提示延迟 |
| `RTDS_*` / `CHAINLINK_*` / `RTDS_OPEN_*` | 见 `print_run_config` 与 `chainlink_rtds` 模块注释 |
| `BINANCE_REST_BASE*` / `BINANCE_HTTP_*` | Binance REST |

---

## 16. 线程与并发

| 线程名 | daemon | 说明 |
|--------|--------|------|
| `rtds-ws` / `rtds-ping` / `rtds-watchdog` | 是 | RTDS 连接与保活 |
| `arb-poll` | **是** | 套利后台（每窗狙击阶段） |
| `settlement-queue` | **否** | 单消费者处理 `QueuedDrySettle` / `QueuedLiveRedeemHint` |

共享状态：**`BotState.bankroll/trades/...`** 用 **`_BOT_STATE_LOCK`**；结算线程与主线程通过锁与队列隔离。

---

## 17. 已知边界与审查点（非「少写」而故意列出）

1. **主循环门控**：**`t_left < SNIPE_START`** 时 **不跑** `run_trade_cycle`，若启动较晚可能 **整窗无交易无周期日志**（除短眠打印）。  
2. **`ENABLE_KELLY`** 与 **`ENABLE_ARBITRAGE_*`** 的布尔规则 **不一致**（Kelly 仅认 `"1"`）。  
3. **`orderbook_has_asks` 异常返回 True**：可能掩盖无流动性。  
4. **实盘 FOK/GTC 成交价** 与 **`entry` 模型** 无链上回填，干跑 PnL 为 **模型近似**。  
5. **尖峰** 路径 **不要求** `confidence >= min_conf`。  
6. **`compare_runs`** 的阈值网格 **≠** `min_confidence_for_mode`。  
7. **套利** 两腿 FOK **非原子**：可能单边成交，代码已警告需人工处理。  
8. **结算队列** `_settlement_consumer_loop`：单条任务异常会打印栈并 **吞掉**（不自动重试该条 settle）。结算结果打印为 **`┌─── 结算 XXXX ──────────────────────`** 卡片块（✓/✗ 结果、方向、payout、余额、Binance价格、若bust则打印重置信息）。  
9. **干跑跳过盘口检查**：`dry_run=True` 时 `mx_sum`/`only_lt` 过滤被跳过；干跑无真实持仓，不需要流动性闸值。  
10. **狙击 K 线获取**：Binance 历史 K 线仅保留最近约 2 分钟；窗口距今较旧时，直接历史请求返回空，改用「拉最近 K 线过滤窗口前」的兜底方案。  
11. **Excel pnl 为累计值**：`bot_trades.xlsx` 中 `pnl = post_settle_bankroll - 会话初始余额`，是**累计**盈亏，不是单笔盈亏。

---

## 18. `auto_claim.py`（与 bot 决策无关）

- 周期性打开 **`POLYMARKET_CLAIM_URL`**（默认 Portfolio）。  
- 尝试点击 **Redeem / Claim / Collect**。  
- 可选 **`POLYMARKET_STORAGE_STATE`** 存储状态文件。

---

## 附录 A — `strategy.analyze` 各加成分项（修复后版本）

> ⚠️ **旧版 `_window_delta_weight` 已移除**（循环论证）。新版有 7 个子信号。

以下 **`candles`** 均为「最老在前」，不含窗口期内 K 线。

### A.1 `_micro_momentum(candles)`

- `len < 2` → 0
- 否则：`last.close > prev.close` → **+2**；`<` → **-2**；相等 → **0**

### A.2 `_acceleration(candles)`

- `len < 3` → 0
- `m0 = last.close - last.open`，`m2 = candles[-3].close - candles[-3].open`
- `m0 > 0 and m0 > m2` → **+1.5**
- `m0 < 0 and m0 < m2` → **-1.5**
- `m0 > 0 and m0 < m2` → **-0.5**
- `m0 < 0 and m0 > m2` → **+0.5**
- 否则 **0**

### A.3 `_ema_cross(candles)`

- `len(closes) < 21` → 0
- `E9` 与 `E21` 为对 **closes** 的 EMA(9)、EMA(21) 序列（首值为 SMA 种子）
- `e9[-1] > e21[-1]` → **+1**；`<` → **-1**；否则 **0**

### A.4 `_rsi_weight(candles)`

- RSI 为 Wilder 风格近似（见 `_rsi`：14 根涨跌和平均）
- `RSI > 75` → **-2**；`< 25` → **+2**；`> 60` → **-1**；`< 40` → **+1**；否则 **0**

### A.5 `_volume_surge(candles)`

- `len < 6` → 0
- `recent = 最后 3 根均量`，`prior = 再往前三根均量`；`prior==0` → 0
- 若 `recent < 1.5 * prior` → 0
- 否则：`last.close >= last.open` → **+1**，否则 **-1**

### A.6 `_trend_strength(candles)`

- `len < 10` → 0
- 统计最近 10 根 K 线中 `close > open`（阳线）的数量 `up_count`
- `dn_count = 10 - up_count`，`bias = up_count - dn_count`（范围 -10 ~ +10）
- `bias >= 7` → **+2**；`bias <= -7` → **-2**
- `bias >= 4` → **+1**；`bias <= -4` → **-1**
- 否则 **0**

### A.7 `_tick_trend(tick_prices)`

- `len < 5` → 0
- `move_pct = (last-first)/first*100`；`|move_pct| < 0.005` → 0
- `ups` = 相邻上升对数，`downs` = 相邻下降对数，`n = ups+downs`；`n==0` → 0
- `ups/n >= 0.60` 且 `move_pct > 0` → **+2**
- `downs/n >= 0.60` 且 `move_pct < 0` → **-2**
- 否则 **0**

### A.8 汇总（修复后）

**`score = micro_momentum + acceleration + ema_cross + rsi_weight + volume_surge + trend_strength + tick_trend`**
**`direction = 1 if score >= 0 else -1`**
**`confidence = min(abs(score) / 7.0, 1.0)`**（分母改为 7，因为有 7 个子信号）

> 旧版 `score` 范围约 -13 ~ +13（6 个子信号），新版也是 7 个子信号。

---

## 附录 B — `token_price_from_delta` 分段（`d = abs_window_pct`）

| 区间 | 返回值 |
|------|--------|
| `d < 0.005` | 0.50 |
| `0.005 <= d < 0.02` | `0.50 + 0.05 * (d - 0.005) / (0.02 - 0.005)` |
| `0.02 <= d < 0.05` | `0.55 + 0.10 * (d - 0.02) / (0.05 - 0.02)` |
| `0.05 <= d < 0.10` | `0.65 + 0.15 * (d - 0.05) / (0.10 - 0.05)` |
| `0.10 <= d < 0.15` | `0.80 + 0.12 * (d - 0.10) / (0.15 - 0.10)` |
| `d >= 0.15` | `min(0.97, 0.92 + 0.05 * min(1.0, (d - 0.15) / 0.05))` |

---

## 附录 C — 结算队列与进程退出

- **`ensure_settlement_worker`**：若尚无 worker，创建 **`queue.Queue`**，启动 **`_settlement_consumer_loop`**（**非 daemon**），并把 **`_settlement_feed_cell[0] = feed`**。  
- **`enqueue_settlement`**：每次更新 **`_settlement_feed_cell`** 后 **`put(item)`**。  
- **消费者**：`get()` → 若为 **`_SETTLEMENT_SENTINEL`** 则 **return** 结束线程；否则 dry / live hint 分支；异常 **`print` + `traceback.print_exc()`**，**不 re-raise**。  
- **`shutdown_settlement_worker`**：`put(哨兵)` + **`join(timeout=240)`**（主程序退出时调用）。

---

## 附录 D — `setup_creds.py`（凭证）

一次性脚本：**`load_dotenv()`** → 读 **`POLY_PRIVATE_KEY`** → 用 **`ClobClient(host, chain_id, key, signature_type, funder)`**（环境变量与 `make_clob_client` 一致，**无 ApiCreds**）调用 **`create_or_derive_api_creds()`** → **打印**三行 **`POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE`** 供用户**手动追加**到 `.env`。不参与 `bot.py` 运行时决策。

---

## 附录 E — 产品说明与运行指南（原英文 build 文档并入，已按当前代码校正）

### E.1 项目做什么

机器人针对 Polymarket 上 **「BTC 在接下来 5 分钟窗口收盘时，相对窗口开盘价更高还是更低」** 的二元市场：你买入 **Up** 或 **Down** 份额（价格约在 0.5～0.95 之间），若判断正确，每份约按 1 USDC 思路兑付（真实规则与手续费以 Polymarket 为准）。程序在 **窗口末段** 用 **Binance 1m K 线 + 自研多指标打分** 决定方向，并调用 CLOB 下单；可选接入 **Polymarket RTDS 的 Chainlink btc/usd**，使 **开盘价 / 结算** 尽量与页面 **Price to beat** 同源。**不构成投资建议**；实盘有本金与合规风险。

### E.2 依赖（`requirements.txt`）

| 包 | 用途 |
|----|------|
| `py-clob-client==0.34.5` | Polymarket 官方 CLOB 客户端 |
| `python-dotenv>=1.0.0` | 加载 `.env` |
| `requests>=2.31.0` | Binance / Gamma / 公开 CLOB REST |
| `playwright>=1.40.0` | 仅 `auto_claim.py` 浏览器辅助赎回 |
| `openpyxl>=3.1.0` | `compare_runs.py` 导出 Excel |
| `websocket-client>=1.6.0` | `chainlink_rtds.py` RTDS |

Playwright 需额外执行：`playwright install chromium`。

### E.3 时钟与 slug（不「搜市场」）

- **`window_ts = floor(now/300)*300`**（与代码 `current_window_ts` 一致）。  
- **`close_at = window_ts + 300`**。  
- **`slug = "btc-updown-5m-" + str(window_ts)`**，再 **`GET gamma-api.polymarket.com/events?slug=...`** 取 token id。

### E.4 狙击节奏与设计取舍

- 默认在距收盘 **`SNIPE_START`（代码默认 20s，`.env` 默认 60s）** 内进入高速轮询（须 ≥20s 才能保证 Binance K 线有时间获取）。
- 轮询间隔 **`POLL=0.75s`**：每次调用 **`fetch_history_candles_before_window`** 尝试获取窗口前历史 K 线；若 Binance 历史返回空则用最近 K 线过滤兜底。
- **K 线获取**：每次狙击迭代都尝试获取，成功一次即止（不再限制 `t_left >= 15s`）。
- **尖峰**：相邻两次 **`|score|`** 差 **≥ `SPIKE_JUMP`（1.5）** 时 **立即** 采用当前结果下单，**不要求** 达到模式最低置信度。
- **置信度达标**则提前返回；K 线未就绪时继续循环等待。
- **与旧英文说明的差异（重要）**：旧稿写「T-5 必用 best 信号、绝不跳过」。**当前代码**在狙击循环退出时（距收盘 ≤5s），若 **`best.confidence < min_conf`** 或 **`best.details.get("skip_trade")`**，会置 **`skip_trade`**，**`run_trade_cycle` 本窗不下方向单**。degen 模式 **`min_conf=0`**，但 `MIN_ABS_SCORE`（默认 2.0）和 `MIN_DECISION_CONFIDENCE`（默认 0.30）仍可能过滤。

### E.5 七种指标的设计意图（与附录 A 数值对应）

1. **窗口偏离（权重 1～7）**：直接对应市场问题「现价比窗口开盘高/低多少」；`|window_pct|` 越大权重越大，是主导项。  
2. **微动量（±2）**：最近两根 1m 收盘方向。  
3. **加速度（±1.5 / ±0.5）**：最后一根实体与两根前实体对比，表示动能增强或衰减。  
4. **EMA9/EMA21（±1）**：短趋势。  
5. **RSI（±1～±2）**：极端区反转倾向权重更大。  
6. **放量（±1）**：近 3 根均量 ≥ 前 3 根 1.5 倍时顺最后一根阴阳确认方向。  
7. **tick 趋势（±2）**：狙击阶段累积的现价采样（约 2s 一次）上/下行比例与总位移，补 1m K 之间的缝。

**置信度**：**`min(|score|/7, 1)`** — 除以 7 是为了在 5m 场景里让「窗口项」更容易推到高置信；长周期指标权重相对弱。

### E.6 三种模式（哲学 + 代码行为）

| 模式 | 最低置信度（狙击内） | 名义思路（未启用 Kelly 时） |
|------|----------------------|-----------------------------|
| **safe** | 0.45 | `max(MIN_BET, min(bankroll, 25% bankroll))` |
| **aggressive** | 0.35 | 本金以下全押；有盈利后只用 **利润部分** `bankroll - principal` |
| **degen** | 0.0 | **全仓** `bankroll`（仍受 `MAX_USD` 等约束） |

另：**`FIXED_DIRECTIONAL_USD`**、**`ENABLE_KELLY`**、**`MAX_USD`** 会覆盖或裁剪上述名义。

### E.7 实盘下单路径（摘要）

- 优先：**FOK 市价买**，美元名义 = `bet`。  
- 若无卖盘：**GTC 0.95 限价**，数量 **`MIN_SHARES_POLY=5` 股**，名义下限约 **0.95×5 = 4.75 USD**；资金不足则退回 FOK 尝试。  
- 窗口关闭前每 **`ORDER_RETRY`（3s）** 重试。

### E.8 干跑与「模型入场价」

- 干跑走完整狙击与 **`analyze`**，但 **不** 向 CLOB 提交真实单；扣虚拟 **`bankroll`**，收盘后由 **结算队列** 判输赢并写 JSON。  
- **领先侧/逆势侧** 的模型价见 **`directional_entry_from_window_pct`**（§7.3 与附录 B），避免旧版「永远用 `abs(delta)` 高价」导致逆势赢钱被高估。  
- 结算 oracle：**RTDS Chainlink** 优先，失败则 **Binance 五根 1m 首尾**（见 §11）；可选 **`DRY_RUN_BINANCE_SETTLE=1`** 强制 Binance。

### E.9 `compare_runs.py`（网格回测，修复版）

> ⚠️ 2024 年修复：旧版有循环论证 bug，已重写。详见 §13。

- **9 个置信阈值 × 3 种 sizing（flat/safe/aggressive）= 27 组**
- 决策用窗口前 60 根历史 K 线做 TA 分析，**不含窗口期内 K 线**
- 入场价用 `estimate_fair_prob` + `entry_price_from_fair_prob`（基于 TA confidence）
- 输出 Excel：Summary（含跳过原因统计）/ Best Config Trades / Bankroll Curves

### E.10 结算数据源（以代码为准，纠正旧英文表述）

旧英文写「主要 Binance、失败再 Polymarket API」。**当前实现**为：若启用 **`ChainlinkBtcUsdRtds`**，结算优先 **RTDS 窗口开盘链上价与收盘边界后首条 tick**；不足时 **打印诊断并回退 Binance K 线首尾**；未接 RTDS 则 **直接 Binance**。**不存在**「调 Polymarket HTTP 查 outcome 价」的结算分支。

### E.11 环境与运行命令

最小 `.env` 字段示例见 **`README.md`**。常用命令：

```bash
pip install -r requirements.txt
python setup_creds.py                    # 打印 API 三字段追加到 .env
python bot.py --dry-run --mode safe      # 干跑
python bot.py --mode safe               # 实盘（需凭证）
python bot.py --dry-run --once          # 只跑一个周期
python bot.py --dry-run --max-trades 20
python compare_runs.py --hours 72 --output results.xlsx
python chainlink_rtds.py                # RTDS 自检
python auto_claim.py --headed           # 浏览器赎回辅助
```

### E.12 经验与排障（翻译自旧稿 + 与实现对齐）

1. **趋势一致性**：`analyze()` 用 7 个子信号综合打分，包括 `_trend_strength` 捕捉最近 10 根的方向一致性。  
2. **入场时机**：`SNIPE_START` 可调；过早易反转，过晚价差差；默认 10s 为折中。  
3. **置信度与必交易**：当前 **不再** 保证「压哨必下单」；若全程未达阈值且未触发尖峰，**safe/aggressive 可跳过**。  
4. **模型入场价**影响干跑/Excel 可信度；已用基于 TA confidence 的 **`entry_price_from_fair_prob`**。  
5. **Polymarket 最小 5 股** 与 GTC 0.95 组合 ≈ **4.75 USD** 下限，小资金可能无法走限价退路。  
6. **Binance**：狙击段每约 2s 拉 K 线，失败会重试；`backtest.binance_get` 对断连有退避；网络差可调 **`BINANCE_HTTP_RETRIES`** 等。

---

## 附录 F — 「少送钱」方向单闸（可选环境变量，默认关闭）

以下均在 **`run_trade_cycle`** 完成狙击且未 `skip_trade` 之后、扣款前生效；**默认不设置 = 与旧版完全一致**。

| 变量 | 示例 | 行为 |
|------|------|------|
| `DIRECTION_ORDERBOOK_MAX_SUM` | `1.05` | 双边卖一合计 **>** 该值 → 不做方向单。 |
| `DIRECTION_ONLY_WHEN_BOOK_SUM_LT` | `0.99` | 合计 **≥** 该值 → 不做方向单（仅偏松盘参与）。 |
| `DIRECTION_STRATEGY` | `reversal` / `imbalance` | `ta`=原 `analyze`；`reversal`=反转偏离；`imbalance`=盘口失衡（见附录 G）。 |
| `REVERSAL_MIN_ABS_PCT` | `0.08` | 与 `w_pct=(px-open)/open*100` 同单位（百分数）。 |
| `USE_BOOK_ASK_FOR_ENTRY` | `1` | `entry` 用对应侧的 **best ask**；`None` 或 `>0.97` 跳过。 |
| `MIN_DECISION_CONFIDENCE` | `0.2` | 仅 **`DIRECTION_STRATEGY=ta`** 时若 `confidence` 低于则跳过。 |
| `SPIKE_JUMP` | `999` | 覆盖默认 **1.5**，极大值可关闭尖峰分支。 |
| `SNIPE_START` | `45` | 提前进场秒数（仍受代码上下限夹紧）。 |

**建议干跑组合试验**（非投资建议）：例如同时设 `DIRECTION_ORDERBOOK_MAX_SUM=1.05`、`USE_BOOK_ASK_FOR_ENTRY=1`、`MIN_DECISION_CONFIDENCE=0.2`；若完全走反转再加 `DIRECTION_STRATEGY=reversal`。

---

## 附录 G — 第二阶段：盘口失衡 + 概率错价 + edge 仓位（可选）

| 变量 | 默认 | 含义 |
|------|------|------|
| `DIRECTION_STRATEGY` | `ta` | 设 **`imbalance`** 时：用 Up/Down 两 token 各自前 N 档 **`(bid-ask)/(bid+ask)`**，仅单侧 **`> IMBALANCE_THRESHOLD`** 才给方向；**双侧同时过阈 → 不下**（降噪）。 |
| `ORDERBOOK_IMBALANCE_DEPTH` | `3` | 每侧累加档数。 |
| `IMBALANCE_THRESHOLD` | `0.25` | 失衡绝对值下限。 |
| `USE_FAIR_PROB_EDGE` | 关 | 用策略 confidence 作为概率基准，避免用窗口内价格变动的循环论证。买 Up 要求 **`fair - entry > MIN_PRICE_EDGE`**，买 Down 要求 **`(1-fair) - entry > MIN_PRICE_EDGE`**。 |
| `MIN_PRICE_EDGE` | `0.03` | 最小概率优势。 |
| `FAIR_PROB_SIGMOID_SCALE` | ~~`50`~~ | **已废弃**，不再使用 sigmoid。 |
| `USE_EDGE_POSITION_SIZING` | 关 | 在 **无** 固定名义、**无** Kelly 时，`bet = bankroll * EDGE_SIZING_BANKROLL_FRAC * min(1, edge*EDGE_SIZING_EDGE_SCALE)`，再与 `MAX_USD`、资金、`MIN_BET` 约束。 |
| `EDGE_SIZING_BANKROLL_FRAC` | `0.02` | 基准资金比例。 |
| `EDGE_SIZING_EDGE_SCALE` | `10` | edge 放大系数。 |
| `MIN_SECONDS_BEFORE_CLOSE_FOR_TRADE` | 未设 | 例 **`8`**：距收盘不足该秒则不做方向单。 |
| `LOSS_STREAK_COOLDOWN` | 关 | **`1`** 启用；仅 **`dry_history`** 中 `directional_settle` 计数。 |
| `LOSS_STREAK_MIN_TRADES` | `6` | 总成交笔数 ≥ 此值才检查连亏。 |
| `LOSS_STREAK_WINDOW` | `5` | 看最近几条结算。 |
| `LOSS_STREAK_MAX_LOSSES` | `4` | 其中至少几条为输则暂停一周期。 |

**推荐组合（干跑）**：`USE_BOOK_ASK_FOR_ENTRY=1` + `USE_FAIR_PROB_EDGE=1` + `USE_EDGE_POSITION_SIZING=1` + `DIRECTION_ORDERBOOK_MAX_SUM=1.05`；方向源在 **`ta` / `reversal` / `imbalance`** 中三选一。

---

*文档路径：`TRADING_AND_SYSTEM_LOGIC.md`。若修改 `bot.py` 等分支逻辑，请同步更新本文件对应章节。*
