"""SessionController: drives today's Session machine (designs/state-machines.md §4, #63).

`tick()` runs every minute (orchestrator `session_tick`). Each tick:
  1. settles open trades: `TradeSupervisor.reconcile()` (stops, targets, OCO);
  2. builds the guard context ONCE (SM2): clock, feed health, bot mode, live
     circuit breakers, watchlist size;
  3. fires at most the transitions the current state and time call for.

Time-based transitions win over everything: from 15:14 the session squares off
(never blocked, SM3), from 14:30 no new entries. Every same-day resume goes
HALTED → RECONCILING → (clean broker reconcile) → TRADING (#58).

Threading: the supervisor and this controller share one SQLite connection; every
public method takes `lock` so the session tick and the decision cycle (SM-3b)
never interleave on it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from executor import session_machine as sm
from executor.brokers.feeds.base import NIFTY_50
from executor.session_inputs import (
    LegStatus,
    ReconcileReport,
    net_by_symbol,
    nifty_move_pct,
    realised_pnl,
    reconcile_report,
    tripped_breakers,
)
from statemachine import record_created, transition

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
ENTRY_CUTOFF = time(14, 30)
SQUARE_OFF = time(15, 14)
FEED_MAX_TICK_AGE_SECONDS = 60


class SessionController:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        mode: str,
        feed: object | None,
        supervisor: object | None,
        broker: object | None,
        bot_mode: Callable[[], str],
        watchlist_size: Callable[[date], int],
        ledger: Callable[[], object | None],
        risk_config: Callable[[], object],
        alerter: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
        lock: threading.RLock | None = None,
    ) -> None:
        self._conn = conn
        self._mode = mode
        self._feed = feed
        self._sup = supervisor
        self._broker = broker
        self._bot_mode = bot_mode
        self._watchlist_size = watchlist_size
        self._ledger = ledger
        self._risk_config = risk_config
        self._alerter = alerter
        self._clock = clock
        self.lock = lock or threading.RLock()

    # ── Queries ───────────────────────────────────────────────────────────────

    def state(self, day: date) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM trading_sessions WHERE session_date=?", (day.isoformat(),)
        ).fetchone()
        return row[0] if row else None

    def entries_allowed(self, day: date) -> bool:
        """SM7: the only question the decision cycle asks before scoring anything."""
        with self.lock:
            return self.state(day) in sm.ENTRIES_ALLOWED

    # ── Triggers from other tasks ─────────────────────────────────────────────

    def ensure(self, day: date) -> str:
        """Create today's row in PRE_OPEN if missing (`session_start`). Idempotent."""
        with self.lock:
            current = self.state(day)
            if current is not None:
                return current
            now = self._clock().replace(tzinfo=None).isoformat()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO trading_sessions (session_date, mode, state, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)",
                    (day.isoformat(), self._mode, sm.PRE_OPEN, now, now),
                )
                record_created(
                    self._conn,
                    sm.SESSION_SPEC,
                    day.isoformat(),
                    rule="session_start",
                    inputs={"mode": self._mode},
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return sm.PRE_OPEN

    def on_restart(self, day: date) -> None:
        """Boot mid-session (SM6/SM9): nothing held in memory can be trusted."""
        with self.lock:
            if self.state(day) in (sm.CONNECTED, sm.READY, sm.TRADING):
                self._fire(day, "restarted", rule="process_restarted")

    def acknowledge_mismatch(self, day: date, actor: str) -> None:
        """Operator has looked at a reconcile mismatch; the next reconcile may resume."""
        if not actor:
            raise ValueError("actor is required for an auditable acknowledgement")
        with self.lock:
            self._conn.execute(
                "UPDATE trading_sessions SET halt_acked_at=? WHERE session_date=?"
                " AND halt_reason='reconcile_mismatch'",
                (self._clock().replace(tzinfo=None).isoformat(), day.isoformat()),
            )
            self._conn.commit()
            logger.warning("session_mismatch_acknowledged date=%s actor=%s", day, actor)

    # ── The minute tick ───────────────────────────────────────────────────────

    def tick(self) -> str:
        """Advance today's session one step. Returns the state after the tick."""
        now = self._clock()
        day = now.date()
        with self.lock:
            state = self.ensure(day)
            if state in sm.TERMINAL:
                return state
            if self._sup is not None:
                self._sup.reconcile()  # type: ignore[attr-defined]
            ctx = self._context(day, now)
            self._step(day, now, state, ctx)
            return self.state(day) or state

    def _step(self, day: date, now: datetime, state: str, ctx: dict) -> None:
        t = now.time()
        if t >= SQUARE_OFF:
            self._end_of_day(day, state, ctx)
        elif t >= ENTRY_CUTOFF and state in (sm.TRADING, sm.HALTED, sm.RECONCILING):
            self._fire(day, "entry_cutoff", rule="entry_cutoff_1430")
        elif state == sm.PRE_OPEN:
            self._fire(day, "setup_ok", rule="feed_connected", ctx=ctx)
        elif state == sm.CONNECTED:
            self._fire(day, "watchlist_ready", rule="watchlist_chosen", ctx=ctx)
        elif state == sm.READY and t >= MARKET_OPEN:
            self._fire(day, "market_open", rule="market_open", ctx=ctx)
        elif state == sm.TRADING and ctx["blocker"]:
            self._halt(day, ctx["blocker"], ctx)
        elif state == sm.HALTED and ctx["halt_reason"] in sm.RESUMABLE:
            if self._fire(day, "recover", rule="cause_cleared", ctx=ctx):
                self._reconcile(day, ctx)
        elif state == sm.RECONCILING:
            self._reconcile(day, ctx)

    def _end_of_day(self, day: date, state: str, ctx: dict) -> None:
        if state in sm.NEVER_TRADED and ctx["active_trades"] == 0:
            self._fire(day, "abandon", rule="never_reached_trading", inputs={"state": state})
            return
        if state != sm.SQUARING_OFF:
            self._fire(day, "square_off", rule="square_off_1514", severity="warn")
        if self._sup is not None:
            self._sup.square_off_all()  # type: ignore[attr-defined]  # idempotent
        ctx["active_trades"] = self._active_trades()
        ctx["broker_open_symbols"] = sorted(self._broker_net())
        self._fire(day, "all_flat", rule="flat_at_broker", ctx=ctx)

    def _halt(self, day: date, reason: str, ctx: dict) -> None:
        resumable = reason in sm.RESUMABLE
        self._fire(
            day,
            "halt",
            rule=reason,
            inputs=dict(ctx),
            severity="warn" if resumable else "critical",
            fields={"halt_reason": reason, "halt_acked_at": None},
        )
        title = f"Trading HALTED — {reason}"
        body = (
            "Resumes after a clean reconcile once the cause clears."
            if resumable
            else "No new entries for the rest of the day. Open trades stay managed."
        )
        self._alert("warn" if resumable else "critical", title, body)

    def _reconcile(self, day: date, ctx: dict) -> None:
        report = self.reconcile_report(day, ctx)
        from observability.emit import emit

        emit(
            "decision",
            "reconcile_report",
            severity="info" if report.clean else "critical",
            entity_type="session",
            entity_id=day.isoformat(),
            **report.as_inputs(),
        )
        if report.clean:
            self._fire(
                day,
                "reconciled",
                rule="reconcile_clean",
                inputs=report.as_inputs(),
                ctx=ctx,
                fields={"halt_reason": None, "halt_acked_at": None},
            )
            return
        self._fire(
            day,
            "reconcile_failed",
            rule="reconcile_mismatch",
            inputs=report.as_inputs(),
            severity="critical",
            fields={"halt_reason": "reconcile_mismatch", "halt_acked_at": None},
        )
        self._alert(
            "critical",
            "Reconcile MISMATCH — trading halted",
            f"Failed checks: {', '.join(report.failures())}. {report.checks}. "
            "The bot will not trade against this. Fix it at the broker, then acknowledge.",
        )

    def reconcile_report(self, day: date, ctx: dict) -> ReconcileReport:
        """§4.5: our trades vs the broker (source of truth), feed, watchlist."""
        legs = [LegStatus(*x) for x in self._sup.open_legs()] if self._sup else []  # type: ignore[attr-defined]
        return reconcile_report(
            trade_net=self._sup.net_positions() if self._sup else {},  # type: ignore[attr-defined]
            broker_net=self._broker_net(),
            open_legs=legs,
            feed_fresh=ctx["feed_fresh"],
            watchlist_size=ctx["watchlist_size"],
        )

    # ── Context (SM2: built once per tick, guards stay pure) ─────────────────

    def _context(self, day: date, now: datetime) -> dict:
        health = self._feed.health() if self._feed is not None else None  # type: ignore[attr-defined]
        feed_fresh = bool(health and health.is_fresh(now, FEED_MAX_TICK_AGE_SECONDS))
        row = self._conn.execute(
            "SELECT halt_reason, halt_acked_at FROM trading_sessions WHERE session_date=?",
            (day.isoformat(),),
        ).fetchone()
        breakers = self._breakers(day, now)
        ctx = {
            "now": now.isoformat(),
            "feed_connected": bool(health and health.connected),
            "feed_fresh": feed_fresh,
            "tick_age_s": health.tick_age_seconds(now) if health else None,
            "bot_mode": str(self._bot_mode()),
            "breakers": breakers,
            "watchlist_size": self._watchlist_size(day),
            "market_open": now.time() >= MARKET_OPEN,
            "before_cutoff": now.time() < ENTRY_CUTOFF,
            "halt_reason": row[0] if row else None,
            "mismatch_acked": bool(row and row[1]),
            "active_trades": self._active_trades(),
        }
        ctx["blocker"] = _blocker(ctx, has_execution=self._sup is not None)
        return ctx

    def _breakers(self, day: date, now: datetime) -> list[str]:
        try:
            ledger = self._ledger()
            if ledger is None:
                return ["capital_unavailable"]
            nifty = self._feed.latest(NIFTY_50) if self._feed is not None else None  # type: ignore[attr-defined]
            move = (
                nifty_move_pct(nifty.prev_close, nifty.ltp) if nifty else nifty_move_pct(None, None)
            )
            return tripped_breakers(
                pnl=realised_pnl(self._conn, day),
                ledger=ledger,  # type: ignore[arg-type]
                nifty_move=move,
                now=now,
                config=self._risk_config(),  # type: ignore[arg-type]
            )
        except Exception as exc:  # fail closed: an unreadable breaker blocks entries
            logger.exception("session_breakers_failed")
            return [f"breakers_unavailable:{type(exc).__name__}"]

    def _active_trades(self) -> int:
        return self._sup.active_count() if self._sup is not None else 0  # type: ignore[attr-defined]

    def _broker_net(self) -> dict[str, int]:
        if self._broker is None:
            return {}
        return net_by_symbol(self._broker.list_positions())  # type: ignore[attr-defined]

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def _fire(
        self,
        day: date,
        trigger: str,
        *,
        rule: str,
        inputs: dict | None = None,
        ctx: dict | None = None,
        severity: str = "info",
        fields: dict | None = None,
    ) -> bool:
        """Transition (plus any session columns) in one transaction. True if it moved.
        A blocked guard commits only its `transition_blocked` event, never `fields`."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            r = transition(
                self._conn,
                sm.SESSION_SPEC,
                day.isoformat(),
                trigger,
                rule=rule,
                inputs=inputs if inputs is not None else dict(ctx or {}),
                ctx=ctx,
                severity=severity,
            )
            if r.changed and fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(
                    f"UPDATE trading_sessions SET {sets} WHERE session_date=?",
                    (*fields.values(), day.isoformat()),
                )
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        logger.info("session %s %s -> %s (%s)", day, trigger, r.state, r.blocked_by or "ok")
        return r.changed

    def _alert(self, level: str, title: str, body: str) -> None:
        log = logger.error if level == "critical" else logger.warning
        log("session_alert %s: %s", title, body)
        if self._alerter is not None:
            getattr(self._alerter, level)(title, body, source_task_id="session_tick")


# Blockers, most severe first: a halt records the first one, and only the
# resumable ones (feed_stale, nifty_move, paused) can clear the same day (§4.3).
def _blocker(ctx: dict, *, has_execution: bool) -> str | None:
    if ctx["bot_mode"] == "emergency_stop":
        return "emergency_stop"
    hard = [b for b in ctx["breakers"] if b not in sm.RESUMABLE]
    if hard:
        return hard[0]
    if not has_execution:
        return "execution_unavailable"
    if "breaker:nifty_move" in ctx["breakers"]:
        return "breaker:nifty_move"
    if ctx["bot_mode"] == "paused":
        return "paused"
    if not ctx["feed_fresh"]:
        return "feed_stale"
    return None
