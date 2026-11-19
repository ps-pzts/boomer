"""Order basics on the paper KiteBroker + KiteSim (#86; ported from the pre-#86 simulator's tests).

Every call goes KiteBroker -> CheckedKite -> KiteSim, so each response is also
checked against Kite's documented shapes.
"""

from __future__ import annotations

import kiteconnect
import pytest

from executor.models import (
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    PriceBar,
    ProductType,
)
from tests.kitesim_kit import make_sim, paper_broker


def make_bar(symbol: str, close: float, low: float = 0.0, high: float = 0.0) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        date="2024-01-02",
        open=close,
        high=high or close * 1.01,
        low=low or close * 0.99,
        close=close,
        volume=100_000,
    )


def limit_buy_request(symbol: str, price: float, qty: int = 10) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        product=ProductType.MIS,
        price=price,
        validity=OrderValidity.DAY,
    )


def market(symbol: str, qty: int, side=OrderSide.BUY, product=ProductType.MIS) -> OrderRequest:
    return OrderRequest(symbol, "NSE", side, OrderType.MARKET, qty, product)


@pytest.fixture()
def sim():
    return make_sim()


@pytest.fixture()
def broker(sim):
    b = paper_broker(sim)
    yield b
    b._kite.verify()


class TestIdentity:
    def test_paper_rows_are_labelled_paper(self, broker):
        assert broker.broker_id == BrokerName.PAPER

    def test_authenticate_is_a_no_op_on_the_simulator(self, broker):
        assert broker.authenticate() is None


class TestMarketOrder:
    def test_market_buy_fills_immediately_at_the_last_price(self, sim, broker):
        sim.on_bar(make_bar("TCS", close=3000.0))
        oid = broker.place_order(market("TCS", 5))
        status = broker.get_order_status(oid)
        assert status["status"] == OrderStatus.FILLED
        assert status["filled_quantity"] == 5
        assert status["average_fill_price"] == 3000.0

    def test_market_order_blocks_mis_margin(self):
        """Kite keeps available.cash at the opening balance and books the MIS margin
        (value x 20%) in utilised.debits; the pre-#86 simulator debited the full value from cash.
        Worked: 10 x 1500 = 15,000 value -> 3,000 margin; net 50,000 - 3,000."""
        sim = make_sim(cash=50_000)
        broker = paper_broker(sim)
        sim.on_bar(make_bar("INFY", close=1500.0))
        broker.place_order(market("INFY", 10))
        funds = broker.get_funds()
        assert funds.available_cash == pytest.approx(50_000)
        assert funds.used_margin == pytest.approx(10 * 1500.0 * 0.20)
        assert sim.margins(segment="equity")["net"] == pytest.approx(50_000 - 3_000)

    def test_market_order_with_no_price_yet_rests_until_the_first_tick(self, sim, broker):
        oid = broker.place_order(market("TCS", 5))
        assert broker.get_order_status(oid)["status"] == OrderStatus.PENDING
        sim.on_price("TCS", "NSE", 3001.0)
        assert broker.get_order_status(oid)["average_fill_price"] == 3001.0


class TestLimitOrder:
    def test_limit_buy_fills_at_limit_when_bar_low_crosses(self, sim, broker):
        bid = broker.place_order(limit_buy_request("RELIANCE", price=2400.0, qty=5))
        assert broker.get_order_status(bid)["status"] == OrderStatus.PENDING  # no price yet
        sim.on_bar(PriceBar("RELIANCE", "2024-01-02", 2410, 2420, 2380, 2390, 50_000))
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED
        assert broker.get_order_status(bid)["average_fill_price"] == 2400.0

    def test_limit_buy_does_not_fill_when_price_above_limit(self, sim, broker):
        bid = broker.place_order(limit_buy_request("RELIANCE", price=2300.0, qty=5))
        sim.on_bar(PriceBar("RELIANCE", "2024-01-02", 2410, 2420, 2350, 2390, 50_000))
        assert broker.get_order_status(bid)["status"] == OrderStatus.PENDING

    def test_limit_sell_fills_when_bar_high_crosses(self, sim, broker):
        req = OrderRequest("WIPRO", "NSE", OrderSide.SELL, OrderType.LIMIT, 20, ProductType.CNC,
                           price=450.0)  # fmt: skip
        bid = broker.place_order(req)
        sim.on_bar(PriceBar("WIPRO", "2024-01-02", 440, 460, 435, 448, 200_000))
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED
        assert broker.get_order_status(bid)["average_fill_price"] == 450.0

    def test_marketable_limit_fills_at_the_market_not_the_limit(self, sim, broker):
        """Kite fills a BUY limit above the market at the market (the pre-#86 simulator filled at
        the limit price, overstating the cost)."""
        sim.on_price("INFY", "NSE", 1500.0)
        oid = broker.place_order(limit_buy_request("INFY", price=1502.0))
        assert broker.get_order_status(oid)["average_fill_price"] == 1500.0

    def test_resting_limit_reached_later_fills_at_its_limit(self, sim, broker):
        sim.on_price("INFY", "NSE", 1500.0)
        oid = broker.place_order(limit_buy_request("INFY", price=1490.0))
        sim.on_price("INFY", "NSE", 1489.5)
        assert broker.get_order_status(oid)["average_fill_price"] == 1490.0


