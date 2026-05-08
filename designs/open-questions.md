# Open Design Questions

This document captures every question that remains unanswered before or during implementation. Questions are organized by phase, prioritized within each phase (must-resolve before coding that phase vs. can-decide-as-you-go), and structured with context so the decision can be made without re-reading the full phase document.

A question is removed from this list when a decision is made and recorded in the relevant phase document. Do not make decisions inline in this file — record them in the phase doc, then delete the question here.

---

## Pre-Implementation (must resolve before any code)

### Q0-1: SEBI algo trading registration

**Context:** SEBI's 2022 circular on algorithmic trading requires retail investors using broker APIs for automated trading to have their strategies registered and approved by their broker. Zerodha and Fyers both have registration processes for algo trading via Kite Connect / Fyers API.

**Question:** Has the operator completed algo trader registration with Zerodha and Fyers? What are the specific compliance requirements for this system's use case?

**Why it blocks:** Operating an automated trading system without completing broker registration is a regulatory violation. Account suspension is the likely consequence. This must be verified before placing any real orders.

**Action needed:** Contact Zerodha support for Kite Connect algo registration requirements. Contact Fyers for their equivalent process. Document the registration status in the runbook.

---

### Q0-2: Screener.in terms of service for automated scraping

**Context:** Phase 2 uses `pd.read_html()` on Screener.in for quarterly financials. Screener.in's ToS permits personal use but prohibits commercial redistribution. This system's use is personal (single operator, own money).

**Question:** Does Screener.in's current ToS permit automated scraping for personal algorithmic trading research? Is there a rate limit or robots.txt to observe?

**Action needed:** Review https://www.screener.in/about/legal/ and robots.txt. If scraping is borderline, consider Screener.in's registered API (they offer a free-tier API for registered users that is explicitly permitted). Document the decision.

---

### Q0-3: NSE/BSE filing scraping ToS risk

**Context:** NSE's terms of use historically prohibit automated scraping. The collector fetches BSE corporate filings, NSE corporate filings, bulk deals, and promoter disclosures via HTTP polling. This is automated, though at a very low rate (1 req/60-90s).

**Question:** Is the filing scraping defensible under NSE/BSE's current terms? Is there a licensed data feed alternative that would remove this risk?

**Options:**
- A: Continue with gentle scraping at current rates — low risk at this volume, personal use
- B: Use a licensed data vendor (Refinitiv, Bloomberg, or Indian providers like Tickertape API, StockEdge) — removes ToS risk but adds cost
- C: Use only SEBI's official public data feeds (SEBI EDGAR) which are unambiguously public — limited data

**Action needed:** Operator decision. Document the chosen approach in Phase 2 and the runbook.

---

### Q0-4: Fyers API vs Kite as primary tick-feed broker

**Context:** The design uses Kite's WebSocket tick feed for live price data (intraday signals, PaperBroker fills, live capital view LTP). Fyers also has a WebSocket feed. Currently, Kite is kept as the sole tick-feed provider because all three implementations (PaperBroker, intraday, live capital view) depend on it.

**Question:** If Fyers is handling delivery orders, do we need Kite running at all times just for the tick feed? Or should Fyers be the tick-feed provider with Kite as secondary?

**Implication:** If Kite session expires on a non-intraday day, does the tick feed (and thus the live capital view) fail even though no Kite orders are being placed?

**Recommended decision:** Keep Kite as the single tick-feed provider for v1. Kite's feed is more established. Accept that a valid Kite session is always required even when no intraday orders are placed. Document this dependency explicitly.

---

## Phase 1 — Capital and Risk

### Q1-1: Regime exposure scaling vs. bucket isolation

**Context:** Layer 1 portfolio risk defines regime-based exposure scaling (bull_calm: 100%, bull_volatile: 70%, sideways: 50%, bear: 30%). This applies to total portfolio exposure. But the long-term bucket alone is 70% of capital. In a `sideways` regime (50% max exposure), the long-term bucket would need to be partially in cash even if all long-term positions are healthy and the operator wants to hold them.

**Question:** Does regime scaling apply to (a) new entries only — no new positions in a bear/sideways regime beyond the cap; or (b) total deployed capital — existing positions must be partially liquidated if regime shifts and total exposure exceeds the cap?

