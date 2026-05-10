from __future__ import annotations

import pytest

from executor.brokers.mock_broker import MockBroker
from executor.models import (
    GttRequest,
    GttStatus,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    PriceBar,
    ProductType,
)


def make_bar(symbol: str, close: float, low: float = 0.0, high: float = 0.0) -> PriceBar:
    return PriceBar(
        symbol=symbol, date="2024-01-02",
        open=close, high=high or close * 1.01,
        low=low or close * 0.99, close=close, volume=100_000,
    )


def limit_buy_request(symbol: str, price: float, qty: int = 10) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, exchange="NSE", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=qty, product=ProductType.MIS,
        price=price, validity=OrderValidity.DAY,
    )


def market_sell_request(symbol: str, qty: int = 10) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, exchange="NSE", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=qty, product=ProductType.CNC,
    )


class TestMockBrokerIdentity:
    def test_broker_id(self):
        assert MockBroker().broker_id.value == "mock"


class TestMockBrokerMarketOrder:
    def test_market_buy_fills_immediately_at_bar_open(self):
        broker = MockBroker(initial_cash=100_000)
        req = OrderRequest(
            symbol="TCS", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=5, product=ProductType.MIS,
        )
        bar = make_bar("TCS", close=3000.0)
        broker.set_price_bar(bar)

        broker_order_id = broker.place_order(req)
        status = broker.get_order_status(broker_order_id)

        assert status["status"] == OrderStatus.FILLED
        assert status["filled_quantity"] == 5
        assert status["average_fill_price"] == 3000.0

    def test_market_order_debits_cash(self):
        broker = MockBroker(initial_cash=50_000)
        bar = make_bar("INFY", close=1500.0)
        broker.set_price_bar(bar)
        req = OrderRequest(
            symbol="INFY", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10, product=ProductType.MIS,
        )
        broker.place_order(req)
        funds = broker.get_funds()
        assert funds.available_cash == pytest.approx(50_000 - 10 * 1500.0)


class TestMockBrokerLimitOrder:
    def test_limit_buy_fills_when_bar_low_crosses(self):
        broker = MockBroker(initial_cash=100_000)
        req = limit_buy_request("RELIANCE", price=2400.0, qty=5)
        bid = broker.place_order(req)
        # No bar yet — stays pending
        assert broker.get_order_status(bid)["status"] == OrderStatus.PENDING

        # Bar where low = 2380 crosses limit of 2400
        bar = PriceBar(
            "RELIANCE", "2024-01-02", open=2410, high=2420, low=2380, close=2390, volume=50_000
        )
        broker.set_price_bar(bar)
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED
        assert broker.get_order_status(bid)["average_fill_price"] == 2400.0

    def test_limit_buy_does_not_fill_when_price_above_limit(self):
        broker = MockBroker(initial_cash=100_000)
        req = limit_buy_request("RELIANCE", price=2300.0, qty=5)
        bid = broker.place_order(req)
        bar = PriceBar(
            "RELIANCE", "2024-01-02", open=2410, high=2420, low=2350, close=2390, volume=50_000
        )
        broker.set_price_bar(bar)
        assert broker.get_order_status(bid)["status"] == OrderStatus.PENDING

    def test_limit_sell_fills_when_bar_high_crosses(self):
        broker = MockBroker(initial_cash=100_000)
        req = OrderRequest(
            symbol="WIPRO", exchange="NSE", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=20, product=ProductType.CNC, price=450.0,
        )
        bid = broker.place_order(req)
        bar = PriceBar(
            "WIPRO", "2024-01-02", open=440, high=460, low=435, close=448, volume=200_000
        )
        broker.set_price_bar(bar)
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED


class TestMockBrokerCancelOrder:
    def test_cancel_pending_order(self):
        broker = MockBroker(initial_cash=100_000)
        req = limit_buy_request("HDFC", price=1600.0)
        bid = broker.place_order(req)
        broker.cancel_order(bid)
        assert broker.get_order_status(bid)["status"] == OrderStatus.CANCELLED

    def test_cancel_filled_order_is_noop(self):
        broker = MockBroker(initial_cash=100_000)
        bar = make_bar("HDFC", close=1600.0)
        broker.set_price_bar(bar)
        req = OrderRequest(
            symbol="HDFC", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1, product=ProductType.MIS,
        )
        bid = broker.place_order(req)
        broker.cancel_order(bid)
        assert broker.get_order_status(bid)["status"] == OrderStatus.FILLED


