# Phase 3 — System 2 Brain

## Goal

System 2 is the reasoning layer. It takes the collector's normalised data, computes features with point-in-time correctness, detects market regime, generates timeframe-specific signals with full attribution, converts views into concrete trade plans, applies portfolio constraints, and produces a final approval queue.

This is the longest single component, broken into six explicit stages plus an APM gate.

## Six stage decomposition

1. **Stage 0 — Feature store:** "what was true about stock X on date D?"
2. **Stage 1 — Regime detector:** "what kind of market are we in today?"
3. **Stage 2 — Signal tracks (×3):** "what's the directional opinion for stock X in timeframe T?"
4. **Stage 3 — Trade decision layer:** "given that opinion, is there a trade to take?"
5. **Stage 3.5 — Entry timing classifiers:** "at what price, with what strategy, when?"
6. **Stage 4 — Portfolio constructor:** "which candidates fit the portfolio?"
7. **Stage 4b — Position review:** "should existing positions exit?"
8. **Stage 5 — Recommendation packager + APM gate:** "who decides — human or APM?"

Each stage has a single responsibility and a typed contract with the next stage.

---

## Stage 0 — Feature store

### Schema (long-format)

| Field | Purpose |
|-------|---------|
| `stock_symbol`, `exchange` | Identity |
| `feature_name` | e.g., `promoter_holding_pct_change_90d` |
| `feature_value` | The computed number |
| `feature_metadata` | JSON for extras (raw inputs used) |
| `valid_from` | The "as-of" date this feature applies to |
| `valid_to` | When superseded by a newer computation (NULL if current) |
| `source_max_observed_at` | Latest `observed_at` of any underlying data |
| `computer_version` | Which version of the feature computer produced this |
| `computed_at` | When this row was created |

### Point-in-time query

```
WHERE valid_from <= D AND source_max_observed_at <= D
```

This guarantees no lookahead. **The entire point of the feature store.**

### Catalog principle

A feature exists only because a signal needs it. No pre-computation for hypothetical signals. The catalog grows organically — roughly 40-60 features for v1.

### Compute schedule

1. **End of day (~7 PM):** recompute all daily features (technicals, liquidity, fundamentals from prices)
2. **After collector run:** recompute event-driven features (promoter, bulk deal, filing sentiment)
3. **End of week:** slower-moving features (sector momentum, regime inputs)

By 7 AM next morning, all features are fresh.

### Foundational rule

**Every feature must be backfillable from raw archive alone.** If a new feature `xyz_score` is added, it must be computable for all stocks for all historical dates by running the feature computer over the archive. This is what enables backtesting on any new signal idea.

---

## Stage 1 — Regime detector

### Four regimes

**`bull_calm`** — Nifty above 200 DMA, India VIX in bottom 50% of 252-day range, breadth (% of Nifty 500 above 50 DMA) > 60%.

**`bull_volatile`** — Nifty above 200 DMA but India VIX in top 30%, OR breadth between 40-60%.

**`sideways`** — Nifty within 5% of 200 DMA in either direction, no clear breadth signal.

**`bear`** — Nifty below 200 DMA, OR breadth < 30%, OR India VIX above 80th percentile.

Most severe matching regime wins (bear > volatile > calm).

### Why these specific rules

- Simple enough to verify visually
- No parameter tuning per market
- Stable across 20+ years of Indian market history
- Each component (trend, vol, breadth) brings independent information

A simple regime detector you trust beats a complex one you can't explain.

### Stickiness

Regime requires 3 consecutive days of disagreement to flip. Adds slight lag but prevents thrashing.

### Failure mode

If regime cannot be computed, fall back to the most conservative regime (`bear`). Failing safe.

### Intraday regime downgrade

The morning regime is set once and used all day by the intraday continuous pipeline. This creates a blind spot: the regime can be `bull_calm` at 9:15 AM but conditions can materially deteriorate by noon — without ever reaching the 3% Nifty drop that triggers the black-swan circuit breaker.

**Rule:** During intraday hours, if Nifty's intraday move from the previous close drops below **-1.5%**, the effective regime for the intraday signal track is downgraded to `bear` for the remainder of the day. Long-term and swing morning-batch decisions are unaffected (they were already made pre-market). This downgrade:

