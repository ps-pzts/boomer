# Phase 4 — Executor + Backtesting + Intraday Continuous + Stage 4b

This phase covers the four interrelated components that make "the trading day in motion" work:

- **Executor (System 3)** — places orders, manages lifecycle, reconciles with broker, handles failures
- **Backtesting harness** — replays history through same code as live, validates edge
- **Intraday continuous pipeline** — System 2 stages running every 30 minutes during market hours
- **Stage 4b position review** — exit-side counterpart to Stage 4 (referenced from Phase 3, operationalised here)

---

## Component 1 — Executor

### Goal

The executor is the only component that talks to brokers. It converts approved trade plans into actual orders, tracks lifecycle from submission to fill to exit, and maintains the source of truth for what positions are actually held.

**The rest of the system never talks to a broker directly.** This isolation is what makes the architecture testable — replace the executor with a mock and the whole bot can run end-to-end without placing a real trade.

### Inputs and outputs

**Inputs:**
- Approved `TradePlan` records (from Stage 5)
- `ExitRecommendation` records (from Stage 4b)
- Manual override commands from operator (force-close, cancel, modify)

**Outputs:**
- `orders` table — every order ever submitted, with full lifecycle
- `positions` table — current and historical positions with realised/unrealised P&L
- `executions` table — individual fills (one order can have multiple)
- Health signals — broker connectivity, order queue depth, rejection rate

### Broker abstraction

```
Broker interface contract:
  authenticate() → session
  place_order(order_request) → broker_order_id
  modify_order(broker_order_id, changes) → status
  cancel_order(broker_order_id) → status
  get_order_status(broker_order_id) → status
  list_positions() → list of positions
  list_holdings() → list of delivery holdings
  get_funds() → cash + margin available
  on_order_update(callback) → subscribes to push events
  on_tick(symbols, callback) → subscribes to price updates
  # GTT methods (delivery/swing only — see GTT section below)
  place_gtt(gtt_request) → gtt_id
  modify_gtt(gtt_id, changes) → status
  cancel_gtt(gtt_id) → status
  get_gtt(gtt_id) → gtt_record
  list_gtts() → list of active GTTs
```

**v1 implementations — dual broker:**

| Broker | Handles | Reason |
|--------|---------|--------|
| `KiteBroker` | All intraday (MIS) orders | Proven reliability for intraday; required for tick feed |
| `FyersBroker` | All delivery (CNC) orders — swing and long-term | ₹0 delivery brokerage vs ₹20/order on Kite |
| `MockBroker` | Backtesting and tests | Historical price replay, no real orders |
| `PaperBroker` | Paper trading | Fakes fills using realistic models |

Both `KiteBroker` and `FyersBroker` are v1 implementations, not deferred. Every order carries a `broker_id` field. The executor routes: `track == intraday → kite_broker`; `track in (swing, long_term) → fyers_broker`. At the ₹2.5L milestone, this routing can be reviewed or changed without architectural work.

**Fyers specifics:** Fyers API Python client (`fyers-apiv3`) uses OAuth2 daily token refresh. Authentication at 9:00 AM IST, same as Kite. Fyers supports GTC (Good Till Cancelled) orders for delivery, which map to GTT semantics in the abstraction. Fyers brokerage: ₹0 for equity delivery, ₹20 for intraday.

**PaperBroker price dependency:** `PaperBroker` simulates order fills but requires a live price feed to determine whether a resting order fills. This feed comes from `KiteBroker`'s tick subscription (Kite is always running for intraday even if Fyers handles delivery orders):

```python
paper_broker = PaperBroker(price_source=kite_broker)
```

`PaperBroker.authenticate()` delegates to `price_source.authenticate()`. Paper trading fails if the Kite session is invalid.

**Critical principle:** the rest of the executor uses *only* the abstract interface. No code anywhere does `if broker == "kite"`. Routing is a configuration table, not conditional logic.

### Order lifecycle state machine

