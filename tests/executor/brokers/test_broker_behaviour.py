"""Shared broker-behaviour suite (#86, conformance (c)).

What our executor relies on from a broker, end to end through OrderManager and
the Broker interface. Today one implementation runs it: the paper KiteBroker on
KiteSim (exactly how paper mode is built, postbacks wired to OrderManager).
Hostile mode (#87) can add its own `stack` param and must pass the same suite.

Worked numbers (INFY tick 0.10, qty 10, MIS margin 20%):
  entry LIMIT 100 on a 100 tick              -> filled 10 @ 100.0
  SL-M SELL 98, tick 97.5 gaps through       -> filled @ 97.5; realised -25.0
  position 10 @ 100                          -> used margin 10 x 100 x 0.20 = 200.0
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import kiteconnect
import pytest

from db.migrations import run_migrations
from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kitesim import KiteSim, KiteSimConfig
from executor.brokers.kitesim.paper import build_paper_broker
from executor.brokers.paper_book import PaperBook
from executor.models import BrokerName, OrderRequest, OrderSide, OrderStatus, OrderType
from executor.models import ProductType as P
from executor.order_manager import OrderManager
from tests.kitesim_kit import CheckedKite, Clock, fixture_instruments

DAY = date(2026, 11, 19)
MIGRATIONS = Path(__file__).parents[3] / "migrations"


@dataclass
class Stack:
    feed: ManualFeed
    broker: KiteBroker
    sim: KiteSim
    om: OrderManager
    db: sqlite3.Connection
    db_path: str
    clock: Clock

    def tick(self, ltp: float, sym: str = "INFY") -> None:
        self.feed.push(Tick(sym, "NSE", ltp, self.clock.now))

    def submit(self, side, kind, qty=10, price=0.0, trigger=0.0) -> str:
        req = OrderRequest("INFY", "NSE", side, kind, qty, P.MIS, price, trigger)
        return self.om.submit(req, track="behaviour")

    def record(self, oid: str):
        return self.om._load_order(oid)  # what postbacks wrote, without polling


def build_stack(db_path: str, feed: ManualFeed | None = None, cash=1_000_000.0, **config) -> Stack:
    """Paper mode's wiring (build_paper_broker + PaperBook), SDK view contract-checked."""
    clock = Clock()
    feed = feed or ManualFeed()
    broker = build_paper_broker(
        feed,
        cash,
        instruments=fixture_instruments(),
        book=PaperBook(db_path, DAY),
        clock=clock,
        config=KiteSimConfig(**config),
    )
    sim: KiteSim = broker._kite  # type: ignore[assignment]
    broker._kite = CheckedKite(sim)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    om = OrderManager(db, {BrokerName.KITE: broker}, ltp_cache={})
    broker.on_order_update(om.handle_broker_update)
    return Stack(feed, broker, sim, om, db, db_path, clock)


def _fresh_db(tmp_path) -> str:
    path = str(tmp_path / "b.db")
    run_migrations(path, MIGRATIONS)
    return path


@pytest.fixture(params=["paper-kitesim"])
def stack(request, tmp_path):
    s = build_stack(_fresh_db(tmp_path))
    yield s
    s.broker._kite.verify()


BUY, SELL = OrderSide.BUY, OrderSide.SELL


class TestEntries:
    def test_entry_limit_fills_and_postback_reaches_order_manager(self, stack):
        stack.tick(100.0)
        oid = stack.submit(BUY, OrderType.LIMIT, price=99.0)
        assert stack.record(oid).status == OrderStatus.PENDING
        stack.tick(98.9)  # no polling: the postback updates our row
        rec = stack.record(oid)
        assert (rec.status, rec.filled_quantity, rec.average_fill_price) == ("filled", 10, 99.0)

    def test_sl_limit_breakout_entry(self, stack):
        stack.tick(100.0)
        oid = stack.submit(BUY, OrderType.SL_LIMIT, price=101.5, trigger=101.0)
        stack.tick(100.9)
        assert stack.om.sync_status(oid).status == OrderStatus.PENDING
        stack.tick(101.2)
        rec = stack.record(oid)
        assert (rec.status, rec.average_fill_price) == (OrderStatus.FILLED, 101.2)


class TestStops:
    def test_sl_m_stop_gap_through_fills_worse_and_flattens(self, stack):
        stack.tick(100.0)
        stack.submit(BUY, OrderType.LIMIT, price=100.0)
        stop = stack.submit(SELL, OrderType.SL, trigger=98.0)
        stack.tick(98.5)
        assert stack.record(stop).status == OrderStatus.PENDING
        stack.tick(97.5)
        rec = stack.record(stop)
        assert (rec.status, rec.average_fill_price) == (OrderStatus.FILLED, 97.5)
        assert stack.broker.list_positions() == []
        (row,) = stack.sim.positions()["net"]
        assert row["realised"] == -25.0

    def test_trailing_modify_moves_the_trigger(self, stack):
        stack.tick(100.0)
        stack.submit(BUY, OrderType.LIMIT, price=100.0)
        stop = stack.submit(SELL, OrderType.SL, trigger=98.0)
        stack.tick(103.0)
        assert stack.om.modify_trigger(stop, 101.0).trigger_price == 101.0
        stack.tick(100.9)
        assert stack.record(stop).average_fill_price == 100.9

    def test_trail_through_the_market_is_refused(self, stack):
        stack.tick(100.0)
        stop = stack.submit(SELL, OrderType.SL, trigger=98.0)
        with pytest.raises(kiteconnect.exceptions.InputException):
            stack.om.modify_trigger(stop, 100.5)
        assert stack.om.sync_status(stop).trigger_price == 98.0

    def test_cancel(self, stack):
        stack.tick(100.0)
        stop = stack.submit(SELL, OrderType.SL, trigger=98.0)
        assert stack.om.cancel(stop).status == OrderStatus.CANCELLED
        stack.tick(97.0)
        assert stack.om.sync_status(stop).status == OrderStatus.CANCELLED


