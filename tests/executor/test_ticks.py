"""#75: order prices on the instrument's own tick, rounded the safe way."""

from __future__ import annotations

import pytest

from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kite_instruments import InstrumentCache
from executor.brokers.paper_broker import PaperBroker
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
    """Paper broker that uses a 0.10 tick for every instrument, like a Kite dump would."""
    h = Harness(tmp_path)
    h.paper = PaperBroker(price_source=h.feed, initial_cash=1_000_000, tick_size=lambda s, e: 0.1)
    h.om = OrderManager(h.db, {BrokerName.KITE: h.paper}, ltp_cache={}, clock=h.clock)
    return h


class TestEndToEnd:
    def test_paper_refuses_off_tick_like_kite(self, h):
        h.tick(100.0)
        with pytest.raises(ValueError, match="not a multiple of tick"):
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
