"""Emergency stop: flatten broker MIS positions no trade owns (#67).

Operator rule (2026-11-19): an emergency stop flattens *everything*, including a
position opened by hand in the Kite app. The TradeSupervisor flattens our own
trades; this exits the rest at market.

Safety:
- Only symbols with NO active trade. Where a trade also holds the symbol, the
  trade's own exit is already in flight and the broker/our net can race (a fill
  between the two reads would look like a short and trigger a wrong BUY), so
  those are left to the `all_flat` guard + CRITICAL alert.
- At most one emergency exit in flight per symbol: the next tick re-reads the
  broker and only acts once the previous exit is terminal. Never doubles up.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import date

from executor.models import TERMINAL_STATUSES, OrderRequest, OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)

TRACK = "intraday"


def _key_prefix(day: date, symbol: str) -> str:
    return f"emergency-{day.isoformat()}-{symbol}-"


def _in_flight(conn: sqlite3.Connection, om: object, day: date, symbol: str) -> tuple[bool, int]:
    """(an earlier emergency exit is still working, how many were placed today)."""
    prefix = _key_prefix(day, symbol)
    rows = conn.execute(  # exact prefix: LIKE would treat "_" in a symbol as a wildcard
        "SELECT order_id FROM orders WHERE substr(idempotency_key, 1, ?) = ?",
        (len(prefix), prefix),
    ).fetchall()
    working = any(om.sync_status(r[0]).status not in TERMINAL_STATUSES for r in rows)  # type: ignore[attr-defined]
    return working, len(rows)


def flatten_untracked(
    conn: sqlite3.Connection,
    om: object,
    positions: Iterable[object],
    tracked_symbols: set[str],
    day: date,
) -> list[dict]:
    """Exit every broker MIS position whose symbol no active trade holds.
    Returns what was placed (or skipped and why), for the event and the alert."""
    actions: list[dict] = []
    for p in positions:
        qty = int(p.quantity)  # type: ignore[attr-defined]
        sym = p.symbol  # type: ignore[attr-defined]
        if str(p.product) != "mis" or qty == 0:  # type: ignore[attr-defined]
            continue
        if sym in tracked_symbols:
            actions.append({"symbol": sym, "net": qty, "action": "skipped_tracked"})
            continue
        working, n = _in_flight(conn, om, day, sym)
        if working:
            actions.append({"symbol": sym, "net": qty, "action": "exit_in_flight"})
            continue
        side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        try:
            order_id = om.submit(  # type: ignore[attr-defined]
                OrderRequest(
                    symbol=sym,
                    exchange=p.exchange,  # type: ignore[attr-defined]
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=abs(qty),
                    product=ProductType.MIS,
                    idempotency_key=f"{_key_prefix(day, sym)}{n + 1}",
                    tag="estop",
                ),
                TRACK,
            )
            actions.append(
                {"symbol": sym, "net": qty, "action": "exit_placed", "order_id": order_id}
            )
        except Exception as exc:  # keep going: one refusal must not stop the others
            logger.exception("emergency_flatten_failed symbol=%s", sym)
            actions.append({"symbol": sym, "net": qty, "action": "exit_failed", "error": str(exc)})
    return actions