**Recommended decision:** Apply to new entries only, not existing positions. Forcing liquidation of healthy long-term positions on a regime shift creates unnecessary churn and realised losses. The circuit breakers handle existing position risk. Document this as the explicit policy.

---

### Q1-2: Tax fund source in the self-funding flow

**Context:** Loophole 3 in Phase 1 says "Reserve 25% of net profit annually in a separate tax fund." The self-funding flow only allocates harvest to `ops_fund` and `dev_fund`. The tax fund is not part of the harvest calculation.

**Question:** Where does the tax fund come from?

**Options:**
- A: Deducted from owner withdrawal (owner receives net-of-tax amount)
- B: Deducted from harvest before ops_fund/dev_fund split — becomes a third harvest destination
- C: Operator manually sets aside 25% of net annual P&L outside the bot's accounting

**Action needed:** Operator decision. Update the self-funding flow section in Phase 1 once decided.

---

### Q1-3: Paper trading 90-day comparison — decision threshold

**Context:** After 90 days of paper trading alongside real trading, "comparison reports surface which tracks have actual edge." No threshold is defined for what constitutes "edge."

**Question:** What metric and minimum value makes a paper-trading track eligible for real-money scaling?

**Suggested threshold:** Paper track must show: (a) positive cumulative P&L, AND (b) expectancy (avg win × win rate − avg loss × loss rate) > 0, AND (c) at least 20 completed paper trades. If fewer than 20 trades, extend the paper period until 20 trades accumulate regardless of calendar days.

**Action needed:** Decide the threshold, add it to Phase 1.

---

## Phase 2 — Collector

### Q2-1: FinBERT model variant

**Context:** Phase 2 specifies `ProsusAI/finbert`. Alternative: `yiyanghkust/finbert-tone` (three classes: positive/negative/neutral with slightly different training data). A third option: fine-tune FinBERT on a sample of BSE/NSE filing headlines for better Indian corporate language accuracy.

**Question:** Is `ProsusAI/finbert` sufficient, or should we evaluate `yiyanghkust/finbert-tone` or a fine-tuned variant before deployment?

**Recommended decision:** Use `ProsusAI/finbert` for v1. Run both models on 200 manually-labelled BSE filings before deployment and pick the higher-accuracy one. Fine-tuning is a v2 task — requires labelled training data that will only be available after 6+ months of operation.

---

### Q2-2: Exact URL and file format for NSE CM Bhavcopy with Market Cap

**Context:** The design references "NSE CM Bhavcopy with Market Cap" as the source for TOTAL_SHARES (total issued capital). The standard NSE CM bhavcopy does NOT include this column.

**Question:** What is the exact NSE URL, filename pattern, and column name for total shares outstanding?

**Known information:** NSE publishes under Equity Archives → Bhav Copy multiple files. The file containing market cap data is published as `"bhav copy with delivery data"` or the `"NSE Market Capitalisation Data"` file. Verify: does this file include total issued capital or only market cap (from which shares = mktcap/close)?

**Action needed:** Download and inspect the actual NSE files before implementing the `shares_outstanding` fetcher. Confirm column name and update the schema if the source uses market cap rather than direct share count.

---

### ~~Q2-3: Intraday minute-bar data source for backtesting~~ ✓ RESOLVED

**Decision (2026-05-09):** Option C — organic accumulation via Kite tick feed into date-partitioned parquet files. No vendor cost; Kite/Fyers provide up to 60-day rolling window via API, but this window is insufficient for walk-forward backtesting. The collector writes minute bars to parquet from day one of operation. After ~12 months, the intraday track becomes walk-forward testable. In the interim, paper trading is the validation gate. `prices_minute` table is parquet-only (never SQLite). Documented in `designs/phase-2-collector.md`.

---

### Q2-4: Screener.in update trigger — how quickly after results filing?

**Context:** Screener.in is scraped within 48 hours of a `quarterly_results` filing appearing. But Screener.in itself may take 24-72 hours to parse and display the results after announcement.

**Question:** Is 48-hour scraping delay acceptable, or does the timing of `earnings_quality` features need to be faster?

