"""Stop-loss fills, MIS positions and order-update payloads in MockBroker (#40).

Paper trading (PaperBroker) feeds every live tick to MockBroker as a bar with
open = high = low = close = LTP, so most tests use tick bars.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.mock_broker import MockBroker
from executor.brokers.paper_broker import PaperBroker
from executor.models import (
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceBar,
    ProductType,
)
from executor.order_manager import OrderManager

IST = ZoneInfo("Asia/Kolkata")


def tick_bar(symbol: str, ltp: float) -> PriceBar:
    return PriceBar(symbol=symbol, date="", open=ltp, high=ltp, low=ltp, close=ltp, volume=0)


def req(side, order_type, qty=10, price=0.0, trigger=0.0, product=ProductType.MIS, sym="INFY"):
    return OrderRequest(
        symbol=sym,
        exchange="NSE",
        side=side,
        order_type=order_type,
        quantity=qty,
        product=product,
        price=price,
        trigger_price=trigger,
    )


def _status(broker: MockBroker, oid: str) -> OrderStatus:
    return broker.get_order_status(oid)["status"]


class TestStopMarket:
    def test_sell_stop_waits_then_fills_at_tick_through_trigger(self):
        b = MockBroker(initial_cash=1_000_000)
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        b.set_price_bar(tick_bar("INFY", 99.0))
        assert _status(b, oid) == OrderStatus.PENDING
        b.set_price_bar(tick_bar("INFY", 97.6))  # gapped through 98 between ticks
        assert _status(b, oid) == OrderStatus.FILLED
        assert b.get_order_status(oid)["average_fill_price"] == 97.6  # worse than trigger

    def test_sell_stop_fills_at_trigger_when_bar_trades_down_through_it(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        b.set_price_bar(
            PriceBar("INFY", "", open=101.0, high=101.5, low=97.0, close=99.0, volume=0)
        )
        assert b.get_order_status(oid)["average_fill_price"] == 98.0

    def test_trigger_touch_exactly_fills(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        b.set_price_bar(tick_bar("INFY", 98.0))
        assert b.get_order_status(oid)["average_fill_price"] == 98.0

    def test_buy_stop_mirrors(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.BUY, OrderType.SL, trigger=102.0))
        b.set_price_bar(tick_bar("INFY", 101.9))
        assert _status(b, oid) == OrderStatus.PENDING
        b.set_price_bar(tick_bar("INFY", 102.4))
        assert b.get_order_status(oid)["average_fill_price"] == 102.4


class TestStopRejection:
    """Kite rejects a stop already on the wrong side of the market (design L9)."""

    @pytest.mark.parametrize(
        ("side", "trigger"),
        [
            (OrderSide.SELL, 100.0),
            (OrderSide.SELL, 101.0),
            (OrderSide.BUY, 100.0),
            (OrderSide.BUY, 99.0),
        ],
    )
    def test_wrong_side_trigger_rejected_with_update(self, side, trigger):
        b = MockBroker()
        updates: list[dict] = []
        b.on_order_update(updates.append)
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(side, OrderType.SL, trigger=trigger))
        assert _status(b, oid) == OrderStatus.REJECTED
        assert "trigger" in b.get_order_status(oid)["rejection_reason"]
        assert updates[-1]["status"] == OrderStatus.REJECTED

    def test_missing_trigger_rejected(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=0.0))
        assert _status(b, oid) == OrderStatus.REJECTED

    def test_no_price_known_yet_is_accepted(self):
        b = MockBroker()
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        assert _status(b, oid) == OrderStatus.PENDING

    def test_cancel_keeps_rejected_status(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=101.0))
        b.cancel_order(oid)
        assert _status(b, oid) == OrderStatus.REJECTED


class TestStopLimit:
    def test_triggers_then_fills_when_limit_reachable(self):
        b = MockBroker()
        updates: list[dict] = []
        b.on_order_update(updates.append)
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL_LIMIT, trigger=98.0, price=97.5))
        b.set_price_bar(tick_bar("INFY", 97.0))  # triggers; 97.0 < limit 97.5: no fill
        assert _status(b, oid) == OrderStatus.TRIGGERED
        assert updates[-1]["status"] == OrderStatus.TRIGGERED
        b.set_price_bar(tick_bar("INFY", 97.6))
        assert _status(b, oid) == OrderStatus.FILLED
        assert b.get_order_status(oid)["average_fill_price"] == 97.5

    def test_triggered_order_can_be_cancelled(self):
        b = MockBroker()
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL_LIMIT, trigger=98.0, price=97.5))
        b.set_price_bar(tick_bar("INFY", 97.0))
        b.cancel_order(oid)
        assert _status(b, oid) == OrderStatus.CANCELLED


class TestMisPositions:
    def test_average_price_net_and_flat(self):
        b = MockBroker(initial_cash=1_000_000)
        b.set_price_bar(tick_bar("INFY", 100.0))
        b.place_order(req(OrderSide.BUY, OrderType.MARKET, qty=10))
        b.set_price_bar(tick_bar("INFY", 110.0))
        b.place_order(req(OrderSide.BUY, OrderType.MARKET, qty=10))
        (pos,) = b.list_positions()
        assert (pos.quantity, pos.average_price) == (20, 105.0)  # (100*10 + 110*10) / 20

        b.place_order(req(OrderSide.SELL, OrderType.MARKET, qty=5))
        (pos,) = b.list_positions()
        assert (pos.quantity, pos.average_price) == (15, 105.0)  # reducing keeps avg

        b.place_order(req(OrderSide.SELL, OrderType.MARKET, qty=15))
        assert b.list_positions() == []

    def test_selling_through_zero_flips_at_fill_price(self):
        """The double-exit hazard PR 4b must guard against: both OCO legs fill."""
        b = MockBroker(initial_cash=1_000_000)
        b.set_price_bar(tick_bar("INFY", 100.0))
        b.place_order(req(OrderSide.BUY, OrderType.MARKET, qty=10))
        b.place_order(req(OrderSide.SELL, OrderType.MARKET, qty=20))
        (pos,) = b.list_positions()
        assert (pos.quantity, pos.average_price) == (-10, 100.0)

    def test_cnc_fills_not_in_mis_positions(self):
        b = MockBroker(initial_cash=1_000_000)
        b.set_price_bar(tick_bar("INFY", 100.0))
        b.place_order(req(OrderSide.BUY, OrderType.MARKET, product=ProductType.CNC))
        assert b.list_positions() == []


class TestUpdatePayload:
    def test_payload_has_kite_order_id_key_and_is_a_copy(self):
        b = MockBroker()
        got: list[dict] = []
        b.on_order_update(got.append)
        b.set_price_bar(tick_bar("INFY", 100.0))
        oid = b.place_order(req(OrderSide.BUY, OrderType.MARKET))
        assert got[-1]["order_id"] == oid == got[-1]["broker_order_id"]
        got[-1]["status"] = "tampered"
        assert _status(b, oid) == OrderStatus.FILLED


def _om_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    for f in (
        "migrations/0001_initial_schema.sql",
        "migrations/0004_executor_schema.sql",
        "migrations/0012_events.sql",
    ):
        with open(f) as fh:
            db.executescript(fh.read())
    return db


class TestPaperEndToEnd:
    """Real feed ticks → PaperBroker → OrderManager sees the stop fill (regression #40)."""

    def test_stop_fill_reaches_order_manager_via_update(self):
        feed = ManualFeed()
        paper = PaperBroker(price_source=feed, initial_cash=1_000_000)
        om = OrderManager(db=_om_db(), brokers={BrokerName.KITE: paper}, ltp_cache={})
        paper.on_order_update(om.handle_broker_update)

        def tick(ltp: float) -> None:
            feed.push(Tick(symbol="INFY", exchange="NSE", ltp=ltp, received_at=datetime.now(IST)))

        tick(100.0)
        entry = om.submit(req(OrderSide.BUY, OrderType.MARKET), track="paper-test")
        stop = om.submit(req(OrderSide.SELL, OrderType.SL, trigger=98.0), track="paper-test")
        assert om.sync_status(entry).status == OrderStatus.FILLED
        assert om.sync_status(stop).status == OrderStatus.PENDING

        tick(97.9)

        record = om._load_order(stop)  # updated by the push callback, no polling
        assert record.status == OrderStatus.FILLED
        assert record.average_fill_price == 97.9
        assert paper.list_positions() == []
