from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.gtt_manager import GttManager
from executor.models import (
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionRecord,
    ProductType,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class PositionManager:
    """
    Manages the positions table and all position lifecycle operations:
    - open_position(): create on entry fill
    - close_position(): mark closed on exit fill
    - update_ltp(): refresh unrealised P&L from tick
    - trail_stop(): delegate to GttManager when 2×ATR gain is reached
    - handle_exit_recommendation(): process Stage 4b ExitRecommendation
    - mark_unprotected() / clear_unprotected(): unprotected flag lifecycle
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        gtt_manager: GttManager,
        order_manager: object,
    ) -> None:
        self._db = db
        self._gtt = gtt_manager
        self._om = order_manager  # OrderManager (local import avoids circular)

    # ── Position lifecycle ────────────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        exchange: str,
        track: str,
        bucket_id: str,
        broker_id: BrokerName,
        quantity: int,
        average_entry_price: float,
        stop_loss_price: float,
        target_price: float,
        atr_at_entry: float,
        entry_order_id: str,
        trade_plan_id: str | None = None,
        recommendation_id: str | None = None,
    ) -> str:
        """Create position record on entry fill. Returns position_id."""
        position_id = str(uuid.uuid4())
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        self._db.execute(
            """
            INSERT INTO positions (
                position_id, symbol, exchange, track, bucket_id, broker_id,
                quantity, average_entry_price, current_price, unrealised_pnl, realised_pnl,
                stop_loss_price, target_price, atr_at_entry, entry_order_id, gtt_oco_id,
                unprotected_flag, unmanaged, health_score, is_open, entry_at,
                trade_plan_id, recommendation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,NULL,1,0,100,1,?,?,?)
            """,
            (
                position_id,
                symbol,
                exchange,
                track,
                bucket_id,
                broker_id,
                quantity,
                average_entry_price,
                average_entry_price,
                stop_loss_price,
                target_price,
                atr_at_entry,
                entry_order_id,
                now,
                trade_plan_id,
                recommendation_id,
            ),
        )
        self._db.commit()
        logger.info("Position opened %s %s %s qty=%d", position_id, symbol, track, quantity)
        return position_id

    def close_position(self, position_id: str, exit_price: float, realised_pnl: float) -> None:
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        self._db.execute(
            """
            UPDATE positions
            SET is_open=0, exit_at=?, current_price=?, realised_pnl=?, unrealised_pnl=0
            WHERE position_id=?
            """,
            (now, exit_price, realised_pnl, position_id),
        )
        self._db.commit()

    def update_ltp(self, symbol: str, ltp: float) -> None:
        rows = self._db.execute(
            "SELECT position_id, quantity, average_entry_price"
            " FROM positions WHERE symbol=? AND is_open=1",
            (symbol,),
        ).fetchall()
        for position_id, qty, avg_price in rows:
            unrealised = (ltp - avg_price) * qty
            self._db.execute(
                "UPDATE positions SET current_price=?, unrealised_pnl=? WHERE position_id=?",
                (ltp, unrealised, position_id),
            )
        if rows:
            self._db.commit()

    def mark_unprotected(self, position_id: str) -> None:
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        self._db.execute(
            "UPDATE positions SET unprotected_flag=1, unprotected_since=? WHERE position_id=?",
            (now, position_id),
        )
        self._db.commit()

    def clear_unprotected(self, position_id: str) -> None:
        self._db.execute(
            "UPDATE positions SET unprotected_flag=0, unprotected_since=NULL WHERE position_id=?",
            (position_id,),
        )
        self._db.commit()

    def link_gtt_oco(self, position_id: str, gtt_id: str) -> None:
        self._db.execute(
            "UPDATE positions"
            " SET gtt_oco_id=?, unprotected_flag=0, unprotected_since=NULL WHERE position_id=?",
            (gtt_id, position_id),
        )
        self._db.commit()

    # ── Trail stop ────────────────────────────────────────────────────────────

    def trail_stop(self, position_id: str, current_price: float) -> bool:
        pos = self._load(position_id)
        if not pos or not pos.is_open or pos.track == "intraday":
            return False
        return self._gtt.trail_stop(pos, current_price)

    # ── Exit recommendation handler ───────────────────────────────────────────

    def handle_exit_recommendation(
        self,
        position_id: str,
        reason: str,
    ) -> str | None:
        """
        Process a Stage 4b ExitRecommendation — submit a market sell order immediately.
        Returns order_id if an order was submitted, None if the position wasn't found/open.
        """
        from executor.order_manager import OrderManager

        om: OrderManager = self._om  # type: ignore[assignment]

        pos = self._load(position_id)
        if not pos or not pos.is_open:
            return None

        product = ProductType.MIS if pos.track == "intraday" else ProductType.CNC
        close_req = OrderRequest(
            symbol=pos.symbol,
            exchange=pos.exchange,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=pos.quantity,
            product=product,
            tag=f"exit_{reason}",
            recommendation_id=pos.recommendation_id,
        )
        order_id = om.submit(close_req, pos.track)
        logger.info(
            "Exit order submitted: %s %s reason=%s order_id=%s",
            position_id,
            pos.symbol,
            reason,
            order_id,
        )
        return order_id

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load(self, position_id: str) -> PositionRecord | None:
        row = self._db.execute(
            "SELECT * FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        if not row:
            return None
        from executor.reconciliation import ReconciliationLoop

        return ReconciliationLoop._row_to_position(dict(row))

    def load_open(self, track: str | None = None) -> list[PositionRecord]:
        query = "SELECT * FROM positions WHERE is_open=1"
        params: list = []
        if track:
            query += " AND track=?"
            params.append(track)
        rows = self._db.execute(query, params).fetchall()
        from executor.reconciliation import ReconciliationLoop

        return [ReconciliationLoop._row_to_position(dict(r)) for r in rows]
