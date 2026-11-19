# Intraday Live Pipeline (Blocker B7)

> **Status:** APPROVED 2026-11-19. All open questions (Q1 to Q5) resolved by the operator.
> **Author date:** 2026-11-19
> **Supersedes:** the intraday sections of the retired Phase 3/4 docs (`designs/` was deleted 2026-09-06; recover via `git show e20ea00^:designs/`).
> **Source of truth:** once approved, this doc wins over the code (CLAUDE.md rule 8).

---

## 1. Problem

Today, intraday "trading" is a single pre-market batch job (09:10 to 09:25 IST) that can only see
stale data: the prior session's F&O open interest and overnight news. The live 30-minute
pipeline the system was designed around has never run:

| Gap | Where |
|-----|-------|
| `IntradayPipeline` is never constructed; `Orchestrator()` never receives `intraday_runner=` | `orchestrator.py:main()` |
| `signal_runner.run_intraday()` has no implementation anywhere | `executor/intraday.py:_run_intraday_signals` |
| `_intraday_cycle` calls `run_cycle(run_date=...)`, but `run_cycle()` takes no arguments, so it would raise `TypeError` the first time it is wired | `tasks_executor.py:67` |
| 4 of 6 signal sub-scores read live features nothing computes (`premarket_gap_pct`, `orb_range_vs_20d_avg_ratio`, `nifty_intraday_direction`, `bid_ask_spread_pct`) | `brain/signals/intraday.py` |
| `IntradayClassifier` needs `vwap_current`, `orb_high`, `minutes_since_market_open`, `price_vs_open_pct`, which nothing writes | `brain/entry_timing.py` |
| Circuit breakers are evaluated once, pre-market, with intraday P&L hard-coded to 0 | `tasks_brain.py:_morning_batch_recommendations` |

### 1a. A live-money hazard in the path that does run (found during this review)

When `KITE_API_KEY`/`KITE_ACCESS_TOKEN` are set, `gtt_dispatch` (09:25) sends every
`queued_for_execution` intraday recommendation to Kite as a **CNC OCO-GTT valid for 365 days**
(`kite_broker.place_gtt` hard-codes `"product": "CNC"`). Kite GTTs do not support MIS. So an
"intraday" signal becomes a **delivery position that can be held for up to a year**. This breaks
CLAUDE.md §10 ("never convert an intraday position to swing or long-term"). There is no
paper-mode switch: if credentials are present, orders are real.

**Resolved 2026-11-19 (operator):** only MIS orders with a stop-loss, never long-validity orders.
Hotfix PR #26 removes `gtt_dispatch` and adds an `OrderManager` pre-trade check that rejects any
non-MIS product.

### 1b. The data constraint

**Resolved 2026-11-19 (operator):** the Kite Personal account provides everything needed live:
WebSocket ticks, quotes/LTP, order APIs, and a registered postback (webhook) for order updates.
It does **not** provide historical candles. Consequences:

- The data feed is Kite only. No second provider.
- `KiteBroker.get_historical_ohlcv()` cannot be used anywhere. Daily history keeps coming from
  NSE bhavcopy (`prices` table); intraday history only exists if we record it ourselves
  (§3.2), so minute-bar recording is a **hard requirement**, not a nice-to-have.
- Any feature that needs past intraday data (e.g. "20-day average opening range") must either
  wait for our own recorded bars or use a daily-data proxy (Q4).
- The startup capability check (§3.1) stays, so a plan or permission change fails loudly instead
  of silently degrading.

---

## 2. Goals and non-goals

**Goals (v1)**
- A 30-minute cycle, 09:30 to 14:30 IST, that scores a watchlist using fresh live features and
  turns approved signals into same-day **MIS** entries with broker-side protective stops.
- **Operator rule: MIS only, and every entry has a stop-loss.** No CNC, no GTT, no order whose
  validity extends past the session. Enforced by `OrderManager` check #0 (PR #26) and L9.
- Execution goes through `PaperBroker` by default (real ticks, simulated fills), behind an
  explicit `BOOMER_TRADING_MODE=paper|live` switch that defaults to `paper`.
- Every approve/reject/modify decision is logged with rule, inputs and outcome (CLAUDE.md §9).
- Mandatory square-off at 15:14 IST, triggered only by the orchestrator.

