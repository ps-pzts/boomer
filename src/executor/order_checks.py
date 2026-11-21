"""Pre-trade safety checks for OrderManager.submit (the one choke point every order
passes through). Split out of order_manager.py (#95); the checks are unchanged."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.models import OrderRequest, OrderSide, OrderType, PreTradeCheckError, ProductType

IST = ZoneInfo("Asia/Kolkata")

_DUPLICATE_WINDOW_SECONDS = 30
_PRICE_SANITY_PCT = 0.05  # order must be within 5% of LTP


def pre_trade_checks(
    request: OrderRequest,
    broker: Broker,
    ltp: float | None,
    track: str,
    *,
    db: sqlite3.Connection,
    clock: Callable[[], datetime],
    now_fn: Callable[[], datetime],
) -> None:
    """Raise PreTradeCheckError if the request fails any of the checks below.
    `clock` is the injectable market clock (check 6); `now_fn` is wall time (check 3)."""
    # 0. Product must be MIS. Boomer trades intraday only: every position is
    # squared off the same day. A CNC (delivery) order would silently turn an
    # intraday trade into a held position (CLAUDE.md §10), so refuse it here,
    # the one choke point every order passes through.
    if request.product != ProductType.MIS:
        raise PreTradeCheckError(
            f"Product {request.product} rejected — only MIS (intraday) orders are allowed"
        )

    # 1. Quantity sanity
    if request.quantity <= 0:
        raise PreTradeCheckError(f"Invalid quantity: {request.quantity}")

    # 2. Price sanity vs LTP (skip for market orders without LTP)
    if ltp and request.order_type != OrderType.MARKET and request.price > 0:
        deviation = abs(request.price - ltp) / ltp
        if deviation > _PRICE_SANITY_PCT:
            raise PreTradeCheckError(
                f"Order price {request.price} deviates {deviation:.1%} from LTP {ltp} (max 5%)"
            )

    # 3. Duplicate detection — identical request in last 30s
    cutoff = (
        now_fn().replace(tzinfo=None) - timedelta(seconds=_DUPLICATE_WINDOW_SECONDS)
    ).isoformat()
    if request.idempotency_key:
        row = db.execute(
            "SELECT order_id FROM orders WHERE idempotency_key=? AND created_at>?",
            (request.idempotency_key, cutoff),
        ).fetchone()
        if row:
            raise PreTradeCheckError(f"Duplicate order (idempotency_key={request.idempotency_key})")

    # 4. Funds available. BUY only: an MIS SELL here is an exit (stop, target,
    # square-off) of shares already held, and must never be blocked for cash.
    funds = broker.get_funds()
    required = request.price * request.quantity if request.price else 0
    if request.side == OrderSide.BUY and required > 0 and funds.available_cash < required:
        raise PreTradeCheckError(
            f"Insufficient funds: need {required:.0f}, have {funds.available_cash:.0f}"
        )

    # 5. Symbol valid (not empty)
    if not request.symbol or not request.exchange:
        raise PreTradeCheckError("Symbol or exchange is empty")

    # 6. Market hours for intraday — caller must enforce; logged here only
    if track == "intraday":
        now = clock()
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if not (market_open <= now <= market_close):
            raise PreTradeCheckError("Intraday order outside market hours")

    # 7. Circuit limit — reject if LTP itself is at upper/lower circuit band
    # (meaning the stock is locked and orders will not execute)
    if ltp and request.price > 0:
        deviation_from_ltp = abs(request.price - ltp) / ltp
        if deviation_from_ltp >= 0.20:
            raise PreTradeCheckError(
                f"Order price {request.price} is at/beyond 20% circuit band (LTP={ltp})"
            )

    # 8. GTT duplicate check is handled in GttManager.place_gtt_for_position()
