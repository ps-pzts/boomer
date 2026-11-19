# Boomer — Project Status

> This file is updated in the same commit as every feature or bug fix.
> Its purpose is context continuity across Claude sessions and developer handoffs.
> Format: newest entry at top. One entry per PR/commit. Never delete old entries.

---

## Status: Single-focus redesign — intraday-only, single broker, docs reset

**Current phase:** The system is being deliberately narrowed. `swing` and `long_term` tracks removed — `intraday` is now the only track. The `designs/` folder (all phase docs, open-questions.md, design-evolution.md) was retired along with the 3-track architecture it described; a fresh design pass will follow once the intraday-only system is validated in paper trading. Kite remains the sole broker (see prior Fyers-removal entry below). The human-approval feature (dashboard `/approvals`, Telegram approve/reject) was removed — it existed only to gate `long_term` recommendations, which no longer exist.

**Last updated:** 2026-11-19

---

## What's done

| Area | Item | Notes |
|------|------|-------|
| Design | `designs/state-machines.md`, `designs/observability.md` (#47) | Approved 2026-11-19. Three separate machines (Session / Trade / Order) on one shared core; every component emits to one `events` table, read only by the observability module; trading code never reads events (enforced by a test). |
| Design | `designs/` folder (Phases 1-5, open-questions.md, design-evolution.md) | **Retired 2026-09-06** — described the 3-track (long_term/swing/intraday), dual-broker system being replaced. Deleted rather than rewritten; a fresh design doc will be written once the intraday-only system is rebuilt and validated in paper trading. Historical content still recoverable via git log/PR descriptions. |
| Infra | CLAUDE.md | Project rules: git workflow, tests, PRs, file limits, context continuity |
| Infra | UTC→IST migration | All timestamps now use IST (Asia/Kolkata) throughout — DB writes, market hours checks, cron comparisons, all tests. CLAUDE.md Rule 9 updated: "All timestamps use IST, never UTC." Root cause: system is India-only with no DST; UTC was unnecessary overhead. |
| Bug | APM gate + GTT dispatch (historical) | `_morning_batch_recommendations` was routing all tracks to `AWAITING_HUMAN`, bypassing packager's APM logic. Fixed at the time to route swing/intraday through `packager.apm_decide()`. **Superseded 2026-09-06**: `AWAITING_HUMAN`/`requires_human` removed entirely (see Change log) — all recommendations now route through APM unconditionally. The GTT dispatch task (renamed `gtt_dispatch`, was `swing_gtt_dispatch`, 09:25 IST) still reads `queued_for_execution` recs and places OCO-GTT via Kite. **Superseded 2026-11-19**: `gtt_dispatch` removed (see Change log). |
| Hotfix | Intraday recs were sent as 365-day CNC GTTs | Symptom: with Kite credentials set, `gtt_dispatch` (09:25) placed every queued intraday rec as a CNC OCO-GTT valid 365 days, so an intraday signal became a year-long delivery position. Root cause: Kite GTTs only support CNC/NRML, and the task predates the intraday-only redesign. Fix: task removed; `OrderManager` pre-trade check #0 now rejects any non-MIS product. |
| Bug | Overnight news window wrong (#35) | Symptom: `overnight_news_sentiment` ignored pre-market news and counted the previous session's. Root cause: IST `observed_at` strings (misleading `Z`) compared against UTC bounds (5.5 h shift), calendar-day lower bound (lost weekend/holiday news), wall-clock upper bound (lookahead). Fix: window = [prev trading day 15:30, as_of 09:15) IST in the collector's string format. |
| Bug | Paper stops never filled (#40) | Symptom: in paper mode a stop-loss never triggered, so losing paper trades rode to the 15:14 square-off. Root cause: `MockBroker` only simulated MARKET/LIMIT fills; it also never tracked positions and sent updates without Kite's `order_id` key. Fix: SL/SL-M/SL_LIMIT trigger + fill simulation, Kite-style wrong-side stop rejection, MIS positions, `order_id` in payloads. |
| Bug | OrderManager KeyError in paper mode (#41) | Symptom: syncing or cancelling any paper order raised `KeyError: paper`. Root cause: orders go through the `KITE` registry slot (holding `PaperBroker`) but rows record the broker's own id `paper`, which was then looked up as a key. Fix: resolve by key, then by each broker's own id. |
| Bug | Live MARKET/SL-M orders would be rejected (#43) | Symptom (latent; live mode only): Kite rejects API MARKET and SL-M orders without `market_protection`, which would have blocked every stop-loss, market exit and square-off on the first live day. SL (stop-limit) orders were also sent with price 0. Fix: `KiteBroker.place_order` sends `market_protection=-1` for MARKET/SL-M and `price` for LIMIT/SL; `kiteconnect>=5.2.2`. |
| Bug | main CI red after #48 (#51) | Symptom: Lint failed on main. Root cause: ruff 0.17's `format --check` also formats Python code blocks in Markdown; the code sketch in `designs/state-machines.md` wasn't formatted, and #48 was merged without confirming every check passed. Fix: formatted the block; gate now run for docs-only PRs and merges wait for all checks. |
| Bug | Hidden ₹10 cr liquidity gate (#38) | Symptom: `risk_config`'s 5 cr turnover floor had no effect below 10 cr; names between 5 and 10 cr were silently dropped. Root cause: hardcoded `LIQUIDITY_GATE` in `signals/base.py` (also broke the no-hardcoded-rupees rule). Fix: gate removed; one floor in `risk_config`, now ₹10 cr (operator decision; migration 0011); missing volume/turnover now fails closed in `TradePlanGenerator`. |
| Bug | BE stocks in intraday universe (#49) | Symptom: trade-for-trade (BE) stocks could enter the watchlist. Root cause: universe query used `series IN ('EQ','BE')`; BE is delivery-only, MIS rejected. Fix: `series = 'EQ'`. |
| Bug | Repeating tasks ran once a day (#61) | Symptom: `minute_bar_flush` (first slot 09:00, nothing to write) never flushed again, so minute bars were never persisted; `position_review` ran once instead of hourly. Root cause: `Scheduler.should_run` skipped any task that already succeeded today. Fix: `TaskDefinition.repeats`; repeating tasks run every slot; registry test enforces the flag. |
| Bug | Weekend dispatch test | `test_orchestrator_dispatches_task_and_records_result` probe task added `run_on_holiday=True` so it dispatches on weekends/holidays (test does not depend on market being open). |
| Ops | Nightly self-healing | `nightly_health_check` task at 01:45 IST: SQLite integrity_check, WAL checkpoint + VACUUM, stuck RUNNING task detection (>1h), disk space check (<20% warns). Sends single Telegram report (✅ all clear / ⚠️ issues found) before the 03:00 restart_guard fires. |
| Ops | Full nightly service restart | `ops/restart_guard.sh` now restarts `boomer-dashboard.service` and `boomer-bot.service` in addition to the orchestrator. |
| Ops | Bot systemd unit | New `ops/systemd/boomer-bot.service` — runs `python -m src.alerts.telegram_bot`, same shape as orchestrator/dashboard units. |
| Bug | GTT last_price silent fallback | `KiteBroker.place_gtt()` used `get_ltp() or sl_trigger_price` as the Kite `last_price` field. When `get_ltp()` returned `None` (no market data subscription, empty tick cache), the fallback set `last_price = sl_trigger_price = trigger_values[0]`, causing Kite to reject with "Trigger cannot be created with one of the trigger values equal to the last price." Fix: `get_ltp()` extended with a Tier 2 fallback to `holdings()` + `positions()` (base plan); `place_gtt()` now raises `RuntimeError` explicitly when LTP is unavailable instead of silently using a trigger price. |
| Refactor | Fyers removed — Kite is the single broker | `FyersBroker`, `fyers_auto_login()`, `scripts/fyers_login.py`, the `fyers-apiv3` dependency, and every Fyers-specific branch (GTT int-status parsing in `gtt_manager.py`, dual-broker reconciliation split, CI/CD env vars) removed. `OrderManager._TRACK_BROKER`'s dangerous fallback (`.get(track, BrokerName.FYERS)` — an unknown track would have tried to route to a broker key no longer in the `brokers` dict) fixed to route everything to Kite. `reconciliation.py.reconcile_intraday()` restructured to check both `list_positions()` (MIS) and `list_holdings()` (CNC) on Kite instead of branching on broker identity, since Kite now also handles delivery. The original design rationale (Fyers ₹0 delivery brokerage vs Kite's ₹20/order) was factually wrong — Kite has always charged ₹0 on equity delivery too, same as Fyers — so there was no real cost saving being given up. Fyers auto-login was also blocked in practice (MPIN issue) and every track was already routed to Kite in `OrderManager` before this cleanup made it official. `instruments.fyers_symbol` column left in schema (unpopulated going forward) rather than dropped via migration. See `designs/design-evolution.md` for full history. |
| Bug | KiteTicker never subscribed | `_start_ticker_if_needed()` connected the WebSocket but never called `subscribe()`/`set_mode()` — zero ticks ever arrived, silently. `_on_ticks_received` also keyed on `tradingsymbol`, a field absent from real Kite tick payloads. Fix: `_on_ticker_connect` subscribes all known instrument tokens in `MODE_QUOTE` once the handshake completes; ticks are matched via `instrument_token` through a `_token_to_symbol` map. Root cause of a related race: `self._ticker` is truthy immediately after construction but `connect(threaded=True)` is async, so a second `on_tick()` call before the handshake completed would call `subscribe()` on a dead socket and raise uncaught. Fix: added `_ticker_connected` flag — new tokens are deferred to `_on_ticker_connect` until the handshake actually completes; `_on_ticker_close` resets state for future reconnects. Also fixed: `_resolve_instrument_token`'s instrument cache was keyed globally (comment claimed per-exchange busting that didn't exist) — now keyed by exchange. Known gap (not yet fixed): nothing in `orchestrator._build_brokers()` or executor wiring calls `.on_tick()` yet, so this fix has no production caller today — `get_ltp()`'s REST-tier fallback is what actually serves LTP in the running system. Also: mid-day re-authentication (Loophole 6, phase-4 design doc) refreshes `self._kite` but never reconnects `self._ticker`, so a mid-day session death would silently stop the tick feed until the next full process restart — not addressed here, flagged for a future fix. 19 tests added to `test_kite_broker.py` covering subscription timing, tick mapping, and per-exchange caching. |
| Bug | Risk config defaults had two homes | `RiskConfigStore.seed_defaults()` embedded 21 bare numbers inline in a SQL string with no names, while `capital/models.py` held related-but-separate thresholds (`MIN_RR`, `ATR_K`) as named module constants — no single place to see "what are the defaults." Fix: added `RISK_CONFIG_DEFAULTS` named dict in `risk_config.py` as the source of truth for `seed_defaults()`; cross-reference comment added in `models.py` near `MIN_RR`/`ATR_K` (kept separate — they're structural design constants, not operator-tunable, so not moved into `RiskConfig`). |

---

## What's in progress

- **B7 intraday live pipeline: design approved** (`designs/intraday-live-pipeline.md`). Approved 2026-11-19; all open questions resolved. Kite Personal gives live ticks/quotes but no historical candles, so minute bars are self-recorded. MIS-only with stop-loss (hotfix #26). Watchlist N = 50; ORB measured against ATR14; paper→live needs 20 clean paper sessions plus SEBI (B4). PR 1 (market-data feed + trading-mode switch) done 2026-11-19. PR 2 (minute bars + live features) done 2026-11-19. PR 3 (watchlist, #37) done 2026-11-19. PR 4 split into 4a/4b/4c; 4a (paper fill realism, #40/#41) done 2026-11-19. 4b (trade lifecycle supervisor) is in PR #46, unmerged. Re-sequenced 2026-11-19 (#47). SM-1 + OBS-1 (#56) and SM-2 (Trade machine, #45) done 2026-11-19. SM-3a (Session machine runs the day, #63) done 2026-11-19. SM-3b (decision cycle, #69) done 2026-11-19: **paper trading can start.** Next: OBS-2..4 (observability module). Was: SM-2 (Trade machine; reworks #46), SM-3 (Session machine = 4c), OBS-* (observability module = old PR 5). Designs: `designs/state-machines.md`, `designs/observability.md`. Open: #38 liquidity gate 5 vs 10 crore (operator decision).

---

## What's next

1. **End-to-end integration testing** — wire all phases with a shared SQLite DB, simulate a full trading day
2. **Live paper trading** — run with real market data, paper broker, monitoring dashboard
3. **SEBI registration** (B4 blocker) — required before any real-money orders

---

## Hard blockers (must resolve before real-money trading)

| ID | Blocker | Owner | Status |
|----|---------|-------|--------|
| B4 | SEBI algo trading registration | User | Not started — Zerodha (Kite) only now that Fyers is removed |
| B6 | Minute-bar historical data source | User | ~~Resolved — organic collection via parquet~~ That writer never existed (found 2026-11-19). **Actually resolved 2026-11-19** by B7 PR 2: `minute_bar_flush` writes `minute_bars/date=YYYY-MM-DD/part-HHMM.parquet` from live ticks. Kite Personal has no historical candles, so history starts the first day the feed runs. |
| B7 | Intraday live pipeline is not fully wired | User/Claude | **New, found during 2026-09-06 audit.** `IntradayPipeline` is never instantiated in production (`Orchestrator()` never passes `intraday_runner=`); `run_intraday()` has no implementation; 4 of 6 `IntradaySignalGenerator` sub-signal weights depend on live features (`premarket_gap_pct`, `orb_range_vs_20d_avg_ratio`, `nifty_intraday_direction`, `bid_ask_spread_pct`) that nothing computes; `IntradayClassifier.classify()` needs `vwap_current`/`orb_high`/`minutes_since_market_open` which are never written anywhere. What actually runs today is the 09:10 pre-market `morning_batch_signals` call, which can only use `fo_signals` (stale prior-session OI) and `news_trade_decay`. Must be built before intraday recommendations are genuinely live-informed. |

---

## Open decisions (non-blocking)

`designs/open-questions.md` was retired along with the rest of `designs/` on 2026-09-06 (see Change log). Its Fyers-related and dual-broker questions were moot by the time it was deleted; the SEBI registration question (Q0-1) still applies, scoped to Zerodha only. Anything not yet answered will be re-opened in the fresh design pass once one is written.

---

## Change log

### 2026-11-19 — Bug: Kite order handling would have broken live trading (#73)

Live-only bugs that paper could never show (MockBroker uses our own statuses):
- **Status map:** Kite statuses missing from it (`MODIFY PENDING`, `VALIDATION PENDING`, ...) became a terminal ERROR. The Trade machine read that as a lost stop and exited at market; every trailing-stop modify risked this. A triggered stop-limit entry (`TRIGGER PENDING` → `OPEN`) hit an illegal TRIGGERED → PENDING transition, and partial fills were missed while the status stayed `OPEN`.
  - Fix: `map_kite_status()` handles the final statuses. `TRIGGER PENDING` → PENDING; `OPEN` → PENDING, TRIGGERED (SL/SL-M) or PARTIAL. In-flight and unknown statuses mean "no change". Fills are recorded whenever the filled quantity grows.
- **`place_order` indexed the SDK's return value** as a dict, but kiteconnect returns the id string. Every live order would have raised *after* Kite accepted it.
- **Order tags** are now alphanumeric only.
- **`cancel()`** waits up to ~1 s for Kite's final answer, so a fill racing a `CANCEL PENDING` is recorded.

### 2026-11-19 — Feat: paper/live toggle (#76)

- **Operator request:** a toggle to switch between paper and live.
- **Controls:**
  - Telegram: `/mode` shows today's and the next session's mode plus the paper record; `/mode paper`; `/mode live confirm`.
  - Dashboard: a panel on Today with a reason field and a real-money checkbox for live.
- **Rules:**
  - The env var `BOOMER_TRADING_MODE=live` stays the server-level arm; without it, live is refused. The toggle writes the audited `live_trading_authorisation` row (actor + reason).
  - Switching to live shows the paper record against the §9 Q5 criteria and needs confirmation.
  - The mode is **locked into the day's session row** at session start: a toggle or restart mid-day applies from the next session only, so paper positions never meet the live broker.
- **Execution:** rebuilt per session day in that day's mode, never while a trade is active.
- The dashboard's emergency-stop confirmation now says what it does (flattens everything).
- 970 tests pass.

### 2026-11-19 — Feat: orders at the broker that no trade tracks (#65)

- **Before:** the reconcile-before-resume check couldn't see an order working at the broker that nothing tracks (an order placed by hand, or one left by a crash), so it could fire later and open a position nobody manages. An emergency stop couldn't cancel such orders either.
- **Now:**
  - `Broker.list_open_orders()` (Kite: anything not COMPLETE, CANCELLED or REJECTED; paper: resting orders).
  - `OrderManager.live_broker_order_ids()`.
  - A `no_untracked_resting_order` reconcile check, which fails closed if the broker can't be asked.
  - The emergency stop now also cancels every untracked working order.
- 960 tests pass.

### 2026-11-19 — Bug: order prices on a fixed ₹0.05 grid (#75)

- **Symptom (audit):** every price was rounded to ₹0.05. Kite rejects a price or trigger that isn't a multiple of the instrument's own `tick_size`, so entries, targets and stop moves could fail live for some stocks. Paper didn't check, so it could never show this.
- **Fix:**
  - `Broker.tick_size()`: Kite reads it from its instrument dump (`InstrumentCache`); paper uses the same source when Kite is configured.
  - `OrderManager` rounds every order at the one choke point (`executor/ticks.py`): BUY limit down, SELL limit up, triggers up, so a protective stop only tightens and is never widened. `modify_trigger` does the same.
  - The trailing stop records the trigger actually sent.
  - The paper broker now refuses off-tick prices like Kite.
- 953 tests pass.

### 2026-11-19 — Bug: paper broker lost its book on restart (#64)

- **Symptom:** after a restart during market hours, paper trades still existed in our tables but the new paper broker was empty. The session halted with `reconcile_mismatch` for the rest of the day, and the trades could no longer be managed.
- **Root cause:** `MockBroker` keeps its orders, positions and cash in memory only.
- **Fix:**
  - `executor/brokers/paper_book.py` writes the whole book as one JSON row per session day (migration 0016) after every place, modify, cancel and fill, and `PaperBroker` restores it when built.
  - One lock now guards the simulator: fills arrive on the feed thread while orders come from task threads, a race that predates this change.
- 937 tests pass.

### 2026-11-19 — Bug: a fresh DB never traded (#74)

- **Symptom (audit):** a fresh database never trades. Nothing seeded `risk_config`, so the watchlist task failed and the session reported `breakers_unavailable`. The capital ledger first appeared at the 16:00 EOD, so day one had no execution.
- **Deeper issue:** execution was built once at the 03:00 boot, when the Kite token has expired and the ledger may be missing. So it stayed unavailable all day even after the ledger appeared.
- **Fix:**
  - `orchestrator/bootstrap.py` seeds the default `risk_config` at boot if the table is empty. It creates the first capital ledger from Kite available cash at boot and again after the 08:45 token refresh, only if none exists, with an alert.
  - `SessionController` builds execution lazily through a factory, retried every tick until it succeeds, and never replaced mid-session.
  - The decision cycle uses the session's current supervisor.
- 933 tests pass.

### 2026-11-19 — Feat: trailing stop (#71)

- **Operator decision (option 1):** once a trade is up 1R, the stop moves to breakeven. After that it trails 1×ATR below the highest LTP since entry. The fixed 1.5:1 target stays.
- **Rules:** the stop only moves up. It stays below the LTP (Kite rejects anything else). After breakeven it moves only in steps of at least 0.25×ATR, and at most 20 times (Kite caps modifications at about 25).
- **How:** each move is a broker modify (`OrderManager.modify_trigger`) followed by an `open → open` `stop_raised` transition that records the rule and inputs (migration 0015: `trail_stop`, `high_water`, `stop_moves`). The paper broker now refuses a wrong-side modify, like Kite.
- **Worked example** (fill 100, stop 98, ATR 2): high 102 → stop 100; high 103.5 → stop 101.5. A pullback to 101.4 now exits **+₹14** instead of −₹20. 928 tests pass.

### 2026-11-19 — Feat: SM-3b — decision cycle opens paper trades (#69)

- **What:** `executor/decision_cycle.py` runs every 30 min. It does nothing unless the session is TRADING (SM7) and fewer than 3 trades are open. For each watchlist symbol, best first:
  1. Skip it if a trade is active or one was opened under 60 min ago. Both are derived from `intraday_brackets`, so they survive a restart (L2).
  2. Features = batch (point-in-time) + live (ticks, minute bars, news recency).
  3. `brain/intraday_decision.decide()` runs signal → plan gates → entry strategy → plan **at the entry price** → size.
  4. Signal and plan are saved to `signals` / `trade_plans` (`brain/signal_store.py`), then `TradeSupervisor.open_trade` runs under the session lock after re-checking TRADING.
- **Attribution:** every accept and every reject is an event with its rule and inputs. Three failed cycles in a row → HALTED(`cycle_failures`).
- **Operator decisions:**
  - Size = min(0.5%-risk shares × regime scale, 5% of capital ÷ price) (`capital/sizing.py`). Before this, the concentration check rejected nearly every trade.
  - ORB entries are a stop-limit BUY (trigger ORB high +0.1%, limit +0.2%; never chase above the limit; `TradeRequest.entry_trigger` → Kite "SL"). VWAP entries stay LIMIT.
  - Max 3 open trades.
- **Bug fixed by design:** the stop and target were computed from the LTP, not from the classifier's entry price.
- **Retired:** `executor/intraday.py` (`IntradayPipeline`), the `intraday_runner` wiring, and the `run_cycle(run_date=)` arg bug.
- 911 tests pass.

### 2026-11-19 — Enhancement: emergency stop flattens everything immediately (#67)

- **Operator decision (on review of #66):** an emergency stop means stop everything and flatten everything at once. SM-3a had treated it as a halt that kept managing trades until 15:14.
- **Now:** EMERGENCY_STOP sends the session straight to `square_off` (rule `emergency_stop`, CRITICAL alert). The supervisor cancels every leg and exits every trade at market. `executor/emergency.py` exits broker MIS positions the bot doesn't own (at most one exit in flight per symbol; skipped when a trade also holds the symbol). The session then reaches CLOSED with no resume that day. A never-traded, flat session is ABANDONED.
- **Latency:** an orchestrator watcher thread polls the bot mode every 5 s on trading days, 09:00–16:00, and runs the session step at once. The minute tick is the backstop.

### 2026-11-19 — Feat: SM-3a — Session machine runs the trading day (#63)

- **What:** `trading_sessions` (migration 0014) + `SESSION_SPEC` on the core (`executor/session_machine.py`, pure guards). `SessionController` (`executor/session_controller.py`) is driven by a new every-minute `session_tick` task (`* 9-15`). Each tick runs `TradeSupervisor.reconcile()` (stops, targets, OCO), builds the guard context once (feed tick age, bot mode, live breakers, watchlist size, clock), then fires what the state and time call for: setup → TRADING; halt / RECONCILING / resume; 14:30 `entry_cutoff`; 15:14 `square_off` through the supervisor; `all_flat` once the broker is flat.
- **Live breakers** (`executor/session_inputs.py`): today's realised intraday P&L, losing streak and week P&L from `intraday_brackets`; drawdown vs the ledger high-water mark; NIFTY 50 move from the feed (`prev_close`). Unreadable breakers or a missing capital ledger fail closed.
- **Reconcile before resume** (#58): the broker's MIS net vs our trades' net from fills, untracked broker positions, tracked legs resting, every OPEN stop resting, feed fresh, watchlist present. Any failure → `HALTED(reconcile_mismatch)` + CRITICAL. Resume needs an operator `acknowledge_mismatch` **and** a clean re-check. A `reconcile_report` decision event is emitted on every run.
- **Execution wiring** (`orchestrator/execution.py`): the broker the trading mode picks (paper by default) → OrderManager + PositionManager + TradeSupervisor on one cross-thread connection under the controller's lock. Boot fires `restarted` → RECONCILING for a session found mid-day.
- **Scheduler:** `TaskDefinition.safety_task` runs in PAUSED **and** EMERGENCY_STOP. Before this, EMERGENCY_STOP blocked every task, including square-off. `intraday_squareoff` removed. It was a no-op anyway: its runner was never wired.
- **Design deviation** (recorded in §4.2): `abandon` at 15:14, also from READY.
- **Follow-ups:**
  - #64: the paper broker loses its book on a mid-session restart (fails safe as `reconcile_mismatch`).
  - #65: orphan orders at the broker; needs `Broker.list_orders()`.
- 867 tests pass. The session reaches TRADING, but nothing enters trades until SM-3b.

### 2026-11-19 — Bug: repeating tasks ran only once a day (#61)

- **Symptom:** `minute_bar_flush` (`*/15 9-15`, from #30) first fires at 09:00 before any bar exists, succeeds with nothing to write, and every later slot was skipped, so **minute bars were never written to parquet** (and Kite Personal has no historical candles to recover them). `position_review` ran once a day instead of hourly; `intraday_cycle` likewise.
- **Root cause:** `Scheduler.should_run` returns `already_succeeded` for any task with a successful run for today's `run_date`, which is right only for once-a-day tasks.
- **Fix:** `TaskDefinition.repeats`; repeating tasks skip that check (the orchestrator's 60 s dispatch guard still prevents double fires). Set on `minute_bar_flush`, `position_review`, `intraday_cycle`. A registry test fails if any task firing more than once on a weekday isn't marked `repeats` (and vice versa).
- Found while planning SM-3, whose every-minute session task depends on this. 787 tests pass.

### 2026-11-19 — Feat: SM-2 — Trade machine on the core (#45; supersedes #46)

- **Why:** the trade lifecycle from #46 (never merged) rebuilt as an explicit state machine (`designs/state-machines.md` §5), so every step is legal by construction and recorded.
- `executor/trade_machine.py`: `TRADE_SPEC` — ENTRY_PENDING → **PROTECTING** → OPEN → EXITING → CLOSED, or ENTRY_PENDING → CANCELLED; triggers entry_filled / entry_expired / entry_rejected / square_off / stop_confirmed / stop_failed / stop_lost / leg_filled / corrective_order / exit_failed / flat. Every exit passes through EXITING.
- `executor/trade_lifecycle.py` (`TradeSupervisor`, same behaviour and guards as #46: plan validation, emulated OCO settled by net filled quantity, oversold buy-back, L9 unprotected exit, partial entry, square-off) + new `executor/trade_store.py` (`BracketStore`; `advance()` = bracket fields + transition + event in one transaction). Split to stay under the file-size limit.
- Restart safety: a trade found in PROTECTING reuses a stop it already submitted (found by idempotency key) instead of placing a second resting stop that could later fire into a short; a trade in EXITING with no exit order places one.
- `OrderManager` fixes carried over from #46: `cancel()` records a fill that raced it; funds check BUY only; injectable clock.
- Migration `0013_intraday_brackets.sql` (was `0010` in #46).
- Tests: #46's 18 lifecycle tests ported unchanged and passing; new `tests/executor/test_trade_machine.py` (spec, recorded history with correlation, PROTECTING path, restart safety). 784 tests pass.

### 2026-11-19 — Docs: reconcile before resume after any outage (#58)

- **Why:** operator rule: whenever something recovers (Kite feed, broker session, NSE data, orchestrator crash), reconcile with the source of truth before trading again.
- `designs/state-machines.md` amended: Session gains `RECONCILING`; `HALTED --recover--> RECONCILING --reconciled--> TRADING` (no direct `HALTED → TRADING`); a mid-session boot takes `restarted → RECONCILING`; mismatch → `HALTED(reconcile_mismatch)` + CRITICAL, operator acknowledges. §4.5 defines the checks (broker positions vs open trades, orphan/missing orders, stops resting, feed fresh, watchlist present); the trade supervisor's own reconcile runs first; one `reconcile_report` decision event per run. Data sources recovering (e.g. late NSE surveillance list) trigger a data reconcile without halting: excluded from new entries, held positions managed normally (#50 decision).
### 2026-11-19 — Feat: SM-1 + OBS-1 — state-machine core, events table + emitter, Order machine (#56)

- **Why:** foundation for `designs/state-machines.md` and `designs/observability.md`: one place to watch the system, and order history that is recorded instead of overwritten.
- New `src/statemachine/` (`MachineSpec`, `Transition`, `transition()`, `record_created()`): spec validated at import (known states, reachable, no dead ends, no ambiguous triggers, safe SQL identifiers); `transition()` reads state, checks table + pure guard, updates state + touch column and inserts a `state_transition` (or `transition_blocked`) event in **one** transaction; joins the caller's open transaction instead of committing.
- New migration `0012_events.sql` + `src/observability/emit.py`: envelope (IST ms timestamp, session_date, mode, source, boot_id, per-source seq, correlation ids, JSON payload, schema_version); async `emit()` never raises/blocks (queue + writer thread with own connection, drop counting); sync `envelope()`/`insert()` for the core; secret-key redaction; `lifecycle` + 30 s `heartbeat`. Registers under both `observability.emit` and `src.observability.emit` so one process has one emitter (O10).
- Order machine: `executor/order_machine.py` builds `ORDER_SPEC` from `ALLOWED_TRANSITIONS`; `OrderManager._transition` goes through the core (rule per call site) and order creation is recorded; illegal moves still raise `StateMachineError`.
- Orchestrator boot starts the stream (source `orchestrator`, resolved trading mode; heartbeat carries feed health) and stops it on shutdown.
- `tests/test_events_read_only.py` fails the build if code outside `src/observability/` reads `events` (operator rule: trading never decides from events).
- Tests: `tests/statemachine/`, `tests/observability/`, `tests/executor/test_order_machine.py`, boot test; executor test helpers apply migration 0012. 754 tests pass.

### 2026-11-19 — Feat: F&O-only intraday universe (#50 part 1)

- **Why:** operator decision. A turnover floor can't stop pump-and-dump (manipulation creates volume); F&O stocks meet exchange eligibility criteria, have no price bands and deep books, so they're the hardest to manipulate. The F&O open-interest sub-signal only works for them anyway.
- New `src/brain/universe.py`: `intraday_universe()` = EQ-series instruments ∩ stock-futures underlyings on the latest `fo_oi_daily` trade date **strictly before** the session (point in time); index futures drop out naturally. Fails closed when F&O data is missing or older than 5 trading days.
- `morning_batch_features` now computes only for the universe (~200 instead of ~2,400 symbols). `intraday_watchlist` uses it; if unavailable it saves an empty watchlist, subscribes nothing and sends a CRITICAL "not trading today" alert.
- **Part 2 (ASM/GSM exclusion) blocked:** probing NSE's surveillance API endpoints wasn't permitted from the dev environment; source to be verified by the operator. Tracked on #50.
- Found in passing: `_parse_date` in 4 collector fetchers silently returns today's date on unparseable input (#54, deferred).
- Tests: `tests/brain/test_universe.py` (intersection, lookahead exclusion, options-only rows, stale/missing fail-closed, trading-day age), watchlist task tests for non-F&O and no-F&O-data. 714 tests pass.

### 2026-11-19 — Bug: single ₹10 cr liquidity floor (#38) + EQ-only universe (#49)

- **#38:** removed the hardcoded ₹10 cr `LIQUIDITY_GATE` from `brain/signals/base.py`; the generator now only scores. Liquidity is eligibility, enforced from `risk_config` by the watchlist and `TradePlanGenerator`. Floor set to ₹10 cr (operator decision 2026-11-19): `RISK_CONFIG_DEFAULTS` for fresh DBs, migration `0011` adds a new `risk_config` version for existing DBs with a lower floor (explicit column list; history kept). `TradePlanGenerator` now rejects missing volume/turnover (was `> 0 and <`, so missing passed).
- **#49:** `morning_batch_features` and `intraday_watchlist` select `series = 'EQ'` (BE is trade-for-trade; MIS not allowed).
- Next for the universe: #50 (F&O-only + ASM/GSM surveillance exclusion), operator-approved.
- Tests: missing-data fail-closed, ₹7 cr rejected / ₹10 cr boundary, generator no longer gates, migration 0011 (existing 5 cr → new version, every other column copied; already 10 cr → no-op; fresh DB untouched), BE never in watchlist. 704 tests pass.

### 2026-11-19 — Docs: state machines + one-way event observability (#47)

- **Why:** operator wants one accurate place to watch state and positions instead of querying each subsystem, and asked whether the supervisor should be a state machine. Session logic is currently scattered across tasks, pipeline flags, breakers and clock checks.
- `designs/state-machines.md`: Session (PRE_OPEN → CONNECTED → READY → TRADING ⇄ HALTED → NO_NEW_ENTRIES → SQUARING_OFF → CLOSED; square-off never blocked), Trade (ENTRY_PENDING → **PROTECTING** → OPEN → EXITING → CLOSED / CANCELLED), Order (existing table, now with history). Kept separate (each has its own goal; merging would multiply the states), sharing one in-house core whose `transition()` commits state + event atomically. Machines call each other directly, never through events.
- `designs/observability.md`: every component emits to one `events` table (envelope with source, boot_id, per-source seq, mode, correlation ids); only `src/observability/` reads it, enforced by a test. Completeness via atomic transition events and seq gaps; liveness via 30 s heartbeats; truth via 60 s broker snapshots and DRIFT detection; "why not" via rejection events; no per-tick events; 30-day retention then parquet. Surfaces: `/live`, timelines, Telegram `/status`.
- B7 delivery re-sequenced (pipeline doc §8 note). #46 (4b) to be reworked as SM-2 rather than merged as-is.

### 2026-11-19 — Bug: send market_protection on live Kite MARKET/SL-M orders (#43)

- **Why:** Kite rejects API MARKET and SL-M orders without `market_protection` (in force for algo orders from April 1 per the Kite Connect forum). In live mode every stop-loss leg, market exit and 15:14 square-off would have been rejected. Paper mode never calls Kite for orders, so this would only have surfaced on the first live day.
- `KiteBroker.place_order`: `market_protection=-1` (automatic) for MARKET and SL-M; `price` now sent for SL (stop-limit) orders too, not only LIMIT. `pyproject.toml`: `kiteconnect>=5.2.2` (verified the installed 5.2.2 `place_order` accepts the parameter).
- Tests: `TestPlaceOrderParams` in `tests/executor/brokers/test_kite_broker.py`. 694 tests pass.

### 2026-11-19 — Bug: B7 PR 4a — paper stop-loss fills + OrderManager paper broker lookup (#40, #41)

- **Why:** paper trading is the go-live rehearsal (design Q5), but a paper stop could never trigger, so paper P&L would overstate results; and no paper order could be synced at all.
- `MockBroker` (#40): SL (SL-M) triggers when price trades through the trigger and fills at min(trigger, bar open) for sells (a gap fills at the worse price), mirrored for buys; SL_LIMIT becomes a resting LIMIT once triggered; wrong-side stops are REJECTED at placement like Kite (exercises design L9); MIS fills maintain a net position per symbol (`list_positions()` was always empty); update payloads include Kite's `order_id` key and are copies; `cancel_order` leaves REJECTED orders rejected. The backtester only uses MARKET/LIMIT + GTTs, so its results are unchanged.
- `OrderManager` (#41): `sync_status`/`cancel` resolve the broker by registry key, then by each broker's own `broker_id`; previously `KeyError: paper` for every paper order.
- PR 4 split into 4a/4b/4c (design §8, #33).
- Tests: new `tests/executor/brokers/test_mock_broker_stops.py` (incl. paper end-to-end: feed tick → PaperBroker stop fill → OrderManager row FILLED via push update); `TestBrokerResolution` in `test_order_manager.py`. 690 tests pass.

### 2026-11-19 — Feat: B7 PR 3 — daily intraday watchlist, retire morning recommendation path (#37)

- **Why:** design §3.6/Q3. The feed only streamed NIFTY 50, so PR 2's bars and live features existed for no stock; and the 09:10/09:15 tasks kept writing recommendations nothing has executed since #26.
- New `src/brain/watchlist.py`: `select_watchlist` (eligibility from `risk_config` floors + `atr_14d` + the generator's own gate; LONG candidates ranked by batch-only score, remaining of 50 slots filled by liquidity; rejection counts logged by reason) and `WatchlistStore`. New migration `0009_intraday_watchlist.sql`.
- New 09:10 task `intraday_watchlist` (after `morning_batch_features`): selects, persists, subscribes the feed before the 09:15 open; CRITICAL alert if empty.
- **Removed** tasks `morning_batch_signals` and `morning_batch_recommendations` (and `_save_signal`), which also removes the L14 `_apm_circuit_check` bug. PR 4 replaces them with the live cycle.
- **Mid-session restart recovery** (gap left by PR 1): tasks only fire on their cron minute, so a restart after 08:45 never re-ran the setup or watchlist tasks and the feed stayed disconnected. `resume_market_session` at boot (08:45-15:30 on trading days) starts the bar session, subscribes NIFTY 50 + the persisted watchlist, and connects the feed. Opening range stays unavailable that day for bars missed before the restart (L7).
- Found in passing, not fixed: hardcoded ₹10 cr liquidity gate in `signals/base.py` conflicts with `risk_config`'s 5 cr (#38, needs operator decision). The watchlist applies both gates meanwhile.
- Tests: `tests/brain/test_watchlist.py`, `tests/orchestrator/test_watchlist_session.py`. 669 tests pass, lint and format clean.

### 2026-11-19 — Bug: overnight news sentiment window (#35)

- **Symptom:** `overnight_news_sentiment` (feeds 30% of the gap sub-signal, which is 25% of the intraday score) excluded pre-market announcements and included the previous session's mid-day news.
- **Root cause:** three faults in `compute_overnight_news_features`' window. (1) `filings.observed_at` is IST wall-clock with a misleading `Z` (design L13), but the bounds were converted to UTC strings, shifting the window by 5.5 h: at 09:05 IST the upper bound was "03:35". (2) Lower bound was `as_of_date − 1` calendar day, so Mondays/post-holiday days lost weekend and holiday filings. (3) Upper bound was wall-clock now, a lookahead for any backfill or replay.
- **Fix:** window is [previous trading day 15:30 IST, `as_of_date` 09:15 IST), formatted like the collector writes `observed_at`. Lower bound moved 16:00 → 15:30 (market close) so 15:30–16:00 filings aren't lost between days.
- Regression tests in `tests/brain/features/test_computers_market.py` with a worked example (1.0 / 1.8). 4 of the 7 fail on the old code; the other 3 pass on it only because their dates are after the day the tests were written (the old wall-clock bound happens to cut them off). 639 tests pass.

### 2026-11-19 — Docs: issue-per-PR traceability rule (#31)

- **Why:** operator request: every PR and bug must be traceable via a GitHub issue stating aim/why, consequences if not done, how it advances the end goal, and architecture changes.
- `CLAUDE.md` §11 added (issue lifecycle + body structure + end-goal statement); §6 PR format now starts with `Closes #<issue>` and its checklist gains an issue-update item; §10 gains "never open a PR or fix a bug without a linked issue". Appended as §11 rather than renumbered because code comments cite §9/§10.
- New `.github/ISSUE_TEMPLATE/work-item.md` and `.github/pull_request_template.md`.
### 2026-11-19 — Feat: B7 PR 2 — minute bars to parquet + live feature computer

- **Why:** design `designs/intraday-live-pipeline.md` §3.2-3.4. Kite Personal has no historical candles, so self-recorded minute bars are the only intraday history; and 4 of 6 intraday sub-signals read live features nothing computed (B7).
- New `src/collector/live_bars.py`: `LiveBarStore` builds 1-minute OHLCV bars from ticks (09:15-15:29 IST only; volume = delta of the exchange's cumulative volume; latest exchange VWAP kept per bar); `flush()` writes only completed, not-yet-written bars to `date=YYYY-MM-DD/part-HHMM.parquet` (loophole L7). `SessionBarRecorder` rolls the store each morning so a missed nightly restart can't drop a day's ticks on a date mismatch.
- New `src/brain/features/live.py`: pure functions for `premarket_gap_pct`, opening range (`orb_high`/`orb_low`, `None` until 09:30 or if recording started after 09:15:59), `orb_range_vs_atr` (design Q4), `vwap_current`, `price_vs_open_pct`, `nifty_intraday_direction` (0.10% deadband), `bid_ask_spread_pct`, `minutes_since_market_open`, `minutes_since_latest_news`. `compute_live_features()` omits unknown keys rather than setting `None` (the classifier's `float(features.get(k, 0.0))` would raise). Live features are never written to the date-granular `features` table (L10).
- Feature renamed `orb_range_vs_20d_avg_ratio` → `orb_range_vs_atr` in `IntradaySignalGenerator` and `track_intraday.py` (Q4); registry docs list every live key.
- **L13 verified:** `filings.observed_at` is IST wall-clock with a misleading `Z` suffix. `latest_news_at` reads it as IST. Found while verifying: `compute_overnight_news_features` compares those IST strings against UTC-converted bounds, shifting its window by 5.5 h. Not fixed here; separate `bug/` PR.
- Orchestrator: feed ticks are recorded from boot (`_build_bar_recorder`); the 08:45 setup task starts the day's session; new `minute_bar_flush` task every 15 min 09:00-15:45 IST. New env `BOOMER_MINUTE_BARS_DIR` (runbook, `dev.sh`). New dependency `pyarrow>=15`.
- Tests: `tests/collector/test_live_bars.py`, `tests/brain/features/test_live.py` (incl. the design's worked example: gap +1.20%, ORB 5.20 / ATR 10.40 = 0.50 → sub-score 0.50), orchestrator flush/session/boot tests. 632 tests pass, lint and format clean.

### 2026-11-19 — Feat: B7 PR 1 — market-data feed, paper/live switch, pre-open feed connect

- **Why:** design `designs/intraday-live-pipeline.md` §3.1, §4, §5. The live intraday pipeline needs a tick feed that paper mode and live mode share, and an explicit, fail-closed paper/live switch.
- New `src/executor/brokers/feeds/`: `MarketDataFeed` ABC, provider-neutral `Tick`, `FeedMode`, `FeedHealth`/`FeedCapabilities`; `KiteFeed` (the single owner of the Kite WebSocket, FULL mode for depth); `ManualFeed` (push-driven, for tests and replay). `parse_kite_tick` treats Kite's 0-for-missing prices as `None`.
- **Ticker code moved, not copied**, out of `KiteBroker` into `KiteFeed`, keeping the #22/#23 fixes (deferred subscribe until handshake, re-subscribe on every reconnect). `KiteBroker` now takes an optional feed: `get_ltp` tier 0 is a fresh feed tick, `on_tick`/`on_order_update` delegate to the feed. Per-exchange instrument cache extracted to `executor/brokers/kite_instruments.py`, shared by both.
- `KiteFeed.reauthenticate()` closes and reconnects with the current env token, keeping subscriptions (loophole L6). Verified against the installed kiteconnect SDK: `close()` leaves the Twisted reactor running and `connect()` only starts it if it is not running, so reconnecting is safe.
- `PaperBroker` now takes a `MarketDataFeed`; every feed tick drives simulated fills, even without an `on_tick()` call. `authenticate()` is a no-op.
- New `src/executor/trading_mode.py` + migration `0008_live_trading_authorisation.sql`: live needs `BOOMER_TRADING_MODE=live` **and** an enabled, attributed row in the append-only table. Anything else, including a DB error, resolves to paper. Mode is logged at boot. Nothing places orders yet in either mode (execution wiring is PR 4). Dedicated table instead of a `risk_config` column: documented deviation in design §5.
- Orchestrator: `pre_market_executor_setup` moved 09:20 → **08:45**, no dependencies (feed must stream before the 09:15 open); after the token refresh it subscribes NIFTY 50 and (re)connects the feed. New **09:17 `feed_health_check`** task: CRITICAL alert unless connected with a tick under 60 s old. Feed is built at boot but never connected then (03:00 restart holds an expired token).
- Tests: `tests/executor/brokers/feeds/` (KiteFeed incl. ported #23 regressions, FeedHealth boundaries, ManualFeed), `test_kite_instruments.py`, `test_trading_mode.py`, `tests/orchestrator/test_tasks_executor.py`, `test_orchestrator_boot.py`; KiteBroker and PaperBroker tests updated to the feed. 583 tests pass, lint and format clean.

### 2026-11-19 — Chore: `ruff format` the codebase so CI's format gate passes

- **Why:** CI runs `ruff format --check`, which had failed on every merge to `main` since at least #24 (32 unformatted files). The CLAUDE.md pre-push gate only runs `ruff check`, so this was never caught locally.
- Formatting pushed `src/backtester/simulation.py` (590 → 613 lines) and `tests/backtester/test_simulation.py` past the 600-line limit, so they were split by responsibility first: SQL persistence moved to the new `src/backtester/run_store.py` (`BacktestSimulation` keeps thin delegating methods; `run_store.py` added to the code-hash file list), and acceptance-gate tests moved to `tests/backtester/test_simulation_acceptance.py`, with shared fixtures in `tests/backtester/sim_helpers.py`. New `tests/backtester/test_run_store.py`. No behaviour change. The backtest code hash changes, which resets holdout-burn tracking.
- Follow-up, not done here: `tests/test_integration_full_pipeline.py` is 652 lines (over the limit before this change).
- 519 tests pass, `ruff check` and `ruff format --check` clean.

### 2026-11-19 — Hotfix: MIS-only, remove 365-day CNC GTT dispatch

- **Why:** operator rule: Boomer places only MIS orders with a stop-loss, never long-validity delivery orders. `gtt_dispatch` violated this whenever Kite credentials were present (no paper-mode gate), and CLAUDE.md §10 forbids intraday becoming a held position.
- Removed `_gtt_dispatch` from `orchestrator/tasks_executor.py` and its `gtt_dispatch` entry from the task registry. No other task depended on it. `morning_batch_recommendations` still writes `queued_for_execution` recs, but nothing executes them now; the live MIS path replaces this in B7 (`designs/intraday-live-pipeline.md`, on its own branch).
- `OrderManager._pre_trade_checks` gains check #0: reject any product other than `ProductType.MIS`, before the order is persisted or sent to a broker.
- GTT code (`GttManager`, `KiteBroker.place_gtt`) is left in place with no production caller; deletion is part of B7's retirement PR.
- Tests: new `tests/orchestrator/test_tasks.py` (no GTT task registered, no dangling task dependencies); 3 new `OrderManager` tests (CNC rejected, CNC never reaches the broker or the `orders` table, MIS passes). 515 tests pass, lint clean.

### 2026-11-19 — Bug: KiteTicker subscription race + per-exchange instrument cache

- Originally raised 2026-09-05 as PR #23; rebased onto the single-focus redesign (#24/#25) before merge. Follow-up to the merged KiteTicker subscription fix (PR #22, which added `subscribe()`/`set_mode()` wiring but shipped with no regression tests and had not yet updated this file — both addressed here).
- Root cause of a latent race: `self._ticker` is truthy immediately after `KiteTicker(...)` construction, but `connect(threaded=True)` completes the WebSocket handshake asynchronously. A second `on_tick()` call in that window hit the "ticker already running" branch and called `subscribe()` on a socket that doesn't exist yet inside the SDK, raising uncaught out of `on_tick()`.
- Fix: added `_ticker_connected` flag, set `True` only in `_on_ticker_connect` (fires on initial connect and every auto-reconnect). New tokens found while not yet connected are left in `_token_to_symbol` and picked up automatically once the handshake completes. Added `_on_ticker_close` to reset state on disconnect.
- Fix: `_resolve_instrument_token`'s instrument cache was a single global dict despite a comment claiming per-exchange busting — now keyed by exchange (`dict[str, dict[str, int]]`), closing a latent bug that would have silently mis-resolved or failed lookups for a second exchange (only NSE is used today, so dormant in practice).
- Added 19 tests to `tests/executor/brokers/test_kite_broker.py`: ticker subscription timing (deferred-until-connected, immediate-once-connected, close resets state), tick→symbol mapping via `instrument_token`, zero-price tick filtering, and per-exchange cache isolation.
- Flagged, not fixed here (documented in `project-status.md` "What's done" bug row and left for a follow-up): (1) nothing in `orchestrator._build_brokers()` or executor task wiring calls `.on_tick()` yet, so the tick feed has no production caller — `get_ltp()`'s REST-tier fallback serves all LTP in the running system today; (2) mid-day re-authentication (Loophole 6, phase-4 design doc) refreshes the REST client but never reconnects the WebSocket ticker, so a mid-day session death would silently stop ticks until the next full process restart.

### 2026-09-06 — Refactor: single-focus redesign — remove swing/long_term, remove designs/, remove human-approval

- **Why:** Operator assessment: `swing` and `long_term` had become dead weight — complexity spread across too many verticals without producing validated ROI. A fresh-analysis audit of the surviving `intraday` track (prompted by the operator doubting whether the system was "genuine enough") found the live 30-min intraday pipeline itself was never fully wired (see hard blocker B7, added below) — so the redesign's real value is removing everything not needed for the one track that has to be made to actually work.
- A multi-repo split (separate repos for broker core / intraday / F&O) was proposed mid-redesign and rejected — see the reasoning captured in this commit's PR description. Decision: stay in one repo; F&O was also dropped from scope entirely (kept away, not built).
- **`designs/` folder deleted whole** (`README.md`, all 5 phase docs, `open-questions.md`, `design-evolution.md`, ~230KB) — it described the 3-track, and briefly dual-broker, system being retired. Not rewritten in place; a fresh design doc will be written once the intraday-only system is rebuilt and validated in paper trading. Full historical content remains recoverable via `git log`/PR descriptions.
- **`capital/models.py`**: `Track` enum reduced to `INTRADAY` only. `INITIAL_ALLOCATION`/`STEADY_ALLOCATION`/`CAPITAL_MILESTONE`/`allocation_for_capital()`'s two-tier milestone mechanism collapsed to a single `ALLOCATION = {INTRADAY: 1.00}` constant — with one track there was nothing left to switch between. `RiskConfig` and `CapitalLedgerRow` dropped their swing/long_term fields.
- **New migration `0007_remove_swing_long_term_columns.sql`**: drops `capital_ledger.{long_term,swing}_allocated_pct`/`{long_term,swing}_deployed`, `risk_config`'s swing/long_term fields, and `recommendations.requires_human`. No CHECK constraint depended on any of these — dropped for cleanliness, not correctness. Historical rows with `track='swing'`/`'long_term'` in `signals`/`recommendations`/`positions` are left as audit trail.
- **Bug caught while rewriting `RiskConfigStore.update_live_backtest_ratio()`**: its `INSERT INTO risk_config SELECT ...` had no explicit column list, relying on `SELECT` output order matching the table's physical column order positionally. Dropping columns shifted that order (`created_at` ended up before `min_stock_price` et al., not after) — a plain rename would have silently written `now`'s timestamp into `min_avg_daily_turnover_cr`'s slot. Fixed by using an explicit `INSERT (...) SELECT` column list; verified end-to-end against a real migrated DB, not just unit-tested.
- **Bug caught in `dashboard/queries.py`**: `get_capital_view()` was dividing `allocated_pct` by 100, but `capital/state.py` writes it as a fraction (0.0-1.0, e.g. `Decimal("0.80")`), not a percentage (confirmed against `tests/capital/test_state.py`'s existing assertions) — the dashboard was displaying allocated capital ~100x too small. Fixed alongside the required column removal; `tests/dashboard/test_queries.py`'s fixture data was itself written to the old (wrong) convention and updated too.
- **Human-approval feature removed entirely** (not left dormant): `RecommendationStatus.AWAITING_HUMAN`, `Recommendation.requires_human`, `packager.package()`'s `requires_human` branch, `RecommendationPackager.apm_decide()`'s human-routed guard, dashboard's whole `/approvals` view (routes + `approvals.html` + nav link), Telegram bot's `/approve`+`/reject` commands and inline-keyboard callback handling. It existed only to gate `long_term` recommendations; with `long_term` gone it would have been permanently dead code.
- **`brain/position_review.py`'s `health_score()` restructured**, not just trimmed — intraday was the `if` branch, swing the `elif`, long_term the bare `else` (not an explicit check), so removing swing/long_term required converting the time/thesis factor to a single unconditional intraday computation (`minutes_to_squareoff`-based), not deleting two branches.
- **`entry_timing.py`**: deleted `LongTermClassifier`, `SwingClassifier`, and `check_stacking_gate()` (the Loophole-12 cross-track stacking gate — meaningless with no swing position to stack on top of).
- **`executor/position_manager.py`**: deleted `graduate_position()` (Q3-5 swing→long_term reclassification, unreachable with both endpoints gone) and simplified `handle_exit_recommendation()` by dropping its now-meaningless `requires_human` parameter — it had zero callers anywhere in the codebase, another already-dead code path this audit surfaced.
- **`capital/circuit_breakers.py`**: fixed a pre-existing, unrelated bug noticed while trimming this file — `CircuitBreakerState.any_tripped()` iterated `self.__dataclass_fields__` (field *names*, always-truthy strings) instead of field *values*, so it always returned `False`. Fixed to use `dataclasses.astuple(self)`; regression tests added.
- **`orchestrator/tasks_brain.py`'s `_morning_batch_features`** was hardcoded to compute only long_term/swing-relevant features (`compute_promoter_features`, `compute_smart_money_features`, `compute_filing_sentiment_features`, `compute_earnings_quality_features`) and never called intraday's own batch computers (`compute_fo_features`, `compute_beta_features`, `compute_overnight_news_features`) — a pre-existing gap flagged in the prior Fyers-removal audit. Fixed to call `run_track_computers(INTRADAY_COMPUTERS, ...)` instead of the hardcoded list.
- **Orchestrator task `swing_gtt_dispatch` renamed to `gtt_dispatch`** — it already dispatched for any `queued_for_execution` recommendation regardless of track; the old name was misleading before this change and actively wrong after it.
- **New hard blocker B7** (see table above): the live 30-min intraday signal pipeline (`IntradayPipeline`, `run_intraday()`, live feature injection for premarket gap / ORB / VWAP / index direction / spread) is designed but never wired into the running orchestrator. This was found during the audit that motivated this whole redesign and is not fixed by this PR — it's the next real piece of work.
- Deferred, not done in this pass: 8 `compute_*` feature functions became fully orphaned (4 from deleting the swing/long_term registries, 4 more from fixing `_morning_batch_features` above) and are not yet deleted from `computers.py`/`computers_market.py` — flagged for a follow-up rather than expanding this PR further.
- Test worked-examples recalculated by hand and verified against actual output, not assumed: `tests/capital/test_pre_trade.py`'s position-sizing math changed because intraday's bucket (100% vs old 15% swing) and risk_pct (0.5% vs old 1% swing) combination produces different share counts, which also changes which pre-trade check fires first (concentration runs before trade-quality in `PreTradeChecker.check()`) — several tests needed new stop-distance values to avoid tripping the wrong check.
- 337 tests pass, lint clean, no file over 600 lines.

### 2026-09-06 — Refactor: remove Fyers, single-broker (Kite-only) architecture

- **Why:** Fyers was already dead weight in practice — auto-login blocked on an unresolved MPIN issue, every track already routed to Kite in `OrderManager._TRACK_BROKER`, and the original cost-arbitrage rationale (Fyers ₹0 delivery brokerage vs Kite ₹20/order) was factually wrong: Kite (Zerodha) has always charged ₹0 brokerage on equity delivery too. There was no saving being given up.
- Deleted `src/executor/brokers/fyers_broker.py`, `scripts/fyers_login.py`, `fyers_auto_login()` and its ~14 tests, the `fyers-apiv3` dependency.
- `src/executor/order_manager.py`: removed the dormant `_TRACK_BROKER` routing table; fixed `_broker_for()`'s fallback default from `BrokerName.FYERS` (would have raised `RuntimeError: No broker registered for fyers` for any unrecognized track once Fyers was gone) to always route to `BrokerName.KITE`.
- `src/executor/gtt_manager.py`: removed `_normalise_gtt_status()`'s `isinstance(status, int)` branch and its local `fyers_broker` import — that branch existed only to parse Fyers' integer GTT status codes.
- `src/executor/reconciliation.py`: `reconcile_intraday()` restructured from `if broker_id == KITE: list_positions() else: list_holdings()` to always check both on every broker, since Kite now also carries delivery (CNC) positions that used to be Fyers'.
- `src/executor/models.py`: removed `BrokerName.FYERS` enum member.
- `src/orchestrator/orchestrator.py`: removed the Fyers block from `_build_brokers()`.
- `src/collector/fetchers/instruments.py`: stopped computing/writing `fyers_symbol`; column left in schema (unpopulated) rather than dropped via migration — no CHECK constraint or enum depended on it, purely denormalized data.
- `src/backtester/costs.py`: `_delivery_cost()`'s `brokerage = 0.0` value is unchanged (it was already correct) — only the misleading comment attributing it to Fyers was fixed.
- Design docs updated to reflect single-broker reality: `designs/phase-4-executor-and-backtesting.md` (superseded-note + broker table + failure-handling sections), `designs/design-evolution.md` (reversal documented under the original "Dual broker in v1" entry), `designs/open-questions.md` (Q0-4, Q4-3, Q5-3, Q5-4 marked moot; Q0-1 SEBI registration scoped to Zerodha only), `designs/phase-2-collector.md` (instruments table doc updated).
- CI/CD (`ci.yml`, `cd.yml`), `dev.sh`, `ops/runbook.md`: removed all `FYERS_*` env var references.
- Tests: `test_gtt_manager.py`, `test_position_manager.py`, `test_order_manager.py` — `BrokerName.FYERS` (used only as an incidental dict key, always backed by `MockBroker`) replaced with `BrokerName.KITE`. `test_capital_sync.py` — renamed a `fyers` variable used purely as a second mock broker instance. `test_costs.py` — renamed `TestCostModelFyersSaving` (false premise) to `TestCostModelDeliveryVsIntraday` with a corrected docstring.
- 444 tests pass, lint clean.

### 2026-05-12 — End-to-end pipeline run: full signal→recommendation→GTT flow verified

- Collected 46 trading days of NSE bhavcopy prices (139,578 rows) using new `BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` format
- Feature computation: 66,016+ features written for 2026-05-11 (prices for 2,452 stocks, sentiment/smart-money/filing for 9,778)
- Signal generation: 3,688 signals (668 LONG, 813 SHORT, 2,207 neutral); regime=bull_calm (80.7% breadth)
- Recommendation packager: 19 swing LONG recommendations produced; APM gate approved all (paper trading, all circuit breakers clear); 19 OCO-GTT orders written to gtt_orders table with valid_until=2027-05-11
- Bug fixed: `risk_config._allocated_pct(track)` in `_morning_batch_recommendations` — correct call is `ledger._allocated_pct(track)` (method lives on `CapitalLedgerRow`, not `RiskConfig`)
- Bug fixed: `volume_zscore_5d` not written for stocks with 19 trading days in 30-day window (April holiday months); lowered threshold from >=20 to >=6 rows, use available rows as baseline
- Bug fixed: `compute_price_features` missing `price_close` write — all 3,688 signals had `direction=neutral` because recommendation packager skipped every signal with price_close=None
- Bug fixed: feature computer column mismatches (symbol→stock_symbol, observed_date→trade_date, shares_outstanding→total_shares, acquirer_shares_after→shares_held_after, filing_category→category, quarter_end_date→period_end, is_buy→transaction_type in 5 compute functions)
- Test: updated `test_prices.py` fixture to new BhavCopy_NSE_CM column format (TckrSymb/TradDt/TtlTrfVal in ₹ not lacs)
- Missing orchestrator gap identified: APM gate (generated→approved_by_apm→queued_for_execution) is not wired as a task; currently requires manual step or dashboard approval for swing recommendations

### 2026-05-12 — Codebase audit: lint, deprecations, file-size enforcement

- Ruff: auto-fixed 11 issues (unsorted imports, unused imports, bare f-strings); manually fixed 11 more (E501 wraps, E402 import order)
- `src/alerts/alerter.py`: replaced 3× `datetime.utcnow()` (deprecated in Python 3.12+) with `datetime.now(datetime.UTC)`; fixed naive/aware mismatch in `_last_warn_flush` initialization
- `src/orchestrator/tasks.py` (727 lines, exceeded 600-line limit): split by responsibility into `tasks_collector.py` (61L), `tasks_brain.py` (313L), `tasks_executor.py` (165L), `tasks_maintenance.py` (76L); `tasks.py` now a thin registry (159L)
- Bug fixed: `_position_review` had dangling implicit string concatenation on SQL query — the old simple SELECT was appended to the full JOIN query, producing invalid SQL
- Bug fixed: `_position_review` used `pos["entry_price"]` in price fallback — correct column is `pos["average_entry_price"]`
- 427 tests, 0 warnings, lint clean

### 2026-05-11 — Bug: task function API contract fixes (upfront audit)

- `morning_batch_features`: arg order was `(sym, run_date, fs, db_path)`; correct `(db_path, fs, sym, exchange, as_of_date)`; run_date not converted to date; instruments `symbol`→`nse_symbol`; missing `exchange="NSE"`
- `morning_batch_signals`: `RegimeDetector(db_path)` takes no args; `detect(run_date)` wrong → needs market inputs; generators take no args; `generate_all()` does not exist → per-symbol `generate()` loop; added `_compute_market_regime()` from prices breadth; added `_save_signal()` to persist to signals table
- `morning_batch_recommendations`: signals query used non-existent `signal_date`/`status` → `generated_at LIKE date%`; `TradePlanGenerator`/`PortfolioConstructor` constructors take no args; `generate(sym, track, run_date)` → `(signal, price, atr, capital, risk_config, dt)`; `package(plan, run_date)` → `(plan, entry_plan, signal, position_size_shares)`; `per_trade_risk_pct` → `risk_per_trade_pct(track)`
- `position_review`: `reviewer.review()` does not exist → `health_score()` + `check_thesis_broken()`; positions columns: `entry_price`→`average_entry_price`, `entry_date`→`entry_at`, `expected_target`→`target_price`, `original_stop`→`stop_loss_price`; signal_id fetched via JOIN trade_plans
- `weekly_harvest_check`: `live_capital_view()` requires broker LTP → replaced with `latest_ledger()`; `harvest_store.record()` → `harvest_store.run()`; `harvest_triggered`→`fired`, `ops_fund`→`ops_credit`, `dev_fund`→`dev_credit`

### 2026-05-10 — CI/CD and repository hygiene

- `.github/workflows/ci.yml`: CI pipeline — triggers on PR open/sync/reopen and `workflow_dispatch`; runs on Python 3.11 and 3.12 in parallel; gates: ruff check, ruff format check, py_compile syntax check, migrations dry-run, pytest with coverage, 600-line file limit enforcement (CLAUDE.md Rule 4)
- `.github/workflows/cd.yml`: CD pipeline — manual trigger only (`workflow_dispatch`); requires typing `deploy` to confirm; pre-flight: lint + tests + checks for open intraday positions; deploy steps: pause bot → backup DB → git pull → pip install → run migrations → restart systemd services → dashboard health check → resume bot to auto; posts summary to GitHub step summary
- `.gitignore`: added `.env`, `data/`, `secrets.env`, `guide.md` — prevents secrets and local data from being committed accidentally
- `guide.md` (local only, gitignored): end-to-end local setup guide covering virtualenv, migrations, seed data, dashboard startup, orchestrator startup, daily broker token refresh (Kite + Fyers), lint/test commands, Docker usage, common troubleshooting
- Required GitHub secrets for CD: `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`, and `BASIC_AUTH_USER`/`BASIC_AUTH_PASSWORD` — set under repo Settings → Environments → `production`

### 2026-05-11 — Enhancement: Broker auto-login, Telegram alerts, Kite-only execution

- `src/executor/auto_login.py`: fully automated TOTP login for Kite (`kite_auto_login`) and Fyers (`fyers_auto_login`); `refresh_all_broker_tokens()` refreshes both, updates `os.environ` and `.env` in-place; Fyers blocked (MPIN issue — deferred, manual script as fallback)
- `src/executor/order_manager.py`: all tracks (`intraday`, `swing`, `long_term`) routed to Kite until Fyers trading validated; optional `alerter` param — sends Telegram on order submit and on fill
- `src/orchestrator/tasks.py` `pre_market_executor_setup`: calls `refresh_all_broker_tokens()` at 08:30 IST daily; re-authenticates broker objects with fresh tokens
- Telegram notifications wired: login events, trade placed, trade filled, task FAILED_FINAL alerts
- `scripts/auto_login.py`: CLI `--broker kite|fyers|all` for manual token refresh
- `scripts/kite_login.py`, `scripts/fyers_login.py`: interactive fallback login scripts
- `tests/executor/test_auto_login.py`: 14 tests covering all auto_login paths; 0 failures
- Bug fixed: `NameError: name 'os'` in `orchestrator.py _build_brokers()` — missing `import os`
- Bug fixed: `nightly_eod_collector` called `build_fetcher_registry(db_path=...)` — fixed to `(db=conn, raw_dir=Path(...))`; called `CollectionRunStore(db_path)` — fixed to `CollectionRunStore(conn)`; called `run_context(name, run_date=...)` — fixed to `run_context(source)` (takes DataSource enum); called `fetcher.fetch(run_date=...)` — fixed to `fetcher.run(trade_date=date)`
- Bug fixed: `base.py archive()` used `json.dumps(params)` — crashed with date objects; fixed to `json.dumps(params, default=str)`
- Bug fixed: `nse_filings.py` sent `Accept-Encoding: br` — NSE replied with brotli; requests doesn't auto-decompress brotli; fixed by removing `br` from the header
- Bug fixed: `base.py transport()` called `raise_for_status()` before validate() — prevented PermanentFetchError from firing on 404; removed the call
- Bug fixed: `shares_outstanding.py` and `screener.py` raised `ValueError` on 404 — retried 4× instead of skipping; changed to `PermanentFetchError`
- Bug fixed: `PositionReviewer()` was instantiated with kwargs — constructor takes no arguments
- Bug fixed: `positions` query used `WHERE status='open'` — correct column is `is_open=1`
- Bug fixed: `latest_for_date` used `ORDER BY attempt DESC` — all rows had `attempt=1` so ordering was non-deterministic; fixed to `ORDER BY id DESC`
- Bug fixed: orchestrator dispatched same task twice per cron-minute (30s poll interval) — added 60-second cooldown guard in `_last_dispatched`
- Bulk deals now fetch previous weekday (`_prev_weekday(trade_date)`) since NSE/BSE publish the file the morning after
- `tests/test_integration_full_pipeline.py`: 16 integration tests covering migrations, crash recovery, task runner state machine, scheduler already_succeeded gate, latest_for_date ordering, CollectionRunStore run_context, BaseFetcher PermanentFetchError, archive deduplication, date params serialisation, nightly_eod_collector end-to-end, bulk deals prev-weekday, orchestrator dispatch

### 2026-05-10 — Phase 5: Orchestrator, Dashboard, Operations

- `migrations/0005_orchestrator_schema.sql`: 5 tables — `bot_mode` (singleton, auto/paused/emergency_stop), `bot_mode_log` (audit), `task_runs` (9 status states), `trading_calendar` (2026 NSE holidays pre-seeded), `alert_log`, `critical_notification_failures`
- `src/orchestrator/models.py`: `TaskStatus`/`BotMode` StrEnums, `RetryPolicy` with exponential backoff, `TaskDefinition` dataclass, `BotModeStore`/`TaskRunStore` with full SQLite persistence, `is_trading_day()` checks weekday + trading_calendar
- `src/orchestrator/task_runner.py`: `run_task()` contextmanager (SIGALRM timeout enforcement, RUNNING→SUCCESS/FAILED/TIMEOUT), `execute_with_retry()` with configurable backoff
- `src/orchestrator/tasks.py`: 12 task definitions with IST-anchored cron schedules (stored as UTC); all tasks wired with db_path + optional runtime deps (intraday_runner, reconciler)
- `src/orchestrator/scheduler.py`: `cron_matches()` (croniter or stdlib fallback), `dependency_met()`, `Scheduler.should_run()` (8 checks including holiday, already-succeeded, 3-consecutive-intraday-fail gate)
- `src/orchestrator/orchestrator.py`: `Orchestrator` class — crash recovery (`RUNNING→INTERRUPTED`), 30s poll loop, daemon threads per task, CRITICAL alert on FAILED_FINAL
- `src/alerts/models.py`, `telegram.py`, `email_alert.py`: stdlib-only send functions (no third-party HTTP client)
- `src/alerts/alerter.py`: `AlertManager` — INFO (persist only), WARN (6h batch flush), CRITICAL (both channels + `critical_notification_failures` on double-failure); `get_alerter()` singleton; `from_env()` classmethod
- `src/dashboard/queries.py`: 5 read-only queries via `PRAGMA query_only=ON` WAL connection; all column names verified against actual schema
- `src/dashboard/app.py`: FastAPI + HTTP Basic auth; 5 views (Today, Approvals, Positions, Capital/Risk, System Health); approve/reject/validate/mode-change/acknowledge-alert POST endpoints; WebSocket live push
- `src/dashboard/websocket.py`: `ConnectionManager` broadcast; `live_pusher` sends snapshot every 5s
- `src/dashboard/templates/`: base.html (WebSocket auto-reconnect, live indicator), today.html, approvals.html (HTMX validate at 400ms debounce), positions.html, capital_risk.html, system_health.html
- `src/dashboard/static/dashboard.css`: dark theme, monospace, CSS custom properties
- `ops/restart_guard.sh`: blocks 3AM systemd restart if any task_runs row has status=RUNNING
- `ops/systemd/`: boomer-orchestrator.service, boomer-dashboard.service, boomer-executor.service (executor owns WebSocket per Q5-1)
- `ops/runbook.md`: first-deploy, daily ops, 5 incident playbooks (broker down, reconciliation failed, DB corruption, emergency stop, rollback)
- 76 new Phase 5 tests (391 total); 0 failures; lint clean
- Bug fixed: `recommendations` query used wrong column names (`rec_id`, `symbol`, `entry_low`, `valid_until`, `status='pending'`) — fixed to actual schema (`recommendation_id`, `stock_symbol`, `entry_zone_low`, `generated_at`, `status='awaiting_human'`)
- Bug fixed: `signals` query used `signal_date` — fixed to `generated_at`
- Bug fixed: `circuit_breaker_events` query used non-existent `capital_audit_log` table

### 2026-06-02 — Backtester overhaul: entry logic, GTT linkage, holiday calendar, OOS gate

- `src/market_calendar/nse_holidays.py` (NEW): global NSE holiday calendar — `NSE_HOLIDAYS` frozenset (2020–2027, weekday-only), `is_trading_day()`, `trading_days()`, `next_trading_day()`, `prev_trading_day()`, `trading_days_between()`; used by backtester and available to all other modules
- `src/market_calendar/__init__.py` (NEW): package re-export
- **Bug fixed (blocker)**: `BacktestSimulation` never opened any positions — `_open_positions` started empty; new `_generate_entries()` evaluates `entry_decider(symbol, date, features, bar)` each day, sizes via `max_position_pct`, places market/limit orders through MockBroker, records position, places OCO GTT linked via `parent_order_id`
- **Bug fixed (blocker)**: GTT→position linkage broken — `MockBroker.place_gtt()` never stored `parent_order_id`; fixed to store it; triggered GTTs are immediately cancelled to prevent double-close on future days
- **Bug fixed**: cash disconnected — exit proceeds (`actual_exit × qty − costs`) now credited to MockBroker in `_close_position`; capital = cash + open MTM is correct end-to-end
- **Bug fixed**: exit slippage used synthetic ±1% bar; `_close_position` now uses actual OHLCV bar (graceful fallback if unavailable)
- **Bug fixed**: `_trading_days()` yielded Mon–Fri with no holiday exclusions; replaced with `trading_days()` from `market_calendar`; ~10 phantom trading days per year eliminated
- **Bug fixed**: OOS Sharpe gate (`min_oos_pct_of_is`) was dead config — `_check_acceptance` now evaluates it when `in_sample_sharpe` is provided
- **Bug fixed**: `_compute_code_hash()` always returned same static value; fixed to hash actual backtester source files so holdout tracking resets on code changes
- **Bug fixed**: `TradeCost.total_bps` always returned `0.0`; removed — use `CostModel.round_trip_cost_bps()`
- **Performance**: daily state commits batched (was 1 `COMMIT` per row; now 1 flush at run end)
- `backtester/models.py`: added `EntryDecision` dataclass; added `max_position_pct` and `max_open_positions` to `BacktestConfig`
- 79 new tests across `tests/backtester/` and `tests/market_calendar/` (536 total); 0 failures; lint clean

### 2026-05-10 — Phase 4: Executor & Backtesting

- `migrations/0004_executor_schema.sql`: 9 tables — orders, executions, positions, gtt_orders, reconciliation_alerts, executor_errors, backtest_runs, backtest_trades, backtest_daily_state
- `src/executor/models.py`: OrderStatus (10 states), ALLOWED_TRANSITIONS dict, TERMINAL_STATUSES, GttStatus/OrderSide/OrderType/OrderValidity/ProductType/GttType/BrokerName enums; OrderRequest/GttRequest/OrderRecord/PositionRecord/GttOrderRecord/BrokerPosition/BrokerFunds/PriceBar/StateMachineError/PreTradeCheckError dataclasses
- `src/executor/brokers/base.py`: Abstract Broker — 15 interface methods; `get_historical_ohlcv()` and `get_ltp()` as optional overrides
- `src/executor/brokers/mock_broker.py`: `set_price_bar()` drives time; deterministic fills (market at open, limit when bar crosses, GTT single/OCO trigger detection)
- `src/executor/brokers/paper_broker.py`: Wraps MockBroker with live KiteBroker as price source; `register_tick()` feeds fills
- `src/executor/brokers/kite_broker.py`: kiteconnect SDK; Kite WebSocket authoritative tick feed; 5-min LTP staleness; GTT single/OCO; historical OHLCV (1 instrument/request per Q4-4)
- `src/executor/brokers/fyers_broker.py`: fyers-apiv3 SDK; NSE:SYMBOL-EQ format; GTT single/OCO via `triggerType=1/2`; `on_tick()` no-op (Kite is authoritative)
- `src/executor/order_manager.py`: `_TRACK_BROKER = {intraday: KITE, swing: FYERS, long_term: FYERS}`; 8 pre-trade checks (qty, price sanity 5%, idempotency 30s, funds, symbol, market hours, circuit 20%, GTT dup); state machine enforces ALLOWED_TRANSITIONS
- `src/executor/gtt_manager.py`: GTT lifecycle (place/modify/cancel/trail); `trail_stop()` — gain ≥ 2×ATR advances stop 1×ATR; `daily_reconcile()` syncs broker GTT status; `graduate_position()` hook to PositionManager
- `src/executor/reconciliation.py`: 60s intraday (Kite positions + Fyers holdings), EOD full (both brokers + cash); `has_open_alerts()` / `resolve_alert()` for blocking
- `src/executor/position_manager.py`: `open_position()` sets unprotected_flag=1; `graduate_position()` swing→long_term reclassification; `handle_exit_recommendation()` auto-submits for swing/intraday, defers long_term unless forced_derisking
- `src/executor/intraday.py`: 30-min cycle with threading.Lock (skip if busy); 30-min signal validity; 60-min per-stock cooldown; 3 failures → disabled_for_day; `square_off_all_intraday()` checks 09:30-09:50 UTC (15:00-15:20 IST)
- `src/backtester/costs.py`: Indian cost model — intraday (min(₹20, 0.03%) brokerage + 0.025% STT sell-only + exchange + GST + SEBI + stamp); delivery (₹0 brokerage + 0.1% STT both legs); worked example matches Phase 4 design doc
- `src/backtester/slippage.py`: Market 5 bps base × liquidity/volatility adj; stop 1.5× base; limit fills at limit price; `SlippageResult` dataclass
- `src/backtester/simulation.py`: `BacktestSimulation(db, config, price_loader, feature_loader, universe)` — full walk-forward; Sharpe threshold 1.3 (survivorship bias correction); holdout tracking via code hash; persists runs/trades/daily_states to SQLite
- 93 new executor+backtester tests (315 total); 0 failures; lint clean (ruff)
- Bug fixed: GTT `daily_reconcile()` key lookup — extended to handle `broker_gtt_id` key in MockBroker dicts alongside `id`/`trigger_id`
- Bug fixed: circuit check test — price sanity check (5%) fires before circuit check (20%) for the same extreme-price scenario; test renamed to `test_extreme_price_rejected`

### 2026-05-25 — Phase 3: Feature Computer Registry

- **Root cause fixed**: signal generators consumed 29 feature keys; `computers.py` only computed 16 — ~55% of swing signal weight and the entire intraday track running on `None`
- `src/brain/features/computers.py`: `compute_price_features` expanded with second SQL query (290-day window, LIMIT 200) to write `dma_20`, `high_20d`, `dma_50`, `dma_200`; added `compute_filing_count_features` (writes `filing_count_7d`) and `compute_catalyst_proximity_features` (writes `days_to_next_catalyst`)
- `src/brain/features/computers_market.py` (NEW): `compute_technical_pattern_score` (3-component price structure, must run after price_features), `compute_fo_features` (overnight OI change + max pain proximity), `compute_sector_relative_strength` (20d return z-score vs sector peers, handles zero-std case), `compute_price_mode_classifier` (lag-1 autocorrelation, 12 rows), `compute_beta_features` (20d beta vs NIFTY 50 in prices table), `compute_overnight_news_features` (confidence-weighted filing sentiment since prior day 4PM IST)
- `src/brain/features/runner.py` (NEW): `FeatureComputer` frozen dataclass (`fn`, `writes`, `essential`, `requires_live`); `run_track_computers()` executes registry in list order, skips live-only, propagates essential failures, warns on non-essential
- `src/brain/features/track_swing.py` (NEW): `SWING_COMPUTERS` — 10-entry ordered registry; price_features → technical_pattern_score → filing/catalyst/sector/mode/sentiment/smart_money/promoter/earnings computers
- `src/brain/features/track_intraday.py` (NEW): `INTRADAY_COMPUTERS` — 4 batch + 5 live-only computers; live-only (`fn=None`, `requires_live=True`) skipped by runner, injected at signal time
- `src/brain/features/track_long_term.py` (NEW): `LONG_TERM_COMPUTERS` — price_features + filing_sentiment + smart_money + promoter + earnings; `pe_percentile_5y` not computable from current schema (open Q3-4)
- 37 new feature tests in `tests/brain/features/` (test_computers.py, test_computers_market.py, test_runner.py)
- Bug fixed: `compute_sector_relative_strength` returned None when all sector peers had identical returns (sector_std=0); now maps +3.0/-3.0 for clear outperformer/underperformer
- 491 total tests; 0 failures; lint clean

### 2026-05-10 — Phase 3: Brain Framework

- `migrations/0003_brain_schema.sql`: 6 tables — features (point-in-time indexed), sector_classifications, signals, trade_plans, recommendations, recommendation_outcomes
- `src/brain/models.py`: Direction/RecommendationStatus/RecommendationOutcome/EntryStrategy/SkipReason enums; RED_FLAG_CATEGORIES frozenset; ContributingSignal, SignalRecord, TradePlan, EntryPlan, Recommendation (mutable), PositionHealthScore dataclasses; COOLDOWN_DAYS table + cooldown_days_for()
- `src/brain/feature_store.py` (Stage 0): FeatureStore with point-in-time `get_features_as_of()` — enforces `valid_from <= as_of AND source_max_observed_at <= as_of`; write_feature() supersedes existing row for same symbol+name+valid_from
- `src/brain/regime.py` (Stage 1): Exhaustive 4-regime taxonomy (bull_calm/bull_volatile/sideways/bear); RegimeDetector with 3-day stickiness and -1.5% intraday downgrade; Q3-1 resolved — bull_volatile covers VIX 50-80th pct above-DMA gap
- `src/brain/signals/base.py` (Stage 2): BaseSignalGenerator ABC; LIQUIDITY_GATE by track (LT=5cr, swing=2cr, intraday=10cr); confidence = 0.5×|raw_score| + 0.3×agreement + 0.2×freshness
- `src/brain/signals/long_term.py`: 5 sub-signals with regime-specific weight tables; returns None when key data unavailable
- `src/brain/signals/swing.py`: 6 sub-signals
- `src/brain/signals/intraday.py`: 6 sub-signals; large gap (>2.5%) zeroes out premarket_gap_score
- `src/brain/features/computers.py`: compute_promoter/smart_money/filing_sentiment/earnings_quality/price_features() — all write to feature store with point-in-time metadata
- `src/brain/trade_decision.py` (Stage 3): 7-step TradePlanGenerator — EV gate with live_backtest_ratio haircut (p_win = confidence × haircut), ATR-based stops (k=1.5/2.0/3.0), RR gates (1.5/1.5/2.0 by track); ROUND_TRIP_COST_BPS=30
- `src/brain/entry_timing.py` (Stage 3.5): LT1/LT2/SW1/SW2/SW3/ID1/ID2/ID3 fixed strategy classifiers; check_stacking_gate() (3 conditions: pnl>1%, independence≥50%, concentration cap)
- `src/brain/portfolio.py` (Stage 4): PortfolioConstructor with 6 constraint checks; check_pyramid() forbids averaging down
- `src/brain/position_review.py` (Stage 4b): 4-component health score (P&L 40%, signal 30%, time 15%, regime 15%); handle_material_filing() implements Q3-2 Option B — immediate exit rec on RED_FLAG_CATEGORIES, requires_human=False
- `src/brain/packager.py` (Stage 5): RecommendationPackager (routes LT → human, others → APM); RecommendationStore (SQLite persistence, cooldown tracking, injectable recorded_at for deterministic tests)
- 98 new brain tests (222 total); 0 failures; lint clean
- Bug fixed: migration 0003 was self-inserting into schema_version (conflict with runner); removed the duplicate INSERT
- Bug fixed: regime stickiness used `history[-1]` as "current" instead of counting trailing streak
- Q3-1 resolved: exhaustive taxonomy; sideways requires near-DMA + low VIX (below 35th pct for below-DMA paths)
- Q3-2 resolved: Option B — red-flag filing triggers immediate Stage 4b position review only (not new entries)

### 2026-05-09 — Phase 2: Collector Framework

- `migrations/0002_collector_schema.sql`: 20 tables, 55 indexes — raw_archive, instruments, symbol_history, filings, bulk_deals, promoter_changes, shares_outstanding, fo_oi_daily, prices, quarterly_financials, index_constituents_history, corporate_actions, collection_runs; `prices_minute` is parquet-only (not SQLite)
- `designs/phase-2-collector.md`: Rewritten with dual-storage architecture (operational SQLite + historical parquet lake), free-source data strategy (5 layers), and updated source taxonomy (Categories A–D)
- `src/collector/models.py`: All enums as StrEnum — DataSource (11 sources), FilingCategory, SentimentLabel, Exchange, TransactionType, TransactionMode, InstrumentType; RawArchiveRow, FetchResult, CollectionRunRow dataclasses
- `src/collector/base.py`: BaseFetcher abstract class — 5-method anatomy (fetch_url, transport, validate, archive, parse), SHA-256 content-hash dedup, gzipped raw storage, exponential backoff [30s, 60s, 300s, 1800s]
- `src/collector/health.py`: CollectionRunStore — start/finish/latest/recent_failures/run_context (sets FAILED on unhandled exception)
- `src/collector/fetchers/bse_filings.py`: BSE announcements API JSON; _classify_bse_category (8 categories); _parse_bse_datetime handles ddmmmyyyy + ISO
- `src/collector/fetchers/nse_filings.py`: NSE filings with homepage cookie refresh; _cookie_refreshed_at tracks 30-min validity window
- `src/collector/fetchers/bulk_deals.py`: NSE CSV + BSE JSON bulk deals; _is_smart_money substring match against LIC/GIC/MF names
- `src/collector/fetchers/prices.py`: NSE CM bhavcopy primary price source (sec_bhavdata_full); EQ/BE/SM/ST series; prune_old_prices (30-day rolling SQLite window)
- `src/collector/fetchers/fo_oi.py`: NSE F&O bhavcopy ZIP; handles both pre-2023 and 2023+ column naming conventions
- `src/collector/fetchers/shares_outstanding.py`: NSE market cap file; VERIFY flag on URL and column name; falls back to mktcap/close if TOTAL_SHARES absent
- `src/collector/fetchers/screener.py`: Screener.in HTML scrape via pd.read_html(); observed_at = announcement_date + 2h (point-in-time correct); optional CFO extraction
- `src/collector/fetchers/instruments.py`: Kite instruments CSV; NSE EQ/BE series only; upsert pattern; derives fyers_symbol
- `src/collector/sentiment.py`: SentimentPipeline (lazy-loaded ProsusAI/finbert, batch=32); apply_sentiment_to_filings (confidence < 0.60 → unclassified)
- `src/collector/parser.py`: ParseWorker — dispatch to fetcher registry, mark failed on error, run sentiment post-parse; build_fetcher_registry() constructs all 9 fetchers
- 81 new tests (124 total) across 9 test files; 0 failures; lint clean
- Q2-3 resolved: Option C — organic parquet accumulation via Kite tick feed; no vendor cost
- Q2-2 still open: shares_outstanding URL and TOTAL_SHARES column name need live verification

### 2026-05-04 — Phase 1: Capital & Risk Framework
- Repo scaffolded: `pyproject.toml`, ruff config (line-length=100, py311 target), pytest config
- Forward-only migrations runner at `src/db/migrations.py`; initial schema in `migrations/0001_initial_schema.sql`
- `src/capital/models.py`: Track/Regime/BotMode enums, allocation_for_capital(), RiskConfig, CapitalLedgerRow, LiveCapitalView, TradeRequest/TradePermission, LTPSource/ConcentrationSource protocols
- `src/capital/risk_config.py`: RiskConfigStore (seed_defaults, load_current, update_live_backtest_ratio)
- `src/capital/state.py`: CapitalStateManager (initialise, write_eod_ledger, live_capital_view, apply_capital_flow, circuit breaker audit log)
- `src/capital/circuit_breakers.py`: 9-breaker CircuitBreakerState, evaluate_circuit_breakers() pure function
- `src/capital/pre_trade.py`: 7-step PreTradeChecker, regime-scaled position sizing and concentration check
- `src/capital/harvest.py`: evaluate_harvest() pure function + SelfFundingHarvest persistence; harvest takes PREVIOUS HWM (before EOD write)
- 43 unit tests covering all public paths, worked numerical examples matching Phase 1 design doc
- Virtualenv at `.venv` with Python 3.14; use `.venv/bin/pytest` and `.venv/bin/ruff`

### 2026-05-02 — Initial skeleton
- Created project-status.md
- Design phase complete: all 5 phase documents finalized
- CLAUDE.md created with project rules
