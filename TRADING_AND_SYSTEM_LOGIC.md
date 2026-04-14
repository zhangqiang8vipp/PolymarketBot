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
- **`SNIPE_DEADLINE = 5`**：`snipe_loop` 在距收盘 **小于 5 秒** 时退出主循环（不再做常规 2s 轮询），进入收尾逻辑。
- **`POLL = 2.0`**：狙击阶段内两次 `analyze` 之间的 `sleep` 秒数。
- **`SPIKE_JUMP = 1.5`**：相邻两次 `analyze` 的 `|score|` 差 ≥ 1.5 视为「尖峰」，**立即**返回当前 `res`（见 §6）。
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
3. 若 **`t_left < _snipe_start_s()`**（默认 `_snipe_start_s()` 来自 `SNIPE_START`，且被夹在 `[SNIPE_DEADLINE+1, WINDOW-5]`，即默认 **6～295** 之间）：  
   - **`sleep_s = t_left + 0.5`**，休眠后 **`continue`**。  
   - **含义**：若进程在「本 5m 窗已剩余不足 `SNIPE_START` 秒」时才进入循环，**本窗不会调用 `run_trade_cycle`**，直接睡到近似下一窗。这是与「每窗必交易」不同的 **门控**。
4. 否则调用 **`run_trade_cycle(...)`**，外层 **`try/except`**：异常打印栈并 `sleep(5)` 继续。
5. **`--once`** 或 **`max_trades`** 达标则退出循环。
6. 若 **`now() < close_this`**（`close_this = wts + WINDOW`）：再 **`sleep(max(0.5, close_this - now() + 0.5))`**，避免同一窗内重复进周期。

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
3. **打印 `[周期]` 行**（含 `dry_run`、`client`、套利 POLL、套利日志/实盘开关）。
4. **`log_up_down_ask_spread(..., silent=False)`**（周期 **第一次** 套利检测）。若返回 **`True`**（实盘两腿 FOK 成功且扣款），**整个 `run_trade_cycle` 直接 `return`**：本窗 **不再** 取 Chainlink 开盘价、不再狙击、不下方向单。
5. **`window_open_oracle(window_ts, chainlink_feed)`**（§5）。若抛异常：打印错误，**`return`**。
6. 打印开盘价与来源说明。
7. **`sleep_s = close_at - _snipe_start_s() - now()`**；若 `> 0` 则 `time.sleep(sleep_s)`（距进入狙击尚早的长睡）；可能打印 `[调度]`。
8. **套利后台线程**（§9）：若 **`ARBITRAGE_POLL_S > 0`** 且 **`(ENABLE_ARBITRAGE_LOG 为真 OR (ENABLE_ARBITRAGE_TRADE 且 client 非空且非 dry_run))`**，则启动 **`_arb_worker`** 守护线程，在狙击阶段内每 `poll` 秒调用 **`log_up_down_ask_spread(..., silent=True)`**；否则若 POLL=0 且开了日志或实盘开关，打印一句说明「仅周期开头测一次」。  
   - 若 **`poll > 0` 但既未开日志也未满足 trade 条件**，则 **不启动** 后台线程（`_enable_arbitrage_trade()` 为真但 dry_run 时 `trade_a` 为假，**只要开了日志仍会启动**）。
9. **`snipe_loop(..., arb_hit=arb_hit_ev)`**（§6）。若抛出 **`ArbitrageCycleDone`**：**`return`**。  
   **`finally`**：**`stop_arb.set()`**，若 `arb_thread` 存在则 **`join(timeout=min(8, poll+2) 或 2)`**。
10. 若 **`decision.details.get("skip_trade")`**：打印置信度不足，**`return`**（本窗不下方向单）。
11. **`token_up = (decision.direction == 1)`**，**`token_id = up_tid if token_up else down_tid`**。
12. **`px_decide = ticks[-1] if ticks else snipe_current_price(chainlink_feed)`**。
13. **`w_pct = (px_decide - window_open) / window_open * 100.0`**。
14. **`entry = directional_entry_from_window_pct(decision.direction, w_pct)`**（§7.3）。
15. **`cap_mx = _max_directional_usd()`**（仅 `MAX_USD`），**`fix_usd = _fixed_directional_usd()`**。
16. **`with _BOT_STATE_LOCK:`** 内决定 **`bet`**（§8）；任一失败条件则 **`return`**。
17. 打印信号、得分、置信度、下注、参考入场价。
18. **实盘且 `client` 非空**：在 **`now() < close_at`** 内循环下单（§10）；失败则 `ORDER_RETRY` 秒重试；超时未 `placed` 则 **`return`**（**注意**：此分支 `return` 前 **未** 扣 `state.bankroll`，因扣款在下单成功逻辑之后）。
19. **`with _BOT_STATE_LOCK:`**：**`state.bankroll -= bet`**（无论干跑或实盘，只要走过扣款点前路径；实盘若上面 `return` 则不会到此）。
20. **干跑**：构造 **`QueuedDrySettle`** → **`enqueue_settlement`** → `trades+=1`、写 history、`**_save_dry_run_state**` → **`return`**。
21. **实盘**：**`QueuedLiveRedeemHint`** 入队、`trades+=1`、打印赎回提示 → **`return`**（实盘方向单 **不** 在程序内自动结算盈亏，仅提醒）。