```
States:
  CREATED         — record exists, not yet submitted
  SUBMITTING      — submitting to broker, awaiting acknowledgment
  PENDING         — broker accepted, in order book
  TRIGGERED       — for stop/conditional orders that fired
  PARTIAL         — some shares filled, others pending
  FILLED          — fully filled
  CANCELLED       — cancelled before any fill
  REJECTED        — broker rejected
  EXPIRED         — order's validity period ended
  ERROR           — bot lost track (force investigation)

Allowed transitions:
  CREATED → SUBMITTING → PENDING → TRIGGERED → PARTIAL → FILLED
                              ↘                ↘
                                CANCELLED       CANCELLED (remainder)
                              ↘
                                REJECTED
                              ↘
                                EXPIRED

  Anything → ERROR (one-way; requires human investigation)
```

State machine enforced by executor: invalid transitions throw. If broker reports FILLED on an order that was already CANCELLED, that's a bug, and the system screams rather than silently accept.

### Three execution paths

**Path 1 — Immediate orders** (intraday entries, intraday stops):
- Submit → expect fill within seconds
- Not filled in 30s → cancel and alert
- Tag: `MIS` via Kite
- No GTT. Intraday orders are same-session only.

**Path 2 — GTT entry orders** (swing/long-term pullback limits, breakout stop-buys):
- Placed as a GTT single-leg trigger via Fyers
- Persist for up to 365 days without daily re-submission
- GTT fires when price crosses trigger level → a CNC limit order is placed automatically
- The executor monitors GTT status (see GTT lifecycle below)

**Path 3 — GTT OCO orders** (swing/long-term stop-loss + target after entry fills):
- Placed as a GTT OCO (One Cancels Other) immediately after CNC entry fills
- Two legs: stop-loss trigger (sell if price drops to SL level) + target trigger (sell if price rises to target)
- When one leg fires, broker cancels the other automatically
- No manual stop order management for delivery positions

### GTT order lifecycle

GTT is a broker-side construct separate from regular orders. The executor maintains a `gtt_orders` table alongside `orders`.

```
GTT States:
  GTT_ACTIVE      — monitoring price condition at broker
  GTT_TRIGGERED   — condition met; broker placed the underlying order
                    → creates a row in `orders` with parent_gtt_id
  GTT_CANCELLED   — cancelled by executor before triggering
  GTT_EXPIRED     — reached 365-day limit without triggering
  GTT_DELETED     — manually deleted via dashboard

GTT_TRIGGERED produces a normal order that follows the standard order state machine.
```

**`gtt_orders` table:**

| Field | Purpose |
|-------|---------|
| `gtt_id` | UUID (local) |
| `broker_gtt_id` | Broker's GTT identifier |
| `broker_id` | Which broker holds this GTT |
| `stock_symbol`, `exchange` | What |
| `gtt_type` | `single` (entry) or `oco` (sl+target) |
| `trigger_price` | For single; primary trigger for OCO |
| `limit_price` | The order price placed on trigger |
| `sl_trigger_price`, `target_trigger_price` | For OCO legs |
| `quantity` | Shares |
| `status` | GTT state above |
| `parent_order_id` | The entry order that this OCO protects |
| `triggered_order_id` | The `orders` row created when GTT fires |
| `valid_until` | Up to 365 days from placement |
| `created_at`, `last_checked_at` | Timing |

**GTT reconciliation:** GTT statuses are polled once per day (6 AM pre-market check), not every 60 seconds. GTTs are broker-side and don't require intraday polling. Exception: after a known entry fill, the executor polls GTT status within 30 minutes to confirm the OCO is active.

**GTT vs trailing stop interaction:** Trailing stop logic modifies the OCO GTT's SL leg. When price moves favorably by 2× ATR, the executor calls `modify_gtt()` to update the SL trigger upward. The target leg is unchanged. This is a single `modify_gtt` API call — simpler than cancelling and re-placing a separate stop order.

**GTT count limits:** Kite Connect supports up to 10,000 active GTTs per account. At max 15 positions with 2 GTTs each (entry + OCO) this is negligible. Fyers has similar limits. No constraint in practice.

### Bracket orders decision

**Decision: don't use bracket orders. Use separate orders.**

