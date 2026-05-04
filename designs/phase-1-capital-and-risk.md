# Phase 1 — Capital and Risk Framework

## Goal

The capital and risk framework is the source of truth for "how much money is where" and "what trades are currently allowed." It gates every trade decision the system makes.

Every other component asks this framework two questions: **how much can I deploy?** and **am I allowed to deploy right now?** Those two questions, answered correctly, are the difference between a bot that survives and one that does not.

## Inputs and outputs

**Inputs:**
- Realised P&L from closed trades (from the executor)
- Current open position values (continuously updated)
- Today's market regime (from the regime detector)
- System clock
- Manual capital injections or withdrawals (rare, operator-driven)

**Outputs:**
- *Capital state* — snapshot of total, by-bucket allocation, deployment, HWM, drawdown
- *Trade permission* — yes/no with reason, including computed position size on yes
- *Bucket budget* — for any track, how much risk capital is available now
- *Health status* — which circuit breakers are armed or tripped

## Capital allocation

### v1 allocation

- **Long-term: 70%**
- **Swing: 15%**
- **Intraday: 15%**

This was settled after extended discussion. The original proposal was 50/40/10 (swing/intraday/long-term); challenged on the basis that 90% of capital was being allocated to tracks with structurally weak retail edge. The synthesis is to use real money cautiously while paper-trading at full volume to build evidence.

### Initial allocation (until ₹2,50,000 capital)

- **Long-term: 80%**
- **Swing: 15%**
- **Intraday: 5%**

The reduced intraday allocation at the starting capital level reflects a granularity issue: at ₹50,000 capital with 5% intraday allocation, the intraday bucket has ₹2,500 to work with, which permits only very small positions. The 15% allocation kicks in at the milestone capital.

### Paper-trading parallel

All three tracks generate signals at full volume regardless of real-money allocation. Paper-traded signals run through the same code with realistic fill assumptions and accumulate as data points. After 90 days of paper trading, comparison reports surface which tracks have actual edge before scaling real money.

### Intraday auto-demote rule

If after 60 days of real intraday trading the track shows negative cumulative P&L, the system auto-switches intraday to paper-only and pings the operator for review. The operator can override and reinstate real intraday, but the default is "evidence required."

**Clock start:** The 60-day clock starts at the ₹2,50,000 capital milestone — when intraday reaches its full 15% allocation. Trades before that milestone use only 5% allocation and represent a different risk profile; including them in the evaluation would produce a misleading comparison. Additionally, the evaluation requires a minimum of 30 completed intraday trades (not just 60 calendar days) before auto-demote can fire. Under 30 trades, the sample is too small and the system continues with a WARN alert instead.

### Scale invariance

No rupee amounts are hardcoded. Every threshold is a percentage or a function of capital. The system works identically at ₹50,000 or ₹50,00,000 — only the absolute numbers scale.

## Bucket isolation

Each bucket's capital is isolated. If intraday loses ₹500, that ₹500 is gone from the intraday bucket *until the operator does an explicit rebalance*. Swing does not lend money to intraday to keep the lights on.

This means **intraday losses cannot directly transfer money out of the long-term bucket**. However, bucket isolation does not prevent intraday losses from contributing to portfolio-level drawdown. Intraday P&L reduces `total_capital`, which is the denominator for the Layer 1 kill switch (8% drawdown from HWM). A sustained run of intraday losses can exhaust the portfolio drawdown budget and trigger a full shutdown — affecting long-term entries too. Bucket isolation protects long-term *capital*, not long-term *trading activity*, in a prolonged intraday drawdown scenario.

Quarterly rebalance schedule: every three months, the operator reviews bucket performance and decides whether to adjust allocations. Never automatic, never within a quarter.

## The capital state object

The capital state exists in two forms with distinct responsibilities:

### Daily ledger (one row per trading day)

Written once per day at end-of-day reconciliation. Source of truth for audit, reporting, and backtesting.

