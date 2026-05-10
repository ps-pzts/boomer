from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from executor.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
)

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_MARKET_OPEN_UTC = (3, 45)  # 09:15 IST
_LAST_ENTRY_UTC = (9, 0)  # 14:30 IST — no new intraday entries after this
_SQUAREOFF_UTC = (9, 44)  # 15:14 IST — orchestrator calls square_off at this time
_MARKET_CLOSE_UTC = (10, 0)  # 15:30 IST

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
    ) -> None:
        self._om = order_manager
        self._pm = position_manager
        self._signal_runner = signal_runner
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
            now = datetime.now(UTC)
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

        now = datetime.now(UTC)
        squareoff_window_start = now.replace(hour=9, minute=30)  # 15:00 IST
        squareoff_window_end = now.replace(hour=9, minute=50)  # 15:20 IST
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
        elapsed = (datetime.now(UTC) - generated_at).total_seconds()
        return elapsed < _SIGNAL_VALIDITY_MINUTES * 60

    def is_in_cooldown(self, symbol: str) -> bool:
        """Per-stock 60-minute cooldown prevents signal flicker (Loophole 2)."""
        last = self._last_signal_time.get(symbol)
        if not last:
            return False
        elapsed = (datetime.now(UTC) - last).total_seconds()
        return elapsed < _STOCK_COOLDOWN_MINUTES * 60

    def record_signal_acted(self, symbol: str) -> None:
        self._last_signal_time[symbol] = datetime.now(UTC)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _is_entry_window(self, now: datetime) -> bool:
        open_h, open_m = _MARKET_OPEN_UTC
        cutoff_h, cutoff_m = _LAST_ENTRY_UTC
        market_open = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        entry_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)
        return market_open <= now <= entry_cutoff

    def _run_intraday_signals(self, now: datetime) -> None:
        if self._signal_runner is None:
            logger.debug("No signal runner configured — intraday cycle is monitoring only")
            return
        # Signal runner injected by orchestrator; calls Stages 0→5 for intraday track
        self._signal_runner.run_intraday(as_of=now)

    def _monitor_positions(self) -> None:
        from executor.position_manager import PositionManager

        pm: PositionManager = self._pm  # type: ignore[assignment]
        open_pos = pm.load_open(track="intraday")
        logger.debug("Monitoring %d open intraday positions", len(open_pos))
