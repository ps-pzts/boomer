"""KiteSim's placement rules (#86): Kite's input errors raise; RMS rejects show REJECTED.

Worked margin example (MIS rate 20%): BUY 100 INFY @ 1500 = 150,000 value ->
30,000 margin; with 20,000 cash it is REJECTED with Kite's message.
"""

from __future__ import annotations

from datetime import datetime, time

import kiteconnect
import pytest

from executor.brokers.kitesim import rules
from executor.brokers.kitesim.config import bar_path
from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from tests.kitesim_kit import IST, Clock, make_sim, paper_broker

InputException = kiteconnect.exceptions.InputException


def req(qty=10, kind=OrderType.LIMIT, price=1500.0, trigger=0.0, side=OrderSide.BUY,
        product=ProductType.MIS, sym="INFY") -> OrderRequest:  # fmt: skip
    return OrderRequest(sym, "NSE", side, kind, qty, product, price, trigger)


def _rejected(broker, oid) -> str:
    st = broker.get_order_status(oid)
    assert st["status"] == OrderStatus.REJECTED
    return st["rejection_reason"]


class TestInputErrors:
    def test_price_off_the_instrument_tick(self):
        sim = make_sim()
        b = paper_broker(sim)
        with pytest.raises(InputException, match="Tick size for this script is 0.10"):
            b.place_order(req(price=1500.05))  # INFY tick 0.10 (fixture dump)
        assert sim.orders() == []

    def test_unknown_instrument(self):
        from executor.brokers.kitesim import KiteSim, KiteSimConfig
        from tests.kitesim_kit import fixture_instruments

        sim = KiteSim(fixture_instruments(synthesize_unknown=False), 1e6, Clock(), KiteSimConfig())
        with pytest.raises(InputException, match="does not exist"):
            paper_broker(sim).place_order(req(sym="NOPE"))

    def test_market_without_protection(self):
        sim = make_sim()
        with pytest.raises(InputException, match="market protection"):
            sim.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "MARKET")

    def test_tag_must_be_alphanumeric(self):
        sim = make_sim()
        with pytest.raises(InputException, match="tag"):
            sim.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1500.0,
                            tag="has-hyphen")  # fmt: skip

    def test_sl_limit_on_the_wrong_side_of_its_trigger(self):
        sim = make_sim()
        sim.on_price("INFY", "NSE", 1500.0)
        with pytest.raises(InputException, match="limit price"):
            paper_broker(sim).place_order(
                req(kind=OrderType.SL_LIMIT, side=OrderSide.BUY, trigger=1510.0, price=1505.0)
            )

    def test_unsupported_variety(self):
        with pytest.raises(InputException, match="Variety"):
            make_sim().place_order("amo", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1500.0)


class TestRmsRejections:
    def test_insufficient_margin(self):
        sim = make_sim(cash=20_000)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)
        oid = b.place_order(req(qty=100))
        reason = _rejected(b, oid)
        assert reason.startswith("Insufficient funds. Required margin is 30000.00")
        assert "available margin is 20000.00" in reason
        (o,) = sim.orders()
        assert o["status_message_raw"].startswith("RMS:Margin Exceeds")

    def test_exit_needs_no_margin(self):
        sim = make_sim(cash=4_000)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        b.place_order(req(qty=100, price=100.0))  # 10,000 value -> 2,000 margin
        oid = b.place_order(req(qty=100, kind=OrderType.SL, side=OrderSide.SELL, trigger=98.0,
                                price=0.0))  # fmt: skip
        assert b.get_order_status(oid)["status"] == OrderStatus.PENDING

    def test_working_orders_block_margin(self):
        sim = make_sim(cash=40_000)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)
        first = b.place_order(req(qty=100, price=1400.0))  # rests; blocks 28,000
        assert b.get_order_status(first)["status"] == OrderStatus.PENDING
        second = b.place_order(req(qty=100, price=1400.0))
        assert "Insufficient funds" in _rejected(b, second)
        b.cancel_order(first)  # releases the block
        third = b.place_order(req(qty=100, price=1400.0))
        assert b.get_order_status(third)["status"] == OrderStatus.PENDING

    def test_price_outside_the_circuit_band(self):
        sim = make_sim(circuit_pct=0.10)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)  # first price of the day: the reference
        reason = _rejected(b, b.place_order(req(price=1340.0)))
        assert reason.startswith("Your order price is lower than the current lower circuit")
        assert "1350.00" in reason
        assert "upper circuit" in _rejected(b, b.place_order(req(price=1660.0)))

    def test_freeze_quantity(self):
        sim = make_sim(freeze_quantity=500)
        b = paper_broker(sim)
        assert "freeze" in _rejected(b, b.place_order(req(qty=501)))

    def test_new_mis_after_the_cutoff_but_exits_still_allowed(self):
        clock = Clock(datetime(2026, 11, 19, 15, 19, 59, tzinfo=IST))
        sim = make_sim(clock=clock)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)
        b.place_order(req(qty=10))  # filled before the cutoff
        clock.now = datetime(2026, 11, 19, 15, 20, tzinfo=IST)
        assert "after 15:20" in _rejected(b, b.place_order(req(qty=10)))
        exit_id = b.place_order(req(qty=10, side=OrderSide.SELL, price=1500.0))
        assert b.get_order_status(exit_id)["status"] == OrderStatus.FILLED

    def test_cutoff_is_configurable(self):
        clock = Clock(datetime(2026, 11, 19, 15, 0, tzinfo=IST))
        b = paper_broker(make_sim(clock=clock, mis_cutoff=time(14, 30)))
        assert "after 14:30" in _rejected(b, b.place_order(req()))

    def test_rejection_postback_reaches_listeners(self):
        sim = make_sim(cash=1.0)
        b = paper_broker(sim)
        got: list[dict] = []
        b.on_order_update(got.append)
        b.place_order(req())
        assert [u["status"] for u in got] == ["REJECTED"]


class TestPureRules:
    @pytest.mark.parametrize(
        ("side", "trig", "p", "continuous", "fill"),
        [
            ("SELL", 98.0, 97.0, True, 98.0),
            ("SELL", 98.0, 97.0, False, 97.0),
            ("SELL", 98.0, 98.0, False, 98.0),
            ("BUY", 102.0, 103.0, False, 103.0),
            ("BUY", 102.0, 103.0, True, 102.0),
        ],
    )
    def test_stop_fill_price(self, side, trig, p, continuous, fill):
        assert rules.stop_crossed(side, trig, p)
        assert rules.stop_fill_price(side, trig, p, continuous) == fill

    def test_on_tick(self):
        assert rules.on_tick(100.1, 0.1) and not rules.on_tick(100.05, 0.1)
        assert rules.on_tick(0.0, 0.1)

    @pytest.mark.parametrize(
        ("bar", "path"),
        [
            ((100, 102, 97, 99), [100, 102, 97, 99]),  # high nearer the open
            ((100, 105, 99, 101), [100, 99, 105, 101]),  # low nearer the open
            ((100, 102, 98, 100), [100, 98, 102, 100]),  # tie: low first
        ],
    )
    def test_bar_path(self, bar, path):
        assert bar_path(*bar) == path
