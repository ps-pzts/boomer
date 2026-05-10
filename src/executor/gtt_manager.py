from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from executor.brokers.base import Broker
from executor.models import (
    BrokerName,
    GttOrderRecord,
    GttRequest,
    GttStatus,
    GttType,
    PositionRecord,
)

logger = logging.getLogger(__name__)

_UNPROTECTED_RETRY_INTERVAL_SECONDS = 60
_UNPROTECTED_FORCE_CLOSE_SECONDS = 600   # Loophole 3: force-close after 10 min
_GTT_MODIFY_TRAIL_ATR_MULTIPLIER = 2.0   # price must move 2×ATR before trailing
_GTT_TRAIL_STEP_ATR = 1.0               # stop moves by 1×ATR


class GttManager:
    """
    GTT order lifecycle manager.

    Responsibilities:
    - place_gtt_for_position(): single-leg entry or OCO stop+target after fill
    - trail_stop(): move OCO SL leg up when position gains 2×ATR (Q3-5 support)
    - daily_reconcile(): poll broker for GTT status changes at 6 AM
    - check_unprotected_positions(): retry stop placement, force-close after 10 min

    GTT duplicate check (pre-trade check #8): rejects if an active GTT already
    exists for the same symbol+track with same trigger price.
    """

    def __init__(self, db: sqlite3.Connection, brokers: dict[BrokerName, Broker]) -> None:
        self._db = db
        self._brokers = brokers

    # ── Public API ────────────────────────────────────────────────────────────

    def place_gtt_for_position(
        self,
        position: PositionRecord,
        gtt_type: GttType,
        request: GttRequest,
    ) -> str:
        """
        Place a GTT (single entry or OCO stop+target) for a position.
        Returns gtt_id (internal UUID).
        Raises ValueError on GTT duplicate (pre-trade check #8).
        """
        broker = self._broker_for(position.broker_id)
        trigger = request.trigger_price or request.sl_trigger_price
        self._check_gtt_duplicate(position.symbol, position.exchange, trigger)

        broker_gtt_id = broker.place_gtt(request)
        gtt_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        valid_until = now + timedelta(days=request.valid_days)

        self._db.execute(
            """
            INSERT INTO gtt_orders (
                gtt_id, broker_gtt_id, broker_id, symbol, exchange, gtt_type,
                trigger_price, limit_price, sl_trigger_price, sl_limit_price,
                target_trigger_price, target_limit_price, quantity, status,
                parent_order_id, triggered_order_id, valid_until, created_at, last_checked_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                gtt_id, broker_gtt_id, position.broker_id, position.symbol,
                position.exchange, gtt_type,
                request.trigger_price, request.limit_price,
                request.sl_trigger_price, request.sl_limit_price,
                request.target_trigger_price, request.target_limit_price,
                request.quantity, GttStatus.GTT_ACTIVE,
                request.parent_order_id, None,
                valid_until.isoformat(), now.isoformat(), now.isoformat(),
            ),
        )
        self._db.commit()
        logger.info(
            "GTT placed gtt_id=%s broker_gtt_id=%s type=%s", gtt_id, broker_gtt_id, gtt_type
        )
        return gtt_id

    def trail_stop(self, position: PositionRecord, current_price: float) -> bool:
        """
        Advance the OCO stop-loss leg if price has moved 2×ATR in favour.
        Stop moves up by 1×ATR. Stop never widens.
        Returns True if trailing was applied.
        """
        if not position.gtt_oco_id:
            return False
        gtt = self._load_gtt(position.gtt_oco_id)
        if gtt.status != GttStatus.GTT_ACTIVE:
            return False

        atr = position.atr_at_entry
        gain = current_price - position.average_entry_price
        if gain < _GTT_MODIFY_TRAIL_ATR_MULTIPLIER * atr:
            return False

        new_sl = gtt.sl_trigger_price + _GTT_TRAIL_STEP_ATR * atr
        if new_sl <= gtt.sl_trigger_price:
            return False   # stop only moves forward

        broker = self._broker_for(gtt.broker_id)
        changes = {
            "leg1": {"stopPrice": round(new_sl, 2), "limitPrice": round(new_sl * 0.995, 2)},
        }
        broker.modify_gtt(gtt.broker_gtt_id, changes)
        self._db.execute(
            "UPDATE gtt_orders"
            " SET sl_trigger_price=?, sl_limit_price=?, last_checked_at=? WHERE gtt_id=?",
            (new_sl, new_sl * 0.995, datetime.now(UTC).isoformat(), position.gtt_oco_id),
        )
        self._db.commit()
        logger.info(
            "Trailing stop updated gtt_id=%s new_sl=%.2f symbol=%s",
            position.gtt_oco_id, new_sl, position.symbol,
        )
        return True

    def daily_reconcile(self, broker_id: BrokerName) -> list[str]:
        """
        Poll broker for all GTT statuses. Called at 6 AM daily.
        Returns list of gtt_ids that changed to GTT_TRIGGERED.
        """
        broker = self._broker_for(broker_id)
        broker_gtts = {
            str(g.get("id", g.get("trigger_id", g.get("broker_gtt_id", "")))): g
            for g in broker.list_gtts()
        }
        now = datetime.now(UTC).isoformat()
        triggered = []

        rows = self._db.execute(
            "SELECT gtt_id, broker_gtt_id, status FROM gtt_orders WHERE broker_id=? AND status=?",
            (broker_id, GttStatus.GTT_ACTIVE),
        ).fetchall()

        for gtt_id, broker_gtt_id, _ in rows:
            broker_record = broker_gtts.get(broker_gtt_id)
            if broker_record is None:
                self._raise_gtt_alert(broker_id, gtt_id, "GTT not found at broker")
                continue
            broker_status = self._normalise_gtt_status(broker_record)
            if broker_status == GttStatus.GTT_TRIGGERED:
                self._db.execute(
                    "UPDATE gtt_orders SET status=?, last_checked_at=? WHERE gtt_id=?",
                    (GttStatus.GTT_TRIGGERED, now, gtt_id),
                )
                triggered.append(gtt_id)
            elif broker_status == GttStatus.GTT_EXPIRED:
                self._db.execute(
                    "UPDATE gtt_orders SET status=?, last_checked_at=? WHERE gtt_id=?",
                    (GttStatus.GTT_EXPIRED, now, gtt_id),
                )
            else:
                self._db.execute(
                    "UPDATE gtt_orders SET last_checked_at=? WHERE gtt_id=?",
                    (now, gtt_id),
                )

        self._db.commit()
        return triggered

    def check_unprotected_positions(
        self,
        positions: list[PositionRecord],
        order_manager: object,
    ) -> list[str]:
        """
        Loophole 3: retry stop placement for unprotected positions.
        Force-close via market order after 10 minutes unprotected.
        Returns list of position_ids that were force-closed.
        """
        from executor.order_manager import OrderManager  # local import avoids circular

        om: OrderManager = order_manager  # type: ignore[assignment]
        force_closed: list[str] = []
        now = datetime.now(UTC)

        for pos in positions:
            if not pos.unprotected_flag or not pos.is_open:
                continue
            unprotected_since = pos.unprotected_since or pos.entry_at
            elapsed = (now - unprotected_since.replace(tzinfo=UTC)).total_seconds()
            if elapsed >= _UNPROTECTED_FORCE_CLOSE_SECONDS:
                logger.error(
                    "Force-closing unprotected position %s %s — 10 min unprotected",
                    pos.position_id, pos.symbol,
                )
                from executor.models import OrderRequest, OrderSide, OrderType, ProductType
                close_req = OrderRequest(
                    symbol=pos.symbol,
                    exchange=pos.exchange,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=pos.quantity,
                    product=ProductType.CNC if pos.track != "intraday" else ProductType.MIS,
                    tag="force_close_unprotected",
                )
                try:
                    om.submit(close_req, pos.track)
                    force_closed.append(pos.position_id)
                except Exception as exc:
                    logger.error("Force-close failed for %s: %s", pos.position_id, exc)

        return force_closed

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_gtt_duplicate(self, symbol: str, exchange: str, trigger_price: float) -> None:
        row = self._db.execute(
            """
            SELECT gtt_id FROM gtt_orders
            WHERE symbol=? AND exchange=? AND status=? AND trigger_price=?
            """,
            (symbol, exchange, GttStatus.GTT_ACTIVE, trigger_price),
        ).fetchone()
        if row:
            raise ValueError(
                f"Duplicate active GTT for {exchange}:{symbol}"
                f" at trigger {trigger_price} (gtt_id={row[0]})"
            )

    def _load_gtt(self, gtt_id: str) -> GttOrderRecord:
        row = self._db.execute("SELECT * FROM gtt_orders WHERE gtt_id=?", (gtt_id,)).fetchone()
        if not row:
            raise ValueError(f"GTT not found: {gtt_id}")
        d = dict(row)
        return GttOrderRecord(
            gtt_id=d["gtt_id"],
            broker_gtt_id=d["broker_gtt_id"],
            broker_id=BrokerName(d["broker_id"]),
            symbol=d["symbol"],
            exchange=d["exchange"],
            gtt_type=GttType(d["gtt_type"]),
            trigger_price=d["trigger_price"],
            limit_price=d["limit_price"],
            sl_trigger_price=d["sl_trigger_price"],
            sl_limit_price=d["sl_limit_price"],
            target_trigger_price=d["target_trigger_price"],
            target_limit_price=d["target_limit_price"],
            quantity=d["quantity"],
            status=GttStatus(d["status"]),
            parent_order_id=d["parent_order_id"],
            triggered_order_id=d["triggered_order_id"],
            valid_until=datetime.fromisoformat(d["valid_until"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            last_checked_at=datetime.fromisoformat(d["last_checked_at"]),
        )

    def _broker_for(self, broker_id: BrokerName) -> Broker:
        broker = self._brokers.get(broker_id)
        if broker is None:
            raise RuntimeError(f"No broker registered: {broker_id}")
        return broker

    def _raise_gtt_alert(self, broker_id: BrokerName, gtt_id: str, message: str) -> None:
        alert_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            """
            INSERT INTO reconciliation_alerts
            (alert_id, broker_id, alert_type, bot_value, created_at)
            VALUES (?,?,?,?,?)
            """,
            (
                alert_id, broker_id, "gtt_missing",
                json.dumps({"gtt_id": gtt_id, "note": message}), now,
            ),
        )
        self._db.commit()
        logger.error("GTT alert raised gtt_id=%s: %s", gtt_id, message)

    @staticmethod
    def _normalise_gtt_status(broker_record: dict) -> GttStatus:
        status = broker_record.get("status", "")
        if isinstance(status, int):
            from executor.brokers.fyers_broker import _FYERS_GTT_STATUS_MAP
            return _FYERS_GTT_STATUS_MAP.get(status, GttStatus.GTT_ACTIVE)
        status_str = str(status).lower()
        if "triggered" in status_str or "complete" in status_str:
            return GttStatus.GTT_TRIGGERED
        if "expired" in status_str:
            return GttStatus.GTT_EXPIRED
        if "cancelled" in status_str or "deleted" in status_str:
            return GttStatus.GTT_CANCELLED
        return GttStatus.GTT_ACTIVE