- Uses `bear` regime signal weights for all remaining intraday cycles
- Reduces the regime-based exposure scaling to 30% for new intraday entries
- Does **not** trip the black-swan circuit breaker (that still requires -3%)
- Resets at the next morning batch run (regime is re-evaluated fresh)

The -1.5% threshold is deliberately lower than the black-swan threshold (-3%) to provide an intermediate caution layer. It is not configurable per trade; it applies uniformly to the entire intraday track for that session.

---

## Stage 2 — Three signal tracks

### Signal record (the contract)

| Field | Purpose |
|-------|---------|
| `signal_id` | UUID |
| `stock_symbol`, `exchange` | What |
| `track` | `long_term`, `swing`, `intraday` |
| `direction` | `long`, `short`, `neutral` (v1: long-only) |
| `raw_score` | -1.0 to +1.0 |
| `confidence` | 0.0 to 1.0 |
| `regime_at_signal` | Regime label when signal fired |
| `contributing_signals` | JSON array of attribution |
| `feature_snapshot` | JSON of exact feature values used |
| `generated_at` | Timestamp |
| `generator_version` | Version of signal logic |

The `contributing_signals` array carries attribution:

```
[
  {"name": "promoter_buy", "weight": 0.4, "value": 0.8, "contribution": 0.32},
  {"name": "smart_money", "weight": 0.3, "value": 0.6, "contribution": 0.18},
  ...
]
```

When a trade goes wrong six months later, slicing by signal contribution shows which generators decay first.

### Confidence formula

```
confidence = 0.5 × |raw_score|
           + 0.3 × signal_agreement_factor
           + 0.2 × data_freshness_factor

signal_agreement_factor = (signals firing in same direction) / (total signals)
data_freshness_factor = exp(-days_since_max_observed / characteristic_decay)
```

Confidence is high only when score is meaningful, multiple signals agree, and underlying data is fresh.

### Long-term track signals

**1. Promoter activity score (weight 0.30 in bull_calm)**

```
holding_change_score = clip(promoter_holding_pct_change_90d / 1.0, -1, +1)
buy_intensity_score = clip(promoter_open_market_buy_count_90d / 3.0, 0, +1)
pledge_penalty = -clip(promoter_pledge_pct_current / 50.0, 0, 1)

promoter_score = 0.5 × holding_change_score
               + 0.3 × buy_intensity_score
               + 0.2 × pledge_penalty
```

A 1% increase in promoter holding over 90 days is "max bullish." Three+ open-market buys is max intensity. 50%+ pledged is max penalty.

`promoter_holding_pct` is computed in Stage 0 as:
```
promoter_holding_pct = sum(promoter_shares from SAST Reg 31)
                       / total_shares from shares_outstanding table (joined via ISIN)
```
This requires both tables to be populated. If `shares_outstanding` is missing for a stock on a given date, the promoter percentage cannot be computed and the `promoter_activity` sub-signal returns `direction = neutral` for that stock-date.

**2. Smart money score (weight 0.20)**

```
size_score = clip(net_buy_value / 50_crore, -1, +1)
breadth_score = clip(buyer_count / 3, 0, +1)
smart_money_score = 0.7 × size_score + 0.3 × breadth_score
```

**3. Filing sentiment score (weight 0.15)**

```
net_filing_score = (bullish - bearish) / max(bullish + bearish, 1)
red_flag_penalty = -1.0 if (auditor_change OR pledging_increase) else 0
filing_score = 0.7 × net_filing_score + 0.3 × red_flag_penalty
```

Where `bullish` = count of filings with `sentiment_label = positive AND sentiment_confidence ≥ 0.60`; `bearish` = count with `negative AND confidence ≥ 0.60`. Filings with `sentiment_label = unclassified` (confidence < 0.60 from FinBERT) are excluded from the count. Source: `filings` table, sentiment computed by FinBERT at parse time (see Phase 2).

Red flags max-penalize regardless of other filings. Binary kill switches.

**4. Earnings quality score (weight 0.20)**

