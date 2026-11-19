"""The decision cycle against the hostile broker (#88): at most 3 open trades and
entries only while the session is TRADING, even when entries are refused or the
session halts. Reuses the cycle harness of tests/executor/test_decision_cycle.py."""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor import session_machine as sm
from executor.decision_cycle import MAX_OPEN_TRADES
from tests.executor.test_decision_cycle import Cycle, _trading
from tests.executor.test_session_controller import DAY, Day
from tests.integration.harness import Hostile


@pytest.fixture()
def c(tmp_path) -> Cycle:
    d = Day(tmp_path, harness_cls=Hostile)
    d.nifty = 24100.0
    return Cycle(d)


def active(c: Cycle) -> int:
    return c.d.h.sup.active_count()


def test_a_refused_entry_does_not_let_the_cycle_exceed_the_cap(c):
    _trading(c)
    c.d.h.plan.fail("place_order", kex.InputException("RMS: refused"), times=1)
    c.run()
    assert active(c) <= MAX_OPEN_TRADES
    for minute in (46, 47, 48):  # later cycles top it up, never past the cap
        c.d.at(9, minute)
        c.run()
        assert active(c) <= MAX_OPEN_TRADES
    assert active(c) == MAX_OPEN_TRADES
    c.d.h.check()


def test_a_full_book_stays_full_through_outages_and_restarts(c):
    _trading(c)
    c.run()
    assert active(c) == MAX_OPEN_TRADES
    c.d.h.plan.fail("orders", kex.NetworkException("down"), times=2)
    c.d.at(9, 50)
    c.d.ctl.on_restart(DAY)
    c.d.at(9, 51)
    assert c.run().skipped in ("max_open_trades", "session_reconciling")
    assert active(c) <= MAX_OPEN_TRADES


def test_no_entries_while_the_session_is_halted(c):
    _trading(c)
    c.d.bot_mode = "paused"
    c.d.at(9, 50)
    assert c.d.state() == sm.HALTED
    assert c.run().skipped == "session_halted"
    assert active(c) == 0