Reasoning: BO is intraday-only, doesn't allow stop modification after placement, Zerodha discontinued BO in late 2020. For each entry, place three independent orders (entry, stop-loss, target) linked via `parent_order_id` in the database. Executor manages cascade cancellation when parent fills/exits.

More work but more flexible. Can adjust stops, partial-exit, trail stops, without being constrained by broker BO support.

### Order modifications

After fill, modifications supported:
- **Trail the stop** as position moves favorably
- **Partial exit** at first target (book half at 1R, ride other half to 2R)
- **Cancel target** to let it run if signal strengthens

**Trailing stop logic for v1:** simple "trail by ATR" — when price moves favorably by 2 × ATR, move stop up by 1 × ATR. Stop only moves *toward* current price, never away.

**Hard rule: stops never widen.** The most expensive failure mode in retail.

### Reconciliation loop

Executor's view of positions must match broker's view across **two brokers** and **two order types** (regular orders + GTTs). They will drift.

**Every 60 seconds during market hours (regular orders — Kite intraday):**
- Fetch Kite's current positions and pending orders
- Compare against bot's view
- Three outcomes per position:
  - **Match** — all good
  - **Bot has it, broker doesn't** — likely failed order, mark ERROR, alert
  - **Broker has it, bot doesn't** — manual trade outside bot OR missed fill, alert, log, update bot view

**Every 60 seconds during market hours (delivery positions — Fyers):**
- Fetch Fyers holdings and pending orders (delivery orders triggered today)
- Same three-way comparison for Fyers-routed trades

**Once per day at 6 AM (GTT reconciliation — both brokers):**
- `list_gtts()` on each broker
- Verify every `GTT_ACTIVE` in local table still exists at broker
- Check for any `GTT_TRIGGERED` status changes — if a GTT fired overnight, create the resulting `orders` row and update position state
- Mismatches → `reconciliation_alerts` table

**End of day:**
- Full reconciliation across both brokers (positions, holdings, cash, margin)
- Mismatches go to `reconciliation_alerts` table
- Tomorrow's run can't start until reconciliation alerts cleared

The single biggest source of catastrophic bot failures is "bot's mental model of what it owns is wrong." Two brokers doubles the surface area for drift — the reconciliation loop must cover both.

### Pre-trade safety checks (executor's own layer)

Defence in depth on top of Phase 1 risk checks:

1. **Order price sanity** — within 5% of current LTP
2. **Quantity sanity** — > 0, integer, within liquidity bounds
3. **Duplicate detection** — identical order or GTT in last 30 seconds?
4. **Funds available** — routed broker has the cash (check Kite for intraday, Fyers for delivery)
5. **Symbol valid** — exists, tradable today (not suspended)
6. **Market hours** — for intraday, market open and not in pre-open auction
7. **Circuit limit** — order price not at upper/lower circuit
8. **GTT duplicate check** — for GTT entries: no active GTT already exists for same stock-track with same trigger price (prevents stacking identical resting GTTs after a rejected recommendation is re-issued)

Any failure → reject the order, log reason. Trade plan stays approved but unfilled.

A check that runs twice catches more bugs than one that runs once.

### Broker failure handling

Rules apply per-broker independently. A Fyers outage doesn't stop Kite intraday operations and vice versa.

- **API timeout:** wait 10s, poll for order status. Don't retry placement (could duplicate).
- **Auth failure:** re-authenticate once. Twice fails → alert and pause *that broker's track only*. Kite failing doesn't halt delivery orders; Fyers failing doesn't halt intraday.
- **Rate limit:** exponential backoff with jitter, cap at 5 minutes.
- **Order rejected:** parse rejection reason. Some recoverable (price away from market), some not (margin insufficient).
- **Connection lost mid-day:** stop placing new orders on that broker. Existing orders and GTTs continue at broker. On reconnect, reconcile aggressively.
- **GTT rejected by broker:** mark GTT as `ERROR`, alert immediately. A rejected GTT means an entry or stop-loss is unprotected. Operator must decide to re-place or cancel.
- **Fyers auth fails at market open:** intraday continues on Kite. Swing/long-term entries are blocked for the day. Existing delivery positions have their GTT OCO stops at Fyers — those continue independently of the API session.

