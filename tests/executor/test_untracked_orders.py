"""#65: orders working at the broker that no trade tracks."""

from __future__ import annotations

import pytest

from executor import session_machine as sm
from executor.brokers.kite_broker import KiteBroker
from executor.models import OrderRequest, OrderSide, OrderType, ProductType
from executor.session_inputs import reconcile_report
from tests import kite_fixtures as kf
from tests.executor.test_session_controller import Day
from tests.executor.test_trade_lifecycle import _req
from tests.kite_sdk import respond


def _report(**kw):
    args = dict(trade_net={}, broker_net={}, open_legs=[], feed_fresh=True, watchlist_size=5)
    args.update(kw)
    return reconcile_report(**args)


class TestCheck:
    def test_clean_when_none(self):
        assert _report(untracked_orders=[]).clean

    def test_stray_order_fails(self):
        r = _report(untracked_orders=[{"broker_order_id": "9", "symbol": "TCS"}])
        assert r.failures() == ["no_untracked_resting_order"]

    def test_unavailable_fails_closed(self):
        assert _report(untracked_orders=None).failures() == ["no_untracked_resting_order"]


def test_kite_lists_only_working_orders(kite_sdk):
    """orders.json: one order in each lifecycle state (see its README)."""
    b = KiteBroker()
    b._kite = kite_sdk
    respond(kite_sdk, "orders", kf.sdk("orders"))
    got = b.list_open_orders()
    assert [o["status"] for o in got] == [
        "TRIGGER PENDING",
        "OPEN",
        "OPEN",
        "MODIFY PENDING",
        "CANCEL PENDING",
    ]
    stop = got[0]
    assert (stop["broker_order_id"], stop["side"], stop["symbol"], stop["order_type"]) == (
        "240611000123402",
        "sell",
        "INFY",
        "SL-M",
    )


@pytest.fixture()
def d(tmp_path) -> Day:
    return Day(tmp_path)


def _stray_stop(d: Day) -> str:
    """A SELL stop placed straight at the broker (e.g. by hand in the Kite app)."""
    return d.h.paper.place_order(
        OrderRequest(
            "TCS", "NSE", OrderSide.SELL, OrderType.SL, 5, ProductType.MIS, trigger_price=450.0
        )
    )


def test_our_own_working_orders_are_tracked(d):
    d.trading()
    d.h.sup.open_trade(_req())
    d.at(10, 0)  # entry filled; stop + target resting
    assert d.ctl.untracked_orders() == []


def test_stray_order_blocks_resume(d):
    d.trading()
    d.h.tick(500.0, sym="TCS")
    d.at(9, 16)
    d.at(9, 18, fresh=False)  # halted: feed_stale
    stray = _stray_stop(d)
    assert d.at(9, 20) == sm.HALTED
    assert d.row()["halt_reason"] == "reconcile_mismatch"
    assert [o["broker_order_id"] for o in d.ctl.untracked_orders()] == [stray]


def test_emergency_cancels_stray_orders(d):
    d.trading()
    d.h.tick(500.0, sym="TCS")
    stray = _stray_stop(d)
    d.bot_mode = "emergency_stop"
    assert d.at(10, 0) == sm.CLOSED
    assert d.h.paper.get_order_status(stray)["status"] == "cancelled"
    assert d.ctl.untracked_orders() == []
