"""Inputs the Session machine's guards need, computed from live data (#63).

Pure functions plus small reads of our own state tables. Nothing here reads the
`events` table (designs/observability.md §2): the breakers use realised P&L
from `intraday_brackets`, the reconcile report compares against the broker.

- `tripped_breakers`: live circuit breakers (designs/state-machines.md §4.3)
  from today's realised intraday P&L, the losing streak, the week's P&L, the
  capital ledger's high-water mark and the NIFTY 50 move.
- `reconcile_report`: the §4.5 checks that gate RECONCILING → TRADING.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from capital.circuit_breakers import BreakerStatus, evaluate_circuit_breakers
from capital.models import CapitalLedgerRow, RiskConfig, Track

# Breaker field -> halt reason. intraday_late_entry is not a halt: the 14:30
# entry_cutoff transition owns it. black_swan here is only the NIFTY move.
_BREAKER_REASONS = {
    "intraday_daily_loss": "breaker:intraday_daily_loss",
    "intraday_consecutive_losses": "breaker:consecutive_losses",
    "portfolio_daily_loss": "breaker:portfolio_daily_loss",
    "portfolio_weekly_loss": "breaker:portfolio_weekly_loss",
    "portfolio_max_drawdown": "breaker:portfolio_max_drawdown",
    "black_swan": "breaker:nifty_move",
}


@dataclass(frozen=True)
class PnlToday:
    realised: Decimal
    consecutive_losses: int  # trailing streak of losing trades closed today
    realised_week: Decimal


def realised_pnl(conn: sqlite3.Connection, session_date: date) -> PnlToday:
    """Realised P&L of closed intraday trades: today, the losing streak, and the
    ISO week to date (Monday onwards)."""
    rows = conn.execute(
        "SELECT realised_pnl FROM intraday_brackets WHERE state='closed' AND session_date=?"
        " ORDER BY closed_at",
        (session_date.isoformat(),),
    ).fetchall()
    pnls = [Decimal(str(r[0] or 0)) for r in rows]
    streak = 0
    for p in reversed(pnls):
        if p >= 0:
            break
        streak += 1
    monday = session_date - timedelta(days=session_date.weekday())
    week = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl), 0) FROM intraday_brackets"
        " WHERE state='closed' AND session_date BETWEEN ? AND ?",
        (monday.isoformat(), session_date.isoformat()),
    ).fetchone()[0]
    return PnlToday(sum(pnls, Decimal("0")), streak, Decimal(str(week)))


def nifty_move_pct(prev_close: float | None, ltp: float | None) -> Decimal:
    """NIFTY 50 move today as a fraction (-0.031 = down 3.1%). 0 when unknown: the
    feed-staleness guard, not this breaker, covers a missing index tick."""
    if not prev_close or ltp is None:
        return Decimal("0")
    return (Decimal(str(ltp)) - Decimal(str(prev_close))) / Decimal(str(prev_close))


def tripped_breakers(
    *,
    pnl: PnlToday,
    ledger: CapitalLedgerRow,
    nifty_move: Decimal,
    now: datetime,
    config: RiskConfig,
) -> list[str]:
    """Halt reasons for every tripped breaker, most severe (non-resumable) first."""
    capital = ledger.total_capital
    live_total = capital + pnl.realised
    hwm = max(ledger.high_water_mark, live_total)
    drawdown = (hwm - live_total) / hwm if hwm > 0 else Decimal("0")
    state = evaluate_circuit_breakers(
        intraday_realised_pnl_today=pnl.realised,
        intraday_bucket_capital=ledger.bucket_capital(Track.INTRADAY),
        intraday_consecutive_losses_today=pnl.consecutive_losses,
        portfolio_realised_pnl_today=pnl.realised,  # intraday is the only track
        total_capital=capital,
        portfolio_realised_pnl_this_week=pnl.realised_week,
        live_drawdown_pct=drawdown,
        nifty_intraday_move_pct=nifty_move,
        current_time_ist_hour=now.hour,
        current_time_ist_minute=now.minute,
        black_swan_manually_tripped=False,
        config=config,
    )
    tripped = [
        reason
        for name, reason in _BREAKER_REASONS.items()
        if getattr(state, name) == BreakerStatus.TRIPPED
    ]
    return sorted(tripped, key=lambda r: r == "breaker:nifty_move")  # resumable last


# ── Reconcile before resume (designs/state-machines.md §4.5, #58) ─────────────


@dataclass(frozen=True)
class LegStatus:
    bracket_id: str
    symbol: str
    leg: str  # "stop" | "target"
    order_id: str | None
    resting: bool


@dataclass
class ReconcileReport:
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(c["ok"] for c in self.checks.values())

    def failures(self) -> list[str]:
        return [name for name, c in self.checks.items() if not c["ok"]]

    def as_inputs(self) -> dict[str, Any]:
        return {"clean": self.clean, "checks": self.checks}


def reconcile_report(
    *,
    trade_net: Mapping[str, int],
    broker_net: Mapping[str, int],
    open_legs: Iterable[LegStatus],
    feed_fresh: bool,
    watchlist_size: int,
    untracked_orders: list[dict] | None = (),  # type: ignore[assignment]
) -> ReconcileReport:
    """Pure: compare our trades against the broker. A clean report is the only way
    back to TRADING; any failure halts with `reconcile_mismatch` and the operator
    decides (the bot never trades against an unexplained position)."""
    symbols = sorted(set(trade_net) | set(broker_net))
    qty_mismatch = {
        s: {"ours": trade_net.get(s, 0), "broker": broker_net.get(s, 0)}
        for s in symbols
        if trade_net.get(s, 0) != broker_net.get(s, 0)
    }
    untracked = sorted(s for s in broker_net if broker_net[s] and not trade_net.get(s))
    legs = list(open_legs)
    not_resting = [
        {"bracket_id": leg.bracket_id, "symbol": leg.symbol, "leg": leg.leg}
        for leg in legs
        if not leg.resting
    ]
    stops_missing = [x for x in not_resting if x["leg"] == "stop"]
    r = ReconcileReport()
    r.checks["net_quantity_matches_broker"] = {"ok": not qty_mismatch, "mismatch": qty_mismatch}
    r.checks["no_untracked_broker_position"] = {"ok": not untracked, "symbols": untracked}
    r.checks["tracked_legs_resting"] = {
        "ok": not not_resting,
        "checked": len(legs),
        "not_resting": not_resting,
    }
    r.checks["open_trades_have_stop"] = {"ok": not stops_missing, "missing": stops_missing}
    # #65: an order working at the broker that we don't track (manual, or left by
    # a crash) could fire later and open a position nobody manages. None = the
    # broker couldn't be asked: fail closed.
    r.checks["no_untracked_resting_order"] = (
        {"ok": False, "error": "broker open orders unavailable"}
        if untracked_orders is None
        else {"ok": not untracked_orders, "orders": list(untracked_orders)}
    )
    r.checks["feed_fresh"] = {"ok": feed_fresh}
    r.checks["watchlist_present"] = {"ok": watchlist_size > 0, "size": watchlist_size}
    return r


def net_by_symbol(positions: Iterable[Any]) -> dict[str, int]:
    """Broker MIS net quantity per symbol, zeros dropped."""
    net: dict[str, int] = defaultdict(int)
    for p in positions:
        if str(p.product) == "mis":
            net[p.symbol] += int(p.quantity)
    return {s: q for s, q in net.items() if q}