```
revenue_score = clip((revenue_growth - 10) / 30, -1, +1)
margin_score = clip(opm_trend × 5, -1, +1)
cfo_quality_score = clip((cfo_pat_ratio - 0.7) / 0.5, -1, +1)

earnings_score = 0.4 × revenue_score
               + 0.3 × margin_score
               + 0.3 × cfo_quality_score
```

Source: `quarterly_financials` table (Screener.in HTML scrape). `revenue_growth` = YoY % change in revenue using last two same-quarter rows. `opm_trend` = linear slope of `opm_pct` over last 4 quarters. `cfo_pat_ratio` = most recent quarter's `cfo / pat`. If fewer than 2 quarters of data are available for a company (new listing, scraping lag), `earnings_quality` sub-signal returns `direction = neutral` (no contribution) rather than zero-filling missing data.

**5. Valuation context score (weight 0.10)**

```
valuation_score = clip((50 - pe_percentile_5y) / 50, -1, +1)
```

PE at 5-year low → score +1. Median → 0. 5-year high → -1.

**Liquidity gate:** if `avg_traded_value_20d < ₹5 cr`, signal returns `direction = neutral`. No trades in illiquid stocks.

### Long-term weights by regime

| Signal | bull_calm | bull_volatile | sideways | bear |
|--------|-----------|---------------|----------|------|
| promoter | 0.30 | 0.25 | 0.35 | 0.40 |
| smart_money | 0.20 | 0.20 | 0.20 | 0.15 |
| filing_sentiment | 0.15 | 0.20 | 0.15 | 0.20 |
| earnings_quality | 0.20 | 0.20 | 0.20 | 0.20 |
| valuation | 0.15 | 0.15 | 0.10 | 0.05 |

In bear markets, promoter signals carry more weight (defensive insiders). In bull markets, valuation matters more.

### Swing track signals

- **Catalyst proximity (0.20)** — days to results, ex-dividend, regulatory event
- **Technical setup (0.25)** — flag/pennant/base breakout patterns
- **Volume confirmation (0.20)** — z-score of recent volume vs baseline
- **Sector momentum (0.15)** — sector index relative strength
- **Recent news flow (0.10)** — count of filings/news last 7 days
- **Mean-reversion vs momentum classifier (0.10)** — which mode is the stock in

Liquidity gate: `avg_traded_value_20d > ₹2 cr`.

### Intraday track signals

- **Pre-market gap signal (0.25)** — gap with overnight news context
- **Opening range (0.20)** — first 15 min vs 20-day average
- **F&O signals (0.20)** — overnight OI build-up, max pain proximity
- **News-trade decay (0.15)** — minutes since latest news
- **Index correlation (0.10)** — beta-adjusted entry only when index confirms
- **Bid-ask spread quality (0.10)** — tight spreads = better entry

Liquidity gate: `avg_traded_value_20d > ₹10 cr`. Intraday demands deep liquidity.

---

## Stage 3 — Trade decision layer

### TradePlan record

| Field | Purpose |
|-------|---------|
| `signal_id` | FK to source signal |
| `stock_symbol`, `track`, `direction` | What/how |
| `entry_zone_low`, `entry_zone_high` | Acceptable entry price band |
| `stop_loss_price` | Exit if hit |
| `target_price` | Take-profit target |
| `expected_reward_per_share` | target - entry |
| `expected_risk_per_share` | entry - stop |
| `reward_to_risk` | Ratio |
| `expected_value_per_share` | (P_win × reward) − (P_loss × risk) − costs |
| `decision` | `proceed` or `skip` with reason |
| `skip_reason` | If skipped |

### Decision flow

```
1. Compute entry zone (placeholder, refined by Stage 3.5):
   entry_zone_low = current_price × 0.995
   entry_zone_high = current_price × 1.005

2. Compute ATR-based stop:
   k = 1.5 (intraday) | 2.0 (swing) | 3.0 (long-term)
   stop_price = entry - k × ATR_14d

3. Compute target:
   For intraday/swing: target = entry + RR_min × stop_distance
                      RR_min = 1.5 (intraday) | 2.0 (swing)
   For long-term: thesis-based OR entry + 3 × stop_distance floor

4. RR gate:
   If RR < threshold → skip "RR_too_low"

5. EV gate (with 30% confidence haircut):
   p_win = signal.confidence × 0.7
   p_loss = 1 - p_win
   reward_after_costs = (target - entry) - round_trip_costs
   risk_after_costs = (entry - stop) + round_trip_costs
   EV = p_win × reward_after_costs - p_loss × risk_after_costs
   If EV ≤ 0 → skip "EV_negative"

6. Position-feasibility:
   risk_per_trade = bucket_capital × risk_per_trade_pct
   shares = floor(risk_per_trade / (entry - stop))
   If shares < 1 → skip "position_too_small"

7. All checks pass → proceed
```

