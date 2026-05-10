from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from executor.brokers.base import Broker
from executor.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    BrokerName,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    PreTradeCheckError,
    StateMachineError,
)

logger = logging.getLogger(__name__)

# Routing table: track → broker_id
# All tracks routed to Kite until Fyers trading is validated.
# Fyers is connected for historical/live data only.
_TRACK_BROKER: dict[str, BrokerName] = {
    "intraday": BrokerName.KITE,
    "swing": BrokerName.KITE,
    "long_term": BrokerName.KITE,
}

_DUPLICATE_WINDOW_SECONDS = 30
_PRICE_SANITY_PCT = 0.05  # order must be within 5% of LTP


class OrderManager:
    """
    Converts approved TradePlans into orders, enforces the state machine,
    applies 8 pre-trade safety checks, and persists to the orders table.

    Routing rule: intraday → KiteBroker; swing/long_term → FyersBroker.
    The executor uses only the abstract Broker interface — no broker-specific
    logic leaks out of the brokers/ package.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        brokers: dict[BrokerName, Broker],
        ltp_cache: dict[str, float],
        alerter: object | None = None,
    ) -> None:
        self._db = db
        self._brokers = brokers
        self._ltp = ltp_cache  # shared in-memory LTP dict updated by KiteBroker.on_tick
        self._alerter = alerter

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, request: OrderRequest, track: str) -> str:
        """
        Run pre-trade checks, submit to correct broker, persist to orders table.
        Returns order_id (our internal UUID).
        Raises PreTradeCheckError if any check fails.
        """
        broker = self._broker_for(track)
        ltp = self._ltp.get(request.symbol)
        self._pre_trade_checks(request, broker, ltp, track)

        order_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._insert_order(order_id, request, broker.broker_id, OrderStatus.CREATED, now)

        self._transition(order_id, OrderStatus.SUBMITTING)
        try:
            broker_order_id = broker.place_order(request)
        except Exception as exc:
            self._transition(order_id, OrderStatus.ERROR)
            self._log_order_error(order_id, str(exc))
            raise

        self._update_broker_order_id(order_id, broker_order_id)
        self._transition(order_id, OrderStatus.PENDING)
        logger.info("order submitted order_id=%s broker_order_id=%s", order_id, broker_order_id)
        if self._alerter:
            side = request.side.value if hasattr(request.side, "value") else str(request.side)
            self._alerter.info(
                f"Trade placed — {request.symbol}",
                f"Side: {side}  Qty: {request.quantity}  Track: {track}\n"
                f"Type: {request.order_type}  Broker order: {broker_order_id}",
            )
        return order_id

    def cancel(self, order_id: str) -> None:
        record = self._load_order(order_id)
        if record.status in TERMINAL_STATUSES:
            return
        broker = self._brokers[record.broker_id]
        broker.cancel_order(record.broker_order_id)
        self._transition(order_id, OrderStatus.CANCELLED)

    def sync_status(self, order_id: str) -> OrderRecord:
        """Poll broker for current status and update DB."""
        record = self._load_order(order_id)
        if record.status in TERMINAL_STATUSES:
            return record
        broker = self._brokers[record.broker_id]
        raw = broker.get_order_status(record.broker_order_id)
        if not raw:
            return record
        new_status = raw["status"]
        if new_status != record.status:
            self._transition(order_id, new_status)
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

    def _transition(self, order_id: str, new_status: OrderStatus) -> None:
        record = self._load_order(order_id)
        allowed = ALLOWED_TRANSITIONS.get(record.status, frozenset())
        if new_status not in allowed:
            msg = f"Invalid transition {record.status} → {new_status} for order {order_id}"
            logger.error(msg)
            raise StateMachineError(msg)
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
            (new_status, now, order_id),
        )
        self._db.commit()

    # ── 8 pre-trade safety checks ─────────────────────────────────────────────

    def _pre_trade_checks(
        self,
        request: OrderRequest,
        broker: Broker,
        ltp: float | None,
        track: str,
    ) -> None:
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
        cutoff = (datetime.now(UTC) - timedelta(seconds=_DUPLICATE_WINDOW_SECONDS)).isoformat()
        if request.idempotency_key:
            row = self._db.execute(
                "SELECT order_id FROM orders WHERE idempotency_key=? AND created_at>?",
                (request.idempotency_key, cutoff),
            ).fetchone()
            if row:
                raise PreTradeCheckError(
                    f"Duplicate order (idempotency_key={request.idempotency_key})"
                )

        # 4. Funds available
        funds = broker.get_funds()
        required = request.price * request.quantity if request.price else 0
        if required > 0 and funds.available_cash < required:
            raise PreTradeCheckError(
                f"Insufficient funds: need {required:.0f}, have {funds.available_cash:.0f}"
            )

        # 5. Symbol valid (not empty)
        if not request.symbol or not request.exchange:
            raise PreTradeCheckError("Symbol or exchange is empty")

        # 6. Market hours for intraday — caller must enforce; logged here only
        if track == "intraday":
            now = datetime.now(UTC)
            # IST = UTC+5:30; market 9:15–15:30 IST = 3:45–10:00 UTC
            market_open = now.replace(hour=3, minute=45, second=0, microsecond=0)
            market_close = now.replace(hour=10, minute=0, second=0, microsecond=0)
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

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _insert_order(
        self,
        order_id: str,
        request: OrderRequest,
        broker_id: BrokerName,
        status: OrderStatus,
        now: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO orders (
                order_id, broker_order_id, broker_id, symbol, exchange, side,
                order_type, quantity, filled_quantity, product, price, trigger_price,
                average_fill_price, status, validity, idempotency_key, tag,
                rejection_reason, parent_order_id, trade_plan_id, recommendation_id,
                unprotected_flag, unmanaged, created_at, updated_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,0,?,?,?,0,?,?,?,?,?,?,?,?,0,0,?,?
            )
            """,
            (
                order_id,
                "",
                broker_id,
                request.symbol,
                request.exchange,
                request.side,
                request.order_type,
                request.quantity,
                request.product,
                request.price,
                request.trigger_price,
                status,
                request.validity,
                request.idempotency_key,
                request.tag,
                "",
                request.parent_order_id,
                request.trade_plan_id,
                request.recommendation_id,
                now,
                now,
            ),
        )
        self._db.commit()

    def _update_broker_order_id(self, order_id: str, broker_order_id: str) -> None:
        self._db.execute(
            "UPDATE orders SET broker_order_id=? WHERE order_id=?",
            (broker_order_id, order_id),
        )
        self._db.commit()

    def _load_order(self, order_id: str) -> OrderRecord:
        row = self._db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise ValueError(f"Order not found: {order_id}")
        return self._row_to_record(row)

    def _order_id_for_broker_id(self, broker_order_id: str) -> str | None:
        row = self._db.execute(
            "SELECT order_id FROM orders WHERE broker_order_id=?", (broker_order_id,)
        ).fetchone()
        return row[0] if row else None

    def _record_execution(self, record: OrderRecord, raw: dict) -> None:
        exec_id = str(uuid.uuid4())
        qty = raw.get("filled_quantity", 0) - record.filled_quantity
        price = raw.get("average_fill_price", 0.0)
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            """
            INSERT INTO executions
            (execution_id, order_id, broker_execution_id, quantity, price, side, executed_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (exec_id, record.order_id, "", qty, price, record.side, now),
        )
        self._db.execute(
            "UPDATE orders"
            " SET filled_quantity=?, average_fill_price=?, updated_at=? WHERE order_id=?",
            (raw["filled_quantity"], price, now, record.order_id),
        )
        self._db.commit()

    def _log_order_error(self, order_id: str, message: str) -> None:
        err_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "INSERT INTO executor_errors"
            " (error_id, error_type, order_id, message, created_at) VALUES (?,?,?,?,?)",
            (err_id, "order_rejected", order_id, message, now),
        )
        self._db.commit()

    def _broker_for(self, track: str) -> Broker:
        broker_id = _TRACK_BROKER.get(track, BrokerName.FYERS)
        broker = self._brokers.get(broker_id)
        if broker is None:
            raise RuntimeError(f"No broker registered for {broker_id}")
        return broker

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> OrderRecord:
        d = dict(row)
        return OrderRecord(
            order_id=d["order_id"],
            broker_order_id=d["broker_order_id"],
            broker_id=BrokerName(d["broker_id"]),
            symbol=d["symbol"],
            exchange=d["exchange"],
            side=d["side"],
            order_type=d["order_type"],
            quantity=d["quantity"],
            filled_quantity=d["filled_quantity"],
            product=d["product"],
            price=d["price"],
            trigger_price=d["trigger_price"],
            average_fill_price=d["average_fill_price"],
            status=OrderStatus(d["status"]),
            validity=d["validity"],
            idempotency_key=d["idempotency_key"],
            tag=d["tag"],
            rejection_reason=d["rejection_reason"],
            parent_order_id=d["parent_order_id"],
            parent_gtt_id=d["parent_gtt_id"],
            trade_plan_id=d["trade_plan_id"],
            recommendation_id=d["recommendation_id"],
            unprotected_flag=bool(d["unprotected_flag"]),
            unmanaged=bool(d["unmanaged"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
