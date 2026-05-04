# Design Evolution

This document captures how the Boomer design developed from initial concept to its current state. It exists because the *reasoning behind* decisions matters as much as the decisions themselves — when future-you (or someone else) wants to change something, knowing why it was that way is essential.

This is written as a narrative rather than a spec because the journey shaped the destination. Several early ideas were abandoned, several decisions were reversed mid-design, and some of the strongest parts of the architecture came from challenges to the original plan.

---

## Inception

The original idea, in the operator's own words:

> A bot named Boomer that wakes up early morning at 07:00 IST and goes to BSE and NSE filings, scrapes data of company filings and updates, also gets data of bulk buyers and sellers, promoter buying and selling. With this data it analyses and designs long-term hold/add/sell, swing position add/sell, and best intraday probability. Then finally it reviews with the end user with all collected data points and information, gets final approval before executing buy/sell call. Three independent systems: 1. Data collection 2. Analysis and decision 3. Execution. All three should work in sync and report to a live HTML dashboard. Start point and end point is bot.

This was the seed. The architecture that grew from it ended up substantially different in shape but identical in intent.

## The first pivot: from microservices to monolith

The initial response treated the three subsystems as deployment-independent services with a message bus and shared infrastructure. The operator pushed back with three strong principles:

1. Build skill in **simple, right-sized** systems rather than performative complexity.
2. Avoid jargon and trending patterns without a use-case justification.
3. Run on free-tier cloud, with the system funding its own paid upgrades later.

This forced a complete rethink. The "three independent systems" the operator described are *logical* boundaries, not deployment boundaries. A single Python application with three modules, three scheduled tasks, and one shared SQLite database satisfies the same intent without any of the operational overhead of distributed systems.

This set the tone for the entire design: **boring monolith, well-modularised, with seams clearly drawn** so that pieces can be replaced or extracted later if scale ever demands it. Every subsequent decision was tested against this principle.

## Locking down System 2 first

The operator correctly identified that System 2 (analysis) was where the real intellectual work lay. System 1 is solved engineering (scraping with rate limits, dedup via hashing). System 3 is constrained by broker API surface area. System 2 is where money is made or lost.

A loophole audit on the original "analyse the data and decide" description surfaced seven category errors:

1. "Analyse" was conflating four distinct steps (feature extraction, signal generation, position sizing, portfolio construction).
2. Treating long-term, swing, and intraday as variations of one process rather than three pipelines with different inputs and metrics.
3. No defined success metric per timeframe, so signals could never be validated.
4. No protection against lookahead bias when using historical data.
5. View vs trade conflated — strong opinion ≠ good entry.
6. No regime awareness — strategies that work in bull markets often lose money in bear markets.
7. Confidence treated as binary instead of continuous with attribution.

System 2 was redesigned as six explicit stages, each with a single responsibility and a typed contract with the next stage. This became the spine of the entire design.

## Phase 1: capital and risk first

Before anything else could be designed, the rules of money flow needed to be locked down. Two important decisions emerged from this phase:

### The 70/15/15 allocation discussion

The operator initially proposed 50% swing, 40% intraday, 10% long-term. This was challenged on the basis that it allocated 90% of capital to the two strategies with the worst structural edge for retail traders, and 10% to the strategy where retail historically has the best chance.

The operator correctly counter-argued: with limited starting capital, allocating only 5-10% to swing/intraday means insufficient trade volume to ever validate whether those tracks have edge. Both positions had merit.

The synthesis: **paper trading at full volume across all three tracks, real money at 80/15/5 initially**, then settling to **70/15/15** at a defined milestone (₹2,50,000 capital). The paper trading provides the data to validate edge without putting capital at risk. The intraday auto-demote-to-paper rule was added: if intraday shows negative cumulative P&L after 60 days of real trading, the track auto-switches to paper-only.

### The withdrawal model

The operator initially proposed withdrawing all profits weekly. The math of this was shown to be structurally biased toward depletion: wins capped at HWM, losses uncapped, leading to mean reversion downward over any sequence with non-zero loss probability.