### The 30% confidence haircut

```
p_win = signal.confidence × 0.7
```

Encoded humility. Backtest will say a signal has 65% win rate; live will be 40-50%. Retail strategies typically lose 25-35% of backtested edge to slippage, missed fills, costs, fatigue.

After 60 days of live trades, replace the constant with measured live-vs-backtest ratio per track.

### Stops never widen

Stop is set at entry and stays. Widening stops is the most common failure mode in retail. The only modification permitted is trailing in your favor.

### Target-too-far check

For low-ATR stocks, target derived from `entry + 2 × stop_distance` might be unrealistically close. Additional check: target must be at least 0.5 × ATR away from entry. Otherwise the trade gets noise-completed without meaningful price action.

---

## Stage 3.5 — Entry timing classifiers

This stage was added mid-design when the original Stage 3 was identified as effectively buying at random market price. Three independent classifiers, one per track.

### Long-term classifier

**Strategy LT1 — Staged accumulation (default)**

Split into 3 tranches over 4-8 weeks:

```
Tranche 1 (40%): place at current price or slight pullback
Tranche 2 (30%): place at 50 DMA OR 5% below tranche 1, whichever closer
Tranche 3 (30%): place at 200 DMA OR 10% below tranche 1, whichever closer
```

Each tranche has 30-day validity. End up with a position that's 40-100% of intended size depending on price movement.

**Strategy LT2 — DMA pullback**

Wait for price to touch 50 DMA from above. Place limit at `50_DMA + 0.2 × ATR`. Validity: 30 days.

**Strategy LT3 — Valuation-anchored**

Used when valuation score is dominant. Compute fair price from PE percentile reversion. Enter only if current price within 10% of fair price.

**What's not in long-term:** breakout chasing (recipe for buying near tops on long-term horizon).

### Swing classifier

**Strategy SW1 — Breakout with volume confirmation (default for momentum)**

```
Trigger: above 20-day high or recent pivot high
Volume on breakout day > 1.5× 20-day average
Stop-buy resting at level + 0.3 × ATR
```

Validity: 7 days. Volume confirmation filters out fake breakouts.

**Strategy SW2 — Pullback to support (default for trend continuation)**

Enter on pullback to 20 DMA or recent breakout level (now acting as support). Limit at level + 0.2 × ATR. Validity: 7 days.

**Strategy SW3 — Catalyst event entry**

- **Pre-catalyst:** 50% position entered immediately, full position after bullish catalyst
- **Post-catalyst:** wait for first pullback (day 2-3), don't buy catalyst-day spike

### Intraday classifier

**Strategy ID1 — Opening Range Breakout (default)**

```
ORB = high and low of first 15 minutes (9:15-9:30)
After 9:30:
  Place stop-buy at ORB_high + 0.1%
  Volume on trigger bar > average per-minute volume
  Stop: ORB_low - 0.1%
  Target: entry + 1.5 × (ORB_high - ORB_low)
```

Must trigger by 11:00 AM. Later breakouts unreliable.

**Strategy ID2 — VWAP pullback**

```
Trigger: price moved up >0.5% from open AND now within 0.1% of VWAP
Limit at VWAP
Stop: VWAP - 0.5 × ATR_intraday
Target: prior intraday high
```

VWAP works for intraday because institutional algos benchmark against it, creating real magnetism.

**Strategy ID3 — Gap fade or gap ride**

- **Small gap (<1%):** typically fades. Wait for opening range, fade against gap.
- **Medium gap (1-2.5%) with strong news:** typically rides. Buy on first VWAP pullback.
- **Large gap (>2.5%):** unreliable. Skip entirely.