Each failure type writes to `executor_errors`. Pattern of repeated errors triggers escalation.

### Loopholes and decisions

**Loophole 1 — Partial fills:** stop-loss for 70 of 100 shares filled?

**Decision:** Stop order is placed/modified to match cumulative filled quantity. Whenever fill quantity changes, stop is amended.

**Loophole 2 — Fast price moves:** stop at ₹490, gap-down to ₹470, fills at ₹470.

**Decision:** Accept this — market reality, no magic fix. ATR-based sizing already accounts for this. Document expected gap-down losses in dashboard.

**Loophole 3 — Stop-loss order rejected:** position with no protection.

**Decision:** Mark position with `unprotected_flag=true`. Retry every 60s. After 3 failed retries, urgent alert. After 10 minutes unprotected, executor force-closes the position. Better to exit at market than ride unprotected.

**Loophole 4 — Half fills at end of day for intraday:** auto-square-off interaction.

**Decision:** At 3:00 PM, executor reviews intraday positions. Partial fills get unfilled portion actively cancelled. Filled portion squared off via market order at 3:14 PM if not exited.

**Important:** the executor never self-triggers this square-off on a timer. The square-off at 3:14 PM is initiated by the orchestrator's `intraday_squareoff` task (scheduled 15:14 weekday), which calls the executor's `square_off_all_intraday()` method. The executor provides the method; the orchestrator triggers it. Having a single trigger source prevents duplicate order submissions. Zerodha also auto-squares-off MIS positions at 15:20 — this is the backstop if both the bot's 15:14 square-off and any broker-side rejection occur.

**Loophole 5 — Manual trades outside bot:** operator buys directly via Kite.

**Decision:** Bot tags such positions as `unmanaged=true`. Counts toward concentration caps but bot won't place stops or modify. Dashboard shows separately.

**Loophole 6 — Broker session expiry mid-day:** Kite tokens expire daily.

**Decision:** Re-authenticate at 9:00 AM IST every day. If session dies mid-day, re-auth automatically.

**Loophole 7 — "Ghost order" problem:** API timeout, did the order go through?

**Decision:** Every order has a client-side `idempotency_key`. Before retrying, check for orders with that key. Brokers supporting idempotency tokens (Kite supports `tag` field) get used for this. Others get polling check first.

---

## Component 2 — Backtesting harness

### Goal

The backtester replays historical data through the exact same code as live trading, with realistic execution simulation, to validate whether signals and strategies have edge.

**No backtest, no real money.**

### Inputs and outputs

**Inputs:**
- Historical features from feature store (point-in-time correct)
- Historical prices with corporate action adjustments
- Date range to simulate
- Starting capital and configuration
- Current code for Stages 0-5 + Stage 3.5 + APM logic

**Outputs:**
- `backtest_run` record with summary statistics
- `backtest_trades` table with every simulated trade
- `backtest_daily_state` table tracking capital, drawdown, exposure
- Performance reports — by track, by signal, by strategy

### Core principle: same code, different broker

The backtester is *not* a separate system. It's a `MockBroker` plugged into the exact same executor running the exact same Stages 0-5. The ONLY differences:
- Time advances synthetically (one trading day at a time, or one tick at a time for intraday)
- Broker calls go to MockBroker which simulates fills
- Live data feeds replaced with historical replay

If backtester and live have *different code paths*, the backtest is a lie. Same-code achieved by treating time and broker as injected dependencies.

### Simulation loop

```
For each historical date D:
    1. Set system clock to D (all "now" calls return D)
    2. Read features valid_from <= D and observed_at <= D from feature store
    3. Run Stage 1 (regime detector)
    4. Run Stage 2 (signal generators) — same code as live
    5. Run Stage 3 (trade decision)
    6. Run Stage 3.5 (entry timing)
    7. Run Stage 4 (portfolio constructor)
    8. Run Stage 5 (recommendation packager + APM)
    9. Run executor against MockBroker:
       - Approved trades become orders
       - Resting orders may fill on subsequent days when price reaches limit
       - Stops trigger when historical low/high crosses stop level
       - Realistic slippage and costs applied
    10. Update simulated capital state
    11. Move time to D+1
```

