from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.paper_broker import PaperBroker
from executor.models import (
    BrokerName,
    GttRequest,
    GttStatus,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)

IST = ZoneInfo("Asia/Kolkata")


def _make_price_source() -> ManualFeed:
    return ManualFeed()


def _tick(symbol: str, ltp: float) -> Tick:
    return Tick(symbol=symbol, exchange="NSE", ltp=ltp, received_at=datetime.now(IST))


class TestPaperBrokerIdentity:
    def test_broker_id(self):
        pb = PaperBroker(price_source=_make_price_source())
        assert pb.broker_id == BrokerName.PAPER

    def test_authenticate_is_a_no_op(self):
        # Orders are simulated; the feed's lifecycle belongs to the orchestrator.
        pb = PaperBroker(price_source=_make_price_source())
        assert pb.authenticate() is None


class TestPaperBrokerTicks:
    def test_register_tick_updates_ltp(self):
        pb = PaperBroker(price_source=_make_price_source(), initial_cash=50_000)
        pb.register_tick("RELIANCE", 2500.0)
        assert pb.get_ltp("RELIANCE", "NSE") == pytest.approx(2500.0)

    def test_tick_fills_resting_limit_order(self):
        pb = PaperBroker(price_source=_make_price_source(), initial_cash=100_000)
        req = OrderRequest(
            symbol="TATA",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            product=ProductType.CNC,
            price=500.0,
        )
        order_id = pb.place_order(req)
        pb.register_tick("TATA", 490.0)  # price drops below limit → fills
        status = pb.get_order_status(order_id)
        assert status["status"] == OrderStatus.FILLED

    def test_feed_tick_reaches_simulator_without_on_tick_call(self):
        """Every feed tick drives fills, even if nobody subscribed via on_tick()."""
        feed = _make_price_source()
        pb = PaperBroker(price_source=feed, initial_cash=100_000)
        req = OrderRequest(
            symbol="TATA",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            product=ProductType.MIS,
            price=500.0,
        )
        order_id = pb.place_order(req)
        feed.push(_tick("TATA", 490.0))
        assert pb.get_order_status(order_id)["status"] == OrderStatus.FILLED
        assert pb.get_ltp("TATA", "NSE") == pytest.approx(490.0)

    def test_on_tick_subscribes_and_relays_only_requested_symbols(self):
        feed = _make_price_source()
        pb = PaperBroker(price_source=feed)
        got: list[tuple[str, float]] = []
        pb.on_tick(["INFY"], lambda s, p: got.append((s, p)))
        feed.push(_tick("INFY", 1500.0))
        feed.push(_tick("TCS", 3500.0))
        assert feed.subscribed == {"INFY"}
        assert got == [("INFY", 1500.0)]

    def test_get_ltp_falls_back_to_feed_latest(self):
        feed = _make_price_source()
        feed.push(_tick("WIPRO", 450.0))  # pushed before the broker existed
        pb = PaperBroker(price_source=feed)
        assert pb.get_ltp("WIPRO", "NSE") == pytest.approx(450.0)
        assert pb.get_ltp("NOPE", "NSE") is None


class TestPaperBrokerFunds:
    def test_initial_funds(self):
        pb = PaperBroker(price_source=_make_price_source(), initial_cash=75_000)
        funds = pb.get_funds()
        assert funds.available_cash == pytest.approx(75_000)
        assert funds.broker_id == BrokerName.PAPER


class TestPaperBrokerGTT:
    def test_place_and_cancel_gtt(self):
        pb = PaperBroker(price_source=_make_price_source())
        req = GttRequest(
            symbol="INFY",
            exchange="NSE",
            gtt_type=GttType.SINGLE,
            quantity=5,
            trigger_price=1500.0,
            limit_price=1498.0,
        )
        gtt_id = pb.place_gtt(req)
        assert pb.get_gtt(gtt_id)["status"] == GttStatus.GTT_ACTIVE
        pb.cancel_gtt(gtt_id)
        assert pb.get_gtt(gtt_id)["status"] == GttStatus.GTT_CANCELLED