The fix was a high-water-mark anchored harvest rule: take 50% of any excess above HWM (when excess is at least 3% of HWM), with the harvest going to a layered self-funding flow — operational fund (cloud, data, fees) → development fund (upgrades) → owner withdrawal (only after both are funded). The operator's framing of "the system funds itself first" turned out to be more sophisticated than the initial design, and was adopted in full.

## Phase 2: the collector and point-in-time correctness

The collector design centred on one architectural idea: **two-layer storage**. Raw payloads kept immutably in an archive layer; parsed records in normalised tables that can be wiped and rebuilt entirely from the archive.

This decision pays for itself the first time a parser bug is found, the first time a source format changes, and every time a backtest needs historical data exactly as it was known at the time.

The point-in-time correctness rule was non-negotiable: every feature has a `valid_from` date and a `source_max_observed_at` field, so any historical query can return "what we would have known on date X." Without this, backtests silently lie.

Seven loopholes were addressed: source corrections, symbol changes, time zones, the "first time we see X" problem for initial backfill, storage growth, broker-vs-exchange price reconciliation, and parser version migration.

## Phase 3: the brain (longest phase)

System 2 was decomposed into six stages plus the APM gate. Each stage has explicit inputs, outputs, and contracts.

**Stage 0** — Feature store. Long-format SQL table with temporal columns. Features are added incrementally as signals need them, never pre-computed for hypothetical use cases.

**Stage 1** — Regime detector. Four regimes (bull_calm, bull_volatile, sideways, bear) computed from Nifty 50 trend, India VIX percentile, and Nifty 500 breadth. Three-day stickiness rule prevents whipsaw. Falls back to the most conservative regime if computation fails.

**Stage 2** — Three signal tracks. Each produces a `Signal` record with raw_score, confidence, and full attribution. Weighted-sum scoring for v1, with the ML migration path baked in (every decision logged with feature snapshot for future training).

Concrete signal definitions and weights were specified per track. Long-term track centres on promoter activity, smart money, filing sentiment, earnings quality, and valuation. Swing track centres on catalyst proximity, technical setup, volume, sector momentum. Intraday track centres on pre-market gap, opening range, F&O signals, news decay.

**Stage 3** — Trade decision layer. ATR-based stops, reward-to-risk gate, expected-value gate with a 30% confidence haircut to account for backtest-vs-live decay.

**Stage 3.5** — Entry timing layer. This was added mid-design when the operator pointed out that the original Stage 3 used a 0.5% band around current price, which amounts to "buy at random market price." The operator initially asked about Fibonacci retracement specifically; this was redirected toward empirically-grounded structural levels (DMAs, swing highs/lows, breakout retests, VWAP, volume profile) with reasoning provided.

The operator then made an important architectural improvement: instead of three timing strategies that any track could pick from, build **three independent classifiers**, one per track, each containing only strategies relevant to that track. This is cleaner and was adopted. The operator also asked for the ability to take **stacked intraday positions on a stock with an open swing** — a sophisticated technique with real failure modes that were addressed via a six-condition stacking gate (in-profit swing, signal independence, concentration cap, broker tagging, auto square-off, daily intraday limits).

A hard rule was added: **no intraday position can be converted to swing or long-term, ever**. This is the single most important behavioural guardrail in the system.

**Stage 4** — Portfolio constructor. Concentration caps, correlation caps, position count limits, prioritisation when more candidates exist than slots, pyramiding rules with a hard "no averaging down on losers" prohibition.

**Stage 5** — Recommendation packager + APM gate. Long-term routes to human approval; swing and intraday are auto-decided by the APM with a 10-minute override window for the operator.

Seven loopholes were addressed in Phase 3, including v1 long-only constraint (shorting deferred to v2), signal weight versioning, signal independence assumption, sector mapping curation, and explicit decision against drawdown-conditioning of confidence at the signal layer (it's the APM's job).

## The multi-broker question

