# Observability: One-Way Event Stream

> **Status:** APPROVED 2026-11-19 (operator), issue #47. Not implemented yet. See §12 for delivery.
> **Companion doc:** `designs/state-machines.md`: every state transition there is an event here.
> **Replaces:** B7 PR 5 ("paper-session reporting") in `designs/intraday-live-pipeline.md` §8.

---

## 1. Goal

One place to see, at any moment, **what every part of the system believes, whether it is alive,
what the broker actually holds, what was decided or rejected and why, and the full story of any
trade**, instead of querying orders, positions, task runs, broker state and logs separately.

## 2. Principles

1. **Every component emits events into one `events` table** whenever something happens.
2. **The observability module only reads events.** It is the only reader.
3. **No trading component ever reads events to make a decision.** Decisions use each machine's own state tables (`designs/state-machines.md` §2). Events are a *copy* of what happened, never the channel things happen through. So if observability is broken, slow, or shows something wrong, trading is unaffected.
4. **Enforced, not remembered:** a test fails if any code outside `src/observability/` queries the `events` table (§11).
5. **Emitting never breaks trading.** Ordinary events are best-effort: `emit()` never raises, and failures are counted and reported in heartbeats. The one exception is state-transition events, which commit in the same SQLite transaction as the state change (§5.2). That adds no new failure mode: if that insert fails, the database itself is failing and the state change would fail anyway.
6. **Critical alerts stay at the source.** A component that detects danger (an unprotected position) alerts directly, as today, *and* emits an event. Observability can add derived alerts, but no safety alert depends on it.

## 3. What "see every moment" requires

An event stream silently lies unless it is all of these:

| Property | Threat | Mechanism |
|---|---|---|
| **Complete** | A state change happens but its event is lost or forgotten | State transitions commit atomically with their event (§5.2); per-source sequence numbers expose gaps (§4) |
| **Alive** | A dead component emits nothing; silence looks like calm | Heartbeats from every long-lived component; SILENT flagged after 3 missed beats (§7) |
| **True** | Events only say what the system *believes* | Broker snapshots every minute; the projection compares belief with broker reality and flags DRIFT (§8) |
| **Explained** | "Why didn't it trade at 11:00?" | Decisions *not* taken are emitted with rule and inputs (§6), as CLAUDE.md §9 already requires |
| **Traceable** | One trade's story is spread across components | Correlation ids on every event (§4) |
| **Affordable** | Per-tick events = millions of rows/day | No per-tick events; per-minute aggregates (§9) |

## 4. Event envelope: the `events` table

One table, same SQLite file as the state tables. The same file is required for atomic state + event commits: across `ATTACH`ed databases in WAL mode, SQLite does not guarantee atomicity.

| Column | Type | Meaning |
|---|---|---|
| `event_id` | INTEGER PK AUTOINCREMENT | Global order of insertion |
| `ts` | TEXT | IST ISO timestamp with milliseconds, set by the emitter |
| `session_date` | TEXT | IST trading date the event belongs to |
| `mode` | TEXT | `paper` / `live` (from `executor/trading_mode.py`) |
| `source` | TEXT | Emitting component, e.g. `orchestrator`, `feed`, `trade_supervisor` (§6) |
| `boot_id` | TEXT | Random id per process start |
| `source_seq` | INTEGER | Monotonic per (`source`, `boot_id`), so a gap means lost events |
| `kind` | TEXT | One of the kinds in §5.1 |
| `name` | TEXT | Specific event, e.g. `entry_filled`, `watchlist_selected` |
| `severity` | TEXT | `debug` / `info` / `warn` / `critical` |
| `entity_type` | TEXT NULL | `session` / `trade` / `order` / `symbol` / `component` |
| `entity_id` | TEXT NULL | The entity's id |
| `cycle_id` | TEXT NULL | Correlation: the 30-minute cycle run |
| `trade_id` | TEXT NULL | Correlation: the trade (bracket) |
| `order_id` | TEXT NULL | Correlation: the order |
| `symbol` | TEXT NULL | Correlation: the stock |
| `payload` | TEXT | JSON: everything else (rule, inputs, prices, from/to state) |
| `schema_version` | INTEGER | Envelope version, starting at 1 |

Indexes: `(session_date, ts)`, `(entity_type, entity_id, event_id)`, `(trade_id)`, `(kind, session_date)`.

## 5. Event kinds and emitter

### 5.1 Kinds

