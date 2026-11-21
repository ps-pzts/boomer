from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.lost_orders import cancel_lost_orders
from executor.models import (
    TERMINAL_STATUSES,
    BrokerName,
    BrokerRejected,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    PreTradeCheckError,
    StateMachineError,
)
from executor.order_checks import _DUPLICATE_WINDOW_SECONDS, _PRICE_SANITY_PCT, pre_trade_checks  # noqa: F401
from executor.order_machine import ORDER_SPEC
from executor.order_store import OrderStoreMixin
from executor.ticks import on_tick, round_to_tick
from statemachine import IllegalTransition, transition

logger = logging.getLogger(__name__)

CANCEL_POLLS = 5  # #73: wait up to ~1 s for the broker to confirm a cancel
CANCEL_POLL_SECONDS = 0.25
# Statuses meaning the order is still working at the broker after a cancel attempt (#98).
_STILL_WORKING = frozenset({OrderStatus.PENDING, OrderStatus.TRIGGERED, OrderStatus.PARTIAL})

IST = ZoneInfo("Asia/Kolkata")


class OrderManager(OrderStoreMixin):
    """
    Converts approved TradePlans into orders, enforces the state machine,
    applies 8 pre-trade safety checks, and persists to the orders table.

    All tracks route to KiteBroker — single-broker architecture.
    The executor uses only the abstract Broker interface — no broker-specific
    logic leaks out of the brokers/ package.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        brokers: dict[BrokerName, Broker],
        ltp_cache: dict[str, float],
        alerter: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._brokers = brokers
        self._ltp = ltp_cache  # shared in-memory LTP dict updated by KiteBroker.on_tick
        self._alerter = alerter
        self._clock = clock or (lambda: datetime.now(IST))
        self._sleep = time.sleep

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, request: OrderRequest, track: str) -> str:
        """
        Run pre-trade checks, submit to correct broker, persist to orders table.
        Returns order_id (our internal UUID).
        Raises PreTradeCheckError if any check fails.
        """
        broker = self._broker_for()
        # #75: every price on the instrument's own tick, rounded the safe way.
        request = on_tick(request, broker.tick_size(request.symbol, request.exchange))
        ltp = self._ltp.get(request.symbol)
        self._pre_trade_checks(request, broker, ltp, track)

        order_id = str(uuid.uuid4())
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        self._insert_order(order_id, request, broker.broker_id, OrderStatus.CREATED, now)

        self._transition(order_id, OrderStatus.SUBMITTING, rule="submit")
        try:
            broker_order_id = broker.place_order(request)
        except BrokerRejected as exc:  # refused outright: nothing exists at the broker
            self._transition(order_id, OrderStatus.REJECTED, rule="broker_refused")
            self._log_order_error(order_id, str(exc))
            raise
        except Exception as exc:
            # The broker may have accepted the order and only the answer was lost
            # (timeout): adopt it rather than leave a working order nobody tracks (#97).
            broker_order_id = self._find_lost_order(broker, request)
            if broker_order_id is None:
                self._transition(order_id, OrderStatus.ERROR, rule="broker_place_failed")
                self._log_order_error(order_id, str(exc))
                raise
            logger.warning("order_adopted_after_lost_answer order_id=%s error=%s", order_id, exc)

        self._update_broker_order_id(order_id, broker_order_id)
        self._transition(order_id, OrderStatus.PENDING, rule="broker_accepted")
        logger.info("order submitted order_id=%s broker_order_id=%s", order_id, broker_order_id)
        if self._alerter:
            side = request.side.value if hasattr(request.side, "value") else str(request.side)
            self._alerter.info(
                f"Trade placed — {request.symbol}",
                f"Side: {side}  Qty: {request.quantity}  Track: {track}\n"
                f"Type: {request.order_type}  Broker order: {broker_order_id}",
            )
        return order_id

    def cancel_lost_orders(self) -> int:
        """Cancel orders whose placement outcome was unknown and could not be looked up
        then (#97). See executor/lost_orders.py."""
        return cancel_lost_orders(self)

    @staticmethod
    def _find_lost_order(broker: Broker, request: OrderRequest) -> str | None:
        if not request.idempotency_key:
            return None
        try:
            return broker.find_order(request.idempotency_key)
        except Exception as exc:  # broker unreachable: we cannot tell; treat as failed
            logger.warning("find_order_failed key=%s error=%s", request.idempotency_key, exc)
            return None

    def live_broker_order_ids(self) -> set[str]:
        """Broker ids of every order we still consider working (#65 reconcile)."""
        marks = ",".join("?" * len(TERMINAL_STATUSES))
        rows = self._db.execute(
            f"SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL"
            f" AND status NOT IN ({marks})",
            tuple(str(s) for s in TERMINAL_STATUSES),
        ).fetchall()
        return {str(r[0]) for r in rows}

    def modify_trigger(self, order_id: str, trigger_price: float) -> OrderRecord:
        """Move a resting stop's trigger (trailing stop, #71). The broker decides
        first; only an accepted modify is written to our row. Raises if the order
        isn't resting or the broker refuses (e.g. trigger on the wrong side)."""
        record = self.sync_status(order_id)
        if record.status not in (OrderStatus.PENDING, OrderStatus.TRIGGERED):
            raise PreTradeCheckError(f"order {order_id} is {record.status}, not resting")
        broker = self._broker_for_record(record)
        trigger_price = round_to_tick(  # up: a SELL stop only ever gets tighter (#75)
            trigger_price, broker.tick_size(record.symbol, record.exchange), up=True
        )
        broker.modify_order(record.broker_order_id, {"trigger_price": trigger_price})
        self._db.execute(
            "UPDATE orders SET trigger_price=? WHERE order_id=?", (trigger_price, order_id)
        )
        self._db.commit()
        from observability.emit import emit

        emit(
            "decision",
            "order_modified",
            source="order",
            order_id=order_id,
            symbol=record.symbol,
            trigger_from=record.trigger_price,
            trigger_to=trigger_price,
        )
        return self._load_order(order_id)

    def cancel(self, order_id: str) -> OrderRecord:
        """Cancel at the broker, then record what the broker says actually happened.

        An order can fill between our last status check and the cancel (a stop
        firing as we cancel it). Marking it CANCELLED regardless would hide a real
        fill, so the final state comes from the broker: FILLED/PARTIAL fills are
        recorded, anything else becomes CANCELLED. Returns the final record.
        """
        record = self.sync_status(order_id)
        if record.status in TERMINAL_STATUSES:
            return record
        broker = self._broker_for_record(record)
        try:
            broker.cancel_order(record.broker_order_id)
        except Exception as exc:  # e.g. Kite: "order already complete"
            logger.warning("cancel_failed order_id=%s error=%s", order_id, exc)
        raw = self._final_status(broker, record.broker_order_id)
        if raw is None:  # broker still processing the cancel: decide on a later sync
            logger.warning("cancel_unconfirmed order_id=%s", order_id)
            return self.sync_status(order_id)
        # The broker's postback for this cancel can land (on the feed thread, via
        # handle_broker_update) before cancel_order returns: re-read, never record
        # its fills or its terminal move twice (#86 found it; Kite does the same).
        record = self._load_order(order_id)
        if record.status in TERMINAL_STATUSES:
            return record
        if raw.get("filled_quantity", 0) > record.filled_quantity:
            self._record_execution(record, raw)
        # Anything short of a complete fill ends CANCELLED; a partial fill's quantity
        # was recorded above and the unfilled remainder is what got cancelled.
        if raw.get("status") == OrderStatus.FILLED:
            self._transition(order_id, OrderStatus.FILLED, rule="filled_before_cancel")
        elif raw.get("status") in _STILL_WORKING:
            # The cancel did not take (network error, refused): the order is still
            # working at the broker. Never record CANCELLED over it (#98).
            logger.warning("cancel_not_effective order_id=%s status=%s", order_id, raw["status"])
        else:
            self._transition(order_id, OrderStatus.CANCELLED, rule="cancel_confirmed")
        return self._load_order(order_id)

    def _final_status(self, broker: Broker, broker_order_id: str) -> dict | None:
        """The order's status once the broker has processed the cancel, or None if
        it's still in flight after CANCEL_POLLS tries (#73: Kite answers CANCEL
        PENDING first; a fill can still land in that window)."""
        for attempt in range(CANCEL_POLLS):
            raw = broker.get_order_status(broker_order_id) or {}
            if not raw or raw.get("status") is not None:
                return raw
            if attempt < CANCEL_POLLS - 1:
                self._sleep(CANCEL_POLL_SECONDS)
        return None

    def sync_status(self, order_id: str) -> OrderRecord:
        """Poll broker for current status and update DB."""
        record = self._load_order(order_id)
        if record.status in TERMINAL_STATUSES:
            return record
        broker = self._broker_for_record(record)
        raw = broker.get_order_status(record.broker_order_id)
        if not raw:
            return record
        new_status = raw.get("status")  # None: broker still processing, no change (#73)
        if new_status is not None and new_status != record.status:
            self._transition(order_id, new_status, rule="broker_status_sync")
        # Fills are recorded whenever the filled quantity grows, even if the status
        # didn't change (Kite keeps OPEN while a partial fill accumulates, #73).
        if raw.get("filled_quantity", 0) > record.filled_quantity:
            self._record_execution(record, raw)
            if self._alerter and new_status == OrderStatus.FILLED:
                avg = raw.get("average_fill_price", 0)
                qty = raw.get("filled_quantity", 0)
                self._alerter.info(
                    f"Trade filled — {record.symbol}",
                    f"Qty filled: {qty}  Avg price: ₹{avg:.2f}\n"
                    f"Order ID: {order_id[:8]}…  Broker: {record.broker_id}",
                )
        return self._load_order(order_id)

    def handle_broker_update(self, broker_update: dict) -> None:
        """Process push order update from broker's on_order_update callback."""
        broker_order_id = str(broker_update.get("order_id", ""))
        if not broker_order_id:
            return
        order_id = self._order_id_for_broker_id(broker_order_id)
        if not order_id:
            return
        self.sync_status(order_id)

    # ── State machine ─────────────────────────────────────────────────────────

    def _transition(
        self, order_id: str, new_status: OrderStatus, rule: str = "broker_status"
    ) -> None:
        """Move the order through the Order machine: status update and its
        `state_transition` event commit together (#56). Illegal moves raise
        StateMachineError, as before."""
        record = self._load_order(order_id)
        try:
            transition(
                self._db,
                ORDER_SPEC,
                order_id,
                OrderStatus(new_status).value,
                rule=rule,
                inputs={
                    "broker_order_id": record.broker_order_id,
                    "side": record.side,
                    "quantity": record.quantity,
                    "filled_quantity": record.filled_quantity,
                },
                correlation={"order_id": order_id, "symbol": record.symbol},
            )
        except IllegalTransition as exc:
            msg = f"Invalid transition {record.status} → {new_status} for order {order_id}"
            logger.error("%s (%s)", msg, exc)
            raise StateMachineError(msg) from exc
        if self._db.in_transaction:  # joined a caller's open transaction
            self._db.commit()

    # ── 8 pre-trade safety checks (executor/order_checks.py) ──────────────────

    def _pre_trade_checks(
        self,
        request: OrderRequest,
        broker: Broker,
        ltp: float | None,
        track: str,
    ) -> None:
        pre_trade_checks(
            request,
            broker,
            ltp,
            track,
            db=self._db,
            clock=self._clock,
            now_fn=lambda: datetime.now(IST),
        )

    # ── Broker lookup ─────────────────────────────────────────────────────────

    def _broker_for(self) -> Broker:
        broker = self._brokers.get(BrokerName.KITE)
        if broker is None:
            raise RuntimeError(f"No broker registered for {BrokerName.KITE}")
        return broker

    def _broker_for_record(self, record: OrderRecord) -> Broker:
        """The broker that placed this order (#41).

        Orders are submitted through the KITE slot, which holds whichever execution
        broker the trading mode chose, but each row records that broker's own id
        (e.g. `paper`). Match the registry key first, then each broker's own id.
        """
        broker = self._brokers.get(record.broker_id)
        if broker is not None:
            return broker
        for candidate in self._brokers.values():
            if candidate.broker_id == record.broker_id:
                return candidate
        raise RuntimeError(f"No registered broker matches order broker_id={record.broker_id}")
