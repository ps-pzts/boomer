from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


def _make_price_source(available_cash: float = 100_000) -> MagicMock:
    source = MagicMock()
    source.authenticate.return_value = None
    source.on_tick.return_value = None
    return source


class TestPaperBrokerIdentity:
    def test_broker_id(self):
        pb = PaperBroker(price_source=_make_price_source())
        assert pb.broker_id == BrokerName.PAPER

    def test_authenticate_delegates_to_price_source(self):
        source = _make_price_source()
        pb = PaperBroker(price_source=source)
        pb.authenticate()
        source.authenticate.assert_called_once()


class TestPaperBrokerTicks:
    def test_register_tick_updates_ltp(self):
        pb = PaperBroker(price_source=_make_price_source(), initial_cash=50_000)
        pb.register_tick("RELIANCE", 2500.0)
        assert pb.get_ltp("RELIANCE", "NSE") == pytest.approx(2500.0)

    def test_tick_fills_resting_limit_order(self):
        pb = PaperBroker(price_source=_make_price_source(), initial_cash=100_000)
        req = OrderRequest(
            symbol="TATA", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, product=ProductType.CNC,
            price=500.0,
        )
        order_id = pb.place_order(req)
        pb.register_tick("TATA", 490.0)  # price drops below limit → fills
        status = pb.get_order_status(order_id)
        assert status["status"] == OrderStatus.FILLED


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
            symbol="INFY", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=5, trigger_price=1500.0, limit_price=1498.0,
        )
        gtt_id = pb.place_gtt(req)
        assert pb.get_gtt(gtt_id)["status"] == GttStatus.GTT_ACTIVE
        pb.cancel_gtt(gtt_id)
        assert pb.get_gtt(gtt_id)["status"] == GttStatus.GTT_CANCELLED
