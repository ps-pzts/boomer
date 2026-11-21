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
| Executor | KiteSim: paper and the backtester run the real `KiteBroker` (#86) | `executor/brokers/kitesim/` is an in-process Kite (`KiteConnect` signatures, Kite statuses and payload shapes, RMS rejections, Kite exceptions, partial fills, postbacks). Paper is `KiteBroker(kite=KiteSim, broker_name=PAPER)`, so 20 paper sessions exercise the live translation layer. `MockBroker`, `PaperBroker` and `FakeKite` deleted; the backtester uses a bar-driven `BarBroker` on KiteSim. Canary recordings replay into KiteSim as a conformance test. Hostile mode (`FaultPlan`, #87): seeded API failures (incl. timeout after the effect), fill-races-cancel, MODIFY PENDING, and held/reordered/duplicated/dropped postbacks. |
| Executor | Daily Kite pre-flight (08:55) + order canary (09:15) gate the session (#85) | Paper never exercises the live Kite path, so every trading day (paper days too) `kite_preflight` checks token, funds, instrument dump, orders, positions, LTP (raw payloads shape-checked via `kite_contract.check_shape`) and the feed; `kite_canary` (09:15:05, NSE pre-open rejects stops) places an SL-M SELL + LIMIT BUY qty 1 MIS 3% below LTP on the real KiteBroker, modifies, cancels and verifies CANCELLED/no fill/nothing left (drift guard, cleanup of every `CNRY`-tagged order, raw responses recorded under `data/kite_recordings/`). Results in `kite_daily_checks` (migration 0017). Session can't reach TRADING until both passed today; a failure → `checks_failed` → HALTED(`preflight_failed`/`canary_failed`), not auto-resumable; Telegram `/canary` re-run passing → `checks_passed` → RECONCILING. Opt-in live tests in `tests/live` (`BOOMER_LIVE_KITE=1`). |
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

### 2026-11-21 — Bug: boomer-executor.service ran a module that does not exist (#109)

- **Symptom:** `ops/systemd/boomer-executor.service` ran `python -m executor.main` (no such module); the runbook's `python -m executor.reconciliation --eod` has no `__main__` either; CD restarted and health-checked the dead unit.
- **Root cause:** the unit predates the in-process design: `orchestrator.orchestrator` owns the feed, the one `SessionController` and every executor task. A separate executor process would be a second controller on one DB.
- **Fix:** unit deleted; removed from runbook and `cd.yml` restart/health lines; the manual reconciliation step now points at the orchestrator's `eod_reconciliation` task. `tests/test_entrypoints.py` parses every `-m`/uvicorn target in the units, dev.sh, Dockerfile and runbook and asserts it exists and has a `__main__` guard.

### 2026-11-21 — Feat: paper to live readiness report (#119)

- New read-only `executor/live_readiness.py` (`python -m executor.live_readiness`) and Telegram `/readiness`: pass / fail / insufficient-data with the exact numbers for each design Q5 criterion (20 closed paper sessions; zero unprotected moments from `exit_reason` of forced-out trades (state tables, not events), flagged positions and open trades without a stop; mean adverse slippage per leg class vs `SlippageModel`'s assumption (limit 0, stop 7.5 bps); expectancy after `CostModel` costs > 0; B4 manual flag `BOOMER_B4_RESOLVED`). Paper sessions only; opens the DB `mode=ro`; never enables live trading. Market exits are counted but not judged (no stored reference price).

### 2026-11-21 — Refactor: split files over the size limit (#95, #117)

- `session_controller.py` (534 -> 330 lines) keeps the `SessionController` class; guard context moved to `session_context.py` (`ContextMixin`, timing constants, `_blocker`), reconcile to `session_reconcile.py` (`ReconcileMixin`), emergency cancel/flatten to `session_emergency.py` (`EmergencyMixin`). Constants and `_blocker` are re-exported, so every import path is unchanged.
- `order_manager.py` (528 -> 324 lines) keeps `OrderManager`; the 8 pre-trade checks moved to `order_checks.py` (`pre_trade_checks`, called from `OrderManager._pre_trade_checks`), persistence helpers to `order_store.py` (`OrderStoreMixin`).
- `tests/test_integration_full_pipeline.py` (652) split into `tests/integration/test_pipeline_orchestrator.py`, `test_pipeline_collector.py` and `pipeline_helpers.py`; resolves the follow-up noted under the #54-era entries.
- Zero behaviour change: full suite 1663 passed / 1 skipped / 2 deselected before and after (1832 passed after rebasing on #120). Mutation: `order_checks.pre_trade_checks` 51/51 (same as `_pre_trade_checks` before); reconcile code 4/6 both before (in `session_controller`) and after (`session_reconcile`), same two `untracked_orders` survivors. `scripts/mutation_all.sh` targets updated.

### 2026-11-21 — Bug: collector date parsers invented today's date on failure (#54)

- **Symptom:** `_parse_date()` in `prices`, `fo_oi`, `bulk_deals` and `shares_outstanding` returned `date.today()` when no format matched; `nse_filings`/`bse_filings` returned today for an empty date and the first 10 characters of the string for an unparseable one. Silent lookahead / point-in-time corruption (a price row written under today).
- **Root cause:** a fallback chosen for convenience instead of treating an unparseable date as unknown.
- **Fix:** new `collector/dateparse.py` (`parse_date`/`parse_datetime` return `None`; `ParseTally`). Every affected parser skips the row and counts it, logs `N of M rows skipped` per file, and raises `DateFormatError` (after rolling back that file's inserts, so the ParseWorker marks the raw row FAILED) when more than 5% of date-bearing rows fail, the signature of an NSE/BSE format change. Fetchers audited with no date fallback: `sector`, `instruments`, `screener`. Regression tests added for every fetcher.

### 2026-11-21 — Feat: intraday universe = EQ ∩ F&O minus ASM/GSM surveillance (#50 part 2)

- **Universe:** `brain/universe.py` now subtracts NSE ASM/GSM stocks (`brain/surveillance.py`) from EQ ∩ F&O; applied to both `morning_batch_features` and `intraday_watchlist`. Matching by upper-cased symbol and by ISIN when the list carries one. F&O underlyings with no EQ instrument (renames) are reported (`fno_unmatched`, WARN); no EQ/F&O symbol overlap fails closed (`no_fno_equity_match`). Watchlist rejection counts now include `not_fno` and `under_surveillance`.
- **Fail-safe staleness** (trading days from list fetch date to session, older of ASM/GSM): 0 fresh; 1-3 last list applied + WARN; >3 or never fetched -> F&O-only + CRITICAL. Never silently unfiltered.
- **Collector:** `collector/fetchers/surveillance.py` (cookie handshake, API then CSV fallback, schema-agnostic parser, empty payload rejected) writes `surveillance_list` (migration `0019`); registered in the nightly collector. **UNVERIFIED against the live NSE site** (endpoint unreachable at build time): response shape and the CSV fallback URLs are assumptions.
- **Mid-session reconcile:** new repeating task `surveillance_refresh` (09:25..14:55, :25/:55) fetches a missing list; the decision cycle re-reads the list each cycle and rejects NEW entries on listed stocks (`under_surveillance`); open trades are never touched.
- Tests: `tests/brain/test_surveillance.py` (worked example, staleness boundaries, symbol edge cases), `tests/collector/fetchers/test_surveillance.py`, `tests/orchestrator/test_surveillance_refresh.py`, plus additions to the watchlist-task and decision-cycle tests.

### 2026-11-21 — Bug: open positions never got a current price or unrealised P&L (#106)

- **Symptom:** `positions.current_price`/`unrealised_pnl` stayed at entry/0 all session, so `/positions`, `/pnl` and the UI showed 0 open P&L.
- **Root cause:** `PositionManager.update_ltp()` had no caller.
- **Fix:** `TradeSupervisor.reconcile()` now marks each active symbol to market via its `price` callable (symbols with no tick untouched; closed positions never overwritten). Worked example: qty 10, entry 100, price 101.5 -> +15. Tests: `tests/executor/test_mark_to_market.py`.

### 2026-11-21 — Bug: modules imported under two paths, `src.X` and `X` (#94)

- **Symptom:** `isinstance` across the orchestrator and executor silently failed; module-level state existed twice (worked around by class name in `kite_checks.kite_broker()` and an `sys.modules` alias in `observability/emit.py`).
- **Root cause:** ~16 files imported `src.executor...`, ~49 imported `executor...`; both resolve (editable install + repo-root cwd) but give two module objects.
- **Fix:** one style, bare names, everywhere (src, tests, scripts, patch targets, systemd units, `dev.sh`, Dockerfile, CI/CD). `banner.py` is now a declared `py-module` imported absolutely (its `..banner` relative import only worked under `src.`). Class-name workaround and alias hack removed; `kite_broker()` uses `isinstance`. `tests/test_import_paths.py` fails on any `src.` import/string path, any package-escaping relative import, and smoke-imports every module in a fresh interpreter. Follow-up: #109 (`executor.main`, named by the executor systemd unit, does not exist).

### 2026-11-21 — Bug: order spec had transitions no code path emits; refusals recorded as ERROR (#100)

- **Symptom:** the #88 transition-coverage gate found spec transitions nothing can emit (`* -> error` from finished states, `pending -> expired`, `created -> error`), and a definite Kite refusal (`InputException`/`OrderException`, e.g. RMS) was recorded ERROR, indistinguishable from an outage.
- **Root cause:** `ALLOWED_TRANSITIONS` was written ahead of the code, and `OrderManager.submit` turned every `place_order` exception into ERROR.
- **Fix:** spec trimmed to reachable transitions (`EXPIRED` removed, `FILLED`/`CANCELLED`/`REJECTED`/`ERROR` terminal). `KiteBroker.place_order` raises `BrokerRejected` (also the original Kite exception) for refusals, which land as REJECTED; unknown outcomes (timeouts, #97) stay ERROR. `SPEC_ONLY` list in the integration gate emptied accordingly.
- Tests: refusal-is-REJECTED in `test_broker_behaviour`, `test_order_paths`, `test_lost_orders`; spec terminal states in `test_order_machine`; new `tests/executor/brokers/test_kite_refusal.py`. 1552 tests pass, lint and format clean.

### 2026-11-19 — Feat: Telegram alerts and operator commands (#103)

- **Commands** (`src/alerts/commands/`, all read each machine's own table, never `events`): `/status` rebuilt (session, bot mode, Kite checks, trades, P&L), `/positions`, `/trades`, `/pnl`, `/halt` (why + how to resume, with the failed reconcile checks), `/ack` (acknowledge a reconcile mismatch), `/pause`, `/resume`, `/emergency confirm`, `/toggle` (flip the next session paper <-> live; to live shows the paper record and needs `confirm`). Every mode change is audited with `telegram` as the actor. `/mode` and `/canary` unchanged.
- **Live feed** (`src/observability/telegram_feed.py`, own process, `ops/systemd/boomer-feed.service`, started by `dev.sh`): tails `events` and sends one message per trade event (placed, filled, protected, target/stop hit, closed + P&L, cancelled, unprotected) and session event (halted, resumed, market open, cutoff, square-off, closed), plus an end-of-day summary. Cursor in `telegram_feed_cursor` (migration `0018`), so a restart neither repeats nor skips and a Telegram outage delays instead of dropping. `BOOMER_TELEGRAM_FEED=off` mutes it.
- `trading_sessions.halt_detail` (migration `0018`) records why a session halted so `/halt` needs no events read; `executor/session_halt.py` holds the shared ack.
- Not done: derived alerts from the observability projection (SILENT component, drift, PROTECTING too long: OBS-3); quiet hours; per-chat roles beyond the single configured chat id.

### 2026-11-21 — Test: mutation testing of the money-critical modules (#89)

- `scripts/mutation.py` (AST mutation runner: comparison/arith/boolean/constant/condition/return mutants, one at a time, file always restored) and `scripts/mutation_all.sh`. Slow, run by hand, not in CI. Reports (survivors with a reason for each equivalent/unreachable one) in `reports/mutation/*.json`. Parallel runs must use separate copies of the tree (a mutant in one file is visible to another run's tests).
- **Scores, before -> after (killed/total):** trailing 78.7 -> 100 (47/47); sizing 78.9 -> 94.7 (18/19); ticks 56.8 -> 83.8 (31/37, earlier run); pre_trade gate 48.4 -> 95.6 (87/91); circuit_breakers 78.4 -> 100 (51/51); session_machine 92.9 -> 100 (42/42); session_inputs 77.8 -> 100 (54/54); `map_kite_status` 84.6 -> 100 (13/13); KiteSim rules 77.4 -> 99.1 (114/115); `OrderManager._pre_trade_checks` 62.7 -> 100 (51/51); reconciliation 41.5 -> 95.1 (39/41); trade_lifecycle 66.9 -> 94.9 (112/118). `trade_machine` (1/1) and `order_machine` (0 sites) are pure spec tables, covered by the transition-coverage gate. Every module is above the 85% target.
- Remaining survivors are all equivalent or unreachable (redundant guards, percentage-text rounding, non-zero-integer comparisons, an unreachable `else 0`); each is explained in its JSON.
- Gaps found and closed: the pre-trade gate's sector/cluster/single-stock caps, regime gate and RR/EV edges were untested at their boundaries; reconciliation EOD cash and alert payloads were untested; trade_lifecycle `open_legs`, exit keys (`-exit1`), exit price and alert paths were unasserted. No source bug was found; `_check_time`, `_check_black_swan` and `_check_capital_availability` in `capital/pre_trade.py` are unreachable from `check()` (an earlier rule always fires first) and are tested directly.

### 2026-11-19 — Bugs: four stranded-position bugs found by the integration suite (#97, #98, #99, #101)

- **#97** symptom: a `place_order` answer lost to a timeout left a working order nobody tracked (entry or stop). Root cause: any exception became ERROR, and Kite tags were the UUID truncated to 20 chars, so every order of a trade shared one tag. Fix: tags are hashed when long; `find_order(key)` looks the order up and `OrderManager.submit` adopts it; if the broker is unreachable `cancel_lost_orders()` (run each reconcile) cancels it later, or flattens it if it already filled.
- **#98** symptom: after a failed cancel the order was recorded CANCELLED while still working at Kite, and the trade closed over a resting leg. Fix: `cancel()` never records CANCELLED while the broker says working; `_settle` cancels resting legs first and will not close while one remains (stays EXITING, retries, alerts).
- **#99** symptom: a part-filled entry had no stop until the 30-min expiry. Fix: the first partial fill cancels the remainder and protects what filled (design L12 changed).
- **#101** symptom: a failed exit placement was never retried (`-exit1` reused, refused as duplicate), stranding the position. Fix: the key counts every exit order ever created (`BracketStore.exit_attempts`).
- Regression tests: `tests/executor/test_lost_orders.py` plus the former xfail scenarios, now passing; random days and the Hypothesis machine run with all four faults on (75 extra seeds and a 120-example run clean). #100 (Order spec drift) is still open.

### 2026-11-19 — Test: cross-machine integration suite on KiteSim (#88, in progress)

- `tests/integration/`: `Hostile` harness (real Order + Trade + Session machines, paper KiteBroker on KiteSim, `FaultPlan`, fake clock). Invariants after every step: I1 open trade has a resting stop, I2 trades' net == broker net, I3 no untracked resting order, I5 stop never lowered (per order), I6 every long covered by resting stops, I4 flat and terminal at EOD. The observer reads ground truth (`plan.suspended()`), not the faulty wire.
- Suites: scenarios (postbacks, fill races, lost answers, partial entry, trailing under MODIFY PENDING, restarts), Order paths, Trade paths, Session flows (a day on a fake clock through `SessionController`), process restarts (KiteSim rebuilt from the paper book), decision-cycle cap, 15 seeded random days, a Hypothesis stateful machine, same-seed event-log equality, and replay of `state_transition` events rebuilding every machine row's state.
- **Transition-coverage gate** (`test_zz_transition_coverage.py`): every Session/Trade/Order spec transition is fired by an integration test or listed in `spec_only.py` against an issue. Integration alone now fires 29/29 Session, 14/14 Trade and 12/23 Order; the other 11 Order transitions are spec-only (#100).
- Bugs found: #97, #98, #99, #101 (all fixed, see above), #100 (spec drift, open).
- Known limits: event timestamps use the wall clock, not the injected fake clock; replay checks machine state per row, not every column; `hypothesis` added to the dev extras.

### 2026-11-19 — Feat: KiteSim, paper on the real KiteBroker, MockBroker deleted (#86)

- **Why:** paper used its own broker and vocabulary, so paper validation proved nothing about the live order path.
- `src/executor/brokers/kitesim/`: `KiteSim` (engine, rules, portfolio, GTT, payloads, instruments, `SimFeed`, `build_paper_broker`). `KiteBroker` now takes an injected Kite client; GTT code moved to `kite_gtt.py` to stay under the file limit. `BrokerName.MOCK` removed (old DB comment only; no code writes it).
- `src/backtester/bar_broker.py`: the backtester places orders through `KiteBroker` on KiteSim, walking each daily bar open → nearer extreme → farther extreme → close. `acceptance.py` split out of `simulation.py`; `code_hash` covers both.
- **Bug found (#86):** `OrderManager.cancel` recorded a cancel twice when the broker's postback landed before `cancel_order` returned. Fixed by re-reading the order after the cancel; regression test in `test_order_manager.py`.
- Verification: ruff check/format clean, 1340 tests pass (1 skipped, 2 live-Kite deselected). Run with `PYTHONPATH=src` from a worktree (see #94). Design doc `intraday-live-pipeline.md` updated; `PaperBroker` mentions struck through.

### 2026-11-19 — Bug: `KiteBroker.modify_gtt` could never succeed (found by #84)

- **Symptom (latent; live GTTs only):** every GTT trail (`GttManager.trail_stop`) would raise `TypeError` at the SDK. **Root cause:** `modify_gtt` passed GttManager's `{"leg1": {...}}` straight to `KiteConnect.modify_gtt`, which takes the full trigger (type, symbol, exchange, trigger_values, last_price, orders); a bare `MagicMock` accepted it. **Fix:** read the GTT back with `get_gtt`, change only the named legs, resend it with a live LTP (refuses, like `place_gtt`, when no LTP). Regression tests in `test_kite_broker_sdk.py`.

### 2026-11-19 — Feat: Kite SDK contract, autospec + documented response fixtures (#84)

- **Why:** Kite tests used a bare `MagicMock()` with hand-typed responses, which only confirmed our own assumptions and hid the #73 `place_order` bug.
- `src/executor/brokers/kite_contract.py`: the one source of truth for what Kite returns (order, order_postback, position(s), holding(s), margins_equity, ltp, instrument(s), gtt(s), gtt_ack, place_order, session, historical), Kite's order-status / enum vocabulary and exception classes. `check_shape(kind, payload) -> list[str]` (used by the canary, #85). `kite_broker.py` now takes its status sets from it.
- `tests/fixtures/kite/`: documented wire responses (order lifecycle incl. two realistic REJECTED messages, positions, holdings, margins, LTP, instruments CSV, GTT, session, candles, WebSocket postback, error bodies). Provenance in `_meta.json`; to be replaced by canary recordings.
- `tests/kite_fixtures.py`: `load` / `data` / `sdk(method)`; `sdk` runs the real `KiteConnect` method over a fixture via a fake HTTP session, so values have the SDK's exact Python shape.
- `tests/kite_sdk.py` + `kite_sdk` fixture (`tests/conftest.py`): `create_autospec(KiteConnect, instance=True)` with the SDK's constants. Every response (via `respond()` or `.return_value`) is checked against the contract at call time and again at teardown (so a violation swallowed by e.g. `get_ltp`'s fallback chain still fails); exceptions must be kiteconnect/requests ones; an unconfigured SDK call is a failure. All Kite SDK fakes moved onto it (`test_kite_broker`, `test_kite_order_status`, `test_untracked_orders`, `test_ticks`, `test_auto_login`); new `test_kite_broker_sdk.py` covers positions, holdings, funds, every lifecycle order, modify/cancel, historical, GTT.

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
- ~~Follow-up, not done here: `tests/test_integration_full_pipeline.py` is 652 lines~~ — split in #117.
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

### Older entries

Entries from 2026-09-06 and earlier (phases 1 to 5, the audit, the backtester overhaul, the redesign) are in [`project-status-archive.md`](project-status-archive.md).