---

## 5. 开盘价：`window_open_oracle`

目标：**Price to beat**（与 Polymarket 页面对齐时优先 RTDS Chainlink）。

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

输入：**`window_open`**（oracle 开盘价）、**`window_close`**（= `close_at` 浮点）、**`mode`**、**`chainlink_feed`**、**`arb_hit`**。

1. 打印狙击参数：`_snipe_start_s()`、**`SNIPE_PRICE_SOURCE`**（oracle/binance）、RTDS 状态后缀。
2. **`min_conf = min_confidence_for_mode(mode)`**：safe **0.30**，aggressive **0.20**，degen **0.0**。
3. **`while True`**：  
   - **`t_left = window_close - now()`**。  
   - 若 **`t_left < SNIPE_DEADLINE`**：**`break`** 出循环。  
   - 若 **`arb_hit` 已 set**：抛 **`ArbitrageCycleDone`**。  
   - 若 **`t_left > ss`**（尚未进入狙击窗口）：**`sleep(max(0.15, min(5.0, t_left - ss)))`**，`continue`。  
   - 第一次进入狙击：`[狙击] 已进入狙击阶段...`。  
   - **`px = snipe_current_price`** → **`ticks.append(px)`**。  
   - 再检查 **`arb_hit`**。  
   - 拉 **`fetch_recent_candles_1m(60)`**，最多 5 次重试。  
   - **`res = analyze(window_open, px, candles, tick_prices=ticks[-120:])`**。  
   - 更新 **`best`**：若 `best is None` 或 **`abs(res.score) > abs(best.score)`** 则 **`best = res`**。  
   - **尖峰**：若 **`last_score` 非空** 且 **`abs(res.score - last_score) >= SPIKE_JUMP`**：**立即 `return res, ticks`**（**不** 检查 `min_conf`）。  
   - **置信度达标**：若 **`res.confidence >= min_conf`**：**`return res, ticks`**。  
   - **`last_score = res.score`**，**`sleep(POLL)`**。

4. **退出 `while` 后**（因 `t_left < SNIPE_DEADLINE`）：  
   - 若 **`best is None`**（整个狙击段从未成功跑过带 `ss` 内逻辑 — 理论上少见）：再取一次价与 K 线，**`best = analyze(...)`**。  
   - 若 **`best.confidence < min_conf`**：在 **`details` 中设 `skip_trade=True`**，返回该 **`AnalysisResult`**（**`run_trade_cycle` 会跳过下单**）。  
   - 否则 **`return best, ticks`**。

### 6.1 `snipe_current_price`

- **`SNIPE_PRICE_SOURCE=binance`**：**`fetch_btc_price()`**（Binance `ticker/price`）。  
- **否则**：若 feed 存在且 **`latest_price()`** 非空则用之；否则 **`fetch_btc_price()`**。

---

## 7. 信号与入场价模型

### 7.1 `strategy.analyze(window_open_price, current_price, candles, tick_prices)`

- 若 **`window_open_price <= 0` 或 `current_price <= 0`**：返回 **`direction=1, score=0, confidence=0, details.error=invalid_prices`**（下游若未过滤，可能被当作「方向 Up、零置信」）。
- **`window_pct = (current - open) / open * 100`**。  
- **`w = _window_delta_weight(abs(window_pct))`**（分段：>0.10→7，>0.02→5，>0.005→3，>0.001→1，否则 0）。  
- **`w_score = w` 若 `window_pct>0`；`-w` 若 `window_pct<0`；否则 0**。  
- **`score = w_score + micro_momentum + acceleration + ema_cross + rsi_weight + volume_surge + tick_trend`**（各子项见 **`strategy.py`** 与 **附录 A**）。  
- **`direction = 1 if score >= 0 else -1`**。  
- **`confidence = min(abs(score) / 7.0, 1.0)`**。

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
2. **`resolve_window_direction_with_meta`** **最多 3 次**，失败间隔 **`2*(attempt+1)`** 秒。  
3. 全失败：**`actual = -job.direction`**，**`settle_method=error_fallback_loss`**（**按输处理**，不增发赢钱）。  
4. **`win = (actual == job.direction)`**。  
5. 赢：**`shares = bet / max(entry, 1e-9)`**，**`settle_payout = shares * 1.0`**（模型按每股 1 USD 赎回），**`bankroll += settle_payout`**。  
6. 若 **`bankroll < min_bet`**：**`bankroll = principal`**（**bust 重置**）。  
7. 写 **`directional_settle`** history、**`_save_dry_run_state`**、打印、`TRADE_TRAIN_JSONL` 若设则追加。