**Non-goals (v1)**
- Shorting. `TradePlanGenerator` already rejects non-LONG; that stays.
- F&O instruments (dropped from scope 2026-09-06).
- Real-money trading. Gated behind the paper-validation criteria (Q5) and SEBI blocker B4.
- Any change that lets an intraday position survive past 15:14 IST.

---

## 3. Architecture

```
            pre-market (unchanged batch)                     live (new)
 07:30 eod collector ─► 09:05 batch features ─► 09:10 watchlist ─┐
                                                                 ▼
 MarketDataFeed ──ticks──► LiveBarStore (1-min OHLCV, VWAP, depth snapshot) ──► parquet (EOD)
      ▲                         │
      │ subscribe(watchlist     ▼
      │  + NIFTY 50)      LiveFeatureComputer (pure fns) ─► features dict (in memory)
      │                         │
      │                         ▼
      │       IntradayRunner.run_intraday(as_of)  ← IntradayPipeline.run_cycle()  (*/30 09–14)
      │         merge batch features (point-in-time, today)
      │         IntradaySignalGenerator → TradePlanGenerator(LTP) → IntradayClassifier
      │         live circuit breakers → pre-trade checks → log decision
      │                         │
      │                         ▼
      └──────────────  ExecutionBroker = PaperBroker(price_source=feed) | KiteBroker
                         MIS entry + SL-M stop + LIMIT target (OCO emulated by PositionManager)
 15:14 intraday_squareoff ─► cancel open legs, MARKET exit MIS
```

### 3.1 `MarketDataFeed` (new abstraction)

