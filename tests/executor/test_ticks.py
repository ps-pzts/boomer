"""#75: order prices on the instrument's own tick, rounded the safe way."""

from __future__ import annotations

import kiteconnect
import pytest

from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kite_instruments import InstrumentCache
from executor.brokers.kitesim.paper import build_paper_broker
from executor.models import BrokerName, OrderRequest, OrderSide, OrderType, ProductType
from executor.order_manager import OrderManager
from executor.ticks import is_on_tick, on_tick, round_to_tick
from tests import kite_fixtures as kf
from tests.executor.test_trade_lifecycle import Harness
from tests.kite_sdk import respond


def _req(side, kind, price=0.0, trigger=0.0):
    return OrderRequest("INFY", "NSE", side, kind, 10, ProductType.MIS, price, trigger)


class TestRounding:
    @pytest.mark.parametrize(
        "price, tick, up, expected",
        [
            (1008.05, 0.10, False, 1008.0),
            (978.05, 0.10, True, 978.1),
            (1053.05, 0.10, True, 1053.1),
            (101.5, 0.05, True, 101.5),  # already on tick: unchanged
            (2450.3, 1.0, True, 2451.0),
            (99.99, 0.01, False, 99.99),
        ],
    )
    def test_round_to_tick(self, price, tick, up, expected):
        assert round_to_tick(price, tick, up) == expected

    def test_worked_example_from_the_module_doc(self):
        entry = on_tick(_req(OrderSide.BUY, OrderType.LIMIT, price=1008.05), 0.10)
        stop = on_tick(_req(OrderSide.SELL, OrderType.SL, trigger=978.05), 0.10)
        target = on_tick(_req(OrderSide.SELL, OrderType.LIMIT, price=1053.05), 0.10)
        assert (entry.price, stop.trigger_price, target.price) == (1008.0, 978.1, 1053.1)

    def test_sell_stop_never_widens(self):
        """Rounding a protective SELL stop only ever moves it UP (tighter)."""
        for trigger in (97.01, 97.04, 97.09):
            assert (
                on_tick(_req(OrderSide.SELL, OrderType.SL, trigger=trigger), 0.05).trigger_price
                >= trigger
            )

    def test_buy_stop_limit_keeps_limit_at_or_above_trigger(self):
        r = on_tick(_req(OrderSide.BUY, OrderType.SL_LIMIT, price=100.04, trigger=100.01), 0.05)
        assert (r.trigger_price, r.price) == (100.05, 100.05)

    def test_market_orders_untouched(self):
        r = on_tick(_req(OrderSide.SELL, OrderType.MARKET), 0.10)
        assert (r.price, r.trigger_price) == (0.0, 0.0)

    def test_is_on_tick(self):
        assert is_on_tick(100.1, 0.1) and not is_on_tick(100.05, 0.1) and is_on_tick(0.0, 0.1)


class TestEdges:
    """Mutation-testing survivors (#89): the boundaries the examples above never touch."""

    @pytest.mark.parametrize(
        "price, tick", [(0.0, 0.05), (-1.0, 0.05), (100.03, 0.0), (100.03, -0.05)]
    )
    def test_unusable_price_or_tick_is_returned_unchanged(self, price, tick):
        assert round_to_tick(price, tick, up=True) == price
        assert round_to_tick(price, tick, up=False) == price

    def test_result_is_a_float_on_the_tick(self):
        r = round_to_tick(100.03, 0.05, up=True)
        assert isinstance(r, float) and r == 100.05
        assert round_to_tick(100.03, 0.05, up=False) == 100.0

    def test_a_limit_with_no_price_stays_zero_and_is_not_rounded_up_to_a_tick(self):
        r = on_tick(_req(OrderSide.BUY, OrderType.LIMIT, price=0.0), 0.10)
        assert r.price == 0.0

    def test_only_limit_types_have_their_price_rounded(self):
        r = on_tick(_req(OrderSide.BUY, OrderType.SL, price=100.04, trigger=100.04), 0.05)
        assert (r.price, r.trigger_price) == (100.04, 100.05)  # SL-M ignores `price`

    def test_trigger_zero_means_no_trigger(self):
        assert on_tick(_req(OrderSide.BUY, OrderType.LIMIT, price=100.0), 0.05).trigger_price == 0.0

    def test_a_buy_stop_limit_below_its_trigger_is_lifted_but_equal_is_left(self):
        lifted = on_tick(_req(OrderSide.BUY, OrderType.SL_LIMIT, price=99.0, trigger=100.0), 0.05)
        equal = on_tick(_req(OrderSide.BUY, OrderType.SL_LIMIT, price=100.0, trigger=100.0), 0.05)
        above = on_tick(_req(OrderSide.BUY, OrderType.SL_LIMIT, price=101.0, trigger=100.0), 0.05)
        assert (lifted.price, equal.price, above.price) == (100.0, 100.0, 101.0)

    def test_a_sell_stop_limit_is_never_lifted_to_its_trigger(self):
        r = on_tick(_req(OrderSide.SELL, OrderType.SL_LIMIT, price=97.0, trigger=98.0), 0.05)
        assert (r.price, r.trigger_price) == (97.0, 98.0)

    def test_sell_limit_rounds_up_and_buy_limit_rounds_down(self):
        assert on_tick(_req(OrderSide.SELL, OrderType.LIMIT, price=100.03), 0.05).price == 100.05
        assert on_tick(_req(OrderSide.BUY, OrderType.LIMIT, price=100.03), 0.05).price == 100.0

    @pytest.mark.parametrize(
        "price, tick", [(0.0, 0.05), (-5.0, 0.05), (100.03, 0.0), (100.03, -1.0)]
    )
    def test_is_on_tick_is_true_when_there_is_nothing_to_check(self, price, tick):
        assert is_on_tick(price, tick) is True

    def test_is_on_tick_distinguishes_on_and_off(self):
        assert is_on_tick(100.05, 0.05) is True
        assert is_on_tick(100.04, 0.05) is False