Mid-Phase 4, the operator raised whether to use two different brokers — one for delivery (long-term, swing) and one for intraday — to physically segregate position types and tax accounting.

This was evaluated as a real architectural concern with genuine professional precedent. At ₹50,000 capital, the cost-benefit was unfavourable (fixed integration cost, scale-dependent benefit). But the architectural fix was applied: a **broker abstraction interface** was added to the executor design from day one. Every order carries a `broker_id` field; concrete broker implementations sit behind a common contract; v1 wires up only Kite, but a second broker can be added in 2-3 weeks of work later. The trigger to flip on multi-broker is the same ₹2,50,000 milestone as the allocation shift.

This is a recurring pattern in the design: distinguish between *what to build now* and *what to architect for now*. Architectural seams cost almost nothing to leave open; retrofitting them later is expensive.

## Phase 4: execution and validation

Four components designed together because they all concern "the trading day in motion."

**Executor** — order lifecycle state machine, broker abstraction, separate stop/target orders (no bracket orders, since brokers like Zerodha discontinued BO for retail), reconciliation every 60 seconds during market hours plus an end-of-day full reconciliation. Pre-trade safety checks layered on top of Phase 1 risk checks (defence in depth).

**Backtesting harness** — same code as live with a `MockBroker` substituted. Realistic Indian cost modelling (15-25 bps intraday, 50-100 bps delivery), volume- and volatility-aware slippage, walk-forward validation as the deployment gate, and a 5-run holdout-integrity discipline to prevent overfitting through iterative tweaking.

The operator asked whether Streak or Sensibull could replace the custom backtester. The answer was no, structurally — those tools test technical-only strategies, can't model the bot's actual signal logic (filings, deals, regime conditioning), and wouldn't run the bot's actual code. Streak was repositioned as a complementary tool for sanity checks and idea generation, not as a substitute.

**Intraday continuous pipeline** — a lighter, faster version of System 2 stages running every 30 minutes during market hours, since intraday signals decay too fast to wait for next-day batch processing.

**Stage 4b** — position review. Exits as first-class decisions, with thesis re-validation, position health scores, forced de-risking when bot-wide drawdown approaches breakers, and a graduation path for swing trades that should become long-term holds.

22 loopholes addressed across the four components.

## Phase 5: how it actually runs

Three components tying the system to operational reality.

**Orchestrator** — 12 scheduled tasks managed by a small state machine, not a workflow framework. Cron-style scheduling with explicit dependencies, retry policies, timeouts, and a global `bot_mode` flag (auto / paused / emergency_stop).

**Dashboard** — five views with explicit scope: Today, Approvals, Positions, Capital & Risk, System Health. Built on FastAPI + Jinja2 + HTMX; no React build pipeline, no separate frontend. The dashboard is for situational awareness and approval flow only — not for configuration or analytics.

