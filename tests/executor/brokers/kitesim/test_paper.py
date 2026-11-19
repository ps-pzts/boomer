"""Paper mode's wiring (#86; ported from the paper broker's tests): KiteBroker on KiteSim,
fed by the live market feed through SimFeed."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.kitesim import SimFeed
from executor.brokers.kitesim.paper import build_paper_broker
from executor.models import BrokerName, OrderRequest, OrderSide, OrderStatus, OrderType
from executor.models import ProductType as P
from tests.kitesim_kit import Clock, fixture_instruments, make_sim

IST = ZoneInfo("Asia/Kolkata")


def _tick(symbol: str, ltp: float) -> Tick:
    return Tick(symbol=symbol, exchange="NSE", ltp=ltp, received_at=datetime.now(IST))


def _paper(feed: ManualFeed | None = None, cash: float = 100_000.0):
    return build_paper_broker(
        feed or ManualFeed(), cash, instruments=fixture_instruments(), clock=Clock()
    )


def _limit(symbol: str, price: float, product=P.MIS) -> OrderRequest:
    return OrderRequest(symbol, "NSE", OrderSide.BUY, OrderType.LIMIT, 10, product, price=price)


class TestIdentity:
    def test_broker_id(self):
        assert _paper().broker_id == BrokerName.PAPER

    def test_authenticate_is_a_no_op(self):
        assert _paper().authenticate() is None

    def test_production_clock_is_ist(self):
        """Without an injected clock the simulator reads IST wall-clock time."""
        b = build_paper_broker(ManualFeed(), 1.0)
        assert b._kite._clock().utcoffset().total_seconds() == 5.5 * 3600


class TestTicks:
    def test_tick_updates_ltp(self):
        feed = ManualFeed()
        pb = _paper(feed, cash=50_000)
        feed.push(_tick("RELIANCE", 2500.0))
        assert pb.get_ltp("RELIANCE", "NSE") == pytest.approx(2500.0)

    def test_tick_fills_resting_limit_order(self):
        feed = ManualFeed()
        pb = _paper(feed)
        order_id = pb.place_order(_limit("TATA", 500.0, P.CNC))
        feed.push(_tick("TATA", 490.0))  # price drops below limit -> fills at the limit
        status = pb.get_order_status(order_id)
        assert (status["status"], status["average_fill_price"]) == (OrderStatus.FILLED, 500.0)

    def test_feed_tick_reaches_simulator_without_on_tick_call(self):
        """Every feed tick drives fills, even if nobody subscribed via on_tick()."""
        feed = ManualFeed()
        pb = _paper(feed)
        order_id = pb.place_order(_limit("TATA", 500.0))
        feed.push(_tick("TATA", 490.0))
        assert pb.get_order_status(order_id)["status"] == OrderStatus.FILLED
        assert pb.get_ltp("TATA", "NSE") == pytest.approx(490.0)

    def test_on_tick_subscribes_and_relays_only_requested_symbols(self):
        feed = ManualFeed()
        pb = _paper(feed)
        got: list[tuple[str, float]] = []
        pb.on_tick(["INFY"], lambda s, p: got.append((s, p)))
        feed.push(_tick("INFY", 1500.0))
        feed.push(_tick("TCS", 3500.0))
        assert feed.subscribed == {"INFY"}
        assert got == [("INFY", 1500.0)]

    def test_get_ltp_falls_back_to_feed_latest(self):
        feed = ManualFeed()
        feed.push(_tick("WIPRO", 450.0))  # pushed before the broker existed
        pb = _paper(feed)
        assert pb.get_ltp("WIPRO", "NSE") == pytest.approx(450.0)
        assert pb.get_ltp("NOPE", "NSE") is None


class TestFunds:
    def test_initial_funds(self):
        funds = _paper(cash=75_000).get_funds()
        assert funds.available_cash == pytest.approx(75_000)
        assert funds.broker_id == BrokerName.PAPER


class TestSimFeed:
    def test_delegates_to_the_market_feed(self):
        feed = ManualFeed()
        sf = SimFeed(feed, make_sim())
        sf.connect()
        assert sf.health().connected and feed.health().connected
        sf.subscribe(["INFY"])
        assert feed.subscribed == {"INFY"}
        assert sf.capabilities() == feed.capabilities()
        sf.reauthenticate()
        sf.close()
        assert not feed.health().connected

    def test_order_updates_come_from_the_simulator_not_the_live_socket(self):
        feed = ManualFeed()
        sim = make_sim()
        got: list[dict] = []
        SimFeed(feed, sim).on_order_update(got.append)
        feed.push_order_update({"order_id": "real-account-order"})
        assert got == []
        sim.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1400.0)
        assert [u["status"] for u in got] == ["OPEN"]

    def test_tick_time_stamps_the_fill(self):
        feed = ManualFeed()
        sim = make_sim()
        SimFeed(feed, sim)
        sim.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1400.0)
        at = datetime(2026, 11, 19, 11, 22, 33, tzinfo=IST)
        feed.push(Tick("INFY", "NSE", 1399.0, at))
        (o,) = sim.orders()
        assert o["exchange_update_timestamp"] == "2026-11-19 11:22:33"