| Field | Purpose |
|-------|---------|
| `as_of_date` | Trading date this row represents |
| `total_capital` | Sum of cash + market value of all open positions at EOD |
| `total_cash` | Uninvested cash across all buckets |
| `long_term_allocated_pct` | Configured allocation (e.g., 0.80) |
| `swing_allocated_pct` | Same, for swing |
| `intraday_allocated_pct` | Same, for intraday |
| `long_term_deployed` | Rupee value of long-term open positions at EOD |
| `swing_deployed` | Same, for swing |
| `intraday_deployed` | Same, for intraday |
| `high_water_mark` | HWM at end of this day (after any harvest adjustment) |
| `eod_drawdown_pct` | (HWM − total_capital) / HWM at EOD |
| `consecutive_loss_days` | Days in a row with net negative P&L (after costs) |
| `peak_date` | Date HWM was set |

### Live capital view (computed on-demand, not stored)

Pre-trade checks and intraday circuit breakers **must not use the daily ledger row**, because that reflects yesterday's close. Instead they use a live view computed at query time:

```
live_total_capital = total_cash
                   + sum(position.quantity × current_LTP
                         for all open positions)

live_drawdown_pct = (hwm_from_latest_ledger_row - live_total_capital)
                    / hwm_from_latest_ledger_row

live_intraday_pnl = sum(realised_pnl for intraday trades today)
                  + sum((current_LTP - entry_price) × quantity
                        for open intraday positions)
```

`current_LTP` is fetched from the executor's last-known price (updated from broker tick feed). Pre-trade checks query this view, not the daily ledger. This ensures intraday circuit breakers (daily -2% total, intraday daily loss limit) reflect actual intraday movement in real time.

What's deliberately not stored: rupee amounts as risk limits. Those live in a separate `risk_config` table because they are percentages applied dynamically.

## The risk config (versioned, rarely changes)

| Field | Initial value | Purpose |
|-------|---------------|---------|
| `risk_per_intraday_trade_pct` | 0.5% of intraday bucket | Position sizing input |
| `risk_per_swing_trade_pct` | 1.0% of swing bucket | Position sizing input |
| `risk_per_long_term_trade_pct` | 1.0% of long-term bucket | Position sizing input |
| `intraday_daily_loss_limit_pct` | 2.0% of intraday bucket | Circuit breaker |
| `swing_weekly_loss_limit_pct` | 4.0% of swing bucket | Circuit breaker |
| `portfolio_daily_loss_limit_pct` | 2.0% of total | Portfolio kill switch |
| `portfolio_weekly_loss_limit_pct` | 4.0% of total | Portfolio kill switch |
| `portfolio_max_drawdown_pct` | 8.0% from HWM | Total shutdown |
| `single_stock_cap_pct` | 5.0% of total | Concentration |
| `sector_cap_pct` | 25.0% of total capital (all tracks combined) | Concentration |
| `intraday_consecutive_loss_count` | 3 | Daily auto-pause |
| `swing_30d_loss_count` | 4 | Track pause |
| `nifty_intraday_pause_pct` | 3.0% | Black swan |

Versioned because changes need to be traceable: "from when was this value in effect?" matters for backtest correctness.

## The four-layer risk model

### Layer 4 — Trade risk (foundation)

Every trade has three numbers set *before* the trade is taken: entry, stop-loss, target.

**Position sizing formula:**

```
position_size_in_shares = (bucket_capital × risk_per_trade_pct) / (entry_price - stop_loss_price)
```

This is the most important formula in the entire system. Position size is *derived* from risk tolerance and stop distance, never set arbitrarily.

**ATR-based stop placement:**

```
stop_distance = k × ATR_14d
```

- Intraday: k = 1.0 to 1.5
- Swing: k = 2.0
- Long-term: k = 3.0 or thesis-based (e.g., below 200 DMA, promoter sells)

ATR-based stops make risk consistent across stocks of different volatility profiles.

**Reward-to-risk gate:**

```
reward_to_risk = (target - entry) / (entry - stop)
```