class TestPartialFill:
    def test_partial_fills_accumulate(self, tmp_path):
        stack = build_stack(_fresh_db(tmp_path), liquidity_per_tick=4)
        stack.tick(101.0)
        oid = stack.submit(BUY, OrderType.LIMIT, price=100.0)
        stack.tick(100.0)
        rec = stack.record(oid)
        assert (rec.status, rec.filled_quantity) == (OrderStatus.PARTIAL, 4)
        stack.tick(100.0)
        stack.tick(100.0)
        rec = stack.record(oid)
        assert (rec.status, rec.filled_quantity) == (OrderStatus.FILLED, 10)

    def test_cancel_after_partial_records_the_filled_part(self, tmp_path):
        stack = build_stack(_fresh_db(tmp_path), liquidity_per_tick=3)
        stack.tick(100.0)
        oid = stack.submit(BUY, OrderType.LIMIT, price=100.0)
        rec = stack.om.cancel(oid)
        assert (rec.status, rec.filled_quantity) == (OrderStatus.CANCELLED, 3)


class TestRejections:
    def test_input_errors_become_rejected_orders(self, stack):
        stack.tick(100.0)
        with pytest.raises(kiteconnect.exceptions.InputException):
            stack.submit(SELL, OrderType.SL, trigger=100.5)  # wrong side
        (row,) = stack.db.execute("SELECT status FROM orders").fetchall()
        assert row[0] == OrderStatus.REJECTED

    @pytest.mark.parametrize(
        ("config", "price", "reason"),
        [
            ({"circuit_pct": 0.01}, 98.0, "lower circuit"),
            ({"freeze_quantity": 5}, 100.0, "freeze"),
        ],
    )
    def test_rms_rejections_reach_order_manager(self, tmp_path, config, price, reason):
        """The REJECTED postback arrives before place_order returns, i.e. before
        OrderManager knows the broker id, so it can't be matched; the next status
        poll records the rejection (as it must live: Kite can do the same)."""
        s = build_stack(_fresh_db(tmp_path), **config)
        s.tick(100.0)
        oid = s.submit(BUY, OrderType.LIMIT, price=price)
        rec = s.om.sync_status(oid)
        assert rec.status == OrderStatus.REJECTED
        assert reason in s.broker.get_order_status(rec.broker_order_id)["rejection_reason"]

    def test_margin_rejection_at_the_broker(self, tmp_path):
        """OrderManager's own funds check (full value vs opening cash) refuses first,
        so Kite's margin rejection is checked at the Broker interface:
        10 x 100 x 20% = 200 margin > 150 cash -> REJECTED."""
        s = build_stack(_fresh_db(tmp_path), cash=150.0)
        s.tick(100.0)
        bid = s.broker.place_order(OrderRequest("INFY", "NSE", BUY, OrderType.LIMIT, 10, P.MIS,
                                                price=100.0))  # fmt: skip
        st = s.broker.get_order_status(bid)
        assert st["status"] == OrderStatus.REJECTED
        assert st["rejection_reason"].startswith("Insufficient funds. Required margin is 200.00")

    def test_mis_after_cutoff_rejected(self, stack):
        stack.clock.now = datetime(2026, 11, 19, 15, 21, tzinfo=stack.clock.now.tzinfo)
        stack.tick(100.0)
        oid = stack.submit(BUY, OrderType.LIMIT, price=100.0)
        assert stack.om.sync_status(oid).status == OrderStatus.REJECTED


class TestAccounting:
    def test_positions_and_margins(self, stack):
        stack.tick(100.0)
        stack.submit(BUY, OrderType.LIMIT, price=100.0)
        (pos,) = stack.broker.list_positions()
        assert (pos.quantity, pos.average_price, pos.broker_id) == (10, 100.0, BrokerName.PAPER)
        funds = stack.broker.get_funds()
        assert (funds.available_cash, funds.used_margin) == (1_000_000.0, 200.0)


class TestPersistence:
    def test_restart_round_trip(self, stack):
        stack.tick(100.0)
        stack.submit(BUY, OrderType.LIMIT, price=100.0)
        stop = stack.submit(SELL, OrderType.SL, trigger=98.0)
        again = build_stack(stack.db_path, feed=ManualFeed())  # a new process
        assert again.sim.orders() == stack.sim.orders()
        assert again.sim.positions() == stack.sim.positions()
        again.tick(100.0)
        again.tick(97.9)
        rec = again.om.sync_status(stop)
        assert (rec.status, rec.average_fill_price) == (OrderStatus.FILLED, 97.9)