MockBroker maintains an "open orders" list across simulation days for resting orders.

### Cost modeling (Indian markets)

```
Per round trip (buy + sell):

  INTRADAY (via Kite):
    Brokerage: ₹20 per order or 0.03% (whichever lower)
    STT: 0.025% on sell value
    Exchange transaction charges: 0.00322% on both legs
    GST: 18% on (brokerage + transaction charges)
    SEBI charges: 0.0001% on both legs
    Stamp duty: 0.003% on buy only
    → Approximate round-trip: 15-25 bps of trade value

  DELIVERY — swing/long-term (via Fyers):
    Brokerage: ₹0 (Fyers equity delivery is free)
    STT: 0.1% on buy + 0.1% on sell
    Exchange transaction charges: 0.00322% on both legs
    GST: 18% on (transaction charges only — no brokerage component)
    SEBI charges: 0.0001% on both legs
    Stamp duty: 0.003% on buy only
    → Approximate round-trip: 25-35 bps of trade value (lower than Kite delivery
       due to ₹0 brokerage, especially meaningful for small positions)
```

The Fyers ₹0 delivery brokerage saves ₹40 per round trip on delivery trades (₹20 buy + ₹20 sell). At ₹50,000 capital with typical position sizes of ₹2,500–₹5,000, this represents 0.8–1.6% savings per trade — meaningful at small capital.

Every backtest trade has these costs subtracted. **Without realistic costs, every backtest looks profitable.**

### Slippage modeling

```
For market orders:
  base_slippage = 0.05% (5 bps)
  liquidity_adjustment = max(1, my_quantity / (avg_daily_volume × 0.001))
  volatility_adjustment = max(1, atr_pct / 0.02)
  total_slippage = base × liquidity_adjustment × volatility_adjustment

For limit orders:
  Fill at limit (no slippage)
  Only fills if intraday low <= limit (buys) or high >= limit (sells)

For stop-loss orders:
  Slippage_factor = 1.5 (stops fill worse in fast markets)
  fill_price = stop_price × (1 - slippage_factor × base_slippage)
```

Conservative numbers. Live results may be better; never assume so during backtest.

### Survivorship bias

Most retail backtests use today's stock universe for historical periods. Inflates returns by ~3-5% annually because delisted companies aren't in the test set.

**Decision:** Maintain `historical_universe` table — for each date, which stocks were tradable in Nifty 500. Backtest only considers stocks that existed and were liquid on each date.

For v1 simplification: start with current Nifty 500 only, but explicitly flag this as known bias. Note inflated returns by 3-5% annually. Fix in v2.

**Acceptance criteria adjustment for survivorship bias:** Because v1 backtests are biased upward by ~3-5% annual return, the acceptance criteria are tightened accordingly:
- Walk-forward Sharpe threshold is raised from **≥ 1.0** to **≥ 1.3** to absorb the ~0.3 Sharpe units of upward bias at typical volatility levels
- All reported backtest annualized returns should be mentally discounted by 3-5% when comparing against expected live performance

### Validation hierarchy

**Tier 1 — In-sample backtest:**
- Run on data weights were tuned on
- Sanity check that code works
- Doesn't validate edge

**Tier 2 — Out-of-sample backtest:**
- Train weights on 2020-2022, freeze, test on 2023-2024
- This is where most strategies die

**Tier 3 — Walk-forward analysis:**
- Train on 2020, test on 2021
- Slide forward: train on 2020-2021, test on 2022
- Continues sliding through history
- Simulates how strategy would actually be re-tuned

### Acceptance criteria for moving to paper trading

All five must pass:
- Walk-forward Sharpe ≥ **1.3** (raised from 1.0 to account for survivorship bias in v1 universe)
- Maximum drawdown ≤ 15%
- Win rate × avg reward ≥ 1.5 × loss rate × avg loss
- At least 100 simulated trades per track
- Out-of-sample performance ≥ 50% of in-sample