class TestMockBrokerGTT:
    def test_place_single_gtt(self):
        broker = MockBroker()
        req = GttRequest(
            symbol="TATA", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=10, trigger_price=500.0, limit_price=498.0,
        )
        gtt_id = broker.place_gtt(req)
        gtt = broker.get_gtt(gtt_id)
        assert gtt["status"] == GttStatus.GTT_ACTIVE
        assert gtt["trigger_price"] == 500.0

    def test_single_gtt_triggers_when_price_hits(self):
        broker = MockBroker()
        req = GttRequest(
            symbol="SAIL", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=100, trigger_price=90.0, limit_price=89.0,
        )
        gtt_id = broker.place_gtt(req)
        # Bar where low hits the trigger
        bar = PriceBar("SAIL", "2024-01-02", open=92, high=93, low=88, close=91, volume=500_000)
        broker.set_price_bar(bar)
        assert broker.get_gtt(gtt_id)["status"] == GttStatus.GTT_TRIGGERED

    def test_oco_gtt_triggers_sl_leg(self):
        broker = MockBroker()
        req = GttRequest(
            symbol="ONGC", exchange="NSE", gtt_type=GttType.OCO, quantity=50,
            sl_trigger_price=150.0, sl_limit_price=148.0,
            target_trigger_price=200.0, target_limit_price=202.0,
        )
        gtt_id = broker.place_gtt(req)
        bar = PriceBar(
            "ONGC", "2024-01-02", open=155, high=158, low=148, close=152, volume=1_000_000
        )
        broker.set_price_bar(bar)
        gtt = broker.get_gtt(gtt_id)
        assert gtt["status"] == GttStatus.GTT_TRIGGERED
        assert gtt["triggered_leg"] == "sl"

    def test_oco_gtt_triggers_target_leg(self):
        broker = MockBroker()
        req = GttRequest(
            symbol="ONGC", exchange="NSE", gtt_type=GttType.OCO, quantity=50,
            sl_trigger_price=150.0, sl_limit_price=148.0,
            target_trigger_price=200.0, target_limit_price=202.0,
        )
        gtt_id = broker.place_gtt(req)
        bar = PriceBar(
            "ONGC", "2024-01-02", open=195, high=205, low=192, close=198, volume=1_000_000
        )
        broker.set_price_bar(bar)
        gtt = broker.get_gtt(gtt_id)
        assert gtt["status"] == GttStatus.GTT_TRIGGERED
        assert gtt["triggered_leg"] == "target"

    def test_cancel_gtt(self):
        broker = MockBroker()
        req = GttRequest(
            symbol="SBI", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=100, trigger_price=500.0, limit_price=498.0,
        )
        gtt_id = broker.place_gtt(req)
        broker.cancel_gtt(gtt_id)
        assert broker.get_gtt(gtt_id)["status"] == GttStatus.GTT_CANCELLED

    def test_list_gtts(self):
        broker = MockBroker()
        for price in [100.0, 200.0, 300.0]:
            broker.place_gtt(GttRequest(
                symbol="X", exchange="NSE", gtt_type=GttType.SINGLE,
                quantity=10, trigger_price=price, limit_price=price - 1,
            ))
        assert len(broker.list_gtts()) == 3


class TestMockBrokerCallbacks:
    def test_order_update_callback_fires_on_fill(self):
        broker = MockBroker(initial_cash=100_000)
        updates: list[dict] = []
        broker.on_order_update(lambda u: updates.append(u))

        bar = make_bar("NTPC", close=200.0)
        broker.set_price_bar(bar)
        req = OrderRequest(
            symbol="NTPC", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=5, product=ProductType.MIS,
        )
        broker.place_order(req)
        assert len(updates) == 1
        assert updates[0]["status"] == OrderStatus.FILLED

    def test_tick_callback_fires_on_set_price_bar(self):
        broker = MockBroker()
        ticks: list[tuple] = []
        broker.on_tick(["ICICI"], lambda sym, ltp: ticks.append((sym, ltp)))
        bar = make_bar("ICICI", close=900.0)
        broker.set_price_bar(bar)
        assert ("ICICI", 900.0) in ticks
