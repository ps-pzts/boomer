"""Orders whose placement outcome was unknown (#97).

`place_order` can reach Kite and lose only the answer (a timeout). OrderManager first
tries to find and adopt the order by its idempotency key. If the broker cannot even be
asked at that moment the order row is left ERROR with no broker id, and the order may
be resting at Kite with nothing tracking it. `cancel_lost_orders` runs every reconcile:
it finds each such order and cancels it; if it already filled meanwhile, the shares
belong to no trade, so they are flattened with an opposite MARKET order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
LOOKBACK = timedelta(hours=1)


def cancel_lost_orders(om: object) -> int:
    """Returns how many ghost orders were cancelled."""
    cutoff = (datetime.now(IST).replace(tzinfo=None) - LOOKBACK).isoformat()
    rows = om._db.execute(  # type: ignore[attr-defined]
        "SELECT order_id, idempotency_key FROM orders WHERE status=?"
        " AND (broker_order_id IS NULL OR broker_order_id='')"
        " AND idempotency_key IS NOT NULL AND idempotency_key != '' AND created_at>?",
        (OrderStatus.ERROR.value, cutoff),
    ).fetchall()
    cancelled = 0
    for order_id, key in rows:
        try:
            broker = om._broker_for()  # type: ignore[attr-defined]
            found = broker.find_order(key)
            if found is None:
                continue
            om._update_broker_order_id(order_id, found)  # type: ignore[attr-defined]
            try:
                broker.cancel_order(found)
                cancelled += 1
                logger.warning("lost_order_cancelled order_id=%s found=%s", order_id, found)
            except Exception as exc:
                # Too late: it filled while we could not see it. Those shares belong to no
                # trade, so flatten them rather than hold an unmanaged position.
                _flatten_lost_fill(om, order_id, found, broker, exc)
        except Exception as exc:  # broker still unreachable: try again next reconcile
            logger.warning("cancel_lost_order_failed order_id=%s error=%s", order_id, exc)
    return cancelled


def _flatten_lost_fill(
    om: object, order_id: str, found: str, broker: object, why: Exception
) -> None:
    raw = broker.get_order_status(found) or {}  # type: ignore[attr-defined]
    qty = int(raw.get("filled_quantity") or 0)
    if qty <= 0:
        logger.error("lost_order_not_cancellable order_id=%s error=%s", order_id, why)
        return
    rec = om._load_order(order_id)  # type: ignore[attr-defined]
    opposite = OrderSide.SELL if rec.side == OrderSide.BUY else OrderSide.BUY
    logger.error("lost_order_filled_flattening order_id=%s qty=%s", order_id, qty)
    om.submit(  # type: ignore[attr-defined]
        OrderRequest(
            symbol=rec.symbol,
            exchange=rec.exchange,
            side=opposite,
            order_type=OrderType.MARKET,
            quantity=qty,
            product=ProductType.MIS,
            idempotency_key=f"{order_id}-undo",
        ),
        "intraday",
    )