Strategy doesn't pass all five → don't trade it. Strict.

**Tiered validation for filing-based signals:** Walk-forward validation requires historical data with accurate `observed_at` timestamps. Price and technical data satisfies this cleanly. Filing-based signals (promoter activity, bulk deals, insider trading) present a challenge: NSE/BSE don't provide historical archives with sub-day timing. For the 2020-2024 backtest window, NSE FO bhavcopy and BSE bulk deal CSVs are available as downloadable archives with trade-date granularity, and `observed_at = trade_date + 18:30 IST` approximation is acceptable.

However, if full historical SEBI filing data cannot be obtained for the desired validation window, the following tiered approach applies:

| Signal category | Validation method |
|-----------------|-------------------|
| Price/technical signals (swing, intraday) | Full walk-forward on 2020-2024 data |
| Filing-based signals (long-term track) | Minimum 6 months of live paper trading with real `observed_at` timestamps before scaling to real money |
| F&O signals | Full walk-forward using NSE FO bhavcopy archives |

The long-term track's filing-dependent signals may not be walk-forward validatable from historical data alone. Six months of genuine paper trading (with the system running live and collecting real filings) is the substitute validation gate for those signals specifically.

### Holdout integrity discipline

The more times you tweak weights and re-run a backtest on the same data, the more you overfit. After enough tweaks, any strategy looks great on past data.

**Decision:** Maintain a "research log" — every backtest run logged with hash of code+config. After 5 runs against the same out-of-sample period, that period is "burned" and a new period must be chosen.

This sounds extreme. It exists because retail traders almost universally fool themselves with iterative backtesting until something looks good, then deploy and lose money.

### Streak / Sensibull as backtester substitute

**Decision: not viable as substitute.** Reasoning:

- Streak's strategy vocabulary is technical-only; bot's signals (filings, deals, regime) aren't expressible
- Streak doesn't model regime conditioning
- Streak doesn't have Stage 3.5 timing classifiers
- Streak doesn't simulate APM logic
- Streak's backtest is on Streak's code, not bot's code

