"""Trade machine paths on the hostile paper broker (#88): every transition of the
spec's `entry_pending -> protecting -> open -> exiting -> closed` graph and its
cancellations, each reached through a realistic broker event. Invariants are
checked after every step by Hostile.step()."""

from __future__ import annotations

from datetime import timedelta

import kiteconnect.exceptions as kex
import pytest

from executor.models import OrderStatus
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile
from tests.integration.test_scenarios import open_protected


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def reject_next_market_sell(h: Hostile, times: int = 1) -> None:
    """RMS accepts the market exit, then rejects it."""
    left = [times]
    sim = h.sim
    base = h.plan._intercept

    def intercept(call, params, proceed):
        oid = base(call, params, proceed)
        if call == "place_order" and params["order_type"] == "MARKET" and left[0] > 0:
            left[0] -= 1
            sim._advance(sim._order(oid), "REJECTED", status_message="RMS:Blocked")
        return oid

    sim.interceptor = intercept


class TestEntryEnds:
    def test_entry_rejected_by_the_broker_cancels_the_trade(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        entry = h.om.sync_status(h.bracket(bid)["entry_order_id"])
        h.sim.advance(entry.broker_order_id, "REJECTED", status_message="RMS:Blocked")
        h.step()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("cancelled", "entry_rejected")
        h.check(end_of_day=True)

    def test_unfilled_entry_expires(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.clock.now += timedelta(minutes=31)
        h.step()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("cancelled", "entry_expired")
        h.check(end_of_day=True)

    def test_square_off_cancels_an_unfilled_entry(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.sup.square_off_all()
        h.step()
        assert h.bracket(bid)["state"] == "cancelled"
        h.check(end_of_day=True)


class TestProtectingAndStopLost:
    def test_square_off_while_still_protecting_exits(self, h):
        """Entry filled, the broker went down before the stop could be confirmed: the
        trade is parked in PROTECTING; 15:14 must still flatten it."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        calls = [0]

        def outage_after_the_entry_fill(params: dict) -> bool:
            calls[0] += 1  # 1st reads the entry fill; from the 2nd on the broker is down,
            return calls[0] >= 2  # also for the settle-first reconcile inside square_off_all

        h.plan.fail(
            "orders", kex.NetworkException("down"), times=2, match=outage_after_the_entry_fill
        )
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "protecting"
        h.sup.square_off_all()
        for _ in range(3):
            h.step()
        assert h.bracket(bid)["state"] == "closed"
        h.check(end_of_day=True)

    def test_stop_cancelled_outside_the_bot_is_stop_lost(self, h):
        bid = open_protected(h)
        stop = h.om.sync_status(h.bracket(bid)["stop_order_id"])
        h.paper.cancel_order(stop.broker_order_id)  # operator cancels it in the Kite app
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "exiting"
        for _ in range(2):
            h.step()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("closed", "stop_cancelled")
        h.check(end_of_day=True)


class TestFailingExits:
    def test_rejected_market_exit_is_retried_until_flat(self, h):
        """Stop could not be placed (price through it), so the bot exits at market, and
        the first exit is rejected. It must retry, not strand the position."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)
        reject_next_market_sell(h, times=1)
        for _ in range(4):
            h.sup.reconcile()
        assert h.bracket(bid)["state"] == "closed", dict(h.bracket(bid))
        assert h.paper.list_positions() == []
        assert any("Exit order" in t for t in h.critical_titles())

    def test_failed_exit_placement_is_retried(self, h):
        """The exit order could not be placed at all (network). The next reconcile must
        place it again; today every retry reuses the key and is refused as a duplicate."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)  # stop cannot be placed through the market: exit at market
        h.plan.fail(
            "place_order",
            kex.NetworkException("down"),
            match=lambda p: p["order_type"] == "MARKET",
        )
        for _ in range(4):
            h.sup.reconcile()
        assert h.bracket(bid)["state"] == "closed", dict(h.bracket(bid))
        assert h.paper.list_positions() == []

    def test_exit_that_is_always_rejected_keeps_alerting_and_never_closes_with_shares(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)
        reject_next_market_sell(h, times=50)
        for _ in range(5):
            h.sup.reconcile()
        b = h.bracket(bid)
        net = {p.symbol: p.quantity for p in h.paper.list_positions()}
        if b["state"] == "closed":
            assert net == {} or net.get("INFY", 0) == 0, f"closed while still long: {net}"
        else:
            assert h.critical_titles()
        assert h.om.sync_status(h.bracket(bid)["entry_order_id"]).status == OrderStatus.FILLED