| Kind | Emitted when | Payload (minimum) |
|---|---|---|
| `state_transition` | A machine changes state (`designs/state-machines.md`) | `machine`, `from`, `to`, `trigger`, `rule`, `inputs` |
| `transition_blocked` | A guard blocked a transition | `machine`, `state`, `trigger`, `blocked_by`, `inputs` |
| `decision` | A component chose something (signal accepted, watchlist built, trade sized) | `rule`, `inputs`, `outcome` |
| `rejection` | A component declined something (signal below threshold, liquidity gate, cooldown) | `rule`, `inputs`, `outcome` |
| `heartbeat` | Every 30 s from each long-lived component | `queue_depth`, `dropped`, component-specific health |
| `broker_snapshot` | Every 60 s in market hours | broker positions, open orders, funds |
| `metric` | Per-minute aggregates | e.g. ticks/min, tick age per symbol |
| `alert_sent` | An alert went out (or failed to) | `title`, `channel`, `ok` |
| `lifecycle` | Component started / stopped / crashed | `version`, `config` |
| `error` | An exception was caught and handled | `exception`, `where` |

### 5.2 Two write paths

1. **State transitions: synchronous, in-transaction.** `statemachine.transition()` inserts the event using the machine's own connection inside the same transaction as the state update. These can't be lost and can't disagree with the state tables.
2. **Everything else: asynchronous, best-effort.** `observability.emit(...)` appends to an in-memory queue and returns immediately, never raising. One writer thread per process, with **its own SQLite connection** (so the feed thread can emit safely), batch-inserts every second. If the queue is full (10,000 events), events are dropped and counted; the count goes out in the next heartbeat so the loss itself is visible.

### 5.3 API sketch (`src/observability/emit.py`)

```python
emit(kind, name, *, severity="info", entity=None, symbol=None, cycle_id=None,
     trade_id=None, order_id=None, **payload) -> None      # never raises
emitter.start(db_path, source, mode)                      # once per process: boot_id, writer thread, `lifecycle/started`
```

## 6. Sources