**Recommended decision:** 48-hour delay is fine for the long-term signal track. Earnings quality is a slow-moving feature; a 2-day lag is immaterial for a 6-month investment thesis. Document the known lag in the feature's `source_max_observed_at` so backtests correctly model this delay.

---

## Phase 3 — Brain

### Q3-1: Complete regime taxonomy — unclassified VIX states

**Context:** The four regime conditions leave a gap: if Nifty is above 200 DMA, VIX is between 50th–70th percentile (not "bottom 50%" for bull_calm, not "top 30%" for bull_volatile), and breadth is above 60%, no regime condition matches. The current fallback (compute failure → bear) is incorrectly applied to a normal healthy-market state.

**Question:** What regime should apply when Nifty is above 200 DMA but VIX is in the 50–70th percentile range?

**Recommended fix:** Extend `bull_volatile` definition to cover this gap: `bull_volatile` = Nifty above 200 DMA AND (VIX ≥ 50th percentile OR breadth 40-60%). This makes bull_calm = Nifty above DMA + VIX bottom 50% + breadth ≥ 60%, and everything above-DMA that isn't bull_calm becomes bull_volatile.

**Action needed:** Update Stage 1 regime definitions in Phase 3 with exhaustive coverage (each condition maps to exactly one regime).

---

### Q3-2: Same-day filing events — morning batch update policy

**Context:** The morning batch runs at 7:00-7:15 AM using yesterday's features. A material filing arriving at 11 AM (promoter buys large block) would not influence long-term or swing signals until the next morning's batch. The intraday continuous pipeline doesn't run long-term/swing signals.

**Question:** For material intraday filings (promoter activity, auditor change, fraud disclosure), should the system trigger a partial morning batch re-run at midday?