---

## 12. `chainlink_rtds.py` 行为摘要

- **WebSocket**：`POLY_RTDS_WS` 默认 **`wss://ws-live-data.polymarket.com`**；订阅 **`crypto_prices_chainlink`**，filters **`btc/usd`**；也接受 **`crypto_prices`** 主题的快照数组。  
- **缓冲**：**`(ts_ms, value)`** 列表，**`KEEP_HISTORY_MS = 2h`** 修剪。  
- **`first_price_at_or_after(boundary_unix_s, max_payload_lag_ms=...)`**：最早 **`ts_ms >= boundary*1000`**；若 `max_payload_lag_ms` 非空且最早 tick 相对边界过晚则 **返回 None**（**开盘**用，**收盘**调用不传 lag）。  
- **`wait_first_price_at_or_after`**：轮询直至有值或 **`TimeoutError`**。  
- **`latest_price()`**：缓冲内 **时间戳最大** 的一条的 value。  
- **看门狗**：仅当 **「距上次成功写入 btc/usd 的墙钟秒数」> `RTDS_AUTO_RECONNECT_STALE_S`** 且过 **`RTDS_WATCHDOG_GRACE_S`** 与最小重连间隔时 **`_force_reconnect`**（清空缓冲、关闭 WS）。**不**用 payload 相对墙钟滞后触发。

---

## 13. `compare_runs.py` 与实盘的差异

- **数据**：仅 **Binance 历史 1m**（`fetch_klines_range_hours`），**无** RTDS、**无** 尖峰、**无** 狙击循环。  
- **决策时刻**：每个窗在 **`decision_ms = (window_ts + WINDOW - SNIPE_START) * 1000`** 取 **`rows[i1][1].close`** 为 **`current_price`**；**`window_open = rows[i0][1].open`**，`i0/i1/i_res` 由 **`idx_at_or_before`** 在 `ts_list` 上取。  
- **K 线传入 `analyze`**：**`hist[-60:]`**（至少需 **`i1+1 >= 25`** 才继续）。  
- **结果**：**`outcome`** 来自 **`o0 = rows[i0].open`, `c_end = rows[i_res].close`**，与 **`bot._binance_window_edge_prices`** 语义一致（注释已写明）。  
- **置信过滤**：网格阈值 **`THRESHOLDS 0.0..0.8`** 与 **`res.confidence`** 比较（**不是** `min_confidence_for_mode`）。  
- **sizing**：**`flat` / `safe` / `aggressive`** 三种（**无 degen**）；**`entry`** 使用 **`directional_entry_from_window_pct`**（从 **`bot` 导入**）。  
- **胜负 PnL**：**`shares = bet/entry`**，赢则 **`bankroll += shares`**（与干跑模型一致）。

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
| `SNIPE_START` | 狙击提前秒数，夹紧到 [6, 295] |
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
8. **结算队列** `_settlement_consumer_loop`：单条任务异常会打印栈并 **吞掉**（不自动重试该条 settle）。

---

## 18. `auto_claim.py`（与 bot 决策无关）

- 周期性打开 **`POLYMARKET_CLAIM_URL`**（默认 Portfolio）。  
- 尝试点击 **Redeem / Claim / Collect**。  
- 可选 **`POLYMARKET_STORAGE_STATE`** 存储状态文件。

---

## 附录 A — `strategy.analyze` 各加成分项（与代码逐行一致）

以下 **`candles` 均为「最老在前」，最后一根为当前分钟**。

### A.1 `_window_delta_weight(a)`，`a = abs(window_pct)`

| 条件 | 返回值 |
|------|--------|
| `a > 0.10` | 7.0 |
| `a > 0.02` | 5.0 |
| `a > 0.005` | 3.0 |
| `a > 0.001` | 1.0 |
| 否则 | 0.0 |

**`w_score`**：`window_pct > 0` → `+w`；`window_pct < 0` → `-w`；`window_pct == 0` → `0`。

### A.2 `_micro_momentum(candles)`

- `len < 2` → 0  
- 否则：`last.close > prev.close` → **+2**；`<` → **-2**；相等 → **0**

### A.3 `_acceleration(candles)`

- `len < 3` → 0  
- `m0 = last.close - last.open`，`m2 = candles[-3].close - candles[-3].open`  
- `m0 > 0 and m0 > m2` → **+1.5**  
- `m0 < 0 and m0 < m2` → **-1.5**  
- `m0 > 0 and m0 < m2` → **-0.5**  
- `m0 < 0 and m0 > m2` → **+0.5**  
- 否则 **0**