Market data is separated from order execution so paper mode can use the live feed while orders go
to `PaperBroker`, and so tests can drive the pipeline with a fake feed. v1 has one adapter:
`KiteFeed`, built from the existing `KiteBroker` ticker code (including the #23 race fixes).
Order updates arrive through the existing WebSocket `on_order_update`; the registered postback
URL is a second channel that can be added later if the WebSocket proves unreliable.

```python
class MarketDataFeed(ABC):
    def connect(self) -> None
    def subscribe(self, symbols: list[str], mode: FeedMode) -> None   # FeedMode.QUOTE | FULL
    def on_tick(self, cb: Callable[[Tick], None]) -> None
    def capabilities(self) -> FeedCapabilities   # has_depth, has_index, has_historical
    def health(self) -> FeedHealth               # connected, last_tick_age_s
```

`Tick` is our own dataclass (symbol, ltp, volume_cum, avg_traded_price, ohlc_day, best bid/ask,
ts_ist), so no provider payload leaks past the adapter. Adapters live under
`src/executor/brokers/feeds/` because they import broker SDKs (CLAUDE.md §9).

**Startup capability check:** on boot, the orchestrator calls `feed.capabilities()` and
`feed.health()`. If there is no feed, or no tick arrives within 60 s of 09:15, it sends a CRITICAL
Telegram alert and the intraday pipeline disables itself for the day. It never falls back
silently to the batch path.

### 3.2 `LiveBarStore`

- Builds 1-minute OHLCV bars per subscribed symbol from ticks, keeping the latest depth snapshot
  and the provider's `avg_traded_price` (VWAP).
- In memory for the session. At 15:35 it flushes to `data/parquet/minute_bars/date=YYYY-MM-DD/`.
  This finally implements the "organic minute-bar collection" that B6 marked as resolved but that
  no code writes today.
- Thread-safe: ticks arrive on the feed thread, reads happen on the cycle thread.

### 3.3 `LiveFeatureComputer` (pure functions, fully unit-testable)

| Feature | Definition | Source |
|---------|-----------|--------|
| `premarket_gap_pct` | `(day_open − prev_close) / prev_close × 100` | tick `ohlc.open` after 09:15; `prev_close` from `prices` table |
| `orb_high`, `orb_low` | max high / min low of the 09:15 to 09:29 1-min bars | LiveBarStore |
| `orb_range_vs_atr` | `(orb_high − orb_low) / atr_14d` (Q4) | LiveBarStore + batch `atr_14d` |
| `vwap_current` | provider `avg_traded_price` | tick |
| `price_vs_open_pct` | `(ltp − day_open) / day_open × 100` | tick |
| `nifty_intraday_direction` | `+1` if NIFTY 50 is above prev close by more than 0.10%, `−1` if below by more than 0.10%, else `0` | index tick |
| `bid_ask_spread_pct` | `(best_ask − best_bid) / mid × 100`; `None` when the feed has no depth | FULL-mode tick |
| `minutes_since_latest_news` | `(now − MAX(filings.observed_at for symbol)) / 60` | DB |
| `minutes_since_market_open` | `(now − 09:15) / 60` | clock |

Worked example (for the test): prev_close 500.00, day_open 506.00, so gap = **+1.20%**. ORB
bars give high 509.40 and low 504.20, a range of 5.20. With `atr_14d` 10.40 the ratio is
**0.50**, so the opening-range sub-score is `clip(1 − 0.50) = 0.50`.

### 3.4 Where live features are stored (point-in-time decision)

The `features` table has **date granularity** (`valid_from` is a date). Writing a 14:00 value
there would make it visible to any same-day query at 09:30, which is a lookahead bug under
CLAUDE.md §9. So live features are **not written to `features`**. They are merged in memory
with the batch features for the cycle, and the full merged dict is persisted per signal in
`signals.feature_snapshot`, which already exists. That is enough for audit and replay. Replaying
past days uses the minute-bar parquet plus the same pure functions.

### 3.5 `IntradayRunner.run_intraday(as_of)`

The class `IntradayPipeline._signal_runner` has always expected. Per cycle:

1. `health()` check. If the last tick is older than 120 s, skip entries, monitor only, and send
   an alert.
2. Re-evaluate circuit breakers using **real** inputs: today's realised intraday P&L and
   consecutive losses from `positions`, live NIFTY move %, the current IST time. If tripped, log
   attribution and return.
3. For each watchlist symbol not in cooldown and without an open position: compute the features,
   merge them with `fs.get_features_as_of(sym, today)`, then run `IntradaySignalGenerator`.
4. `TradePlanGenerator.generate(current_price=ltp, ...)`, then `IntradayClassifier.classify()`.
   An empty plan means skip, and it is logged.
5. Pre-trade checks (existing `PreTradeChecker`), then size from the intraday bucket.
6. Submit through `OrderManager` with `ProductType.MIS`: an entry order (LIMIT at the entry-plan
   price, valid 30 min per Loophole 1), then on fill an **SL-M stop** and a **LIMIT target**.
   When one leg fills, `PositionManager` cancels the other (OCO emulation, because GTT does not
   support MIS).
7. `record_signal_acted(sym)` starts the 60-min cooldown, and the decision is logged.

### 3.6 Watchlist (replaces the 09:10 to 09:25 batch recommendation path for intraday)

A full-universe stream (~2,400 symbols) is unnecessary and heavy. The 09:10 task becomes
`intraday_watchlist`: rank the pre-market universe by batch score (F&O OI, news, beta) with
liquidity filters (`avg_traded_value_20d`, `min_stock_price` from `risk_config`, so no hard-coded
rupees), take the **top N = 50** (Q3), and persist them to a new `intraday_watchlist`
table (migration `0008`). The feed subscribes those symbols plus NIFTY 50 at 09:14.

---

## 4. Orchestrator wiring

- `main()` builds the feed, the `ExecutionBroker` (chosen by `BOOMER_TRADING_MODE`), the
  `OrderManager`, the `PositionManager`, the `IntradayRunner` and the `IntradayPipeline`, then
  passes `intraday_runner=` to `Orchestrator`.
- Fix `_intraday_cycle`: call `run_cycle()` with no arguments. The pipeline reads IST time itself.
- Start the cycle at **09:30** (the first `*/30` slot after the ORB window closes). The 09:00
  slot already falls through to monitoring only.
- `intraday_squareoff` (15:14) also cancels open stop/target legs before the MARKET exit.
- `PaperBroker` needs `price_source` to be a `MarketDataFeed`, not a `Broker`. This is a small
  refactor.

## 5. Mode switch

`BOOMER_TRADING_MODE` (`paper` is the default; `live` needs both the env var **and** a
`risk_config.live_trading_enabled = 1` row written by a migration or dashboard action). If
either is missing, the mode is paper. The startup banner and every Telegram decision message
show the mode.

## 6. What gets retired

- `gtt_dispatch` for intraday recommendations (removed from the registry, with a
  `project-status.md` entry). GTT stays in the codebase only for any future delivery use.
- `_morning_batch_recommendations` for intraday, replaced by `intraday_watchlist`.
- The 8 orphaned `compute_*` functions flagged in #25 (separate `refactor/` PR).

---

## 7. Loopholes and decisions

| # | Loophole | Decision |
|---|----------|----------|
| L1 | Signal goes stale between generation and fill | Entry LIMIT valid 30 min; cancel if not filled by the next cycle |
| L2 | The same stock flickers in and out every cycle | 60-min per-stock cooldown (exists). **Persist it** by deriving it from today's `recommendations`, so a mid-day restart doesn't reset it |
| L3 | A cycle overruns into the next one | Non-blocking lock (exists); the second cycle is skipped and logged |
| L4 | Square-off triggered from two places | Only the orchestrator's 15:14 task (exists) |
| L5 | Feed silently dies mid-session | Tick-age health check each cycle; more than 120 s means no new entries plus an alert. Existing stops are broker-side (live) or simulated on the last tick (paper) |
| L6 | Kite session expires mid-day | Existing gap (#23 note): re-auth does not reconnect the ticker. The feed adapter owns reconnect-with-fresh-token |
| L7 | Process restart mid-session loses `LiveBarStore` | On restart, ORB features for that day are `None` (sub-signal skipped, not guessed). Incremental parquet flush every 15 min limits the loss |
| L8 | Gap greater than 2.5% | No entry (signal layer scores 0, classifier returns `[]`), as today |
| L9 | Stop leg rejected after the entry fills (unprotected position) | Operator rule: every entry has a stop. The trade plan's stop price is validated **before** the entry is sent (an entry with no valid stop is never placed). The SL-M is placed immediately on fill; if it is rejected, immediate MARKET exit plus a CRITICAL alert. Never leave an MIS position without a stop |
| L10 | Lookahead via the date-granular feature store | Live features are never written to `features` (§3.4) |
| L11 | Stop moved against the trade | Stops only trail favourably (CLAUDE.md §10); `trail_stop` is reused with an MIS-aware path |
| L12 | Partial fill on entry | Stop and target are sized to the filled quantity; the remainder is cancelled at validity expiry |
| L13 | `filings.observed_at` is commented "UTC" in migration 0002, which predates the IST migration | Verify actual stored values before trusting `minutes_since_latest_news` |
| L14 | `_apm_circuit_check` iterates `__dataclass_fields__` and reads `.name` on a string key, so it raises `AttributeError` whenever a breaker actually trips | Fix in PR 4 with a regression test (same class of bug as the `any_tripped()` fix in #25) |

---

## 8. Delivery plan (one PR each, tests mandatory)

| PR | Scope |
|----|-------|
| 0 | ~~`hotfix/`: remove `gtt_dispatch`, MIS-only pre-trade check~~: PR #26 |
| 1 | `MarketDataFeed` + `Tick` + `KiteFeed` adapter (FULL mode for depth) + capability/health check + `BOOMER_TRADING_MODE` + `PaperBroker` on the feed |
| 2 | `LiveBarStore` + parquet flush + `LiveFeatureComputer` (worked-example tests) |
| 3 | `intraday_watchlist` task + migration `0008` + retire intraday GTT and morning-recommendation path |
| 4 | `IntradayRunner` + orchestrator wiring + MIS entry/SL-M/target OCO emulation + live circuit breakers + L2/L9/L14 fixes |
| 5 | Paper-session reporting: EOD Telegram summary + dashboard intraday view |

---

## 9. Open questions (must be answered before the PRs they block)

| ID | Question | Blocks | Recommendation |
|----|----------|--------|----------------|
| Q1 | ~~Live data source~~ | | **Resolved 2026-11-19:** Kite Personal provides live ticks/quotes; no historical candles (§1b) |
| Q2 | ~~Fix the §1a GTT hazard now?~~ | | **Resolved 2026-11-19:** yes, MIS-only. PR #26 |
| Q3 | ~~Watchlist size N~~ | | **Resolved 2026-11-19:** N = 50 |
| Q4 | ~~ORB ratio definition~~ | | **Resolved 2026-11-19:** permanently `orb_range_vs_atr = (orb_high − orb_low) / atr_14d`. The feature is renamed from `orb_range_vs_20d_avg_ratio` in `IntradaySignalGenerator` and `track_intraday.py` (PR 2) so the name matches what it measures |
| Q5 | ~~Paper → live exit criteria~~ | | **Resolved 2026-11-19:** all of: at least 20 paper sessions; zero positions ever left without a stop; simulated slippage within the backtester's assumption; positive expectancy after costs; B4 (SEBI) resolved |
