from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.gtt_manager import GttManager
from executor.models import (
    BrokerName,
    GttRequest,
    GttType,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionRecord,
    ProductType,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_GRADUATION_ATR_STOP_MULTIPLIER = 3.0  # long-term stop = 3×ATR (vs swing 2×ATR)
_GRADUATION_MIN_GAIN_ATR = 1.0  # minimum gain before graduation is considered


class PositionManager:
    """
    Manages the positions table and all position lifecycle operations:
    - open_position(): create on entry fill
    - close_position(): mark closed on exit fill
    - update_ltp(): refresh unrealised P&L from tick
    - trail_stop(): delegate to GttManager when 2×ATR gain is reached
    - graduate_position(): swing → long-term (Q3-5)
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

    # ── Graduation (Q3-5): swing → long-term ─────────────────────────────────

    def graduate_position(self, position_id: str, current_price: float) -> bool:
        """
        Reclassify a swing position as long-term.
        Steps per Q3-5 design doc:
        1. Cancel existing swing GTT OCO
        2. Place new long-term GTT OCO (3×ATR stop, 2R+ target from current price)
        3. Update positions table: track=long_term, bucket_id=long_term_bucket
        4. Capital accounting is caller's responsibility (debit swing, credit LT)
        5. Checks: LT bucket must have capacity (caller verifies before calling here)
        """
        pos = self._load(position_id)
        if not pos or not pos.is_open or pos.track != "swing":
            return False

        gain = current_price - pos.average_entry_price
        if gain < _GRADUATION_MIN_GAIN_ATR * pos.atr_at_entry:
            logger.info("Graduation blocked: insufficient gain for %s", position_id)
            return False

        # 1. Cancel existing OCO GTT
        if pos.gtt_oco_id:
            try:
                gtt_rec = self._gtt._load_gtt(pos.gtt_oco_id)
                broker = self._gtt._broker_for(pos.broker_id)
                broker.cancel_gtt(gtt_rec.broker_gtt_id)
                self._db.execute(
                    "UPDATE gtt_orders SET status='gtt_cancelled' WHERE gtt_id=?",
                    (pos.gtt_oco_id,),
                )
            except Exception as exc:
                logger.error("Failed to cancel swing GTT during graduation: %s", exc)
                return False

        # 2. New long-term OCO with wider stop (3×ATR) and fresh 2R target
        new_stop = current_price - _GRADUATION_ATR_STOP_MULTIPLIER * pos.atr_at_entry
        new_target = current_price + 2 * (current_price - new_stop)
        gtt_req = GttRequest(
            symbol=pos.symbol,
            exchange=pos.exchange,
            gtt_type=GttType.OCO,
            quantity=pos.quantity,
            sl_trigger_price=round(new_stop, 2),
            sl_limit_price=round(new_stop * 0.995, 2),
            target_trigger_price=round(new_target, 2),
            target_limit_price=round(new_target * 1.005, 2),
            parent_order_id=pos.entry_order_id,
        )
        new_gtt_id = self._gtt.place_gtt_for_position(pos, GttType.OCO, gtt_req)

        # 3. Update positions table
        self._db.execute(
            """
            UPDATE positions
            SET track='long_term', bucket_id='long_term_bucket',
                stop_loss_price=?, target_price=?, gtt_oco_id=?
            WHERE position_id=?
            """,
            (new_stop, new_target, new_gtt_id, position_id),
        )
        self._db.commit()
        logger.info(
            "Position graduated to long-term: %s %s new_stop=%.2f new_target=%.2f",
            position_id,
            pos.symbol,
            new_stop,
            new_target,
        )
        return True

    # ── Exit recommendation handler ───────────────────────────────────────────

    def handle_exit_recommendation(
        self,
        position_id: str,
        reason: str,
        requires_human: bool,
    ) -> str | None:
        """
        Process a Stage 4b ExitRecommendation.
        For swing/intraday: submit market sell order immediately.
        For long-term: requires_human=True → log and surface to dashboard only.
        Forced de-risking bypasses requires_human even for long-term.
        Returns order_id if order submitted, None if deferred to human.
        """
        from executor.order_manager import OrderManager

        om: OrderManager = self._om  # type: ignore[assignment]

        pos = self._load(position_id)
        if not pos or not pos.is_open:
            return None

        if requires_human and reason != "forced_derisking":
            logger.info(
                "Exit rec deferred to human: %s %s reason=%s", position_id, pos.symbol, reason
            )
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