Hard rule: do not take trades with reward-to-risk below 1.5 for swing/intraday, below 2.0 for long-term. This eliminates a huge category of bad trades.

**Expected value gate (after 30% confidence haircut):**

```
p_win = signal_confidence × 0.7
EV = (p_win × reward_after_costs) - (p_loss × risk_after_costs)
```

If EV ≤ 0 after the haircut and costs, skip the trade.

### Layer 3 — Concentration risk

Hard caps, computed every day before generating signals:

- **Single stock:** ≤ 5% of total capital, regardless of conviction
- **Sector:** ≤ 25% of **total** capital (not long-term bucket — all tracks combined)
- **Correlation cluster:** sum of cluster exposures ≤ 35%; cluster = stocks with > 0.7 correlation over last 90 days
- **Open positions count:** 8-15 positions

When a new signal would breach any cap, the recommendation is automatically demoted to "watchlist" instead of "approve."

**Pending resting orders are included in concentration calculations.** A resting limit order (e.g., a swing pullback limit or a breakout stop-buy) counts as reserved capital for concentration purposes. The check is:

```
current_position_value
+ sum(pending_order_value for all pending orders for same stock)
+ proposed_trade_value
≤ 5% of total_capital
```

This prevents two resting orders for the same stock both filling simultaneously and producing a combined position exceeding the cap. Stage 4 (portfolio constructor) and the executor's pre-trade safety check both enforce this. On fill of any pending order, concentration is re-evaluated; any further pending orders for that stock are cancelled if the cap would be breached post-fill.

### Layer 2 — Strategy / track risk

Each track has its own capital bucket and rules, plus per-track daily/weekly limits and decay tracking.

- **Intraday daily loss limit:** 2% of intraday bucket → no more intraday trades that day
- **Swing weekly loss limit:** 4% of swing bucket → swing pauses for the rest of the week, only stop-losses fire
- **Long-term:** no daily/weekly limit; monthly review at 8% drawdown

Per-track rolling 30-trade win rate is tracked. If win rate drops by >15 percentage points from baseline, the track auto-pauses for review.

### Layer 1 — Portfolio risk (the kill switch)

The outermost layer. These rules can shut down the entire bot.

- **Daily loss limit:** -2% total in one day → bot disables for the day
- **Weekly loss limit:** -4% in 5 trading days → bot pauses, only stops fire, manual review
- **Monthly loss limit:** -8% drawdown from HWM → full shutdown, operator decides resume

Plus a black-swan protocol: Nifty -3%+ intraday → pause new orders, existing stops still fire, alert operator.

Plus regime-based exposure scaling:
- Bull calm: 100%
- Bull volatile: 70%
- Sideways: 50%
- Bear: 30%

## High-water mark mechanism

### Rules

1. **HWM increases only from trading performance.** Calculated end of day after positions are marked-to-market. If today's `total_capital` > current HWM → update HWM, record `peak_date`. Otherwise no change from performance alone.

2. **HWM adjusts for capital flows — both up and down.** When ₹10,000 is injected, HWM also increases by ₹10,000. When the operational fund withdraws ₹2,000, HWM *decreases* by ₹2,000. HWM measures *performance*, not *wealth*. Capital-flow adjustments are not "resets" — they correct the baseline so that drawdown continues to measure trading performance only.

   **Note on harvest:** The harvest formula `new_HWM = total_capital - harvest_amount` is a capital withdrawal adjustment, not a performance reset. The mechanics are consistent with Rule 2: money leaves the portfolio, so the HWM baseline decreases by the same amount. The *performance* component of HWM (the distance from the original starting capital) is preserved.

3. **Drawdown is always from HWM, never from yesterday.** This prevents anchoring drift.

4. **HWM does not reset.** Even if the bot is closed for six months and restarted, HWM stays. It is the all-time high-water mark adjusted for all capital flows since inception.