### Stacking gate (intraday on top of swing)

When an intraday signal fires for a stock with an open swing position, all six conditions must be true:

1. **Swing position is in profit** (P&L > 1% of position value)
2. **Intraday signal is independent of swing thesis** (≥50% of intraday's contributing weight from signals not in swing's contributing list)
3. **Concentration cap not breached** (existing swing + intraday ≤ 5% of total)
4. **Broker tagging** (`MIS` for intraday, never `CNC`)
5. **Auto square-off enforced** (intraday squared off by 3:15 PM regardless of P&L)
6. **Daily intraday cap respected** (counts toward intraday daily loss limit)

Each stacked trade gets a `parent_position_id` linking to the swing.

After 3 stacked intraday losses on the same swing, the swing position is flagged for review (intraday losing on a stock you're long-biased on says something).

### The "no convert to swing" rule

**Hard rule: an intraday trade cannot become a swing or long-term hold under any circumstances.**

Implementation: intraday orders use `MIS` tag with forced 3:15 PM square-off. Stop-loss is also intraday. **No code path exists to convert intraday to anything else.** Even on operator click, the bot refuses.

This is the single most important behavioural guardrail.

### Fibonacci excluded

Fibonacci retracement was specifically considered and excluded. Empirical evidence for the specific 38.2/50/61.8 levels beyond what generic retracement-based levels would produce is weak. Structural levels (DMAs, swing pivots, breakout retests, VWAP, volume profile) chosen instead.

---

## Stage 4 — Portfolio constructor

### Constraint order

```
1. Per-track capital availability
   - Drop trades that don't fit, prioritise by confidence
2. Single-stock concentration
   - (current position value + pending resting orders + proposed trade) ≤ 5% of total capital
   - Pending resting orders count as reserved capital — see Phase 1 for full formula
3. Sector concentration
   - Sector total ≤ 25% of total capital (all tracks combined, not per-bucket)
4. Correlation concentration
   - Correlated cluster sum ≤ 35%
5. Open positions count
   - Total ≤ 15
6. Per-track count caps
   - Intraday: max 5 new/day
   - Swing: max 3 new/day, max 8 open total
   - Long-term: max 1 new/week
```

### Prioritisation

```
priority = 0.6 × confidence + 0.3 × EV_normalised + 0.1 × signal_agreement
```

Confidence dominates because it's most stable. EV matters but noisier.

### Pyramiding (scaling in)

Strong fresh signal on existing position → add up to half original size, only if:
- Current position is in profit (no averaging down on losers)
- Original signal is still active
- Adding doesn't breach any constraint

**Averaging down on losers is forbidden. Hard rule.**

### Correlation matrices

90-day matrices flip dramatically during regime changes. Use 30-day rolling instead, recomputed weekly. Faster reaction to regime shifts.

---

## Stage 4b — Position review

### Triggers for exit recommendations

**Thesis-broken:**
- Original signal flips direction
- Confidence drops below 0.4 (for 3 consecutive days)
- Red-flag filing appears
- Promoter selling after entry on promoter buying

**Risk-management:**
- Position in drawdown for >50% of expected hold with no progress
- Sector concentration breached
- Bot-wide drawdown approaching breaker

**Opportunity-cost:**
- Higher-confidence signal arrives, capital constrained, lowest-confidence position exits

**Stage-specific:**
- Long-term: thesis change, fundamental deterioration over 2 quarters
- Swing: catalyst played out
- Intraday: time-based (3:15 PM forced exit)

### Position health score (0-100)

Weighted blend, with a track-specific definition for the time/thesis factor:

- Current P&L vs expected at this point in hold (40%)
- Signal still firing direction-aligned (30%)
- **Time/thesis factor** (15%) — defined differently per track:
  - **Intraday:** time elapsed vs scheduled square-off (linear penalty as 15:15 approaches)
  - **Swing:** time elapsed vs 30-day expected hold window (penalty starts after 21 days with no progress)
  - **Long-term:** *thesis freshness factor* — days since the primary driving signal (promoter, smart money, etc.) was last refreshed above 0.5 confidence. Not a time-to-target measure, because long-term holds have no defined duration. A long-term position where the thesis signals have been silent for 90+ days scores lower than one refreshed last week.
- Regime still favorable (15%)

The long-term time factor substitution is intentional: "time elapsed" is meaningless for long-term positions that could be held for months or years. What matters is whether the original thesis remains alive.

Visible on dashboard. 80+ healthy, 50-79 mixed, 20-49 concerning, <20 exit recommended.

### Routing

Same routing as entries:
- **Long-term exits → human approval**
- **Swing/intraday exits → APM auto-decides**

Exception: **for risk-management exits (forced de-risking), human approval is bypassed even for long-term.** If drawdown approaches breaker, no time to wait.

### Graduation path

A swing trade that's working may deserve to become long-term. If a swing position passes its target *and* fundamental signals support holding, system flags it for graduation. Operator approves; position reclassified as long-term with new stop/target/risk parameters. Counted toward long-term bucket from then on.

---

## Stage 5 — Recommendation packager + APM gate

### Routing rules

- **Long-term:** human approval required
- **Swing & intraday:** APM auto-decides

### For long-term (human)

Bundle shows:
- Stock, current price, trade plan
- Signal score with attribution chain
- Supporting evidence ("Promoter X bought ₹50 cr on April 22")
- Portfolio impact ("Banking goes from 18% to 22%")
- Suggested entry, stop, target — modifiable
- One-click approve / reject / modify

Stored with status `awaiting_human`.

### For swing & intraday (APM)

APM runs full circuit-breaker check tree:
- All checks pass → status `approved_by_apm`, ready for executor
- Any check fails → status `rejected_by_apm` with reason

Operator can override APM decision within 10-minute window. Default is APM's call.

### Recommendation lifecycle

```
generated → pending_approval → approved/rejected
                                  ↓ (if approved)
                          queued_for_execution
                                  ↓
                          submitted_to_broker
                                  ↓
                         filled / partial / rejected
                                  ↓
                            position_open
                                  ↓
                        (eventually) position_closed
                                  ↓
                          outcome_recorded
```

The `outcome_recorded` step closes the loop. Realised P&L joined to original signal attribution. **This is what enables ML eventually replacing hand-tuned weights.**

---

## ML readiness

Every Phase 3 design choice enables future ML migration:

1. **Long-format feature store** — exactly what ML training pipelines want
2. **Versioned feature computer** — train on v1, predict with v2 cleanly
3. **Signal record with feature_snapshot** — input space at decision time = training features
4. **Outcome recording** — closes the loop. `(features, decision, outcome)` triples
5. **Weights as configuration** — ML produces a new weight version, not a new code path
6. **Attribution data** — prevents black-box problem

Migration in 18-24 months: train gradient boosted model on `(features, regime) → forward_return`, replace weighted-sum scoring with model prediction. **Structure stays. Only the scoring engine evolves.**

---

## Loopholes and decisions

### Loophole 1: Designed for one direction (long)

**Decision:** v1 is long-only across all three tracks. `direction` can be `neutral` (don't trade) or `long` (buy). `short` is reserved for v2 with F&O.

### Loophole 2: Stage 4b half-specified initially

**Decision:** Full Stage 4b spec included above. Exit decisions deserve as much rigor as entry decisions.

### Loophole 3: Multi-day signals firing repeatedly

**Decision:** Signals have cooldown applied per stock-track combination, but the cooldown period depends on the outcome of the recommendation:

| Recommendation outcome | Swing cooldown | Long-term cooldown |
|------------------------|----------------|--------------------|
| Approved and position opened | 7 days | 30 days |
| Rejected by operator | 0 days (immediate reset) | 0 days (immediate reset) |
| Expired (never acted on) | 3 days | 7 days |
| Rejected by APM | 2 days | 7 days |

Rationale: a rejected recommendation means the operator disagrees with the signal — not that the signal is invalid. A new, stronger signal in the same stock should surface immediately. An expired recommendation suggests a timing miss but not a signal invalidation. Stage 4 enforces cooldowns by checking the `recommendation_outcomes` table filtered by stock-track and most recent outcome.

### Loophole 4: Intraday signals computed once per day

**Decision:** Intraday gets a separate, lighter pipeline running every 30 minutes during market hours. Designed in Phase 4.

### Loophole 5: 30% confidence haircut is a guess

**Decision:** Start with 30%. After 60 days of live trades, recalibrate per-track based on actual decay observed.

### Loophole 6: Sector mappings can be wrong

**Decision:** Maintain `sector_classifications` as a manually-curated table. NSE primary, override-able. Don't trust automated sector tagging.

### Loophole 7: No drawdown-conditioning of confidence at signal layer

**Decision:** Signal generators are objective. APM (Stage 5) handles drawdown-conditioning via circuit breakers. Don't pollute signal layer with portfolio-state awareness.

### Loophole 8 (Stage 5): Operator modifications bypass validation

When the operator modifies a long-term recommendation (entry zone, stop, target, position size) before approving, the modified parameters could fail Stage 3 gates (RR below threshold, EV negative after haircut) or Stage 4 concentration checks.

**Decision:** Modifications must pass a re-validation before the "Confirm modification" action is permitted. The system re-runs Stage 3 (RR gate, EV gate with modified parameters) and Stage 4 concentration checks (with modified position size) inline in the UI. The operator sees live pass/fail per check as they edit. If any gate fails, the confirm button is disabled with a clear reason. Original recommendation parameters are preserved for audit alongside the accepted modification. This prevents a class of bugs where the executor's pre-trade safety checks reject a trade that the operator thought was approved.

### Loophole 10 (Stage 3.5): Strategy selection within classifier

**Decision:** Fixed mapping for v1 (each signal type maps to specific strategy). Conditional selection later when per-strategy data exists.

### Loophole 11 (Stage 3.5): Strategy decay tracking

**Decision:** Every TradePlan logs `entry_strategy_id`. Outcomes joined back, producing per-strategy stats: win rate, avg RR, avg duration, decay alarms.

### Loophole 12 (Stage 3.5): Stacking-gate threshold

**Decision:** "In profit" = swing P&L > 1% of position value. "Signal independence" = ≥50% of intraday's contributing weight from signals not in swing's contributing list. Refine after observing live data.

### Loophole 13 (Stage 3.5): Tax tracking for stacked positions

**Decision:** Defer detailed tax handling to year-end reconciliation. Bot tags every trade with `intent` and `actual_holding_period`. Tax software reconciles.

### Loophole 14 (Stage 3.5): Validity windows interaction

**Decision:** Different tracks can have different orders open simultaneously for the same stock. Each tracked separately. Concentration cap respected when summed in Stage 4.

### Loophole 15 (Stage 3.5): Long-term staged accumulation cascade

**Decision:** Each tranche has its own validity (30 days). If tranches 2 or 3 trigger AND total drawdown from tranche 1 exceeds 15%, bot pauses remaining tranche and asks for human review.

---

## Stop conditions for Phase 3 (all met)

- Stage 0 schema and rules locked
- Stage 1 regime taxonomy and detection rules locked; intraday downgrade rule (-1.5% Nifty) added
- Stage 2 signal record schema, scoring methodology, all three tracks defined
- Stage 3 trade decision logic with ATR stops, RR check, EV check, haircut
- Stage 3.5 three independent classifiers per track, stacking gate, no-convert rule
- Stage 4 portfolio constructor with constraints (pending orders included in concentration), prioritisation, pyramiding
- Sector cap defined as 25% of total capital (all tracks combined) — consistent with Phase 1
- Stage 4b position review with triggers, health scores, graduation path; long-term health score uses thesis freshness factor
- Stage 5 recommendation packager + APM gate split; operator modifications re-validated before accept
- Cooldown rules updated to depend on recommendation outcome (rejected = immediate reset)
- ML readiness articulated
- Long-only constraint for v1
- Fibonacci excluded with reasoning
- 15 loopholes identified with decisions

## What this design buys

1. **Every decision is explainable.** Trace exactly which signals contributed, what regime, what features.
2. **The brain is composable.** Add/remove signal generators without architectural change.
3. **Backtesting works on the same code.** Point-in-time queries enable historical replay.
4. **ML migration is configuration, not rewrite.** Swap weighted-sum for model prediction, rest stays.
5. **APM auto-runs swing and intraday with full circuit-breaker protection.** Human reviews results, not every individual call.
