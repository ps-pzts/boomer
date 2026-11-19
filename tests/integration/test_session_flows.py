"""Session machine x Trade machine x Order machine on the hostile broker (#88).

A whole trading day on a fake clock through the real SessionController. Covers the
Session transitions no other test fires and the hand-offs between machines: halt
while a trade is open, restart mid-exit, emergency stop with strays, and the
reconcile safety net for the ghost orders in #97.
"""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor import kite_checks as kc
from executor import session_machine as sm
from executor.models import OrderSide
from tests.executor.test_kite_checks import report
from tests.executor.test_session_controller import DAY, Day
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile


def make_day(tmp_path, **kw) -> Day:
    return Day(tmp_path, harness_cls=Hostile, **kw)


@pytest.fixture()
def d(tmp_path) -> Day:
    return make_day(tmp_path)


def fail_check(d: Day, kind: str = "preflight") -> None:
    kc.record(d.h.db, DAY, report(kind, False), source="scheduled")


def clean(d: Day) -> None:
    d.h.check()


class TestSessionTransitionsNotFiredElsewhere:
    def test_checks_failed_from_connected(self, tmp_path):
        d = make_day(tmp_path, kite_checks_passed=False)
        assert d.at(9, 0) == sm.CONNECTED
        fail_check(d)
        assert d.at(9, 1) == sm.HALTED
        assert d.row()["halt_reason"] == "preflight_failed"

    def test_checks_failed_from_reconciling(self, d):
        d.at(9, 0)
        d.at(9, 1)
        d.ctl.on_restart(DAY)
        assert d.state() == sm.RECONCILING
        kc.record(d.h.db, DAY, report("canary", False), source="scheduled")
        assert d.at(9, 5) == sm.HALTED
        assert d.row()["halt_reason"] == "canary_failed"

    def test_restarted_from_connected(self, d):
        assert d.at(9, 0) == sm.CONNECTED
        d.ctl.on_restart(DAY)
        assert d.state() == sm.RECONCILING

    def test_entry_cutoff_while_reconciling(self, d):
        d.at(9, 0)
        d.at(9, 1)
        d.ctl.on_restart(DAY)
        assert d.at(9, 5) == sm.RECONCILING  # clean but the market is not open yet
        assert d.at(14, 30) == sm.NO_NEW_ENTRIES

    def test_square_off_while_reconciling(self, d):
        d.at(9, 0)
        d.at(9, 1)
        d.ctl.on_restart(DAY)
        assert d.at(9, 5) == sm.RECONCILING  # clean, market not open yet
        assert d.at(15, 14) == sm.CLOSED  # flat: squares off and closes in one tick
        assert "square_off" in d.transitions()

    def test_emergency_square_off_from_ready(self, tmp_path):
        d = make_day(tmp_path, kite_checks_passed=False)  # gate holds the session in READY
        d.at(9, 0)
        d.at(9, 1)
        assert d.at(9, 20) == sm.READY
        d.broker_trade(OrderSide.BUY)  # a manual position: the day cannot be abandoned
        d.bot_mode = "emergency_stop"
        assert d.at(9, 21) == sm.CLOSED  # flattened and closed in one tick
        assert "square_off" in d.transitions()
        assert d.h.paper.list_positions() == []

    def test_emergency_square_off_from_connected(self, d):
        d.watchlist = 0  # no watchlist: stays CONNECTED
        d.at(9, 0)
        assert d.at(9, 20) == sm.CONNECTED
        d.broker_trade(OrderSide.BUY)
        d.bot_mode = "emergency_stop"
        assert d.at(9, 21) == sm.CLOSED  # flattened and closed in one tick
        assert "square_off" in d.transitions()
        assert d.h.paper.list_positions() == []

    def test_square_off_while_halted(self, d):
        d.trading()
        d.bot_mode = "paused"
        d.at(10, 0)
        assert d.state() == sm.HALTED
        d.bot_mode = "paused"
        assert d.at(15, 14) == sm.CLOSED  # halted, flat: squares off and closes in one tick
        assert "square_off" in d.transitions()


