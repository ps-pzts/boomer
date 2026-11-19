"""A real process restart on the hostile paper broker (#88): the KiteSim is rebuilt
from the paper book (#64), not kept in memory, so persistence is on the path."""

from __future__ import annotations

import pytest

from tests.integration.harness import Hostile, attach_book, process_restart
from tests.integration.test_scenarios import open_protected


@pytest.fixture()
def h(tmp_path) -> Hostile:
    h = Hostile(tmp_path)
    attach_book(h)
    return h


def test_open_trade_survives_a_restart_and_still_hits_its_target(h):
    bid = open_protected(h)
    process_restart(h)
    h.step()  # reconcile: stop still resting in the restored book, net matches
    assert h.bracket(bid)["state"] == "open"
    h.tick(104.2)
    h.step()
    h.step()
    b = h.bracket(bid)
    assert (b["state"], b["realised_pnl"]) == ("closed", 40.0)
    h.check(end_of_day=True)


def test_open_trade_survives_a_restart_and_still_stops_out(h):
    bid = open_protected(h)
    process_restart(h)
    h.tick(97.5)
    h.step()
    h.step()
    assert h.bracket(bid)["exit_reason"] == "stop_hit"
    h.check(end_of_day=True)


def test_restart_mid_exit_finishes_the_exit(h):
    h.tick(100.0)
    from tests.executor.test_trade_lifecycle import _req

    bid = h.sup.open_trade(_req())
    h.tick(97.0)  # stop can't be placed through the market: EXITING
    h.sup.reconcile()
    assert h.bracket(bid)["state"] == "exiting"
    process_restart(h)
    h.tick(97.0)
    for _ in range(3):
        h.step()
    assert h.bracket(bid)["state"] == "closed"
    h.check(end_of_day=True)


def test_resting_entry_survives_a_restart_and_fills_later(h):
    from tests.executor.test_trade_lifecycle import _req

    h.tick(100.5)
    bid = h.sup.open_trade(_req(entry=100.0))
    process_restart(h)
    h.tick(99.9)
    h.step()
    assert h.bracket(bid)["state"] == "open"
