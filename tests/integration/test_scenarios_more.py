"""More cross-machine scenarios (#88): partial fills, trailing under MODIFY PENDING,
restarts mid-trade, and failing exits. Worked numbers as test_trade_lifecycle:
qty 10, entry 100, stop 98, target 104, ATR 2 (R = 2)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from executor.models import BrokerName
from executor.order_manager import OrderManager
from executor.position_manager import PositionManager
from executor.trade_lifecycle import TradeSupervisor
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile
from tests.integration.test_scenarios import open_protected


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def partial_fill(h: Hostile, bid: str, qty: int, price: float = 100.0) -> None:
    oid = h.bracket(bid)["entry_order_id"]
    broker_id = h.om.sync_status(oid).broker_order_id
    o = next(x for x in h.sim.orders() if x["order_id"] == broker_id)
    h.sim.advance(
        broker_id,
        "OPEN",
        filled_quantity=qty,
        pending_quantity=o["quantity"] - qty,
        average_price=price,
    )


def restarted(h: Hostile) -> TradeSupervisor:
    """A new process: fresh OrderManager and supervisor on the same database."""
    om = OrderManager(h.db, {BrokerName.KITE: h.paper}, ltp_cache={}, clock=h.clock)
    pm = PositionManager(h.db, gtt_manager=None, order_manager=om)
    return TradeSupervisor(h.db, om, pm, h.alerter, clock=h.clock)


class TestPartialEntry:
    @pytest.mark.xfail(strict=True, reason="#99: partial entry fill unprotected until expiry")
    def test_part_filled_entry_is_not_left_without_a_stop_for_long(self, h):
        """4 of 10 filled and the rest resting: those 4 shares exist at the broker."""
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        partial_fill(h, bid, 4)
        h.step()
        b = h.bracket(bid)
        assert b["state"] == "open", f"4 shares held, trade is {b['state']} with no stop"

    def test_remainder_filling_after_expiry_check_is_still_covered(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        partial_fill(h, bid, 4)
        h.clock.now += timedelta(minutes=31)
        h.step()  # expiry: cancel the rest, protect the 4
        h.step()
        assert h.bracket(bid)["state"] == "open"
        assert h.om.sync_status(h.bracket(bid)["stop_order_id"]).quantity == 4
        h.tick(97.5)
        h.step()
        h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)


class TestTrailingUnderFaults:
    def _trailing(self, h: Hostile) -> None:
        om = h.om
        h.sup = TradeSupervisor(h.db, om, h.pm, h.alerter, clock=h.clock, price=lambda s: self._ltp)

    def test_trail_modify_stuck_pending_never_lowers_or_loses_the_stop(self, h):
        self._ltp = 100.0
        self._trailing(h)
        bid = open_protected(h)
        h.plan.modify_pending(times=1)  # the first trail modify sticks; later ones are refused
        for px in (102.0, 103.5, 103.0):  # breakeven at +1R, then trail, then a dip
            self._ltp = px
            h.tick(px)
            h.step()
        h.plan.settle()
        h.step()
        assert h.bracket(bid)["state"] == "open"
        stops = [
            o for o in h.sim.orders() if o["order_type"] == "SL-M" and o["status"] != "CANCELLED"
        ]
        assert len(stops) == 1 and stops[0]["trigger_price"] >= 98.0
        h.tick(90.0)
        h.step()
        h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)


class TestRestarts:
    def test_restart_while_entry_pending_picks_up_a_fill_during_downtime(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.tick(99.9)  # fills while we are "down"
        h.sup = restarted(h)
        h.step()
        assert h.bracket(bid)["state"] == "open"

    def test_restart_in_the_middle_of_an_exit_finishes_it(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)  # stop can't be placed: trade goes EXITING
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "exiting"
        h.sup = restarted(h)
        h.step()
        h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)

    def test_restart_with_a_resting_stop_keeps_protecting(self, h):
        bid = open_protected(h)
        h.sup = restarted(h)
        h.step()
        h.tick(97.5)
        h.step()
        h.step()
        assert h.bracket(bid)["exit_reason"] == "stop_hit"
        h.check(end_of_day=True)
