"""Halt bookkeeping shared by the session controller and the operator surfaces (#103)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger(__name__)


def acknowledge_mismatch(conn: sqlite3.Connection, day: date, actor: str, now: datetime) -> bool:
    """Record the operator's ack of a reconcile mismatch (dashboard, Telegram /ack).
    True if the session is halted on a mismatch and was acknowledged."""
    cur = conn.execute(
        "UPDATE trading_sessions SET halt_acked_at=? WHERE session_date=?"
        " AND halt_reason='reconcile_mismatch'",
        (now.replace(tzinfo=None).isoformat(), day.isoformat()),
    )
    conn.commit()
    if cur.rowcount:
        logger.warning("session_mismatch_acknowledged date=%s actor=%s", day, actor)
    return bool(cur.rowcount)


def halt_detail(ctx: dict) -> dict:
    """What the operator needs to see in /halt: the inputs behind the halt, trimmed."""
    keep = ("feed_connected", "feed_fresh", "tick_age_s", "bot_mode", "breakers", "kite_checks",
            "active_trades", "watchlist_size")  # fmt: skip
    return {k: ctx.get(k) for k in keep if k in ctx}
