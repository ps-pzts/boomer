# Boomer — Project Status

> This file is updated in the same commit as every feature or bug fix.
> Its purpose is context continuity across Claude sessions and developer handoffs.
> Format: newest entry at top. One entry per PR/commit. Never delete old entries.

---

## Status: Phase 1 complete

**Current phase:** Phase 1 (Capital & Risk Framework) implemented and tested. Ready to begin Phase 2 (Collector).

**Last updated:** 2026-05-04

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

_Nothing — Phase 1 complete, Phase 2 not started._

---

## What's next (implementation order)

1. **Phase 2: Collector** — data ingestion services, SQLite schema, migrations runner
2. **Phase 3: Brain** — feature store, signal generation, APM
3. **Phase 4: Executor** — broker abstraction, GTT lifecycle, backtesting engine
4. **Phase 5: Orchestrator** — scheduler, dashboard, alert layer

---

## Hard blockers (must resolve before real-money trading)

| ID | Blocker | Owner | Status |
|----|---------|-------|--------|
| B4 | SEBI algo trading registration | User | Not started — contact Zerodha + Fyers in parallel with coding |
| B6 | Minute-bar historical data source | User | Open — see Q2-3 in open-questions.md |

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
