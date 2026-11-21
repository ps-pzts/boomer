"""Persistence helpers for OrderManager: the orders/executions/executor_errors rows.
Split out of order_manager.py (#95); the SQL is unchanged."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.models import BrokerName, OrderRecord, OrderRequest, OrderStatus
from executor.order_machine import ORDER_SPEC
from statemachine import record_created

IST = ZoneInfo("Asia/Kolkata")


class OrderStoreMixin:
    """Mixed into OrderManager; relies on its `_db`."""

    _db: sqlite3.Connection

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
        record_created(
            self._db,
            ORDER_SPEC,
            order_id,
            rule="order_created",
            inputs={
                "side": request.side,
                "order_type": request.order_type,
                "quantity": request.quantity,
                "price": request.price,
                "trigger_price": request.trigger_price,
                "product": request.product,
                "tag": request.tag,
            },
            correlation={"order_id": order_id, "symbol": request.symbol},
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
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
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
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        self._db.execute(
            "INSERT INTO executor_errors"
            " (error_id, error_type, order_id, message, created_at) VALUES (?,?,?,?,?)",
            (err_id, "order_rejected", order_id, message, now),
        )
        self._db.commit()

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
