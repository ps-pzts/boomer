from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
)

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

_MARKET_OPEN_IST = (9, 15)
_LAST_ENTRY_IST = (14, 30)   # no new entries after this
_SQUAREOFF_IST = (15, 14)    # orchestrator calls square_off at this time
_MARKET_CLOSE_IST = (15, 30)

_SIGNAL_VALIDITY_MINUTES = 30
_STOCK_COOLDOWN_MINUTES = 60  # per-stock intraday cooldown (Loophole 2)


class IntradayPipeline:
    """
    30-minute continuous pipeline for intraday signals.

    The orchestrator schedules run_cycle() every 30 minutes from 09:30 to 14:30 IST.
    square_off_all_intraday() is called by the orchestrator at 15:14 IST only —
    never self-triggered (Loophole 4 from design: single trigger source).

    Cycle mutual exclusion: if a cycle is still running when the next fires,
    the new one is skipped (Loophole 3). This is handled by a threading.Lock.
    """

    def __init__(
        self,
        order_manager: object,
        position_manager: object,
        signal_runner: object | None = None,
        db: sqlite3.Connection | None = None,
    ) -> None:
        self._om = order_manager
        self._pm = position_manager
        self._signal_runner = signal_runner
        self._db = db
        self._cycle_lock = threading.Lock()
        self._last_signal_time: dict[str, datetime] = {}
        self._cycle_count = 0
        self._consecutive_failures = 0
        self._disabled_for_day = False

    # ── Cycle runner ─────────────────────────────────────────────────────────

    def run_cycle(self) -> bool:
        """
        Run one 30-minute intraday cycle.
        Returns True if cycle ran, False if skipped or pipeline disabled.
        """
        if self._disabled_for_day:
            logger.warning("Intraday pipeline disabled for today — skipping cycle")
            return False

        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Intraday cycle skipped — previous cycle still running")
            return False

        try:
            now = datetime.now(IST)
            if not self._is_entry_window(now):
                logger.info("Intraday cycle: outside entry window — monitoring only")
                self._monitor_positions()
                return True

            self._run_intraday_signals(now)
            self._consecutive_failures = 0
            self._cycle_count += 1
            return True

        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("Intraday cycle failed (attempt %d): %s", self._consecutive_failures, exc)
            if self._consecutive_failures >= 3:
                logger.error("3 consecutive intraday cycle failures — disabling intraday for today")
                self._disabled_for_day = True
            return False
        finally:
            self._cycle_lock.release()

    def square_off_all_intraday(self) -> list[str]:
        """
        Called by the orchestrator's intraday_squareoff task at 15:14 IST.
        Submits market sell orders for all open intraday positions.
        Returns list of order_ids submitted.
        Returns immediately if called outside 15:00-15:20 IST window.

        Hard rule: the orchestrator triggers this, never a self-timer.
        """
        from executor.order_manager import OrderManager
        from executor.position_manager import PositionManager

        om: OrderManager = self._om  # type: ignore[assignment]
        pm: PositionManager = self._pm  # type: ignore[assignment]

        now = datetime.now(IST)
        squareoff_window_start = now.replace(hour=15, minute=0, second=0, microsecond=0)
        squareoff_window_end = now.replace(hour=15, minute=20, second=0, microsecond=0)
        if not (squareoff_window_start <= now <= squareoff_window_end):
            logger.warning("square_off_all_intraday called outside valid window — ignoring")
            return []

        open_intraday = pm.load_open(track="intraday")
        order_ids: list[str] = []
        for pos in open_intraday:
            req = OrderRequest(
                symbol=pos.symbol,
                exchange=pos.exchange,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=pos.quantity,
                product=ProductType.MIS,
                tag="intraday_squareoff",
            )
            try:
                oid = om.submit(req, "intraday")
                order_ids.append(oid)
                logger.info("Square-off order: %s %s qty=%d", pos.symbol, oid, pos.quantity)
            except Exception as exc:
                logger.error("Square-off failed for %s: %s", pos.symbol, exc)

        return order_ids

    # ── Signal validity and cooldown ─────────────────────────────────────────

    def is_signal_still_valid(self, symbol: str, generated_at: datetime) -> bool:
        """Intraday signals expire after 30 minutes (Loophole 1)."""
        elapsed = (datetime.now(IST) - generated_at).total_seconds()
        return elapsed < _SIGNAL_VALIDITY_MINUTES * 60

    def is_in_cooldown(self, symbol: str) -> bool:
        """Per-stock 60-minute cooldown prevents signal flicker (Loophole 2)."""
        last = self._last_signal_time.get(symbol)
        if not last:
            return False
        elapsed = (datetime.now(IST) - last).total_seconds()
        return elapsed < _STOCK_COOLDOWN_MINUTES * 60

    def record_signal_acted(self, symbol: str) -> None:
        self._last_signal_time[symbol] = datetime.now(IST)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _is_entry_window(self, now: datetime) -> bool:
        open_h, open_m = _MARKET_OPEN_IST
        cutoff_h, cutoff_m = _LAST_ENTRY_IST
        market_open = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        entry_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)
        return market_open <= now <= entry_cutoff

    def _run_intraday_signals(self, now: datetime) -> None:
        # Execute morning-batch recs that are queued and still within validity window.
        self._execute_queued_recs(now)
        if self._signal_runner is not None:
            # Live signal runner: injects live features and re-scores in real time.
            self._signal_runner.run_intraday(as_of=now)

    def _execute_queued_recs(self, now: datetime) -> None:
        """Place limit orders for queued intraday recs still within 30-min signal validity."""
        if self._db is None:
            return

        from executor.order_manager import OrderManager

        rows = self._db.execute(
            "SELECT * FROM recommendations"
            " WHERE status='queued_for_execution' AND track='intraday'"
        ).fetchall()

        om: OrderManager = self._om  # type: ignore[assignment]
        for row in rows:
            rec_id = row["recommendation_id"]
            symbol = row["stock_symbol"]
            try:
                generated_at = datetime.fromisoformat(row["generated_at"])
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=IST)

                if not self.is_signal_still_valid(symbol, generated_at):
                    self._db.execute(
                        "UPDATE recommendations SET status='rejected', decided_at=?"
                        " WHERE recommendation_id=?",
                        (now.isoformat(), rec_id),
                    )
                    self._db.commit()
                    logger.info("intraday_rec_expired symbol=%s rec_id=%s", symbol, rec_id)
                    continue

                if self.is_in_cooldown(symbol):
                    logger.debug("intraday_rec_cooldown symbol=%s rec_id=%s", symbol, rec_id)
                    continue

                direction = row["direction"]
                if direction == "long":
                    side = OrderSide.BUY
                    limit_price = float(row["entry_zone_high"])
                else:
                    side = OrderSide.SELL
                    limit_price = float(row["entry_zone_low"])

                qty = int(row["position_size_shares"])
                if qty < 1 or limit_price <= 0:
                    continue

                req = OrderRequest(
                    symbol=symbol,
                    exchange=row["exchange"] or "NSE",
                    side=side,
                    order_type=OrderType.LIMIT,
                    quantity=qty,
                    price=limit_price,
                    product=ProductType.MIS,
                    tag="intraday_rec",
                    recommendation_id=rec_id,
                )
                order_id = om.submit(req, "intraday")

                now_str = now.isoformat()
                self._db.execute(
                    "UPDATE recommendations SET status='submitted_to_broker',"
                    " decided_at=?, submitted_at=?"
                    " WHERE recommendation_id=?",
                    (now_str, now_str, rec_id),
                )
                self._db.commit()
                self.record_signal_acted(symbol)
                logger.info(
                    "intraday_order_placed symbol=%s rec_id=%s order_id=%s qty=%d price=%.2f",
                    symbol, rec_id, order_id, qty, limit_price,
                )
            except Exception as exc:
                logger.error(
                    "intraday_rec_failed symbol=%s rec_id=%s error=%s", symbol, rec_id, exc
                )

    def _monitor_positions(self) -> None:
        from executor.position_manager import PositionManager

        pm: PositionManager = self._pm  # type: ignore[assignment]
        open_pos = pm.load_open(track="intraday")
        logger.debug("Monitoring %d open intraday positions", len(open_pos))