class TestSubRupeePrices:
    """Penny stocks (tick 0.01) and whole-rupee ticks: nothing special-cases 0 vs 1."""

    def test_prices_and_ticks_below_one_are_still_rounded(self):
        assert round_to_tick(0.53, 0.05, up=True) == 0.55
        assert round_to_tick(0.53, 0.05, up=False) == 0.5
        assert round_to_tick(100.3, 0.5, up=True) == 100.5
        assert round_to_tick(100.3, 1.0, up=False) == 100.0

    def test_limit_and_trigger_below_one_are_rounded(self):
        buy = on_tick(_req(OrderSide.BUY, OrderType.LIMIT, price=0.53), 0.05)
        sell_stop = on_tick(_req(OrderSide.SELL, OrderType.SL, trigger=0.53), 0.05)
        assert (buy.price, sell_stop.trigger_price) == (0.5, 0.55)

    def test_is_on_tick_below_one(self):
        assert is_on_tick(0.53, 0.05) is False and is_on_tick(0.55, 0.05) is True
        assert is_on_tick(100.5, 1.0) is False and is_on_tick(100.0, 1.0) is True


class TestInstrumentTicks:
    def test_cache_reads_tick_from_the_dump(self):
        rows = [
            {"tradingsymbol": "INFY", "instrument_token": 1, "tick_size": 0.1},
            {"tradingsymbol": "ITC", "instrument_token": 2, "tick_size": 0.05},
        ]
        cache = InstrumentCache(lambda ex: rows)
        assert (cache.tick_size("INFY", "NSE"), cache.tick_size("ITC", "NSE")) == (0.1, 0.05)
        assert cache.tick_size("NOPE", "NSE") is None

    def test_kite_broker_reads_the_dump_and_falls_back_to_005(self, kite_sdk):
        b = KiteBroker()
        b._kite = kite_sdk
        respond(kite_sdk, "instruments", kf.sdk("instruments"))  # INFY 0.1, IDEA 0.01
        ticks = [b.tick_size(s, "NSE") for s in ("INFY", "IDEA", "NOPE")]
        assert ticks == [0.1, 0.01, 0.05]


@pytest.fixture()
def h(tmp_path) -> Harness:
    """Paper broker on an instrument dump with a 0.10 tick for every instrument."""
    h = Harness(tmp_path)
    rows = [dict(r, tick_size=0.1) for r in kf.sdk("instruments")]
    h.paper = build_paper_broker(h.feed, 1_000_000, instruments=lambda ex: rows, clock=h.clock)
    h.om = OrderManager(h.db, {BrokerName.KITE: h.paper}, ltp_cache={}, clock=h.clock)
    return h


class TestEndToEnd:
    def test_paper_refuses_off_tick_like_kite(self, h):
        h.tick(100.0)
        with pytest.raises(kiteconnect.exceptions.InputException, match="Tick size"):
            h.paper.place_order(_req(OrderSide.BUY, OrderType.LIMIT, price=99.95))

    def test_order_manager_rounds_before_sending(self, h):
        h.tick(100.0)
        oid = h.om.submit(_req(OrderSide.BUY, OrderType.LIMIT, price=99.95), "intraday")
        rec = h.om.sync_status(oid)
        assert rec.price == 99.9  # BUY limit rounded down: never pay more

    def test_modify_trigger_rounds_up(self, h):
        h.tick(100.0)
        oid = h.om.submit(_req(OrderSide.SELL, OrderType.SL, trigger=98.0), "intraday")
        assert h.om.modify_trigger(oid, 98.95).trigger_price == 99.0