**Options:**
- A: Accept the lag — filing-based signals update once per day only. Morning batch uses end-of-previous-day features. This is simple and avoids mid-session signal churn.
- B: Add a "material filing trigger" — if a filing with red-flag categories arrives during market hours, immediately re-evaluate Stage 4b exit recommendations for affected positions (but don't generate new entries mid-session).
- C: Full mid-session re-run of long-term and swing signals when a material filing arrives.

**Recommended decision:** Option B — mid-session re-evaluation of Stage 4b exits only for red-flag filings (auditor change, fraud, pledging spike, promoter large sell). New entries wait for next morning batch. This balances responsiveness with stability.

**Action needed:** Update Phase 3 Stage 4b and Phase 4 intraday continuous pipeline sections.

---

### Q3-3: Feature store critical indexes

**Context:** The feature store will grow to ~15 million rows over 3 years (40 features × 500 stocks × 250 days × 3 years). Morning batch query pattern: for each stock, get all current features. Without indexes, this is a full-table scan on every stock × every batch run.

**Question:** Which indexes must be defined in `0001_initial_schema.sql` (or the migration that creates the feature store)?

**Required indexes:**
```sql
-- Primary query pattern: latest feature for a stock on a given date
CREATE INDEX idx_features_stock_name_valid
  ON features(stock_symbol, feature_name, valid_from DESC);

-- Point-in-time query: exclude features from the future
CREATE INDEX idx_features_observed
  ON features(source_max_observed_at);

-- Bulk query for all features for a stock (morning batch)
CREATE INDEX idx_features_stock_valid
  ON features(stock_symbol, valid_from DESC);
```

**Action needed:** Add these indexes to the initial migration file. Verify with `EXPLAIN QUERY PLAN` on the morning batch query pattern before deployment.

---

### Q3-4: Per-track confidence haircut — data model for recalibration

**Context:** The 30% confidence haircut (`p_win = confidence × 0.7`) is currently a hardcoded constant in Stage 3. The design says to recalibrate per-track after 60 days of live trading based on actual backtest-vs-live decay. But the recalibrated values need to live somewhere.

**Question:** Where does the per-track haircut value live in the data model, and what is the recalibration process?

**Recommended approach:** Add `live_backtest_ratio_long_term`, `live_backtest_ratio_swing`, `live_backtest_ratio_intraday` fields to `risk_config` (versioned table). Initial values: 0.70. After 60 live trades per track, compute `actual_win_rate / backtest_predicted_win_rate` and update the ratio. Stage 3 reads from `risk_config` rather than using the hardcoded 0.7.

**Action needed:** Add these fields to the `risk_config` schema in Phase 1, and update Stage 3's EV calculation to use the config value.

---

### Q3-5: Swing graduation mechanics — executor operations

**Context:** When a swing position graduates to long-term, the system "reclassifies with new stop/target/risk parameters." The operational steps are unspecified.

**Question:** What exact executor operations does graduation require?

**Required steps:**
1. Cancel the existing swing GTT OCO (stop-loss + target legs)
2. Place a new long-term GTT OCO with the new stop (3× ATR instead of 2× ATR) and new target (2R+ from current price)
3. Update `positions` table: `track = long_term`, `bucket_id = long_term_bucket`
4. Debit `swing_deployed` and credit `long_term_deployed` in the capital state
5. Check long-term bucket has capacity for this position. If not, graduation is blocked until capacity exists.

**Action needed:** Document these as a `graduate_position()` executor method in Phase 4. Include the capacity-check gate.

---

## Phase 4 — Executor and Backtesting

### Q4-1: LTP source for the live capital view

**Context:** The live capital view formula (`live_total_capital = total_cash + Σ position × LTP`) requires current market prices. Two sources: (a) Kite WebSocket tick feed (updates every 200ms per subscribed symbol), (b) reconciliation loop's broker position poll (updates every 60s from Fyers/Kite REST APIs).

**Question:** Which source is authoritative for the live capital view used in pre-trade checks and intraday circuit breakers?

**Recommended decision:** Kite tick feed is authoritative. The executor maintains an in-memory `{symbol: last_tick_price}` dictionary updated by `on_tick()` callbacks. The live capital view reads from this dictionary. If a symbol has no recent tick (data gap > 5 minutes), fall back to the last reconciliation price from the broker REST call. This gives real-time accuracy with a clean fallback.

**Action needed:** Document this in Phase 4 executor section with the 5-minute staleness threshold.

---

### Q4-2: Trailing stop behavior in paused mode

**Context:** When `bot_mode = paused`, scheduled tasks don't run. A position that moves 5% in the operator's favour while paused will have an un-trailed GTT OCO stop — potentially giving back gains.

**Question:** In `paused` mode, should trailing stop updates continue?

**Options:**
- A: Yes — trailing stop updates are risk management, not new decision-making. They should continue even when paused. Only new entries and signal generation are paused.
- B: No — "paused means paused." Operator accepted this risk when pausing. Broker-side stops protect against catastrophe.

**Recommended decision:** Option A. Trailing stops should continue in `paused` mode. Add a fourth task state to the orchestrator: `trailing_stops_only` tasks that run regardless of `bot_mode`. In `emergency_stop`, nothing runs (not even trailing stops — operator has assessed the situation and decided).

**Action needed:** Update Phase 4 executor and Phase 5 orchestrator `bot_mode` semantics.

---

### Q4-3: GTT reconciliation — Fyers GTC order mapping

**Context:** Fyers doesn't use the term "GTT" — they use "GTC" (Good Till Cancelled) or "Super Order." The broker abstraction maps these to the same GTT interface.

**Question:** Does Fyers' GTC/Super Order support the same single-leg and OCO semantics as Kite's GTT? Are there differences in how modify/cancel work?

**Action needed:** Test Fyers API GTC order placement and OCO support in a paper trading environment before relying on it for live delivery stops. Document any behavioral differences in FyersBroker implementation notes.

---

### Q4-4: Kite Connect rate budget for 500-stock morning batch

**Context:** The morning batch fetches historical OHLCV for all Nifty 500 stocks. Kite's rate limit is 3 requests/second for most endpoints. At 1 request per instrument: 500 stocks / 3 req/s = ~167 seconds. The `morning_batch_features` task has a 10-minute timeout.

**Question:** Does Kite Connect support multi-instrument historical data requests (batch fetch), or is it one instrument per request?

**Action needed:** Check Kite Connect's `/instruments/historical` API. If batch requests are supported, use them to fetch 50-100 instruments per request. If not, implement a connection pool with respect to the 3 req/s rate limit and verify the batch completes within the 10-minute window.

---

## Phase 5 — Orchestrator, Dashboard, Operations

### Q5-1: WebSocket service architecture

**Context:** Phase 5 lists `boomer-websocket.service` as a systemd service. Its architecture is unspecified. The executor needs live tick data via `on_tick()` callbacks.

**Question:** Is the WebSocket tick client a separate process (service) or in-process within the orchestrator/executor?

**Options:**
- A: In-process — the executor owns the Kite WebSocket connection. `on_tick()` is a callback that updates the in-memory LTP dictionary directly. No separate service.
- B: Separate process — a minimal WebSocket client writes ticks to a shared table (e.g., `live_ticks` SQLite table). Executor reads from the table. This provides tick persistence but adds write overhead.

**Recommended decision:** Option A. In-process is simpler. The executor is the right owner — it needs LTPs for circuit breakers and reconciliation. A separate service adds complexity (IPC, process restart coordination) for no benefit at this scale. Rename `boomer-websocket.service` to be the `boomer-executor.service` (the executor process handles its own WebSocket connection). Document this in the deployment shape.

**Action needed:** Update Phase 5 deployment shape.

---

### Q5-2: Rollback procedure for database-changing deployments

**Context:** The migration pattern is forward-only. Rolling back requires restoring the pre-deployment database backup and reverting the code. The runbook mentions backup verification but not an explicit rollback checklist.

**Question:** What are the exact steps to rollback a bad deployment that included a schema migration?

**Recommended checklist (add to runbook):**
1. Switch bot_mode to `emergency_stop`
2. Verify all intraday positions are closed (market hours check)
3. Copy current database to `/var/lib/boomer/rollback-attempt-YYYYMMDD.db`
4. Restore pre-deployment backup: `cp backups/YYYY-MM-DD.db boomer.db`
5. Revert code: `git checkout <previous-commit>`
6. Restart services
7. Verify schema_version shows the expected migration level
8. Run reconciliation manually; verify positions match broker

**Action needed:** Add this checklist to the runbook section in Phase 5.

---

### Q5-3: Fyers token daily refresh

**Context:** Kite Connect tokens expire daily and are refreshed at 9:00 AM IST. Fyers API tokens also expire daily (24-hour OAuth2 tokens).

**Question:** Is the Fyers token refresh automated in the same 9:00 AM `pre_market_executor_setup` task, or does it require a separate flow?

**Note:** Fyers OAuth2 uses a redirect URL for initial authentication (browser-based). Daily refresh may require a different approach — Fyers provides a method to generate a new token from the previous refresh token or via a server-side flow. Clarify whether this can be automated (no browser required) or requires a daily manual step.

**Action needed:** Test Fyers token refresh in isolation. If fully automatable, add to `pre_market_executor_setup`. If not, document the manual step and alert if refresh fails.

---

### Q5-4: Dashboard deployment update — Fyers credentials

**Context:** The operations security section covers Kite credentials. Fyers now requires a second set of API credentials (app_id, secret_key, access_token).

**Question:** Are Fyers credentials handled the same way as Kite credentials (encrypted env file, decrypted at startup)?

**Recommended decision:** Yes — add `FYERS_APP_ID`, `FYERS_SECRET`, and `FYERS_ACCESS_TOKEN` to `secrets.env` alongside Kite credentials. Same encryption and access model. Update the secrets management section in Phase 5.

---

## Recurring questions (revisit after 60 days of live operation)

These are not blockers but are time-gated — they can only be answered after accumulating live trading data:

| Question | When to revisit | What to measure |
|----------|----------------|-----------------|
| 30% confidence haircut per track — is it accurate? | 60 days of live trading per track | `actual_win_rate / backtest_predicted_win_rate` per track |
| 3% harvest threshold — is it too high/too low? | 6 months of live P&L | Standard deviation of weekly P&L; set threshold to 1 SD |
| FinBERT accuracy on Indian filings | After 200 manually-reviewed predictions | Precision/recall per sentiment class |
| Intraday auto-demote: is 30 trades enough? | At 30-trade mark | If win rate clearly negative, fire early; if borderline, extend |
| Regime weights — do they actually improve returns? | 1 year of live data | Compare same-signal returns bucketed by regime |
| Paper trading comparison — which tracks have edge? | 90 days paper + 30 trades per track | Expectancy, Sharpe vs. each other |