class TestCancelOrder:
    def test_cancel_pending_order(self, broker):
        bid = broker.place_order(limit_buy_request("HDFC", price=1600.0))
        broker.cancel_order(bid)
        assert broker.get_order_status(bid)["status"] == OrderStatus.CANCELLED

    def test_cancel_filled_order_is_refused_and_stays_filled(self, sim, broker):
        """Kite answers a cancel of a completed order with an error (the pre-#86 simulator
        ignored it silently); OrderManager.cancel catches it and syncs."""
        sim.on_bar(make_bar("HDFC", close=1600.0))
        bid = broker.place_order(market("HDFC", 1))
        with pytest.raises(kiteconnect.exceptions.InputException, match="complete"):
            broker.cancel_order(bid)
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED

    def test_unknown_order_raises_order_exception(self, broker):
        with pytest.raises(kiteconnect.exceptions.OrderException):
            broker.cancel_order("999")


class TestCallbacks:
    def test_order_update_postbacks_fire_on_fill(self, sim, broker):
        updates: list[dict] = []
        broker.on_order_update(updates.append)
        sim.on_bar(make_bar("NTPC", close=200.0))
        oid = broker.place_order(market("NTPC", 5))
        # Kite sends one postback per state change: accepted (OPEN), then filled.
        assert [u["status"] for u in updates] == ["OPEN", "COMPLETE"]
        assert all(u["order_id"] == oid for u in updates)

    def test_tick_callback_relays_the_feed(self):
        from datetime import datetime

        from executor.brokers.feeds.base import Tick
        from executor.brokers.feeds.manual_feed import ManualFeed

        feed = ManualFeed()
        broker = paper_broker(make_sim(), feed)
        ticks: list[tuple] = []
        broker.on_tick(["ICICI"], lambda sym, ltp: ticks.append((sym, ltp)))
        feed.push(Tick("ICICI", "NSE", 900.0, datetime(2026, 11, 19, 10, 0)))
        assert ("ICICI", 900.0) in ticks


class TestMisPositions:
    def test_average_price_net_and_flat(self, sim, broker):
        sim.on_price("INFY", "NSE", 100.0)
        broker.place_order(market("INFY", 10))
        sim.on_price("INFY", "NSE", 110.0)
        broker.place_order(market("INFY", 10))
        (pos,) = broker.list_positions()
        assert (pos.quantity, pos.average_price) == (20, 105.0)  # (100*10 + 110*10) / 20

        broker.place_order(market("INFY", 5, OrderSide.SELL))
        (pos,) = broker.list_positions()
        assert (pos.quantity, pos.average_price) == (15, 105.0)  # reducing keeps avg

        broker.place_order(market("INFY", 15, OrderSide.SELL))
        assert broker.list_positions() == []

    def test_selling_through_zero_flips_at_fill_price(self, sim, broker):
        """The double-exit hazard: both OCO legs fill."""
        sim.on_price("INFY", "NSE", 100.0)
        broker.place_order(market("INFY", 10))
        broker.place_order(market("INFY", 20, OrderSide.SELL))
        (pos,) = broker.list_positions()
        assert (pos.quantity, pos.average_price) == (-10, 100.0)

    def test_cnc_fills_not_in_mis_positions(self, sim, broker):
        sim.on_price("INFY", "NSE", 100.0)
        broker.place_order(market("INFY", 10, product=ProductType.CNC))
        assert broker.list_positions() == []
        (row,) = sim.positions()["net"]
        assert (row["product"], row["quantity"]) == ("CNC", 10)
