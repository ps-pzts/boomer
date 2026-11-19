"""Named failure scenarios across the Order and Trade machines (#88).

Each runs the real supervisor on the paper KiteBroker + KiteSim with one fault
from FaultPlan, and checks the cross-machine invariants after every step.
Worked numbers as in test_trade_lifecycle: qty 10, entry 100, stop 98, target 104.
"""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def open_protected(h: Hostile) -> str:
    h.tick(100.0)
    bid = h.sup.open_trade(_req())
    h.step()
    assert h.bracket(bid)["state"] == "open"
    return bid


class TestPostbackFaults:
    def test_dropped_postbacks_are_covered_by_polling(self, h):
        h.wire_postbacks()
        h.plan.drop_postbacks()
        bid = open_protected(h)
        h.tick(104.2)
        h.step()
        b = h.bracket(bid)
        assert (b["state"], b["realised_pnl"]) == ("closed", 40.0)
        h.check(end_of_day=True)

    def test_duplicated_postbacks_do_not_double_count_fills(self, h):
        h.wire_postbacks()
        h.plan.duplicate_postbacks(3)
        bid = open_protected(h)
        h.tick(104.2)
        h.step()
        b = h.bracket(bid)
        assert (b["state"], b["realised_pnl"]) == ("closed", 40.0)
        fills = h.db.execute("SELECT COALESCE(SUM(quantity),0) FROM executions").fetchone()[0]
        assert fills == 20  # entry 10 + target 10, each once
        h.check(end_of_day=True)

    def test_reversed_postbacks_still_end_flat(self, h):
        h.wire_postbacks()
        h.plan.hold_postbacks()
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.plan.release_postbacks("reverse")
        h.step()
        h.plan.hold_postbacks()
        h.tick(97.5)  # gap through the stop
        h.plan.release_postbacks("reverse")
        h.step()
        h.step()
        assert h.bracket(bid)["exit_reason"] == "stop_hit"
        h.check(end_of_day=True)


class TestFillRaces:
    def test_entry_fills_while_it_is_being_expired(self, h):
        """The 30-minute expiry cancels the entry, but the exchange had just filled it."""
        from datetime import timedelta

        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.plan.fill_before_cancel()
        h.clock.now += timedelta(minutes=31)
        h.step()
        h.step()
        b = h.bracket(bid)
        assert b["state"] == "open", dict(b)
        assert b["filled_quantity"] == 10

    def test_entry_part_filled_then_expired_protects_what_filled(self, h):
        from datetime import timedelta

        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.plan.fill_before_cancel(quantity=4)
        h.clock.now += timedelta(minutes=31)
        h.step()
        h.step()
        b = h.bracket(bid)
        assert b["state"] == "open", dict(b)
        stop = h.om.sync_status(b["stop_order_id"])
        assert stop.quantity == 4  # exactly the shares that exist

    def test_stop_fills_while_squaring_off(self, h):
        bid = open_protected(h)
        h.plan.fill_before_cancel()  # the stop fills as square_off cancels it
        h.tick(101.0)
        h.sup.square_off_all()
        for _ in range(3):
            h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)


class TestLostAnswers:
    @pytest.mark.xfail(strict=True, reason="#97: ghost stop left resting after exit")
    def test_timeout_after_the_stop_was_placed_leaves_exactly_one_stop(self, h):
        """Kite accepted the stop but the answer was lost; the retry must not stack a
        second stop (two stops would sell 20 against a 10 long)."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.plan.fail(
            "place_order",
            kex.NetworkException("timed out"),
            after_effect=True,
            match=lambda p: p["order_type"] == "SL-M",
        )
        for _ in range(3):
            h.sup.reconcile()
        stops = [o for o in h.paper.list_open_orders() if o["order_type"] in ("SL", "SL-M")]
        assert len(stops) <= 1, stops
        h.check()
        assert h.bracket(bid)["state"] in ("open", "exiting", "closed")

    @pytest.mark.xfail(strict=True, reason="#97: ghost entry left resting")
    def test_timeout_on_entry_leaves_no_untracked_order(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        with pytest.raises(Exception):  # noqa: B017,PT011 - any failure is acceptable
            h.sup.open_trade(_req(entry=100.0))
        h.check()  # the ghost entry is resting at the broker with no trade: I3


class TestExitsUnderFaults:
    @pytest.mark.xfail(strict=True, reason="#98: trade closes with the leg still resting")
    def test_cancel_of_the_surviving_leg_times_out(self, h):
        """The stop fills; cancelling the target fails once (network). The trade must
        not close with the target still working: it could fill later and short us."""
        bid = open_protected(h)
        h.tick(97.5)
        h.plan.fail("cancel_order", kex.NetworkException("timed out"), times=2)
        for _ in range(4):
            h.sup.reconcile()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)

    def test_broker_down_during_reconcile_changes_nothing_and_recovers(self, h):
        bid = open_protected(h)
        h.plan.fail("orders", kex.NetworkException("down"), times=3)
        for _ in range(3):
            h.sup.reconcile()
        h.tick(104.2)
        h.step()
        h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)
