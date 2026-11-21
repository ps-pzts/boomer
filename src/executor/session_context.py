"""Guard context for SessionController (SM2): built once per tick from the clock,
feed health, bot mode, breakers and Kite checks. Split out of session_controller.py (#95)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time

from executor import session_machine as sm
from executor.brokers.feeds.base import NIFTY_50
from executor.session_inputs import net_by_symbol, nifty_move_pct, realised_pnl, tripped_breakers

logger = logging.getLogger(__name__)

MARKET_OPEN = time(9, 15)
ENTRY_CUTOFF = time(14, 30)
SQUARE_OFF = time(15, 14)
FEED_MAX_TICK_AGE_SECONDS = 60


class ContextMixin:
    """Mixed into SessionController; relies on its `_conn`, `_feed`, `_sup`, `_broker`,
    `_ledger`, `_risk_config`, `_bot_mode`, `_watchlist_size`, `_kite_checks`."""

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
            "kite_checks": self._kite_check_state(day),
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

    def _kite_check_state(self, day: date) -> str | None:
        try:
            return self._kite_checks(day)
        except Exception:  # fail closed: unreadable checks block entries
            logger.exception("kite_checks_unavailable")
            return "kite_checks_unavailable"

    def _active_trades(self) -> int:
        return self._sup.active_count() if self._sup is not None else 0  # type: ignore[attr-defined]

    def _broker_net(self) -> dict[str, int]:
        if self._broker is None:
            return {}
        return net_by_symbol(self._broker.list_positions())  # type: ignore[attr-defined]


# EMERGENCY_STOP is not a blocker: it squares off at once (#67, see _step).
# Blockers, most severe first: a halt records the first one, and only the
# resumable ones (feed_stale, nifty_move, paused) can clear the same day (§4.3).
def _blocker(ctx: dict, *, has_execution: bool) -> str | None:
    hard = [b for b in ctx["breakers"] if b not in sm.RESUMABLE]
    if hard:
        return hard[0]
    if ctx.get("kite_checks"):  # #85: today's Kite pre-flight + canary not both passed
        return ctx["kite_checks"]
    if not has_execution:
        return "execution_unavailable"
    if "breaker:nifty_move" in ctx["breakers"]:
        return "breaker:nifty_move"
    if ctx["bot_mode"] == "paused":
        return "paused"
    if not ctx["feed_fresh"]:
        return "feed_stale"
    return None