### A.4 `_ema_cross(candles)`

- `len(closes) < 21` → 0  
- `E9` 与 `E21` 为对 **closes** 的 EMA(9)、EMA(21) 序列（首值为 SMA 种子）  
- `e9[-1] > e21[-1]` → **+1**；`<` → **-1**；否则 **0**

### A.5 `_rsi_weight(candles)`

- RSI 为 Wilder 风格近似（见 `_rsi`：14 根涨跌和平均）  
- `RSI > 75` → **-2**；`< 25` → **+2**；`> 60` → **-1**；`< 40` → **+1**；否则 **0**

### A.6 `_volume_surge(candles)`

- `len < 6` → 0  
- `recent = 最后 3 根均量`，`prior = 再往前三根均量`；`prior==0` → 0  
- 若 `recent < 1.5 * prior` → 0  
- 否则：`last.close >= last.open` → **+1**，否则 **-1**

### A.7 `_tick_trend(tick_prices)`

- `len < 5` → 0  
- `move_pct = (last-first)/first*100`；`|move_pct| < 0.005` → 0  
- `ups` = 相邻上升对数，`downs` = 相邻下降对数，`n = ups+downs`；`n==0` → 0  
- `ups/n >= 0.60` 且 `move_pct > 0` → **+2**  
- `downs/n >= 0.60` 且 `move_pct < 0` → **-2**  
- 否则 **0**

### A.8 汇总

**`score = w_score + 上述六项`**  
**`direction = 1 if score >= 0 else -1`**  
**`confidence = min(abs(score)/7.0, 1.0)`**

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

- 默认在距收盘 **`SNIPE_START`（默认 10）秒** 内进入高速轮询（见 **`_snipe_start_s()`** 夹紧范围）。  
- 轮询间隔 **`POLL=2s`**：每次拉 Binance 最近 60 根 1m K、取现价（oracle 或 binance 见 `SNIPE_PRICE_SOURCE`），把本轮累积的 **`ticks[-120:]`** 喂给 **`analyze`**。  
- **尖峰**：相邻两次 **`|score|`** 差 **≥ `SPIKE_JUMP`（1.5）** 时 **立即** 采用当前结果下单，**不要求** 达到模式最低置信度。  
- **置信度达标**则提前返回。  
- **与旧英文说明的差异（重要）**：旧稿写「T-5 必用 best 信号、绝不跳过」。**当前代码**在因 **`SNIPE_DEADLINE`** 退出循环时，若 **`best.confidence < min_confidence_for_mode(mode)`**，会置 **`skip_trade`**，**`run_trade_cycle` 本窗不下方向单**。degen 模式 **`min_conf=0`**，仍可能在「零置信」边界上下单；safe/aggressive 则严格受阈值约束。

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
| **safe** | 0.30 | `max(MIN_BET, min(bankroll, 25% bankroll))` |
| **aggressive** | 0.20 | 本金以下全押；有盈利后只用 **利润部分** `bankroll - principal` |
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

### E.9 `compare_runs.py`（网格回测）

- **9 个置信阈值 × 3 种 sizing（flat/safe/aggressive）= 27 组**；在 **历史 1m K** 上调用真实 **`strategy.analyze`**。  
- **决策时刻**固定在「距收盘 `SNIPE_START` 秒」那根 K 的 close（见 `compare_runs` 源码），**无** 狙击循环、**无** 尖峰、**无** RTDS。  
- 输出 Excel：Summary / Best Config Trades / Bankroll Curves。

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

1. **窗口内价格方向**仍是 5m 二元问题的核心；EMA/RSI 噪声大，故用大权重 `window_delta`。  
2. **入场时机**：`SNIPE_START` 可调；过早易反转，过晚价差差；默认 10s 为折中。  
3. **置信度与必交易**：当前 **不再** 保证「压哨必下单」；若全程未达阈值且未触发尖峰，**safe/aggressive 可跳过**。  
4. **模型入场价**影响干跑/Excel 可信度；已用 **方向敏感** 的 **`directional_entry_from_window_pct`**。  
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
| `USE_FAIR_PROB_EDGE` | 关 | 用现价相对开盘的 **sigmoid 估计 P(涨)**，与 **entry**（模型或卖一）比：买 Up 要求 **`fair - entry > MIN_PRICE_EDGE`**，买 Down 要求 **`(1-fair) - entry > MIN_PRICE_EDGE`**。 |
| `MIN_PRICE_EDGE` | `0.03` | 最小概率优势。 |
| `FAIR_PROB_SIGMOID_SCALE` | `50` | sigmoid 陡峭度。 |
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