5. **HWM update order-of-operations on harvest day:** First, update HWM for any intraday performance (`total_capital > HWM`); then apply the harvest adjustment (`HWM -= harvest_amount`). Never apply harvest before the performance update, or the drawdown calculation for the week is wrong.

## Self-funding flow

### Trigger

Every Friday after market close, evaluated. Conditions for harvest to fire (all must be true):

1. Total capital > HWM (new peak this week)
2. Excess = (total capital − HWM) ≥ 3% of HWM
3. No circuit breaker tripped this week

### Action when triggered

```
excess = total_capital - HWM
harvest_amount = 0.5 × excess
ops_fund += 0.6 × harvest_amount
dev_fund += 0.4 × harvest_amount
new_HWM = total_capital - harvest_amount
```

### The three funds

**Operational fund** — pays running costs (cloud, data, broker fees). Target balance: 6 months of runway. Auto-pays monthly expenses. Overflow rule: if ops fund > 12 months runway, excess flows to owner withdrawal.

**Development fund** — system upgrades (better data sources, paid APIs, premium tools). No target balance; "spend when needed." Withdrawals require explicit decision.

**Owner withdrawal** — real returns to the operator. Eligibility: only after ops fund is at 12-month runway AND a defined milestone is hit. Annual milestone reward: 5% of net profit, capped at an operator-set absolute amount, withdrawn each year on the bot's anniversary as a discipline marker.

This aligns incentive perfectly: the bot has to be reliably profitable before the operator sees a rupee. Until then, all harvest goes to system sustenance.

## Circuit breaker reset semantics

| Breaker | When it trips | When it auto-resets | Manual override needed? |
|---------|---------------|---------------------|--------------------------|
| Intraday daily loss | Realised intraday P&L below limit | Tomorrow at market open | No |
| Intraday consecutive losses | 3 losing intraday trades today | Tomorrow at market open | No |
| Intraday late-entry | Time > 2:30 PM | Tomorrow at market open | No |
| Swing weekly loss | Realised swing P&L this week below limit | Following Monday | No |
| Swing 30-day loss count | 4 losing swing trades in last 30d | When 30d window has < 4 losses | No |
| Sector concentration | Sector value > 25% of total | When concentration drops naturally | No |
| Black swan (Nifty -3%) | Nifty intraday move > 3% | Manual resume only | **Yes** |
| Portfolio 8% drawdown | DD from HWM > 8% | Never auto-resets | **Yes** |

Stricter breakers require human judgment to resume. Operational pauses (auto-recoverable) are distinct from structural pauses (need a human).

## Pre-trade check order

For any proposed trade, this list runs top to bottom. Any FAIL rejects the trade.

```
1. Bot in shutdown state (drawdown > 8%, manual disable)?
2. Trade's track in cooldown?
3. Regime gate open for this track?
4. Black swan check (Nifty intraday move > 3%)
5. Capital availability in bucket
6. Concentration checks (single stock, sector, correlation)
7. Trade-quality checks (RR threshold, EV ≥ 0, confidence threshold)
8. Time checks (market open, intraday before 2:30 PM)

If all pass → APPROVE with computed position size
```

Order is intentional: cheap checks (boolean state lookups) first, expensive checks (correlation analysis) last. Failing fast saves compute.

## Loopholes and decisions

### Loophole 1: 3% harvest threshold is arbitrary

**Decision:** Use 3% for v1; plan to make it self-calibrating later (e.g., 1 standard deviation of weekly P&L over last 12 weeks).

### Loophole 2: Bucket allocations don't auto-rebalance intraday

**Decision:** Rebalance only at quarterly review or when explicitly triggered. Documented as deliberate to avoid churn and tax events.

### Loophole 3: System doesn't account for taxes

**Decision:** Reserve 25% of net profit annually in a separate "tax fund." Don't model taxes inside the bot — that's a tax software problem.

### Loophole 4: Capital injection HWM update

**Decision:** When capital is injected mid-period, `new_HWM = old_HWM + capital_injection`. This avoids "earning credit" for fresh capital as if it had performed.

