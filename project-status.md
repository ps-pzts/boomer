# Boomer — Project Status

> This file is updated in the same commit as every feature or bug fix.
> Its purpose is context continuity across Claude sessions and developer handoffs.
> Format: newest entry at top. One entry per PR/commit. Never delete old entries.

---

## Status: Pre-implementation

**Current phase:** Design complete, ready to begin Phase 2 (Collector) implementation.

**Last updated:** 2026-05-02

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

_Nothing yet — implementation has not started._

---

## What's next (implementation order)

1. **Repo scaffolding** — directory layout, pyproject.toml, ruff config, pytest config
2. **Phase 2: Collector** — data ingestion services, SQLite schema, migrations runner
3. **Phase 3: Brain** — feature store, signal generation, APM
4. **Phase 4: Executor** — broker abstraction, GTT lifecycle, backtesting engine
5. **Phase 5: Orchestrator** — scheduler, dashboard, alert layer

---

## Hard blockers (must resolve before real-money trading)

| ID | Blocker | Owner | Status |
|----|---------|-------|--------|
| B4 | SEBI algo trading registration | User | Not started — contact Zerodha + Fyers in parallel with coding |
| B6 | Minute-bar historical data source | User | Open — see Q2-3 in open-questions.md |

---

## Open decisions (non-blocking)

See [designs/open-questions.md](designs/open-questions.md) for all 24 questions.
Key ones to resolve before first live trade:
- Q1-1: Regime scaling policy (partial vs full allocation)
- Q3-1: Regime taxonomy gap (no pure bull_volatile → intraday path)
- Q4-1: LTP source contract (Kite WebSocket vs REST fallback)
- Q4-3: Fyers GTC OCO verification (confirm API supports it before coding)

---

## Change log

### 2026-05-02 — Initial skeleton
- Created project-status.md
- Design phase complete: all 5 phase documents finalized
- CLAUDE.md created with project rules
