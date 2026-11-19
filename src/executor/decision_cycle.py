"""The intraday decision cycle: opens trades while the session is TRADING (#69).

designs/intraday-live-pipeline.md §3.5 on the Session machine (SM7): the cycle
asks the session "may we enter?" and does nothing else otherwise. Per run:

  1. session TRADING?  open trades < MAX_OPEN_TRADES (operator: 3)?
  2. for each watchlist symbol, best first, while slots remain:
       skip if a trade is active on it, or one was opened in the last 60 min
       (L2 cooldown, derived from intraday_brackets so a restart keeps it)
       features = batch (point-in-time, today) + live (ticks, minute bars)
       brain.intraday_decision.decide() -> EntryDecision | Rejection
       persist the signal (+ plan); emit the decision or the rejection
       under the session lock: re-check TRADING, then TradeSupervisor.open_trade
  3. three failed cycles in a row -> session HALTED(cycle_failures)

Every accept and reject is an event naming the rule and its inputs (CLAUDE.md §9).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from brain.features.live import compute_live_features
from brain.intraday_decision import EntryDecision, Rejection, decide
from brain.signal_store import save_plan, save_signal
from executor.brokers.feeds.base import NIFTY_50
from executor.trade_lifecycle import TradeRequest

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MAX_OPEN_TRADES = 3  # operator decision 2026-11-19 (#69)
COOLDOWN_MINUTES = 60  # design L2
MAX_CONSECUTIVE_FAILURES = 3  # design §4.3 cycle_failures
_ACTIVE = ("entry_pending", "protecting", "open", "exiting")


@dataclass
class CycleResult:
    skipped: str | None = None
    opened: list[str] = field(default_factory=list)  # bracket ids
    rejected: dict[str, str] = field(default_factory=dict)  # symbol -> rule


class DecisionCycle:
    def __init__(
        self,
        *,
        db_path: str,
        session: object,
        feed: object,
        bar_store: Callable[[], object | None],
        batch_features: Callable[[str, str, date], dict[str, Any]],
        latest_news: Callable[[str], datetime | None],
        regime: Callable[[date], str],
        risk_config: Callable[[], object],
        ledger: Callable[[], object | None],
        watchlist: Callable[[date], list[object]],
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
    ) -> None:
        self._db_path = db_path
        self._session = session
        self._feed = feed
        self._bar_store = bar_store
        self._batch = batch_features
        self._news = latest_news
        self._regime = regime
        self._risk_config = risk_config
        self._ledger = ledger
        self._watchlist = watchlist
        self._clock = clock
        self._failures = 0

    def run(self) -> CycleResult:
        """One cycle. Raises after counting a failure (the task records it)."""
        now = self._clock()
        try:
            result = self._run(now)
        except Exception as exc:
            self._failures += 1
            logger.exception("decision_cycle_failed consecutive=%d", self._failures)
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._session.report_cycle_failures(now.date(), str(exc))  # type: ignore[attr-defined]
            raise
        self._failures = 0
        _emit(
            "decision",
            "cycle_completed",
            skipped=result.skipped,
            opened=len(result.opened),
            rejected=len(result.rejected),
        )
        return result

    # ── One cycle ─────────────────────────────────────────────────────────────

    def _run(self, now: datetime) -> CycleResult:
        day = now.date()
        if not self._session.entries_allowed(day):  # type: ignore[attr-defined]
            state = self._session.state(day)  # type: ignore[attr-defined]
            return CycleResult(skipped=f"session_{state}")
        sup = self._session.supervisor  # type: ignore[attr-defined]
        if sup is None:  # execution not built yet (#74): nothing can be opened
            return CycleResult(skipped="execution_unavailable")
        with self._session.lock:  # type: ignore[attr-defined]
            slots = MAX_OPEN_TRADES - sup.active_count()  # type: ignore[attr-defined]
        if slots <= 0:
            return CycleResult(skipped="max_open_trades")
        ledger = self._ledger()
        if ledger is None:
            return CycleResult(skipped="capital_unavailable")
        config, regime = self._risk_config(), self._regime(day)
        active, cooling = self._busy_symbols(day, now)
        result = CycleResult()
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            for entry in self._watchlist(day):
                if slots <= 0:
                    break
                sym, exch = entry.stock_symbol, entry.exchange  # type: ignore[attr-defined]
                rule = "already_open" if sym in active else "cooldown" if sym in cooling else None
                if rule:
                    result.rejected[sym] = rule
                    continue
                outcome = self._evaluate(conn, sym, exch, day, now, regime, config, ledger)
                if isinstance(outcome, Rejection):
                    result.rejected[sym] = outcome.rule
                    _emit(
                        "rejection",
                        "entry_rejected",
                        symbol=sym,
                        rule=outcome.rule,
                        inputs=outcome.inputs,
                    )
                    continue
                bid = self._open(outcome, day)
                if bid is None:
                    result.rejected[sym] = "order_refused"
                    if not self._session.entries_allowed(day):  # type: ignore[attr-defined]
                        break  # the session left TRADING mid-cycle (halt, emergency)
                    continue
                result.opened.append(bid)
                slots -= 1
        finally:
            conn.close()
        return result

    def _evaluate(self, conn, sym, exch, day, now, regime, config, ledger):
        try:
            batch = self._batch(sym, exch, day)
            store = self._bar_store()
            tick = self._feed.latest(sym)  # type: ignore[attr-defined]
            live = compute_live_features(
                tick,
                self._feed.latest(NIFTY_50),  # type: ignore[attr-defined]
                store.bars(sym) if store is not None else [],  # type: ignore[attr-defined]
                store.coverage_start if store is not None else None,  # type: ignore[attr-defined]
                batch,
                self._news(sym),
                now,
            )
            features = {**batch, **live}
            outcome = decide(
                symbol=sym,
                exchange=exch,
                features=features,
                ltp=tick.ltp if tick else None,
                regime=regime,
                now=now,
                config=config,
                ledger=ledger,
            )
            self._persist(conn, outcome)
            return outcome
        except Exception as exc:  # one bad symbol must not fail the cycle
            logger.exception("decision_symbol_failed symbol=%s", sym)
            return Rejection(sym, f"error:{type(exc).__name__}", {"error": str(exc)})

    def _persist(self, conn: sqlite3.Connection, outcome: EntryDecision | Rejection) -> None:
        if outcome.signal is None:
            return
        save_signal(conn, outcome.signal)
        if outcome.plan is not None:
            strategy = outcome.strategy.value if isinstance(outcome, EntryDecision) else None
            save_plan(conn, outcome.plan, strategy)
        conn.commit()

    def _open(self, d: EntryDecision, day: date) -> str | None:
        req = TradeRequest(
            symbol=d.symbol,
            exchange=d.exchange,
            quantity=d.quantity,
            entry_price=float(d.entry_price),
            stop_price=float(d.stop_price),
            target_price=float(d.target_price),
            atr=float(d.atr),
            trade_plan_id=d.plan.plan_id,
            entry_trigger=float(d.entry_trigger) if d.entry_trigger is not None else None,
        )
        with self._session.lock:  # type: ignore[attr-defined]
            if not self._session.entries_allowed(day):  # type: ignore[attr-defined]
                _emit("rejection", "entry_rejected", symbol=d.symbol, rule="session_not_trading")
                return None
            try:
                bid = self._session.supervisor.open_trade(req)  # type: ignore[attr-defined]
            except Exception as exc:  # ValueError (plan) / PreTradeCheckError (order)
                logger.warning("entry_refused symbol=%s error=%s", d.symbol, exc)
                _emit(
                    "rejection",
                    "entry_rejected",
                    symbol=d.symbol,
                    rule="order_refused",
                    inputs={**d.as_inputs(), "error": str(exc)},
                )
                return None
        _emit("decision", "trade_opened", symbol=d.symbol, trade_id=bid, inputs=d.as_inputs())
        logger.info("trade_opened symbol=%s bracket=%s %s", d.symbol, bid, d.as_inputs())
        return bid

    def _busy_symbols(self, day: date, now: datetime) -> tuple[set[str], set[str]]:
        """(symbols with an active trade, symbols in the 60-min cooldown). From
        intraday_brackets, so a restart keeps both (L2)."""
        since = (now - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            rows = conn.execute(
                "SELECT symbol, state, created_at FROM intraday_brackets WHERE session_date=?",
                (day.isoformat(),),
            ).fetchall()
        finally:
            conn.close()
        active = {s for s, st, _ in rows if st in _ACTIVE}
        cooling = {s for s, _, created in rows if created >= since}
        return active, cooling


def _emit(kind: str, name: str, **payload: Any) -> None:
    from observability.emit import emit

    emit(kind, name, source="cycle", **payload)