### Loophole 5: "Consecutive_loss_days" counter definition

**Decision:** Use net P&L (after all costs). Anything else flatters the system.

### Loophole 6: Black swan path-dependence

**Decision:** Once tripped intraday, the breaker stays tripped for the rest of the day, regardless of recovery. Manual resume next day.

### Loophole 7: HWM harvest direction contradiction

**Decision:** Rule 1 ("HWM only increases from performance") and the harvest adjustment (`new_HWM = total_capital - harvest_amount`) are not contradictory — the harvest is a capital withdrawal, not a performance event. The order of operations on harvest Friday: (1) mark-to-market total_capital; (2) if total_capital > HWM, set HWM = total_capital (performance update); (3) compute harvest; (4) subtract harvest_amount from both total_capital and HWM (capital withdrawal adjustment). The net effect: HWM stays at or above the pre-harvest level net of the capital that left.

### Loophole 8: Pre-trade check uses daily ledger (stale intraday values)

**Decision:** Pre-trade checks use the *live capital view* (total_cash + sum of open positions × current LTP), not the daily ledger row. The daily ledger row reflects yesterday's close and cannot be used for intraday circuit-breaker decisions. The live capital view is a computed query, not a persisted row, and is evaluated fresh on each pre-trade check call.

### Loophole 9: Pending resting orders inflate available capital

**Decision:** Concentration checks (Layer 3) count pending resting orders as reserved capital. A resting order is treated identically to an open position for concentration purposes from the moment it is submitted until it fills, expires, or is cancelled. See Layer 3 section for the exact formula.

## Worked example with starting numbers

Starting capital ₹50,000. Allocation 80/15/5.

```
Day 0:
total_capital = 50,000
long_term_bucket = 40,000
swing_bucket = 7,500
intraday_bucket = 2,500
HWM = 50,000
drawdown = 0%

Day 1 — intraday signal (stock at ₹500, stop ₹490, target ₹520):
RR = 20/10 = 2.0 ✓
risk_per_trade = 0.5% × 2,500 = ₹12.50
position_size = 12.50 / 10 = 1.25 → 1 share
position_value = ₹500
concentration = 500/50,000 = 1% ✓
Trade approved at 1 share.

Day 1 close — stop hit:
Loss = 10 + costs ≈ ₹15
total_capital = 49,985
HWM unchanged at 50,000
intraday_realised_pnl_today = -15
intraday_losing_trades_today = 1

Day 5 — three intraday losses today:
intraday_consecutive_loss_breaker → TRIPS
No more intraday trades today.

Day 30 — swing position closed for ₹600 profit:
total_capital = 50,450
HWM updated to 50,450 (new peak)

Day 90 — sustained gains:
total_capital = 55,000
HWM was 51,800 (set previously)
excess = 3,200 = 6.2% of HWM > 3% trigger
harvest_amount = 1,600
ops_fund += 960 (0.6 × 1,600)
dev_fund += 640 (0.4 × 1,600)
new total_capital = 53,400
new HWM = 53,400

Bot has self-funded ₹1,600 of operations and upgrades.
The remaining ₹1,600 stays compounding.
```

## What this design buys

1. **Cannot lose more than 8% from peak without manual intervention.** The system is structurally incapable of catastrophic loss.

2. **Every decision produces structured data ready for ML training.** Capital state, risk config, breaker status, decision reasoning all logged.

3. **System self-funds its own operations.** Once profitable, it pays for itself. Owner withdrawal only after operational sustenance is secured.

## Stop conditions for Phase 1 (all met)

- Capital allocation policy defined
- Capital state object schema locked
- Risk config schema locked (versioned, percent-based)
- All circuit breakers and thresholds defined
- HWM mechanism rules locked
- Self-funding flow specified end-to-end
- Paper trading parallel structure defined
- Worked numerical example traced
- 6 loopholes identified with decisions
- Intraday auto-demote-to-paper rule added