class TestHandOffsUnderFaults:
    def test_halt_with_an_open_trade_keeps_managing_it(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        assert d.h.bracket(bid)["state"] == "open"
        d.bot_mode = "paused"
        assert d.at(10, 1) == sm.HALTED
        d.at(10, 2, ltp=104.2)  # target fills during the halt
        d.at(10, 3, ltp=104.2)
        assert d.h.bracket(bid)["state"] == "closed"
        clean(d)

    def test_no_entries_in_any_state_but_trading(self, d):
        seen: dict[str, bool] = {}
        for hh, mm in ((9, 0), (9, 1), (9, 15)):
            d.at(hh, mm)
            seen[d.state()] = d.ctl.entries_allowed(DAY)
        d.bot_mode = "paused"
        d.at(10, 0)
        seen[d.state()] = d.ctl.entries_allowed(DAY)
        d.bot_mode = "auto"
        d.at(14, 30)
        seen[d.state()] = d.ctl.entries_allowed(DAY)
        d.at(15, 14)
        seen[d.state()] = d.ctl.entries_allowed(DAY)
        assert {k for k, v in seen.items() if v} == {sm.TRADING}
        assert set(seen) >= {sm.CONNECTED, sm.READY, sm.TRADING, sm.HALTED, sm.NO_NEW_ENTRIES}

    def test_restart_in_the_middle_of_an_exit_recovers_through_reconcile(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.h.tick(100.0)
        d.h.tick(97.0)  # stop cannot be placed through the market: exits at market
        d.h.sup.reconcile()
        assert d.h.bracket(bid)["state"] == "exiting"
        d.ctl.on_restart(DAY)
        for hh, mm in ((10, 0), (10, 1), (10, 2)):
            d.at(hh, mm, ltp=97.0)
        assert d.h.bracket(bid)["state"] == "closed"
        assert d.h.paper.list_positions() == []

    def test_a_ghost_stop_is_caught_by_the_session_reconcile(self, d):
        """#97's safety net: the order is untracked, so resuming after a halt must fail
        its reconcile instead of trading on top of it."""
        d.trading()
        d.h.tick(100.0)
        d.h.plan.fail(
            "place_order",
            kex.NetworkException("timed out"),
            after_effect=True,
            match=lambda p: p["order_type"] == "SL-M",
        )
        d.h.sup.open_trade(_req())
        d.at(10, 0)  # entry fills, stop answer lost, exit at market
        d.at(10, 1)
        d.bot_mode = "paused"
        d.at(10, 2)
        d.bot_mode = "auto"
        assert d.at(10, 3) == sm.HALTED
        assert d.row()["halt_reason"] == "reconcile_mismatch"

    def test_emergency_cancels_a_ghost_order_and_flattens(self, d):
        d.trading()
        d.h.tick(100.0)
        d.h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        with pytest.raises(kex.NetworkException):
            d.h.sup.open_trade(_req(entry=100.0))
        d.bot_mode = "emergency_stop"
        d.at(10, 0)
        d.at(10, 1)
        assert d.state() == sm.CLOSED
        assert d.h.paper.list_open_orders() == []
        d.h.check(end_of_day=True)

    def test_stale_feed_halts_then_a_clean_reconcile_resumes_with_the_stop_intact(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        assert d.at(10, 5, fresh=False) == sm.HALTED
        assert d.at(10, 6) == sm.TRADING
        assert d.h.bracket(bid)["state"] == "open"
        d.h.check()


class TestFullDay:
    def test_a_day_with_two_trades_ends_flat_and_closed(self, d):
        d.trading()
        win = d.h.sup.open_trade(_req())
        d.at(10, 0)
        d.at(10, 30, ltp=104.2)  # target
        d.at(10, 31, ltp=104.2)
        loss = d.h.sup.open_trade(_req(entry=100.0))
        d.at(11, 0, ltp=100.0)
        d.at(11, 30, ltp=97.5)  # stop gapped
        d.at(11, 31, ltp=97.5)
        d.at(14, 30)
        assert d.at(15, 14, ltp=100.0) in (sm.SQUARING_OFF, sm.CLOSED)
        assert d.at(15, 15, ltp=100.0) == sm.CLOSED
        assert (d.h.bracket(win)["exit_reason"], d.h.bracket(loss)["exit_reason"]) == (
            "target_hit",
            "stop_hit",
        )
        d.h.check(end_of_day=True)