**Operations** — single VM deployment, three systemd services, SQLite database, daily local backups plus weekly off-machine backups. Telegram bot for alerts (INFO/WARN/CRITICAL severity). Runbook as a living document. Broker-side stop-loss orders as the first line of protection (the bot's monitoring is the second line — never the only line).

17 loopholes addressed.

## What the design refused to add

A list of things explicitly rejected, with reasoning, in case they come up later:

- **Microservices** — single user, single host, no scaling requirement.
- **Kafka / RabbitMQ / Redis Streams** — three sequential tasks; database-as-queue is sufficient.
- **Kubernetes** — one VM, three services. Use systemd.
- **Airflow / Prefect / Dagster** — 12 scheduled tasks with linear dependencies. Cron-style supervisor is enough.
- **React / Vue / build tooling** — single user, simple status views. HTMX gets it done in a fraction of the code.
- **Prometheus / Grafana / OpenTelemetry** — one process. Logs + a few status tables suffice.
- **HashiCorp Vault / cloud secrets manager** — encrypted env file, OS keyring, and chmod 600.
- **Fibonacci retracement as a primary timing tool** — empirical evidence is weak. Structural levels (DMAs, swing pivots, breakouts, VWAP) chosen instead.
- **Bracket orders (BO)** — discontinued by Zerodha for retail; would limit flexibility anyway.
- **Shorting in v1** — long-only across all three tracks for v1. F&O shorting deferred to v2.
- **Live news sentiment scraping in v1** — too noisy for the value. Filing sentiment captures the most material news.
- **Tax modelling inside the bot** — bot tags every trade with intent and holding period; tax software handles reconciliation at year-end.

## What the design committed to

The architectural commitments that should not be reversed without serious cause:

- **Two-layer storage for the collector** (raw archive + normalised). Loss of raw archive is irrecoverable.
- **Point-in-time correctness for features.** Without this, backtests lie.
- **Same code for backtest and live.** Without this, "passes backtest" is meaningless.
- **HWM-anchored drawdown.** Without this, the kill switch loses meaning.
- **Hard intraday-square-off rule.** Without this, the worst behavioural failure mode of retail trading creeps in.
- **Broker-side stops as first line of protection.** Without this, a VM crash with open positions is catastrophic.
- **Attribution on every signal and decision.** Without this, learning is impossible.
- **Bot mode emergency_stop reachable in 5 seconds.** Without this, you have no escape valve when something goes wrong.

## Post-design review: 18 issues addressed

After the five-phase design was complete, a systematic architectural review identified 18 gaps — cases where contracts between components were broken, internal rules contradicted each other, or edge cases were under-specified. All 18 were addressed inline in the relevant phase documents. Recorded here for narrative continuity.

### Critical issues resolved

**F&O data not collected (Phase 2 → Phase 3 gap).** Phase 3's intraday track assigned 20% weight to F&O signals (OI build-up, max pain). Phase 2's collector had no F&O OI source at all — only static lot-size data. Fixed by adding NSE FO bhavcopy as a Category A daily source in Phase 2, with schema, freshness SLA, and backfill instructions.

**HWM mechanics contradiction.** Rule 1 stated "HWM only increases." The harvest formula and withdrawal rule both move HWM downward. Resolved by clarifying the distinction between performance updates (HWM increases only from trading gains) and capital-flow adjustments (HWM adjusts up or down proportionally to capital injections and withdrawals). These are not contradictory — they operate on different triggers. The order-of-operations on harvest day was also locked down.

**Capital state daily row vs intraday circuit breakers.** The capital state was defined as "one row per day" but intraday circuit breakers (daily loss limits, black-swan checks) need real-time P&L. Resolved by separating the daily ledger (written once at EOD) from the live capital view (computed on-demand from cash + current LTP × open positions). Pre-trade checks use the live view; the ledger is for audit and backtesting.

**No intraday regime downgrade.** The morning regime was locked for the full day. A -2.8% Nifty intraday drop (below the black-swan -3% threshold) would leave the system running bull_calm weights all day. Fixed by adding an intraday regime downgrade rule: if Nifty drops -1.5% intraday, the effective regime for intraday signals is downgraded to bear for the rest of the session.

**Pending resting orders excluded from concentration.** Stage 4 concentration checks counted current positions but ignored pending resting orders. Two resting orders for the same stock could fill simultaneously, producing a combined position exceeding the 5% cap. Fixed by including pending order values in all concentration calculations.

### Significant issues resolved

**Sector cap ambiguity.** Phase 1's Layer 3 description said "25% of long-term capital," Phase 1's risk config said "25% of total," and Phase 3's Stage 4 said "sector total ≤ 25%" with no qualifier. Resolved to: **25% of total capital across all tracks combined**, consistently in all three locations.

**Intraday square-off duplication.** Phase 4 described the executor auto-squaring off at 3:14 PM; Phase 5 described an orchestrator task at 15:14 doing the same thing. Resolved by clarifying the architecture: the executor provides a `square_off_all_intraday()` method; the orchestrator's `intraday_squareoff` task is the single trigger. Zerodha's MIS auto-square-off at 15:20 is the backstop.

**Signal cooldown applied to rejected/expired recommendations.** Cooldown was triggered on "recommendation generated," meaning a rejected recommendation blocked new signals for 30 days. Replaced with outcome-dependent cooldowns: rejected by operator → immediate reset; expired → 7-day soft cooldown; approved and position opened → full cooldown.

**Walk-forward validation data availability.** Walk-forward requires 4+ years of historical data with accurate `observed_at` timestamps. Price and F&O data is obtainable (NSE bhavcopies). Filing-based signal backfill uses approximation. A tiered validation approach was added: technical/F&O signals use walk-forward; filing-based long-term signals require 6 months of live paper trading as their validation gate.

**Survivorship bias not adjusted in acceptance criteria.** The design acknowledged a 3-5% annual upward bias from using the current Nifty 500 universe, then set a walk-forward Sharpe ≥ 1.0 threshold without adjusting for the bias. The threshold was raised to ≥ 1.3.

**"Intraday cannot kill long-term capital, ever" overstated.** Bucket isolation prevents direct capital transfer, but intraday losses do reduce total_capital, which counts toward the portfolio-level 8% HWM drawdown circuit breaker. That breaker affects all tracks. The documentation was corrected to accurately describe what bucket isolation does and does not protect against.

### Design gaps resolved

**PaperBroker price dependency undocumented.** PaperBroker needs live prices for realistic fill simulation. It depends on an injected `KiteBroker` price source and requires a valid Kite session. This dependency was made explicit in the broker abstraction design and deployment runbook.

**Data freshness: binary suppression vs. confidence degradation.** Phase 2 implied hard suppression for stale data; Phase 3's formula implied gradual degradation. Resolved with an explicit threshold: freshness_factor < 0.3 → full suppression; ≥ 0.3 → confidence degradation. Characteristic decay per source documented.

**Long-term health score time factor undefined.** "Time elapsed vs expected hold duration" is undefined for long-term positions with no fixed duration. For long-term, the time factor was replaced with a thesis freshness factor — days since the primary driving signal was last refreshed above threshold. Intraday and swing keep the time-based factor.

**Single Telegram alert channel for critical events.** Telegram is subject to outages and geographic blocks. Email added as a mandatory secondary channel for CRITICAL-severity alerts. INFO and WARN remain Telegram-only.

**SQLite write contention during market hours unacknowledged.** Multiple writers converge during intraday sessions. Added: WAL mode + `busy_timeout = 5s` on all connections; 3-second query timeout on the reconciliation loop; indexed hot-path queries; dashboard on a separate read connection.

**Operator modifications not re-validated.** A modification to a long-term recommendation (wider stop, larger position) could fail the RR gate or breach concentration, then be quietly rejected by the executor. Fixed: the dashboard re-runs Stage 3 and Stage 4 checks inline as the operator edits, with a live pass/fail UI and a disabled confirm button until all gates pass.

**Intraday auto-demote clock start undefined.** The 60-day clock now starts at the ₹2,50,000 milestone when intraday reaches its full allocation, with a minimum-30-trades requirement before auto-demote can fire.

---

## Pre-implementation decisions: 7 hard blockers resolved

A second architectural review identified 7 hard blockers — items that would stop implementation cold without a concrete decision. All 7 were resolved and incorporated into the relevant phase documents.

### GTT orders replace resting-order re-submission (Phase 4)

Standard Kite equity orders expire at market close. The original design assumed resting orders (swing pullback limits, breakout stop-buys) could sit pending for "days" — they cannot. Resolved by switching to **Kite/Fyers GTT (Good Till Triggered) orders** for all delivery entries and stop-loss/target management.

- GTT single-leg: entry orders that trigger when price reaches a level. Persist up to 365 days.
- GTT OCO (One Cancels Other): stop-loss + target pair on an existing holding. When one fires, broker cancels the other.
- GTT eliminates the need for daily re-submission of unfilled resting orders entirely.
- The executor's `gtt_orders` table tracks GTT lifecycle separately from regular orders.
- GTT reconciliation is daily (6 AM pre-market), not per-minute.

### Dual broker in v1: Kite (intraday) + Fyers (delivery) (Phase 4)

The broker abstraction was always designed for multi-broker. Both KiteBroker and FyersBroker are now v1 implementations, not deferred. Routing: intraday MIS orders → Kite; swing/long-term CNC orders and GTTs → Fyers.

Rationale: Fyers charges ₹0 for equity delivery (vs ₹20/order on Kite). At ₹50,000 capital with typical swing position sizes of ₹2,500–5,000, this saves 0.8–1.6% per delivery round trip. Fyers supports GTT/GTC orders natively. Kite is kept for intraday because of its tick feed reliability and established MIS infrastructure.

### FinBERT for local filing sentiment (Phase 2)

Filing sentiment (`sentiment_label`, `sentiment_confidence`) is computed using **ProsusAI/finbert** running entirely locally. No external API calls, no per-filing cost. Model weights (~440 MB) stored on the VM. CPU inference: ~80–120ms per filing. Batch processing at parse time (up to 32 filings per inference call). Filings with confidence < 0.60 are stored as `unclassified` and treated as neutral by the signal layer.

### Screener.in HTML scraping for quarterly financials (Phase 2)

Earnings quality signal requires structured financial data (revenue, OPM, CFO/PAT). Instead of parsing PDFs (complex, fragile), the design uses `pd.read_html()` on Screener.in's quarterly results tables. Screener.in has already parsed the exchange PDFs into clean HTML tables. Rate: 1 req/2s, off-peak only, triggered within 48 hours of a quarterly results filing appearing in the `filings` table.

### NSE bhavcopy for shares outstanding (Phase 2)

SAST Regulation 31 filings contain raw promoter share counts, not percentages. Promoter % requires total shares outstanding. Source: NSE CM Bhavcopy with Market Cap (published daily, includes TOTAL_SHARES column). Formula: `promoter_pct = SAST_promoter_shares / bhavcopy_TOTAL_SHARES`. Joined via ISIN.

### Instruments master table (Phase 2)

BSE uses scrip codes, NSE uses trading symbols, Kite uses numeric tokens, Fyers uses `NSE:SYMBOL-EQ` format. Without a cross-reference, joining BSE filing data to NSE price data is name-matching (fragile). Resolved by adding an `instruments` table populated from Kite's daily instruments CSV (includes ISIN → instrument_token mapping) plus NSE securities master (ISIN → NSE symbol). All collector joins go through this table.

### Forward-only migration pattern (Phase 5)

Schema evolution is managed via numbered SQL files in `migrations/` with a `schema_version` table. The migration runner executes at application startup and applies any unapplied migrations in order. Rules: files are immutable after application; all changes are additive; mistakes are fixed by new migrations, not by editing old ones. Rollback = restore the pre-deployment database backup + revert the code. SQLite's limited `ALTER TABLE` support makes forward-only cleaner than attempting bidirectional migration management.

---

## Statistics of the design

- **5 phases** completed end-to-end.
- **~50 explicit decisions** with reasoning recorded.
- **~40 loopholes** identified and addressed inline during design.
- **18 additional gaps** identified in first post-design review and addressed inline.
- **7 hard blockers** identified in second review and resolved before implementation.
- **~6-10 weeks** estimated implementation time for the full design.
- **2-3 weeks** estimated for a v0 (long-term track only, manual approval) if scoping down.

## What this design does not promise

It does not promise the bot will be profitable. No design can. What it promises is:

- The bot cannot suffer catastrophic loss without conscious operator override.
- Every decision is explainable and auditable.
- The architecture survives signal decay, parser bugs, broker outages, and source format changes.
- The operator can sleep through the night without checking the dashboard.
- The system will not silently degrade — when something is wrong, the operator will know.

These are sufficient. The rest is up to the quality of the signals encoded and the operator's discipline in following the rules the system enforces.

---

The design phase ended on a deliberate note: **slow is smooth, smooth is fast.** Every hour spent designing on paper saves three hours of refactoring code. The eventual implementation should now be mechanical translation, not exploratory discovery.