**Streak as supplement:** valid for sanity checks (compare bot's results to naive baselines), independent cost validation (audit cost model), idea generation (visualize patterns to inspire signals). Don't try to bridge them — different tools, different scopes.

### Loopholes and decisions

**Loophole 1 — Look-ahead in features:** addressed in Phase 2 via `valid_from` and `source_max_observed_at`.

**Loophole 2 — Look-ahead in strategy logic:** signal might inadvertently use today's close to "decide" today's entry.

**Decision:** Backtest runs with explicit `as_of_time` boundary. Stage 3.5 can only see data with `observed_at <= as_of_time`. Tested by deliberately seeding lookahead bugs and verifying they're caught.

**Loophole 3 — Stale signal decay:** signal strong in 2020 may be weak in 2024.

**Decision:** Performance reports broken by year. If 2024 results are dramatically worse than 2020, signal has decayed.

**Loophole 4 — Too few trades:** 30 trades might "look" profitable due to luck.

**Decision:** Each track must have ≥ 100 simulated trades for backtest to count as validated. Fewer = inconclusive.

**Loophole 5 — Behavioral bias in design:** "I designed this so I subconsciously made choices that flatter it."

**Decision:** Adversarial review pass once strategy passes backtest. Find worst 10 trades, find worst drawdown periods, ask "why did my system not handle this?" Investigate beyond luck explanations.

---

## Component 3 — Intraday continuous pipeline

### Goal

A lighter, faster version of System 2 stages running every 30 minutes during market hours, processing only intraday signals — because intraday signals decay too fast to wait for next-day batch processing.

### What runs in continuous (vs morning batch)

**Morning batch (07:00):**
- All Stage 0 daily features
- Stage 1 regime detection
- Long-term Stage 2 signals
- Swing Stage 2 signals
- All Stage 3, 3.5, 4, 5 for long-term and swing

**Continuous (every 30 min during market hours):**
- Intraday-only Stage 0 features (VWAP, ORB levels, gap behaviour, intraday volume)
- Intraday Stage 2 signals (using fresh features + morning's regime)
- Intraday Stage 3 + 3.5 + 4 + 5
- APM auto-decides intraday trades

Regime is *not* recomputed continuously. Set in morning, stays for the day. Stickiness rules apply.

### Schedule

| Time (IST) | What runs |
|------------|-----------|
| 09:00 | Pre-market check — gap analysis, news scan, system health |
| 09:15 | Market open — first ORB observation begins |
| 09:30 | First intraday cycle — ORB levels finalised, signals computed |
| 10:00, 10:30, ..., 14:00 | Continuous cycles every 30 min |
| 14:30 | Last cycle that can generate new entries |
| 15:00 | Position monitoring only — no new entries |
| 15:15 | Auto square-off begins |
| 15:30 | Market close, intraday positions all closed |

The 14:30 cutoff exists because intraday signals after that have insufficient time to play out before forced square-off.

### Resource considerations

13-14 continuous runs per day vs 1 morning batch. Each takes 30-60 seconds.

Cost on free-tier infrastructure: negligible. Even a paid ₹500/month VPS absorbs it.

Logging volume: ~14× more entries from intraday than batch. Manageable but design for log rotation.

### Loopholes and decisions

**Loophole 1 — Stale signals lingering:** signal from 10:00 still in queue at 11:30.

**Decision:** Every intraday signal has 30-minute validity. If not acted on by next cycle, discarded and recomputed.

**Loophole 2 — Signal flicker:** same stock signal fires repeatedly.

**Decision:** Per-stock cooldown of 60 minutes for intraday. Prevents flicker, gives signals room to evolve.

**Loophole 3 — Cycle overlap:** 10:00 cycle takes 90s, 10:30 cycle starts before 10:00 finishes.

**Decision:** Cycles are mutually exclusive — locked execution. If a cycle is still running at next scheduled start, the new one is skipped (logged but not panicked about). Non-event in practice.

**Loophole 4 — Failed cycle:** network hiccup, broker API blip.

**Decision:** Each cycle runs independently. Failed cycle doesn't affect prior or subsequent. Three failures in a row triggers alert and possibly disables intraday for the rest of day.

---

## Component 4 — Stage 4b position review

(Phase 3 introduced this; full design here.)

### Goal

Stage 4b runs daily for swing/long-term and continuously for intraday, evaluating every open position to decide whether it should exit regardless of stop or target hit.

**Not all exits are stop-loss or target hits.** Sometimes thesis is broken before stop is hit; exiting early saves capital.

### Inputs

- Every open position (from `positions` table)
- Current features for the held stock (point-in-time)
- Current regime
- Original signal that drove the entry (for thesis re-validation)

### Outputs

- `ExitRecommendation` records for positions flagged for exit
- Updated `position_health_score` for all positions

### Exit triggers

**Thesis-broken:**
- Original signal flips direction
- Confidence drops below 0.4 from original entry confidence (3 consecutive days)
- Red-flag filing appears (auditor change, pledging spike, fraud)
- Promoter selling after entry on promoter buying

**Risk-management:**
- Position in drawdown for >50% of expected hold with no progress
- Sector concentration breached (forces partial exit)
- Bot-wide drawdown approaching breaker (forced de-risking)

**Opportunity-cost:**
- Higher-confidence signal arrives, capital constrained → lowest-confidence open position exits

**Stage-specific:**
- Long-term: thesis change, fundamental deterioration over 2 quarters
- Swing: catalyst played out
- Intraday: time-based (3:15 PM forced exit)

### Exit decision flow

```
For each open position:
  1. Re-validate original thesis (re-run Stage 2 for that stock-track)
  2. If signal direction flipped or confidence < 0.4 (3 days):
     → ExitRecommendation: thesis_broken
  3. Check time-based filters per track:
     → Swing past 30 days: ExitRecommendation: held_too_long (review)
     → Intraday past 14:30 unfilled-target: time_based
  4. Check fundamental red flags:
     → New filing in red-flag categories: red_flag
  5. Compute position_health_score
```

### Position health score (0-100)

```
score = 0.40 × pnl_vs_expected_factor
      + 0.30 × signal_alignment_factor
      + 0.15 × time_vs_expected_factor
      + 0.15 × regime_alignment_factor
```

Color-coded:
- 80-100: healthy, on track
- 50-79: mixed signals, monitoring
- 20-49: concerning, consider exit
- 0-19: exit recommended

### Routing

- **Long-term exits → human approval**
- **Swing/intraday exits → APM auto-decides**

Exception: **forced de-risking bypasses human approval even for long-term.** If drawdown approaches breaker, no time to wait. Operator notified.

### Graduation path

A swing trade that's working may deserve to become long-term. If a swing position passes its target *and* fundamental signals support holding, system flags for graduation. Operator approves; position reclassified as long-term with new stop/target/risk parameters.

### Loopholes and decisions

**Loophole 1 — Thesis re-validation flicker:** confidence at 0.39 today, 0.42 tomorrow.

**Decision:** Thesis-broken exit requires confidence < 0.4 for 3 consecutive days. Not single-day flicker.

**Loophole 2 — Health score becomes a target to game:** "let me adjust the weights."

**Decision:** Health score weights are versioned. Changes require explicit "design decision" log entry.

**Loophole 3 — Forced de-risking crystallises losses:** force-exits at -10% might recover.

**Decision:** Forced de-risking exits *least conviction* positions first, not deepest losses. By construction, these are the positions you'd exit anyway. And the alternative (waiting and breaching 8%) is worse.

**Loophole 4 — Stage 4b interaction with stacking:** swing + stacked intraday on same stock.

**Decision:** Stage 4b evaluates them as separate positions. Linked only for accounting and concentration.

**Loophole 5 — Exit recommendations queuing up:** operator doesn't review for days.

**Decision:** Exit recommendations have 1-day validity for swing/intraday, 5-day validity for long-term. Auto-cancel if not acted on. Re-evaluated next cycle.

**Loophole 6 — Swing held into long-term:** sometimes a working swing should become long-term.

**Decision:** Graduation path documented above.

---

## Stop conditions for Phase 4 (all met)

- Executor: dual broker (KiteBroker for intraday, FyersBroker for delivery) — both v1 implementations
- Executor: broker abstraction interface defined with GTT methods
- Executor: GTT order lifecycle and `gtt_orders` table defined
- Executor: GTT OCO for delivery stop-loss + target; GTT single-leg for entry orders
- Executor: PaperBroker price-source dependency documented (requires Kite session for live data)
- Executor: order lifecycle state machine locked
- Executor: GTT replaces resting-order re-submission problem entirely
- Executor: reconciliation every 60s (regular orders) + daily 6 AM (GTT status) + EOD (full)
- Executor: pre-trade safety checks specified (includes GTT duplicate check)
- Executor: intraday square-off triggered by orchestrator only (no duplicate self-trigger)
- Executor: failure handling per broker, independent per broker
- Backtesting: same-code principle with MockBroker
- Backtesting: cost model updated for Fyers ₹0 delivery brokerage
- Backtesting: walk-forward validation as deployment gate; tiered approach for filing-based signals
- Backtesting: walk-forward Sharpe threshold raised to 1.3 (survivorship bias adjustment)
- Backtesting: holdout integrity discipline (5-run cap)
- Streak as supplement, not substitute
- Intraday continuous: 30-min cadence, lighter pipeline
- Intraday continuous: cycle isolation, signal validity, cooldowns
- Stage 4b: thesis-broken, risk-management, opportunity-cost exits
- Stage 4b: position health score
- Stage 4b: graduation path
- 22+ loopholes identified across the four components

## What this design buys

1. **Same-code backtesting.** Whatever you backtest is what you trade.
2. **Broker is replaceable.** Today Kite. Tomorrow Upstox. Architecture doesn't care.
3. **Execution failures don't compound.** Robust state machine, reconciliation, pre-trade checks catch most failures.
4. **Intraday gets cadence it needs.** Not as afterthought to morning batch.
5. **Exits are first-class decisions.** Active position management catches thesis breaks early.
6. **Graduation path lets winners run.** Swing trades that should become long-term get reclassified properly.
