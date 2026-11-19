"""Stop orders on the paper KiteBroker + KiteSim (#86; ported from the pre-#86 simulator #40).

A live tick is a price jump: a stop it gaps through fills at the tick (worse than
the trigger). Inside a bar the walk is continuous: a stop the bar trades through
fills at its trigger.
"""

from __future__ import annotations

import kiteconnect
import pytest

from executor.brokers.kite_broker import map_kite_status
from executor.brokers.kite_contract import check_shape
from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, PriceBar, ProductType
from tests.kitesim_kit import make_sim, paper_broker


def req(side, order_type, qty=10, price=0.0, trigger=0.0, product=ProductType.MIS, sym="INFY"):
    return OrderRequest(sym, "NSE", side, order_type, qty, product, price, trigger)


@pytest.fixture()
def sim():
    return make_sim()


@pytest.fixture()
def b(sim):
    broker = paper_broker(sim)
    yield broker
    broker._kite.verify()


def _status(b, oid: str) -> OrderStatus:
    return b.get_order_status(oid)["status"]


def _fill(b, oid: str) -> float:
    return b.get_order_status(oid)["average_fill_price"]


class TestStopMarket:
    def test_sell_stop_waits_then_fills_at_tick_through_trigger(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        sim.on_price("INFY", "NSE", 99.0)
        assert _status(b, oid) == OrderStatus.PENDING
        sim.on_price("INFY", "NSE", 97.6)  # gapped through 98 between ticks
        assert _status(b, oid) == OrderStatus.FILLED
        assert _fill(b, oid) == 97.6  # worse than trigger

    def test_sell_stop_fills_at_trigger_when_bar_trades_down_through_it(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        sim.on_bar(PriceBar("INFY", "", open=101.0, high=101.5, low=97.0, close=99.0, volume=0))
        assert _fill(b, oid) == 98.0

    def test_bar_opening_below_the_stop_fills_at_the_open(self, sim, b):
        """Gap-down open: min(trigger, open), the pre-#86 simulator's gap rule."""
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        sim.on_bar(PriceBar("INFY", "", open=96.0, high=99.0, low=95.0, close=97.0, volume=0))
        assert _fill(b, oid) == 96.0

    def test_trigger_touch_exactly_fills(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        sim.on_price("INFY", "NSE", 98.0)
        assert _fill(b, oid) == 98.0

    def test_buy_stop_mirrors(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.BUY, OrderType.SL, trigger=102.0))
        sim.on_price("INFY", "NSE", 101.9)
        assert _status(b, oid) == OrderStatus.PENDING
        sim.on_price("INFY", "NSE", 102.4)
        assert _fill(b, oid) == 102.4

    def test_kite_lifecycle_strings(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        sim.on_price("INFY", "NSE", 97.0)
        assert [o["status"] for o in sim.order_history(oid)] == [
            "TRIGGER PENDING",
            "OPEN",
            "COMPLETE",
        ]


class TestStopRejection:
    """Kite refuses a stop already on the wrong side of the market (design L9) with
    an InputException (errors.json["input"]) and no order is created. The pre-#86 simulator
    returned an id and showed the order REJECTED; OrderManager now records ERROR."""

    @pytest.mark.parametrize(
        ("side", "trigger", "word"),
        [
            (OrderSide.SELL, 100.0, "sell"),
            (OrderSide.SELL, 101.0, "sell"),
            (OrderSide.BUY, 100.0, "buy"),
            (OrderSide.BUY, 99.0, "buy"),
        ],
    )
    def test_wrong_side_trigger_raises_input_exception(self, sim, b, side, trigger, word):
        updates: list[dict] = []
        b.on_order_update(updates.append)
        sim.on_price("INFY", "NSE", 100.0)
        with pytest.raises(kiteconnect.exceptions.InputException, match=f"stoploss {word}"):
            b.place_order(req(side, OrderType.SL, trigger=trigger))
        assert sim.orders() == [] and updates == []

    def test_missing_trigger_raises(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        with pytest.raises(kiteconnect.exceptions.InputException, match="trigger_price"):
            b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=0.0))

    def test_no_price_known_yet_is_accepted(self, b):
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        assert _status(b, oid) == OrderStatus.PENDING

    def test_cancel_keeps_rejected_status(self):
        """An RMS-rejected order (here: margin) can't be cancelled; it stays REJECTED."""
        sim = make_sim(cash=100.0)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        assert _status(b, oid) == OrderStatus.REJECTED
        with pytest.raises(kiteconnect.exceptions.InputException):
            b.cancel_order(oid)
        assert _status(b, oid) == OrderStatus.REJECTED

    def test_modify_to_wrong_side_is_refused_and_keeps_the_old_trigger(self, sim, b):
        """#71: a trail that would put the trigger through the market is refused."""
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL, trigger=98.0))
        with pytest.raises(kiteconnect.exceptions.InputException, match="stoploss sell"):
            b.modify_order(oid, {"trigger_price": 100.5})
        (o,) = sim.orders()
        assert o["trigger_price"] == 98.0


class TestStopLimit:
    def test_triggers_then_fills_when_limit_reachable(self, sim, b):
        updates: list[dict] = []
        b.on_order_update(updates.append)
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL_LIMIT, trigger=98.0, price=97.5))
        sim.on_price("INFY", "NSE", 97.0)  # triggers; 97.0 < limit 97.5: no fill
        assert _status(b, oid) == OrderStatus.TRIGGERED
        assert updates[-1]["status"] == "OPEN"
        assert map_kite_status("OPEN", updates[-1]["order_type"], 0, 10) == OrderStatus.TRIGGERED
        sim.on_price("INFY", "NSE", 97.6)
        assert _status(b, oid) == OrderStatus.FILLED
        assert _fill(b, oid) == 97.5

    def test_buy_breakout_fills_at_the_market_once_triggered(self, sim, b):
        """SL-LIMIT BUY entry: trigger 101, limit 101.5, tick 101.2 -> fills at 101.2
        (Kite fills a marketable limit at the market; the pre-#86 simulator used the limit)."""
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.BUY, OrderType.SL_LIMIT, trigger=101.0, price=101.5))
        sim.on_price("INFY", "NSE", 101.2)
        assert _fill(b, oid) == 101.2

    def test_triggered_order_can_be_cancelled(self, sim, b):
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.SELL, OrderType.SL_LIMIT, trigger=98.0, price=97.5))
        sim.on_price("INFY", "NSE", 97.0)
        b.cancel_order(oid)
        assert _status(b, oid) == OrderStatus.CANCELLED


class TestUpdatePayload:
    def test_postback_is_kite_shaped_and_a_copy(self, sim, b):
        got: list[dict] = []
        b.on_order_update(got.append)
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(OrderSide.BUY, OrderType.MARKET))
        assert got[-1]["order_id"] == oid
        assert check_shape("order_postback", got[-1]) == []
        got[-1]["status"] = "tampered"
        assert _status(b, oid) == OrderStatus.FILLED
