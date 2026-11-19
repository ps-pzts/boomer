# State Machines: Session, Trade, Order

> **Status:** APPROVED 2026-11-19 (operator), issue #47. Not implemented yet. See §8 for delivery.
> **Companion doc:** `designs/observability.md`: every transition here is emitted as an event there.
> **Relationship to B7:** `designs/intraday-live-pipeline.md` §3.5 (trade lifecycle) and §4 (wiring) are
> re-expressed here as explicit machines. Where the two disagree, this doc wins for state and
> transitions; the pipeline doc still wins for *what* to trade.

---

## 1. Why

Boomer already behaves like several state machines, but mostly implicitly:

| Machine | Today |
|---|---|
| Order | Explicit transition table (`ALLOWED_TRANSITIONS` in `executor/models.py`), but each change **overwrites** `orders.status`; history is lost |
| Trade | States are strings in `intraday_brackets.state` (#45/#46); transitions are written inline in `TradeSupervisor` |
| Session (the trading day) | Scattered: cron tasks, `IntradayPipeline._disabled_for_day`, circuit breakers, feed health, entry-window clock checks, square-off timing, `BotMode` |

The consequences:
- There is no single answer to "what is the bot doing right now, and why?".
- An illegal path (e.g. entering after 14:30 because one clock check was missed) is only prevented if every call site remembers the rule.

Making the machines explicit gives:
1. **Legality by construction.** A transition not in the table raises `IllegalTransition`; it can't happen silently.
2. **One place per rule.** Each guard (e.g. "fresh tick", "no breaker tripped") is a named function evaluated at one transition.
3. **Complete history.** Every transition is recorded as an event (`designs/observability.md`) in the same DB transaction as the state change.

## 2. Principles

1. **Separate machines, separate goals.** Each machine owns one entity with one goal and one lifetime. They are **not** merged into one machine: Session × Trade × Order would multiply into hundreds of combined states nobody can reason about or test.

   | Machine | Owns | Goal | Lifetime |
   |---|---|---|---|
   | **Session** | the trading day | Is trading allowed right now, and why not? | one day |
   | **Trade** | one position | Get in, stay protected, get out, account P&L | minutes to hours |
   | **Order** | one instruction at the broker | Reach a final state at the broker | seconds to minutes |

2. **Converge on mechanics only.** One small core (§3), one event envelope, one events table.
3. **Hierarchy: Session → Trades → Orders**, linked by correlation ids (`session_date`, `trade_id`, `order_id`).
4. **Machines talk by direct calls, never through events.** A parent *calls* a child (the Session tells each Trade to square off). A child reports back through its return value or its own state table. The `events` table is a **copy** of what happened, read only by observability (`designs/observability.md` §2).
5. **State lives in each machine's own table** (`trading_sessions`, `intraday_brackets`, `orders`). Those tables, not the events, are the source of truth for decisions.
6. **IST everywhere** (CLAUDE.md §9). Every transition records the IST wall-clock time.

## 3. The core (`src/statemachine/`)

A small in-house module, about 100 lines. Libraries such as `transitions` and `python-statemachine` keep state in memory and can't commit the state change and its event in one SQLite transaction, which restart safety requires.

```python
@dataclass(frozen=True)
class Transition:
    source: frozenset[str]  # states this transition may leave
    target: str
    trigger: str  # name of the event that causes it, e.g. "entry_filled"
    guard: Callable[[Context], str | None] | None = None  # None = allowed, str = blocked reason


class MachineSpec:  # one per machine type, declared once at import time
    name: str  # "session" | "trade" | "order"
    states: frozenset[str]
    initial: str
    terminal: frozenset[str]
    transitions: tuple[Transition, ...]


def transition(conn, spec, entity_id, trigger, *, rule, inputs, correlation, ctx=None) -> str:
    """Within ONE sqlite transaction on `conn`:
    1. read the entity's current state from the machine's own table
    2. find the transition for (current, trigger); none -> raise IllegalTransition
    3. evaluate the guard; blocked -> record a `transition_blocked` event, return current state
    4. update the entity's state
    5. insert the `state_transition` event (observability §4)
    6. commit
    Returns the new (or unchanged) state."""
```

Properties:
- **Atomic:** state and event commit together or not at all. The event log can never disagree with the state tables.
- **Idempotent triggers:** a trigger that is a no-op in the current state (e.g. a second `entry_filled` after OPEN) is declared as such in the spec and recorded once, not raised. Reconcile loops can be re-run safely.
- **Guards are pure:** they read the `ctx` passed in, never the DB or the clock, so they are unit-testable with plain values.
- **Terminal states have no outgoing transitions.** Any attempt raises.
- **Every spec is validated at import:** all targets are in `states`, every non-terminal state is reachable from `initial`, and every non-terminal state has a path to a terminal one (no dead ends).

## 4. Session machine (`trading_sessions` table, one row per trading day)

### 4.1 States

| State | Meaning | New entries allowed? |
|---|---|---|
| `PRE_OPEN` | Created at 08:45 for a trading day | no |
| `CONNECTED` | Token refreshed, feed connected, bar session started | no |
| `READY` | Watchlist chosen and subscribed (09:10) | no |
| `TRADING` | Market open, fresh ticks, no breaker tripped | **yes** |
| `HALTED` | A guard failed during the session (`halt_reason` column) | no; open trades stay managed |
| `NO_NEW_ENTRIES` | After 14:30 (design L-entry cutoff) | no; open trades stay managed |
| `SQUARING_OFF` | 15:14: flattening every trade | no |
| `CLOSED` | All trades flat, session over (terminal) | no |
| `ABANDONED` | Never reached TRADING (setup failed all day) (terminal) | no |

Non-trading days get no row (holiday / weekend): the scheduler doesn't start a session.

### 4.2 Transitions

| From | Trigger | To | Guard (blocked → stays, `transition_blocked` event) |
|---|---|---|---|
| — | `session_start` (08:45 task) | `PRE_OPEN` | trading day |
| `PRE_OPEN` | `setup_ok` | `CONNECTED` | token valid, feed connected |
| `CONNECTED` | `watchlist_ready` | `READY` | watchlist non-empty |
| `READY` | `market_open` (09:15 +) | `TRADING` | fresh tick (≤ 60 s), no breaker tripped, bot mode RUNNING |
| `TRADING` | `halt` | `HALTED` | — (reason required: `feed_stale`, `breaker:<name>`, `cycle_failures`, `paused`, `emergency_stop`) |
| `HALTED` | `resume` | `TRADING` | cause cleared **and** before 14:30 **and** cause is resumable (§4.3) |
| `TRADING` | `entry_cutoff` (14:30) | `NO_NEW_ENTRIES` | — |
| `HALTED` | `entry_cutoff` (14:30) | `NO_NEW_ENTRIES` | — (halt reason kept in history) |
| `TRADING`, `HALTED`, `NO_NEW_ENTRIES`, `READY`, `CONNECTED` | `square_off` (15:14) | `SQUARING_OFF` | **none: square-off is never blocked** |
| `SQUARING_OFF` | `all_flat` | `CLOSED` | every trade CLOSED/CANCELLED **and** broker net position 0 |
| `PRE_OPEN`, `CONNECTED` | `abandon` (15:30) | `ABANDONED` | — |

### 4.3 Halt policy

| Halt reason | Resumable same day? | Who clears it |
|---|---|---|
| `feed_stale` | yes | automatically, when ticks are fresh again |
| `cycle_failures` (3 consecutive) | no | operator restart next day (matches today's `_disabled_for_day`) |
| `breaker:intraday_daily_loss`, `breaker:consecutive_losses` | no | the day ends |
| `breaker:nifty_move` (market-wide move) | yes | automatically, when the move falls back inside the threshold |
| `paused` | yes | operator sets mode RUNNING |
| `emergency_stop` | no | operator, next day |

### 4.4 Who triggers what

| Trigger | Caller |
|---|---|
| `session_start`, `setup_ok` | `pre_market_executor_setup` (08:45) |
| `watchlist_ready` | `intraday_watchlist` (09:10) |
| `market_open`, `halt`, `resume` | the 30-minute cycle and the every-minute reconcile task (each run evaluates the guards) |
| `entry_cutoff`, `square_off`, `all_flat`, `abandon` | scheduled tasks at 14:30, 15:14, every minute after 15:14, 15:30 |

The **cycle asks the Session machine** "is the state TRADING?" before scoring anything. Every other component that today checks the clock or `_disabled_for_day` stops doing so.

## 5. Trade machine (`intraday_brackets` table, one row per trade)

Replaces the implicit states in `TradeSupervisor` (#45/#46). Adds an explicit `PROTECTING` state: today the position exists flagged "unprotected" between the entry fill and the stop being confirmed, but that window has no name.

### 5.1 States

| State | Meaning |
|---|---|
| `ENTRY_PENDING` | MIS LIMIT entry resting, valid 30 min |
| `PROTECTING` | Entry (partially) filled; stop and target being placed. **The position has no confirmed stop.** |
| `OPEN` | Stop confirmed at the broker (target usually too) |
| `EXITING` | A market exit or buy-back is in flight (net position ≠ 0) |
| `CLOSED` | Net position 0, realised P&L recorded (terminal) |
| `CANCELLED` | Entry expired or rejected with nothing filled (terminal) |

### 5.2 Transitions

| From | Trigger | To | Guard / note |
|---|---|---|---|
| — | `open_trade` | `ENTRY_PENDING` | plan valid: `0 < stop < entry < target`, qty ≥ 1, ATR > 0; **and session is TRADING** |
| `ENTRY_PENDING` | `entry_filled` (full or partial at expiry) | `PROTECTING` | filled qty > 0 |
| `ENTRY_PENDING` | `entry_expired` / `entry_rejected` / `square_off` | `CANCELLED` | filled qty = 0 |
| `PROTECTING` | `stop_confirmed` | `OPEN` | stop order status is resting at the broker |
| `PROTECTING` | `stop_failed` | `EXITING` | stop rejected or couldn't be submitted (L9): market exit + CRITICAL |
| `OPEN` | `leg_filled` | `EXITING` or `CLOSED` | OCO: cancel the other leg, then settle by **net filled quantity** (net 0 → CLOSED; ≠ 0 → corrective order → EXITING) |
| `OPEN` | `stop_lost` | `EXITING` | stop cancelled outside the bot / expired without filling: market exit + CRITICAL |
| `OPEN` | `square_off` | `EXITING` | cancel both legs, market exit |
| `EXITING` | `exit_filled` | `CLOSED` or `EXITING` | re-settle: net 0 → CLOSED, else another corrective order |
| `EXITING` | `exit_failed` | `EXITING` | retry at market + CRITICAL (self-transition, recorded each time) |

`PROTECTING` should last seconds. If a trade stays in it beyond one reconcile, that is itself a CRITICAL condition, and observability flags it (`designs/observability.md` §7).

### 5.3 Invariants (asserted in tests)

- A trade can only be in `OPEN` if its stop order is in a resting state at the broker.
- `CLOSED` implies the net filled quantity across the trade's orders is exactly 0.
- No transition moves `stop_price` (stops are never widened: CLAUDE.md §10). Trailing, if added later, is a separate transition with a guard that the new stop is strictly more favourable.

## 6. Order machine (`orders` table)

Already explicit (`ALLOWED_TRANSITIONS`). Changes:
1. Move `OrderManager._transition` onto the core, so every status change emits a `state_transition` event (history is no longer lost on overwrite).
2. Name the broker-mapping subtlety: Kite's `TRIGGER PENDING` means a stop **waiting** to trigger, but maps to our `TRIGGERED`. In the order machine, `PENDING` and `TRIGGERED` are both **resting** states. Parents must not read `TRIGGERED` as "fired".
3. `cancel()` already resolves the final state from the broker (#45): that becomes the `cancel_requested → FILLED | CANCELLED` transition pair.

## 7. Loopholes and decisions

| # | Loophole | Decision |
|---|---|---|
| SM1 | Two threads transition the same entity at once (cycle + reconcile task) | Each `transition()` runs inside a SQLite transaction that re-reads the state first (`BEGIN IMMEDIATE`); the second caller sees the new state and either no-ops or raises `IllegalTransition`. Orchestrator tasks are already serialized per task id |
| SM2 | A guard reads the clock or DB directly, making it untestable and racy | Guards are pure functions over `ctx`; the caller builds `ctx` (now, tick age, breaker states) once per evaluation |
| SM3 | Square-off blocked by a halt | `square_off` has **no guard** and is allowed from every non-terminal state (§4.2) |
| SM4 | Process restart mid-transition | State + event commit atomically; on restart each machine resumes from its table. A trade found in `PROTECTING` at boot is treated as `stop_failed` unless its stop is confirmed resting at the broker |
| SM5 | Machines start reading each other's events | Forbidden (§2.4). The events table is read only by the observability module, and a test enforces it (`designs/observability.md` §2) |
| SM6 | Session row missing (e.g. orchestrator started at 11:00 after a crash) | `resume_market_session` (#37) also creates or loads today's session row and replays the setup triggers |
| SM7 | Trade opened while the session is not TRADING | `open_trade` guard requires session TRADING; the cycle doesn't even score otherwise |
| SM8 | Broker and our tables disagree (manual trade in the Kite app, missed fill) | Not a machine transition. Observability detects it from broker snapshots (`designs/observability.md` §6) and alerts; the operator decides. The session's `all_flat` guard uses broker truth, so a mismatch blocks `CLOSED` |

## 8. Delivery (re-sequences B7, `designs/intraday-live-pipeline.md` §8)

| PR | Scope |
|---|---|
| SM-1 | `src/statemachine/` core + spec validation + `events` table and emitter (observability §4-5) + the guard test that keeps trading code from reading `events` + Order machine on the core |
| SM-2 | Trade machine on the core: reworks #46 (`TradeSupervisor`), adds `PROTECTING` |
| SM-3 | Session machine + `trading_sessions` migration. This **is** B7 PR 4c: the cycle, live circuit breakers and execution-broker wiring are built as its guards and transitions |
| OBS-* | Observability module (`designs/observability.md` §10). Replaces B7 PR 5 |

## 9. Open questions

| ID | Question | Recommendation |
|---|---|---|
| SMQ1 | Should `breaker:nifty_move` auto-resume, or stay halted for the day like the loss breakers? | Auto-resume when the move is back inside the threshold, before 14:30 |
| SMQ2 | Should `paused` (operator) also stop managing open trades? | No: paused blocks new entries only; stops and square-off always run (matches today's `trailing_stop_task` exemption) |
