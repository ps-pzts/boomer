"""Tests for executor/trade_lifecycle.py — TradeSupervisor (#45).

End to end on a migrated DB with the real OrderManager, PositionManager,
paper KiteBroker on KiteSim (#86) and ManualFeed; the clock is fixed inside market hours.

Worked numbers (qty 10, entry 100, stop 98, target 104):
  target hit  -> (104 - 100) x 10 = +40
  stop gapped -> tick 97.5 under the 98 stop fills at 97.5: (97.5 - 100) x 10 = -25
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from db.migrations import run_migrations
from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.kitesim.paper import build_paper_broker
from executor.models import BrokerName, OrderStatus
from executor.order_manager import OrderManager
from executor.position_manager import PositionManager
from executor.trade_lifecycle import TradeRequest, TradeSupervisor, validate
from tests.kitesim_kit import fixture_instruments

IST = ZoneInfo("Asia/Kolkata")
_MIGRATIONS = Path(__file__).parents[2] / "migrations"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.now


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "t.db")
        run_migrations(db_path, _MIGRATIONS)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.clock = _Clock()
        self.feed = ManualFeed()
        self.paper = build_paper_broker(
            self.feed, 1_000_000, instruments=fixture_instruments(), clock=self.clock
        )
        self.om = OrderManager(
            self.db, {BrokerName.KITE: self.paper}, ltp_cache={}, clock=self.clock
        )
        self.pm = PositionManager(self.db, gtt_manager=None, order_manager=self.om)
        self.alerter = MagicMock()
        self.sup = TradeSupervisor(self.db, self.om, self.pm, self.alerter, clock=self.clock)

    def tick(self, ltp: float, sym: str = "INFY") -> None:
        self.feed.push(Tick(symbol=sym, exchange="NSE", ltp=ltp, received_at=self.clock.now))

    def bracket(self, bracket_id: str) -> sqlite3.Row:
        return self.db.execute(
            "SELECT * FROM intraday_brackets WHERE bracket_id=?", (bracket_id,)
        ).fetchone()

    def position(self, bracket_id: str) -> sqlite3.Row:
        pid = self.bracket(bracket_id)["position_id"]
        return self.db.execute("SELECT * FROM positions WHERE position_id=?", (pid,)).fetchone()

    def critical_titles(self) -> list[str]:
        return [c.args[0] for c in self.alerter.critical.call_args_list]


def _req(entry=100.0, stop=98.0, target=104.0, qty=10) -> TradeRequest:
    return TradeRequest("INFY", "NSE", qty, entry, stop, target, atr=2.0)


@pytest.fixture()
def h(tmp_path) -> Harness:
    return Harness(tmp_path)


def _filled_and_protected(h: Harness) -> str:
    h.tick(100.0)
    bid = h.sup.open_trade(_req())  # LIMIT 100 fills on the 100 tick
    h.sup.reconcile()
    assert h.bracket(bid)["state"] == "open"
    return bid


class TestValidate:
    @pytest.mark.parametrize(
        ("kw", "rule"),
        [
            ({"qty": 0}, "quantity_below_one"),
            ({"stop": 100.0}, "stop_entry_target_not_ordered"),
            ({"stop": 101.0}, "stop_entry_target_not_ordered"),
            ({"target": 100.0}, "stop_entry_target_not_ordered"),
            ({"stop": 0.0}, "stop_entry_target_not_ordered"),
        ],
    )
    def test_invalid_plans(self, kw, rule):
        assert validate(_req(**kw)) == rule

    def test_valid_plan(self):
        assert validate(_req()) is None

    def test_invalid_plan_places_nothing(self, h):
        with pytest.raises(ValueError, match="stop_entry_target_not_ordered"):
            h.sup.open_trade(_req(stop=100.0))
        assert h.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert h.db.execute("SELECT COUNT(*) FROM intraday_brackets").fetchone()[0] == 0


class TestHappyPaths:
    def test_entry_fill_places_stop_and_target_and_clears_unprotected(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        assert b["filled_quantity"] == 10
        stop = h.om.sync_status(b["stop_order_id"])
        target = h.om.sync_status(b["target_order_id"])
        assert (stop.order_type, stop.trigger_price, stop.quantity) == ("sl", 98.0, 10)
        assert (target.order_type, target.price, target.quantity) == ("limit", 104.0, 10)
        assert h.position(bid)["unprotected_flag"] == 0
        assert h.position(bid)["is_open"] == 1

    def test_target_hit_cancels_stop_and_books_profit(self, h):
        bid = _filled_and_protected(h)
        h.tick(104.2)
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["realised_pnl"]) == ("closed", "target_hit", 40.0)
        assert h.om.sync_status(b["stop_order_id"]).status == OrderStatus.CANCELLED
        assert h.position(bid)["is_open"] == 0 and h.position(bid)["realised_pnl"] == 40.0
        assert h.paper.list_positions() == []

    def test_stop_hit_on_gap_books_loss(self, h):
        bid = _filled_and_protected(h)
        h.tick(97.5)
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["realised_pnl"]) == ("closed", "stop_hit", -25.0)
        assert h.om.sync_status(b["target_order_id"]).status == OrderStatus.CANCELLED
        assert h.paper.list_positions() == []


class TestEntryLifecycle:
    def test_unfilled_entry_expires_after_30_minutes(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))  # resting below the market
        h.clock.now += timedelta(minutes=29)
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "entry_pending"
        h.clock.now += timedelta(minutes=1)
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["position_id"]) == (
            "cancelled",
            "entry_expired",
            None,
        )
        assert h.om.sync_status(b["entry_order_id"]).status == OrderStatus.CANCELLED

    def test_entry_filling_later_is_picked_up(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.tick(99.9)
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "open"


class TestGuards:
    def test_stop_through_the_market_exits_at_market_and_alerts(self, h):
        """L9: price fell through the stop before it could be placed. Kite refuses the
        SELL stop above LTP with an InputException (no order is created), so the
        stop is `not submitted` (#86: the pre-#86 simulator returned a REJECTED order instead)."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)  # below the 98 stop
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "exiting"
        assert h.position(bid)["unprotected_flag"] == 1
        assert any("UNPROTECTED" in t for t in h.critical_titles())
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["realised_pnl"]) == (
            "closed",
            "stop_not_submitted",
            -30.0,
        )
        assert h.paper.list_positions() == []

    def test_stop_rejected_by_rms_exits_at_market_and_alerts(self, h):
        """The stop is accepted, then RMS rejects it (status REJECTED)."""
        sim = h.paper._kite

        def reject_stops(call, params, proceed):
            oid = proceed()
            if call == "place_order" and params["order_type"] == "SL-M":
                sim._advance(sim._order(oid), "REJECTED", status_message="RMS:Blocked")
            return oid

        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        sim.interceptor = reject_stops
        h.sup.reconcile()
        assert any("UNPROTECTED" in t for t in h.critical_titles())
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["realised_pnl"]) == (
            "closed",
            "stop_rejected",
            0.0,
        )
        assert h.paper.list_positions() == []

    def test_both_legs_filled_buys_back_oversold(self, h):
        """OCO lag: target and stop both fill before reconcile runs."""
        bid = _filled_and_protected(h)
        h.tick(104.5)  # target fills at 104
        h.tick(97.0)  # stop fills at 97 (gap through 98): now short 10
        assert h.paper.list_positions()[0].quantity == -10
        h.sup.reconcile()
        assert any("Oversold" in t for t in h.critical_titles())
        h.sup.reconcile()
        b = h.bracket(bid)
        # cash: -100*10 + 104*10 + 97*10 - 97*10 (buy-back) = +40
        assert (b["state"], b["realised_pnl"]) == ("closed", 40.0)
        assert h.paper.list_positions() == []

    def test_stop_cancelled_outside_bot_exits_at_market(self, h):
        bid = _filled_and_protected(h)
        stop = h.om.sync_status(h.bracket(bid)["stop_order_id"])
        h.paper.cancel_order(stop.broker_order_id)  # e.g. operator cancels in the Kite app
        h.tick(100.5)
        h.sup.reconcile()
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("closed", "stop_cancelled")
        assert h.om.sync_status(b["target_order_id"]).status == OrderStatus.CANCELLED
        assert h.paper.list_positions() == []


