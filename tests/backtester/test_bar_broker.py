"""The backtester on KiteBroker + KiteSim (#86): what changed from the pre-#86
simulator, each with a hand-checkable example.

Bars below: flat days are o=100 h=102 l=98 c=100. Decision: BUY CNC, stop 90,
target 120, MARKET (entry 0) unless stated; 100,000 capital, max_position 5%
-> 50 shares at 100 (50 x 100 = 5,000 <= 98% of the cash).
"""

from __future__ import annotations

from datetime import date

import pytest

from backtester.bar_broker import BarBroker
from backtester.models import EntryDecision
from backtester.simulation import BacktestSimulation
from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, PriceBar, ProductType
from tests.backtester.sim_helpers import (
    _bar,
    _empty_feature_loader,
    _flat_price_loader,
    _make_config,
    _make_db,
)


def _decider(entry=0.0, sl=90.0, target=120.0):
    def decide(symbol, d, features, bar):
        return EntryDecision(entry, sl, target, ProductType.CNC, "intraday", 0.8, "s")

    return decide


def _loader(special: dict[int, tuple]):
    count: dict[str, int] = {}

    def loader(symbol, a, b):
        count[symbol] = count.get(symbol, 0) + 1
        o, h, lo, c = special.get(count[symbol], (100, 102, 98, 100))
        return [_bar(symbol, a, o=o, h=h, lo=lo, c=c)]

    return loader


def _run(loader, decider) -> BacktestSimulation:
    sim = BacktestSimulation(
        db=_make_db(),
        config=_make_config(max_open_positions=1),
        price_loader=loader,
        feature_loader=_empty_feature_loader,
        universe=["RELIANCE"],
        entry_decider=decider,
    )
    sim.run()
    return sim


class TestBarBroker:
    def _req(self, kind=OrderType.MARKET, price=0.0):
        return OrderRequest("X", "NSE", OrderSide.BUY, kind, 10, ProductType.CNC, price)

    def test_market_entry_fills_at_the_open(self):
        bb = BarBroker(cash=1e6)
        st = bb.enter(self._req(), PriceBar("X", "d", 100, 105, 95, 103, 1))
        assert (st["status"], st["average_fill_price"]) == (OrderStatus.FILLED, 100.0)

    def test_limit_below_the_open_fills_at_its_limit_when_reached(self):
        bb = BarBroker(cash=1e6)
        st = bb.enter(self._req(OrderType.LIMIT, 97.0), PriceBar("X", "d", 100, 105, 95, 103, 1))
        assert st["average_fill_price"] == 97.0

    def test_limit_above_the_open_fills_at_the_open(self):
        """The pre-#86 simulator filled at the limit (101): overstated the cost."""
        bb = BarBroker(cash=1e6)
        st = bb.enter(self._req(OrderType.LIMIT, 101.0), PriceBar("X", "d", 100, 102, 98, 100, 1))
        assert st["average_fill_price"] == 100.0

    def test_unfilled_entry_expires_at_the_close(self):
        bb = BarBroker(cash=1e6)
        st = bb.enter(self._req(OrderType.LIMIT, 90.0), PriceBar("X", "d", 100, 102, 98, 100, 1))
        assert st["status"] == OrderStatus.PENDING
        bb.end_day()
        assert {o["status"] for o in bb.sim.orders()} == {"CANCELLED"}

    def test_triggered_gtts_name_the_fired_leg(self):
        from executor.models import GttRequest, GttType

        bb = BarBroker(cash=1e6)
        bb.start_day(date(2024, 1, 2))
        bb.walk(PriceBar("X", "d", 100, 102, 98, 100, 1))
        gid = bb.place_gtt(GttRequest("X", "NSE", GttType.OCO, 10, sl_trigger_price=90.0,
                                      sl_limit_price=89.1, target_trigger_price=120.0,
                                      target_limit_price=120.0))  # fmt: skip
        bb.walk(PriceBar("X", "d", 100, 101, 88, 95, 1))
        assert bb.triggered_gtts() == [(gid, 0, 89.1)]


class TestSimulationChanges:
    def test_both_legs_in_one_bar_fire_the_one_nearer_the_open(self):
        """Day 3 bar 118/125/85/100: the walk reaches 120 (target) before 90 (stop).
        Pre-#86 always took the stop: -558.47; now the target: +988.42."""
        sim = _run(_loader({3: (118, 125, 85, 100)}), _decider())
        assert [(t.exit_reason, round(t.net_pnl, 2)) for t in sim._trades] == [("target", 988.42)]

    def test_entry_whose_stop_is_already_breached_is_closed_at_once(self):
        """Day 3 bar 92/93/85/88: the stop exits day-1's position, then the decider
        re-enters at the 92 open; with the close (88) below the 90 stop Kite refuses
        the OCO, so the position is closed at the close (pre-#86: held unprotected)."""
        sim = _run(_loader({3: (92, 93, 85, 88)}), _decider())
        assert [t.exit_reason for t in sim._trades] == ["sl", "sl_breached"]
        assert sim._open_positions != {}  # re-entered on day 4 as normal

    def test_entry_whose_target_is_already_reached_is_closed_at_once(self):
        sim = _run(_loader({3: (118, 125, 117, 122)}), _decider())
        assert [t.exit_reason for t in sim._trades] == ["target", "target_breached"]

    def test_unfilled_limit_entry_never_leaks_cash(self):
        """Pre-#86 a limit entry left resting could fill on a later day and debit
        cash for a position the backtest never tracked."""
        sim = _run(_loader({1: (100, 102, 98, 100), 2: (100, 102, 94, 100)}), _decider(95.0))
        assert sim._trades == []
        (pos,) = sim._open_positions.values()
        assert pos["entry_date"] == date(2024, 1, 3)  # day 2's own order, day 1's expired
        assert sim._cash == pytest.approx(100_000 - pos["quantity"] * 95.0)

    def test_flat_market_still_flat(self):
        sim = _run(_flat_price_loader, _decider())
        assert sim._trades == [] and sim._cash == pytest.approx(100_000 - 50 * 100.0)
