"""SessionController: drives today's Session machine (designs/state-machines.md §4, #63).

`tick()` runs every minute (orchestrator `session_tick`). Each tick:
  1. settles open trades: `TradeSupervisor.reconcile()` (stops, targets, OCO);
  2. builds the guard context ONCE (SM2): clock, feed health, bot mode, live
     circuit breakers, watchlist size;
  3. fires at most the transitions the current state and time call for.

An operator EMERGENCY_STOP wins over everything (#67): the session squares off at
once, whatever the time, also exits broker MIS positions no trade owns, and ends
for the day. Otherwise time wins: from 15:14 the session squares off (never
blocked, SM3), from 14:30 no new entries. Every same-day resume goes
HALTED → RECONCILING → (clean broker reconcile) → TRADING (#58).

Threading: the supervisor and this controller share one SQLite connection; every
public method takes `lock` so the session tick and the decision cycle (SM-3b)
never interleave on it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from executor import session_machine as sm
from executor.kite_checks import gate as kite_gate
from executor.session_context import (  # noqa: F401  (re-exported: public import path)
    ENTRY_CUTOFF,
    FEED_MAX_TICK_AGE_SECONDS,
    MARKET_OPEN,
    SQUARE_OFF,
    ContextMixin,
    _blocker,
)
from executor.session_emergency import EmergencyMixin
from executor.session_halt import acknowledge_mismatch, halt_detail
from executor.session_inputs import ReconcileReport  # noqa: F401  (re-exported)
from executor.session_reconcile import ReconcileMixin
from statemachine import record_created, transition

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class SessionController(ContextMixin, ReconcileMixin, EmergencyMixin):
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        mode: str,
        feed: object | None,
        supervisor: object | None,
        broker: object | None,
        order_manager: object | None = None,
        bot_mode: Callable[[], str],
        watchlist_size: Callable[[date], int],
        ledger: Callable[[], object | None],
        risk_config: Callable[[], object],
        alerter: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
        lock: threading.RLock | None = None,
        execution_factory: Callable[[str], tuple | None] | None = None,
        kite_checks: Callable[[date], str | None] | None = None,
    ) -> None:
        self._conn = conn
        # #85: today's Kite pre-flight + canary; None = both passed. Defaults to the
        # kite_daily_checks table on this connection (written by other processes too).
        self._kite_checks = kite_checks or (lambda d: kite_gate(self._conn, d))
        # A str (fixed) or a resolver day -> mode (#76). Read once, when the day's
        # session row is created; after that the row's mode is the truth.
        self._mode_for = mode if callable(mode) else (lambda _d, m=mode: m)
        self._exec_day: date | None = None
        self._feed = feed
        self._sup = supervisor
        self._broker = broker
        self._om = order_manager
        self._bot_mode = bot_mode
        self._watchlist_size = watchlist_size
        self._ledger = ledger
        self._risk_config = risk_config
        self._alerter = alerter
        self._clock = clock
        self.lock = lock or threading.RLock()
        # Built lazily (#74): at the 03:00 boot the Kite token has expired and the
        # capital ledger may not exist yet, so execution is retried every tick
        # until it can be built. Returns (supervisor, broker, order_manager) or None.
        self._execution_factory = execution_factory

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def supervisor(self) -> object | None:
        """The TradeSupervisor; None when execution is unavailable (fail closed)."""
        return self._sup

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
            mode = str(self._mode_for(day))
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO trading_sessions (session_date, mode, state, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)",
                    (day.isoformat(), mode, sm.PRE_OPEN, now, now),
                )
                record_created(
                    self._conn,
                    sm.SESSION_SPEC,
                    day.isoformat(),
                    rule="session_start",
                    inputs={"mode": mode},
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

    def report_cycle_failures(self, day: date, error: str) -> None:
        """The decision cycle failed 3 times in a row (§4.3): no new entries today.
        Open trades stay managed by the tick."""
        with self.lock:
            if self.state(day) == sm.TRADING:
                self._halt(day, "cycle_failures", {"error": error})

    def acknowledge_mismatch(self, day: date, actor: str) -> None:
        """Operator has looked at a reconcile mismatch; the next reconcile may resume."""
        if not actor:
            raise ValueError("actor is required for an auditable acknowledgement")
        with self.lock:
            acknowledge_mismatch(self._conn, day, actor, self._clock())

    # ── The minute tick ───────────────────────────────────────────────────────

    def tick(self) -> str:
        """Advance today's session one step. Returns the state after the tick."""
        now = self._clock()
        day = now.date()
        with self.lock:
            state = self.ensure(day)
            if state in sm.TERMINAL:
                return state
            self._ensure_execution(day)
            if self._sup is not None:
                self._sup.reconcile()  # type: ignore[attr-defined]
            ctx = self._context(day, now)
            self._step(day, now, state, ctx)
            return self.state(day) or state

    def _step(self, day: date, now: datetime, state: str, ctx: dict) -> None:
        t = now.time()
        if ctx["bot_mode"] == "emergency_stop":
            self._end_of_day(day, state, ctx, emergency=True)
        elif t >= SQUARE_OFF:
            self._end_of_day(day, state, ctx)
        elif t >= ENTRY_CUTOFF and state in (sm.TRADING, sm.HALTED, sm.RECONCILING):
            self._fire(day, "entry_cutoff", rule="entry_cutoff_1430")
        elif ctx["kite_checks"] in sm.KITE_CHECK_HALTS and state in sm.CHECKS_HALT_FROM:
            self._halt(day, ctx["kite_checks"], ctx, trigger="checks_failed")
        elif state == sm.HALTED and ctx["halt_reason"] in sm.KITE_CHECK_HALTS:
            if not ctx["kite_checks"] and self._fire(
                day, "checks_passed", rule="kite_checks_rerun_passed", ctx=ctx
            ):
                self._reconcile(day, ctx)
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

    def _end_of_day(self, day: date, state: str, ctx: dict, emergency: bool = False) -> None:
        untracked = self._broker_net() if emergency else {}
        if state in sm.NEVER_TRADED and ctx["active_trades"] == 0 and not untracked:
            rule = "emergency_stop" if emergency else "never_reached_trading"
            self._fire(day, "abandon", rule=rule, inputs={"state": state})
            return
        if state != sm.SQUARING_OFF:
            rule = "emergency_stop" if emergency else "square_off_1514"
            self._fire(day, "square_off", rule=rule, severity="critical" if emergency else "warn")
            if emergency:
                self._alert(
                    "critical",
                    "EMERGENCY STOP — flattening everything",
                    "Cancelling every stop/target, exiting every position at market "
                    "(including positions the bot did not open). Session ends for today.",
                )
        if self._sup is not None:
            self._sup.square_off_all()  # type: ignore[attr-defined]  # idempotent
        if emergency:
            self._cancel_untracked_orders(day)
            self._flatten_untracked(day)
        ctx["active_trades"] = self._active_trades()
        ctx["broker_open_symbols"] = sorted(self._broker_net())
        self._fire(day, "all_flat", rule="flat_at_broker", ctx=ctx)

    def _halt(self, day: date, reason: str, ctx: dict, trigger: str = "halt") -> None:
        resumable = reason in sm.RESUMABLE
        self._fire(
            day,
            trigger,
            rule=reason,
            inputs=dict(ctx),
            severity="warn" if resumable else "critical",
            fields={
                "halt_reason": reason,
                "halt_acked_at": None,
                "halt_detail": json.dumps(halt_detail(ctx), default=str),
            },
        )
        title = f"Trading HALTED — {reason}"
        body = (
            "Resumes after a clean reconcile once the cause clears."
            if resumable
            else "Kite check failed: fix it, then re-run with /canary (resumes only if it passes)."
            if reason in sm.KITE_CHECK_HALTS
            else "No new entries for the rest of the day. Open trades stay managed."
        )
        self._alert("warn" if resumable else "critical", title, body)

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def mode(self, day: date) -> str | None:
        row = self._conn.execute(
            "SELECT mode FROM trading_sessions WHERE session_date=?", (day.isoformat(),)
        ).fetchone()
        return row[0] if row else None

    def _ensure_execution(self, day: date) -> None:
        """Build execution for `day` in the session's locked mode (#74, #76).
        Rebuilt when the day changes, never while a trade is active."""
        if self._execution_factory is None:
            return
        if self._sup is not None:
            if self._exec_day == day:
                return
            if self._sup.active_count():  # type: ignore[attr-defined]
                return  # MIS can't span days; never swap the broker under a trade
            self._sup = self._broker = self._om = None
        mode = self.mode(day) or str(self._mode_for(day))
        try:
            built = self._execution_factory(mode)
        except Exception:
            logger.exception("execution_build_failed mode=%s", mode)
            return
        if built is not None:
            self._sup, self._broker, self._om = built
            self._exec_day = day
            logger.info("execution_ready mode=%s day=%s", mode, day)

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
