"""KiteBroker against documented Kite responses, one SDK call at a time (#84).

The SDK is an autospec'd KiteConnect: a wrong method name or argument fails as
the real SDK would, and every response comes from tests/fixtures/kite.
"""

from __future__ import annotations

import pytest

from executor.brokers.kite_broker import KiteBroker
from executor.models import BrokerName, OrderStatus, ProductType
from tests import kite_fixtures as kf
from tests.kite_sdk import respond


@pytest.fixture()
def broker(kite_sdk) -> KiteBroker:
    b = KiteBroker()
    b._kite = kite_sdk
    return b


class TestPortfolio:
    def test_positions_are_the_open_day_positions(self, broker):
        respond(broker._kite, "positions", kf.sdk("positions"))
        (p,) = broker.list_positions()  # RELIANCE was bought and sold: flat
        assert (p.symbol, p.exchange, p.quantity, p.average_price, p.last_price) == (
            "INFY",
            "NSE",
            10,
            1502.3,
            1510.15,
        )
        assert (p.product, p.broker_id) == (ProductType.MIS, BrokerName.KITE)

    def test_holdings(self, broker):
        respond(broker._kite, "holdings", kf.sdk("holdings"))
        (h,) = broker.list_holdings()
        assert (h.symbol, h.quantity, h.average_price, h.product) == (
            "KPEL",
            18,
            355.2,
            ProductType.CNC,
        )

    def test_funds_from_equity_margins(self, broker):
        respond(broker._kite, "margins", kf.sdk("margins"))
        f = broker.get_funds()
        assert (f.available_cash, f.used_margin) == (245431.6, 145706.55)
        broker._kite.margins.assert_called_once_with(segment="equity")


class TestOrders:
    @pytest.mark.parametrize(
        "order_id, status, filled, avg",
        [
            ("240611000123401", OrderStatus.FILLED, 10, 1502.3),
            ("240611000123402", OrderStatus.PENDING, 0, 0),  # TRIGGER PENDING SL-M
            ("240611000123403", OrderStatus.PENDING, 0, 0),  # OPEN LIMIT
            ("240611000123404", OrderStatus.PARTIAL, 20, 2890.05),
            ("240611000123405", None, 0, 0),  # MODIFY PENDING: no change
            ("240611000123406", None, 0, 0),  # CANCEL PENDING: no change
            ("240611000123407", OrderStatus.CANCELLED, 0, 0),
        ],
    )
    def test_status_of_each_lifecycle_order(self, broker, order_id, status, filled, avg):
        respond(broker._kite, "orders", kf.sdk("orders"))
        got = broker.get_order_status(order_id)
        assert (got["status"], got["filled_quantity"], got["average_fill_price"]) == (
            status,
            filled,
            avg,
        )

    @pytest.mark.parametrize(
        "order_id, reason",
        [
            ("240611000123408", "Insufficient funds"),
            ("240611000123409", "lower circuit limit"),
        ],
    )
    def test_rejection_reason_is_kites_status_message(self, broker, order_id, reason):
        respond(broker._kite, "orders", kf.sdk("orders"))
        got = broker.get_order_status(order_id)
        assert got["status"] == OrderStatus.REJECTED
        assert reason in got["rejection_reason"]

    def test_unknown_order_is_empty(self, broker):
        respond(broker._kite, "orders", kf.sdk("orders"))
        assert broker.get_order_status("999") == {}

    def test_open_orders_are_the_working_and_in_flight_ones(self, broker):
        respond(broker._kite, "orders", kf.sdk("orders"))
        got = {o["broker_order_id"]: o["status"] for o in broker.list_open_orders()}
        assert got == {
            "240611000123402": "TRIGGER PENDING",
            "240611000123403": "OPEN",
            "240611000123404": "OPEN",
            "240611000123405": "MODIFY PENDING",
            "240611000123406": "CANCEL PENDING",
        }

    def test_modify_moves_the_trigger(self, broker):
        respond(broker._kite, "modify_order", "240611000123402")
        assert broker.modify_order("240611000123402", {"trigger_price": 1490.0}) == (
            "240611000123402"
        )
        broker._kite.modify_order.assert_called_once_with(
            variety="regular", order_id="240611000123402", trigger_price=1490.0
        )

    def test_cancel(self, broker):
        respond(broker._kite, "cancel_order", "240611000123403")
        broker.cancel_order("240611000123403")
        broker._kite.cancel_order.assert_called_once_with(
            variety="regular", order_id="240611000123403"
        )

    def test_kite_refusal_propagates(self, broker):
        respond(broker._kite, "modify_order", kf.sdk_error("input"))
        with pytest.raises(Exception, match="Trigger price"):
            broker.modify_order("240611000123402", {"trigger_price": 1600.0})


class TestHistorical:
    def test_daily_candles(self, broker):
        respond(broker._kite, "instruments", kf.sdk("instruments"))
        respond(broker._kite, "historical_data", kf.sdk("historical_data"))
        bars = broker.get_historical_ohlcv("INFY", "NSE", "2024-06-07", "2024-06-10")
        assert [(b.date, b.close, b.volume) for b in bars] == [
            ("2024-06-07", 1497.4, 5123456),
            ("2024-06-10", 1498.4, 4876012),
        ]
        assert broker._kite.historical_data.call_args.kwargs["instrument_token"] == 408065


class TestGtt:
    def test_list_and_get(self, broker):
        respond(broker._kite, "get_gtts", kf.sdk("get_gtts"))
        respond(broker._kite, "get_gtt", kf.sdk("get_gtt"))
        assert [g["id"] for g in broker.list_gtts()] == [123456, 123457]
        assert broker.get_gtt("123456")["status"] == "active"
        broker._kite.get_gtt.assert_called_once_with(trigger_id=123456)

    def test_cancel(self, broker):
        respond(broker._kite, "delete_gtt", kf.sdk("place_gtt"))
        broker.cancel_gtt("123456")
        broker._kite.delete_gtt.assert_called_once_with(trigger_id=123456)