class TestSquareOff:
    def test_open_trade_flattened_and_pending_entry_cancelled(self, h):
        open_bid = _filled_and_protected(h)
        pending = h.sup.open_trade(_req(entry=99.0))  # rests below LTP 100
        h.tick(101.0)
        h.sup.square_off_all()
        h.sup.reconcile()
        assert h.bracket(open_bid)["state"] == "closed"
        assert h.bracket(open_bid)["exit_reason"] == "squareoff"
        assert h.bracket(open_bid)["realised_pnl"] == 10.0  # (101 - 100) x 10
        assert h.bracket(pending)["state"] == "cancelled"
        assert h.paper.list_positions() == []
        assert h.sup.active_count() == 0


class TestRestartSafety:
    def test_new_supervisor_continues_from_db(self, h):
        bid = _filled_and_protected(h)
        fresh = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
        h.tick(104.2)
        fresh.reconcile()
        assert h.bracket(bid)["state"] == "closed"


class TestOrderManagerCancelRace:
    def test_cancel_records_a_fill_that_beat_it(self, h):
        """A stop that fires as we cancel it must be recorded FILLED, not CANCELLED."""
        bid = _filled_and_protected(h)
        stop_id = h.bracket(bid)["stop_order_id"]
        # broker fills the stop; our DB still says pending
        h.tick(97.9)
        final = h.om.cancel(stop_id)
        assert final.status == OrderStatus.FILLED
        assert final.filled_quantity == 10


class TestStopLimitEntry:
    """Breakout entries (#69): SL BUY trigger 100.5, limit 101; stop 98, target 104."""

    def _req(self, trigger=100.5, entry=101.0):
        return TradeRequest("INFY", "NSE", 10, entry, 98.0, 104.0, atr=2.0, entry_trigger=trigger)

    @pytest.mark.parametrize("trigger", [98.0, 97.0, 101.5])
    def test_trigger_must_sit_between_stop_and_limit(self, trigger):
        assert validate(self._req(trigger=trigger)) == "stop_trigger_limit_not_ordered"

    def test_waits_for_the_breakout_then_protects(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(self._req())
        entry = h.om.sync_status(h.bracket(bid)["entry_order_id"])
        assert (entry.order_type, entry.trigger_price, entry.price) == ("sl_limit", 100.5, 101.0)
        h.tick(100.2)
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "entry_pending"  # below the trigger: no fill
        h.tick(100.8)  # breaks out
        h.tick(100.8)
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "open"