| Source | Process | Emits |
|---|---|---|
| `orchestrator` | orchestrator | task started / finished / failed / skipped (with the scheduler's reason: holiday, paused, already_succeeded); heartbeat |
| `session` | orchestrator | Session machine transitions and blocked transitions |
| `feed` | orchestrator (feed thread) | connected / closed / reauthenticated; per-minute tick count and tick age per subscribed symbol; heartbeat |
| `bars` | orchestrator | session started; flush (rows, file) |
| `watchlist` | orchestrator | selected (size, by_score, by_liquidity); rejection counts by rule |
| `cycle` | orchestrator | cycle start / end; per-symbol `decision` / `rejection` with rule and inputs |
| `risk` | orchestrator | circuit breakers evaluated (inputs and status); tripped / cleared |
| `trade_supervisor` | orchestrator | Trade machine transitions; heartbeat of its reconcile loop |
| `order_manager` | orchestrator | Order machine transitions; pre-trade check rejections |
| `broker_snapshot` | orchestrator | `broker_snapshot` every 60 s (execution broker: paper or Kite) |
| `alerter` | every process | `alert_sent` |
| `dashboard`, `telegram_bot` | their own processes | lifecycle and heartbeat only |

## 7. Liveness

- Every long-lived component emits `heartbeat` every **30 s**. That covers the orchestrator loop, feed, reconcile loop, emitter writer, dashboard and bot.
- The projection marks a component **SILENT** after **90 s** without a heartbeat (3 missed beats) during the hours it should be alive. The feed, cycle and reconcile loop only need to beat during market hours.
- A gap in `source_seq` within one `boot_id` means events were lost. It is shown, and a derived `warn` alert is raised.
- A new `boot_id` for a source means it restarted. The `lifecycle/started` event and the previous boot's last event bracket the downtime.
- Derived condition: a trade in `PROTECTING` for more than one reconcile interval (`designs/state-machines.md` §5.2) is **critical**.

## 8. Truth: broker snapshots and drift

Events describe beliefs; the broker is reality.
- **Snapshot:** every 60 s from 09:00 to 15:45 IST, the `broker_snapshot` source asks the *execution broker* (paper or Kite) for positions, open orders and funds, and emits them as one event. Three Kite calls a minute is well within rate limits.
- **Drift** is computed by the projection, not by trading code:
  - **Position drift:** for each symbol, the net quantity implied by open trades (Trade machine events) vs the broker's net MIS quantity.
  - **Order drift:** open orders we track vs open orders at the broker (catches manual orders placed in the Kite app).
  - **Funds drift:** an informational comparison against the capital ledger.
- Drift persisting across **two consecutive snapshots** raises a derived critical alert. One snapshot of drift is normal, because a fill can land between the snapshot and the next reconcile.

## 9. Volume and retention

- **No per-tick events.** Ticks are aggregated per minute per symbol (`metric`). The minute bars (`designs/intraday-live-pipeline.md` §3.2) already hold the price history.
- Expected volume per trading day:
  - per-minute metrics, about 51 symbols × 375 min ≈ 19k rows (one row per minute carrying all symbols if that becomes too many)
  - heartbeats, about 6 sources × 2/min × 420 min ≈ 5k
  - snapshots ≈ 400
  - decisions and rejections ≈ 500-1,000
  - transitions ≈ a few hundred

  That's tens of thousands of rows a day.
- **Retention:** 30 days in SQLite. A nightly task moves older days to `events/date=YYYY-MM-DD/*.parquet`, the same layout as the minute bars, then deletes them from SQLite.

## 10. The observability module (`src/observability/`)

The only reader of `events`.

1. **Projection:** an in-memory model built by replaying today's events, then following new `event_id`s (polling every 1 s). It holds:
   - the Session state and the time and reason it entered it
   - per-component liveness (OK / SILENT / restarted)
   - feed tick age per symbol
   - circuit breaker status
   - every trade: state, quantity, entry, stop, target, last price (from metrics), unrealised P&L, protected yes/no
   - positions from the latest broker snapshot and the drift flags
   - the last N alerts

   Snapshots and heartbeats re-baseline it, so it self-corrects after a missed event.
2. **Surfaces:**
   - dashboard **`/live`**: the projection, pushed over the existing WebSocket manager
   - **`/live/trade/{id}`** and **`/live/session/{date}`**: timelines, i.e. every event with that correlation id, in order
   - Telegram **`/status`**: **Done 2026-11-19 (#103)** as a rendering of the session, Trade and Kite-check tables (not yet of the projection). The live trade/session feed and end-of-day summary run as `observability/telegram_feed.py`, a read-only events tail
3. **Derived alerts** via the existing alerter: SILENT component, `source_seq` gap, persistent DRIFT, PROTECTING too long, Session entering HALTED. These are *additional* to the critical alerts sent at source (§2.6).

## 11. Enforcement

`tests/test_events_read_only.py` scans `src/` and fails if any file **outside `src/observability/`**:
- contains SQL reading the events table (`FROM events`, `JOIN events`, case-insensitive), or
- imports the projection or reader modules.

Writing remains allowed everywhere, through `emit()` or `statemachine.transition()` only. This keeps principle §2.3 true as the code grows.

## 12. Delivery

| PR | Scope | Pairs with |
|---|---|---|
| OBS-1 | `events` migration, emitter (queue + writer thread, own connection, boot_id/seq), `lifecycle` and `heartbeat` events, enforcement test. **Done 2026-11-19 (#56)**: migration `0012`, `src/observability/emit.py`, `tests/test_events_read_only.py`; orchestrator heartbeat carries feed health | SM-1 |
| OBS-2 | `broker_snapshot` source; decision/rejection events in watchlist, cycle and risk; orchestrator task events | SM-3 (B7 4c) |
| OBS-3 | Projection + `/live`, timelines, Telegram `/status`, derived alerts | after SM-3 |
| OBS-4 | Nightly archive to parquet + 30-day retention | any time after OBS-1 |

## 13. Loopholes and decisions

| # | Loophole | Decision |
|---|---|---|
| O1 | Trading code starts reading events "just this once" | Forbidden; the enforcement test (§11) fails the build |
| O2 | Event write blocks trading (lock contention) | Async path: one writer thread per process, batched every 1 s, WAL mode. Sync path (transitions) is a single extra insert in a transaction that already writes |
| O3 | Queue overflow during an incident (when events matter most) | Drop and count; the count rides on the next heartbeat; a `source_seq` gap shows it too. Never block the emitting thread |
| O4 | Clock skew between processes | All processes run on one host with one clock; `ts` is IST wall-clock; ordering within a process uses `source_seq`, globally `event_id` |
| O5 | Paper and live events mixed | `mode` column on every event; the projection and surfaces show the mode prominently |
| O6 | Sensitive data in payloads (tokens, credentials) | Emitter rejects payload keys matching a denylist (`token`, `secret`, `password`, `api_key`) and logs the attempt |
| O7 | Projection wrong after a missed event | Broker snapshots and heartbeats re-baseline every minute; DRIFT surfaces disagreement instead of hiding it |
| O8 | Observability process down | Trading unaffected by design. Critical alerts still go out from source. The dashboard shows "projection stale since …" based on its own last processed `event_id` time |
| O10 | The codebase imports packages under two roots (`observability` via the editable install, `src.observability` from orchestrator code), which would create two emitters in one process, half the events going to the unconfigured one | `emit.py` registers itself under both module names at import; a test checks both import orders (#56) |
| O9 | Event schema drifts | `schema_version` in the envelope; the projection ignores unknown kinds/names (logs once) instead of crashing |

## 14. Open questions

| ID | Question | Recommendation |
|---|---|---|
| OQ1 | Retention in SQLite before archiving | 30 days |
| OQ2 | Heartbeat interval / SILENT threshold | 30 s / 90 s |
| OQ3 | Dashboard update: WebSocket push or polling | Push over the existing `/ws` manager; fall back to 5 s polling |
| OQ4 | Per-minute tick metrics: one row per symbol, or one row per minute with all symbols | One row per minute with all symbols in the payload (≈375 rows/day instead of ≈19k) |
