# Boomer — Project Status

> This file is updated in the same commit as every feature or bug fix.
> Its purpose is context continuity across Claude sessions and developer handoffs.
> Format: newest entry at top. One entry per PR/commit. Never delete old entries.

---

## Status: All phases complete — CI/CD pipeline in place

**Current phase:** Phase 5 complete. GitHub Actions CI (lint + test on every PR) and manual CD (SSH deploy with pre-flight checks) added. 391 tests, lint clean.

**Last updated:** 2026-05-10

---

## What's done

| Area | Item | Notes |
|------|------|-------|
| Design | Phase 1 — Capital & Risk | Finalized with 9 loopholes documented |
| Design | Phase 2 — Collector | Finalized with Screener.in, FinBERT, NSE bhavcopy, dual-broker instruments |
| Design | Phase 3 — Brain | Finalized with 15 loopholes, signal cooldown table, walk-forward Sharpe ≥ 1.3 |
| Design | Phase 4 — Executor & Backtesting | Finalized with GTT architecture, dual-broker (Kite intraday + Fyers delivery) |
| Design | Phase 5 — Orchestrator & Ops | Finalized with forward-only migrations, 3AM restart guard, dual-alert layer |
| Design | open-questions.md | 24 open questions organized by phase |
| Design | design-evolution.md | Narrative history: 18 issues + 7 hard blockers resolved |
| Infra | CLAUDE.md | Project rules: git workflow, tests, PRs, file limits, context continuity |

---

## What's in progress

_Nothing — all 5 phases complete._

---

## What's next

1. **End-to-end integration testing** — wire all phases with a shared SQLite DB, simulate a full trading day
2. **Live paper trading** — run with real market data, paper broker, monitoring dashboard
3. **SEBI registration** (B4 blocker) — required before any real-money orders

---

## Hard blockers (must resolve before real-money trading)

| ID | Blocker | Owner | Status |
|----|---------|-------|--------|
| B4 | SEBI algo trading registration | User | Not started — contact Zerodha + Fyers in parallel with coding |
| B6 | Minute-bar historical data source | User | Resolved — Option C (organic collection via parquet); see Q2-3 |

---

## Open decisions (non-blocking)

See [designs/open-questions.md](designs/open-questions.md) for all 24 questions.
Key ones resolved in Phase 1 implementation:
- Q1-1: Regime scaling applies to NEW ENTRIES only — existing positions not force-liquidated on regime shift
- Q3-4: Per-track confidence haircut (`live_backtest_ratio_*`) in `risk_config`, initial value 0.70

Key ones resolved in Phase 3 implementation:
- Q3-1: bull_volatile now covers all above-DMA states with elevated VIX; ATR stops scaled 1.5× in Volatile Uptrend
- Q3-2: Option B — red-flag filings (fraud, auditor change, pledging) trigger immediate Stage 4b exit re-evaluation; entries still morning-batch only

Key ones resolved in Phase 5 implementation:
- Q5-1: WebSocket in-process within boomer-executor.service (not a separate service)
- Q5-2: Rollback checklist added to ops/runbook.md
- Q5-3: Fyers token refresh is a manual pre-market step with CRITICAL alert on failure
- Q5-4: Fyers credentials (`FYERS_APP_ID`, `FYERS_SECRET`, `FYERS_ACCESS_TOKEN`) in secrets.env alongside Kite

Key ones resolved in Phase 4 implementation:
- Q4-1: Kite WebSocket tick feed is authoritative LTP; 5-minute staleness threshold falls back to REST quote
- Q4-3: FyersBroker confirmed (user has API access); GTC/OCO pending paper-trading verification before first live delivery trade
- Q4-2: Trailing stops continue in paused mode; orchestrator (Phase 5) owns pause/resume signal
- Q3-5: `graduate_position()` implemented — cancels OCO, places 3×ATR stop OCO, updates track to long_term

---

## Change log

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
