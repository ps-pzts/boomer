"""Paper/live trading-mode switch (designs/intraday-live-pipeline.md §5).

Live needs two independent keys: the BOOMER_TRADING_MODE env var set to "live"
AND an enabled row in live_trading_authorisation. Anything else (unset env,
typo, missing table, no row, DB error) resolves to paper. Fail closed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.brokers.feeds.base import MarketDataFeed
from executor.brokers.paper_broker import PaperBroker

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ENV_VAR = "BOOMER_TRADING_MODE"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class ModeDecision:
    mode: TradingMode
    reason: str


class LiveTradingAuthorisationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def is_enabled(self) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM live_trading_authorisation ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return bool(row and row["enabled"] == 1)

    def set_enabled(self, enabled: bool, actor: str, reason: str) -> None:
        if not actor or not reason:
            raise ValueError("actor and reason are required for an auditable change")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO live_trading_authorisation (enabled, actor, reason, created_at)"
                " VALUES (?, ?, ?, ?)",
                (1 if enabled else 0, actor, reason, datetime.now(IST).isoformat()),
            )
        logger.warning(
            "live_trading_authorisation_changed enabled=%s actor=%s reason=%s",
            enabled,
            actor,
            reason,
        )


def resolve_trading_mode(
    env_value: str | None, store: LiveTradingAuthorisationStore
) -> ModeDecision:
    """Decide the mode and log which rule decided it (CLAUDE.md §9 attribution)."""
    requested = (env_value or "").strip().lower()
    if requested != TradingMode.LIVE:
        decision = ModeDecision(TradingMode.PAPER, f"{ENV_VAR}={env_value!r} (not 'live')")
    else:
        try:
            authorised = store.is_enabled()
        except Exception as exc:  # missing table, locked DB, ...: fail closed
            decision = ModeDecision(TradingMode.PAPER, f"authorisation check failed: {exc}")
        else:
            decision = (
                ModeDecision(TradingMode.LIVE, "env=live and DB authorisation enabled")
                if authorised
                else ModeDecision(TradingMode.PAPER, "env=live but DB authorisation not enabled")
            )
    logger.info("trading_mode_resolved mode=%s reason=%s", decision.mode, decision.reason)
    return decision


def build_execution_broker(
    mode: TradingMode,
    feed: MarketDataFeed,
    live_broker: Broker | None,
    paper_initial_cash: float,
    paper_book: object | None = None,
) -> Broker:
    """Paper: simulated fills on the live feed, Kite's tick sizes when a live broker
    is configured (#75). Live: the real broker."""
    if mode == TradingMode.LIVE:
        if live_broker is None:
            raise RuntimeError("live mode requested but no live broker is configured")
        return live_broker
    if paper_initial_cash <= 0:
        raise ValueError("paper_initial_cash must be positive")
    return PaperBroker(
        price_source=feed,
        initial_cash=paper_initial_cash,
        book=paper_book,
        tick_size=live_broker.tick_size if live_broker is not None else None,
    )


# ── Operator toggle (#76) ─────────────────────────────────────────────────────
#
# The env var stays the server-level arm ("live is allowed on this machine at
# all"); the toggle flips the audited live_trading_authorisation row. A mode is
# locked into a day's trading_sessions row when that session starts, so a
# toggle (or a restart) never changes the mode under open positions: it applies
# from the next session.


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    message: str


def session_mode(db_path: str | Path, day: date, env_value: str | None) -> str:
    """Today's locked mode if the session has started, else what it would start as."""
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        row = conn.execute(
            "SELECT mode FROM trading_sessions WHERE session_date=?", (day.isoformat(),)
        ).fetchone()
    if row:
        return row[0]
    return resolve_trading_mode(env_value, LiveTradingAuthorisationStore(db_path)).mode.value


def paper_record(db_path: str | Path) -> dict:
    """What the paper→live criteria (design §9 Q5) look like so far. Read-only."""
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        sessions = conn.execute(
            "SELECT COUNT(*) FROM trading_sessions WHERE mode='paper' AND state='closed'"
        ).fetchone()[0]
        trades, pnl, unprotected = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(b.realised_pnl), 0),"
            " SUM(CASE WHEN b.exit_reason LIKE 'stop\\_%' ESCAPE '\\'"
            "          AND b.exit_reason != 'stop_hit' THEN 1 ELSE 0 END)"
            " FROM intraday_brackets b JOIN trading_sessions s"
            " ON s.session_date = b.session_date AND s.mode = 'paper'"
            " WHERE b.state = 'closed'"
        ).fetchone()
    return {
        "paper_sessions_closed": sessions,
        "paper_trades": trades,
        "paper_realised_pnl": round(float(pnl), 2),
        "unprotected_exits": int(unprotected or 0),
    }


def request_mode(
    db_path: str | Path,
    target: str,
    *,
    actor: str,
    reason: str,
    env_value: str | None,
    confirmed: bool,
    today: date,
) -> SwitchResult:
    """Switch paper/live from the next session. Live needs the server armed (env)
    and an explicit confirmation after seeing the paper record."""
    target = (target or "").strip().lower()
    if target not in (TradingMode.PAPER, TradingMode.LIVE):
        return SwitchResult(False, f"Unknown mode {target!r}: use paper or live.")
    store = LiveTradingAuthorisationStore(db_path)
    current = session_mode(db_path, today, env_value)
    if target == TradingMode.LIVE:
        if (env_value or "").strip().lower() != TradingMode.LIVE:
            return SwitchResult(
                False,
                f"Live refused: this server is not armed ({ENV_VAR} is not 'live').",
            )
        rec = paper_record(db_path)
        if not confirmed:
            return SwitchResult(
                False,
                "Switching to LIVE (real money). Paper so far: "
                f"{rec['paper_sessions_closed']} sessions closed (criterion: ≥ 20), "
                f"{rec['paper_trades']} trades, P&L ₹{rec['paper_realised_pnl']:,.2f}, "
                f"{rec['unprotected_exits']} unprotected exits (criterion: 0). "
                "Repeat with confirmation to proceed.",
            )
    store.set_enabled(target == TradingMode.LIVE, actor=actor, reason=reason)
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        started = conn.execute(
            "SELECT 1 FROM trading_sessions WHERE session_date=?", (today.isoformat(),)
        ).fetchone()
    when = "from the next session" if started else "from today's session (08:45)"
    note = f" Today's session stays {current.upper()}." if started and current != target else ""
    return SwitchResult(True, f"Mode set to {target.upper()} {when}.{note}")
