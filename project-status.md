# Boomer — Project Status

> This file is updated in the same commit as every feature or bug fix.
> Its purpose is context continuity across Claude sessions and developer handoffs.
> Format: newest entry at top. One entry per PR/commit. Never delete old entries.

---

## Status: Phase 2 complete

**Current phase:** Phase 2 (Collector) implemented and tested. Ready to begin Phase 3 (Brain).

**Last updated:** 2026-05-09

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

_Nothing — Phase 2 complete, Phase 3 not started._

---

## What's next (implementation order)

1. **Phase 3: Brain** — feature store, signal generation, APM
2. **Phase 4: Executor** — broker abstraction, GTT lifecycle, backtesting engine
3. **Phase 5: Orchestrator** — scheduler, dashboard, alert layer

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

Key ones to resolve before first live trade:
- Q3-1: Regime taxonomy gap (no pure bull_volatile → intraday path)
- Q4-1: LTP source contract (Kite WebSocket vs REST fallback)
- Q4-3: Fyers GTC OCO verification (confirm API supports it before coding)

---

## Change log

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
