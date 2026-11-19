"""#97 / #98 / #101 regressions at the OrderManager, KiteBroker and BracketStore level.

An order whose placement answer is lost must be found and tracked; a cancel that did
not take must never be recorded as CANCELLED; exit keys must not be reused. Run on the
hostile paper broker so each fault is what Kite would do."""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def limit_buy(key: str, price: float = 100.0) -> OrderRequest:
    return OrderRequest(
        "INFY", "NSE", OrderSide.BUY, OrderType.LIMIT, 10, ProductType.MIS,
        price=price, idempotency_key=key,
    )  # fmt: skip


def order_row(h: Hostile, order_id: str):
    return h.db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


class TestFindOrder:
    def test_finds_the_order_by_its_idempotency_key(self, h):
        h.tick(100.5)
        oid = h.om.submit(limit_buy("find-me-1"), "intraday")
        assert h.paper.find_order("find-me-1") == order_row(h, oid)["broker_order_id"]

    def test_unknown_key_is_none(self, h):
        assert h.paper.find_order("never-placed") is None

    def test_keys_of_one_trade_do_not_share_a_tag(self, h):
        """The UUID prefix is the same for entry/stop/target; the tag must still differ."""
        h.tick(100.5)
        uuid = "8a66f9e6-aca9-4b06-b49d-ca0c05aa5ec7"
        a = h.om.submit(limit_buy(f"{uuid}-entry"), "intraday")
        b = h.om.submit(limit_buy(f"{uuid}-stop", price=99.0), "intraday")
        assert h.paper.find_order(f"{uuid}-entry") == order_row(h, a)["broker_order_id"]
        assert h.paper.find_order(f"{uuid}-stop") == order_row(h, b)["broker_order_id"]


class TestLostAnswer:
    def test_a_lost_answer_is_adopted_not_errored(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        oid = h.om.submit(limit_buy("lost-1"), "intraday")
        row = order_row(h, oid)
        assert row["status"] == OrderStatus.PENDING.value and row["broker_order_id"]
        assert len(h.sim.orders()) == 1  # one order at the broker, and we track it

    def test_a_refusal_with_no_order_is_still_an_error(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.InputException("refused"))
        with pytest.raises(kex.InputException):
            h.om.submit(limit_buy("refused-1"), "intraday")
        assert h.db.execute("SELECT status FROM orders").fetchone()[0] == "error"
        assert h.sim.orders() == []

    def test_lookup_failure_leaves_it_to_cancel_lost_orders(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        h.plan.fail("orders", kex.NetworkException("still down"))
        with pytest.raises(kex.NetworkException):
            h.om.submit(limit_buy("lost-2"), "intraday")
        assert h.db.execute("SELECT status FROM orders").fetchone()[0] == "error"
        assert len(h.sim.orders()) == 1  # ghost resting at the broker
        assert h.om.cancel_lost_orders() == 1  # broker reachable again
        assert h.paper.list_open_orders() == []
        assert h.om.cancel_lost_orders() == 0  # found once, never again

    def test_a_ghost_that_filled_is_flattened(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        h.plan.fail("orders", kex.NetworkException("still down"))
        with pytest.raises(kex.NetworkException):
            h.om.submit(limit_buy("lost-3"), "intraday")
        h.tick(99.0)  # the ghost entry fills while we cannot see it
        assert {p.symbol: p.quantity for p in h.paper.list_positions()} == {"INFY": 10}
        h.om.cancel_lost_orders()
        assert h.paper.list_positions() == []

    def test_no_idempotency_key_means_no_lookup(self, h):
        h.tick(100.5)
        h.plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        req = limit_buy("")
        with pytest.raises(kex.NetworkException):
            h.om.submit(req, "intraday")


class TestCancelThatDidNotTake:
    def test_order_stays_working_when_the_cancel_fails(self, h):
        h.tick(100.5)
        oid = h.om.submit(limit_buy("c-1"), "intraday")
        h.plan.fail("cancel_order", kex.NetworkException("timed out"), times=1)
        rec = h.om.cancel(oid)
        assert rec.status == OrderStatus.PENDING  # still resting at Kite: say so
        assert len(h.paper.list_open_orders()) == 1
        assert h.om.cancel(oid).status == OrderStatus.CANCELLED  # the retry works


class TestExitKeys:
    def test_exit_attempts_counts_failed_orders_too(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        b = h.bracket(bid)
        assert h.sup._store.exit_attempts(b) == 0
        h.tick(97.0)
        h.plan.fail(
            "place_order", kex.NetworkException("down"), match=lambda p: p["order_type"] == "MARKET"
        )
        h.sup.reconcile()  # the first exit fails and burns -exit1
        assert h.sup._store.exit_attempts(h.bracket(bid)) == 1
        h.sup.reconcile()
        assert h.sup._store.exit_attempts(h.bracket(bid)) == 2
        assert h.bracket(bid)["state"] in ("exiting", "closed")


class TestPartialEntry:
    def test_first_partial_fill_cancels_the_rest_and_protects_what_filled(self, h):
        """L12 (#99): 4 of 10 filled -> cancel the remaining 6, stop and target for 4."""
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        entry = h.om.sync_status(h.bracket(bid)["entry_order_id"])
        h.sim.advance(
            entry.broker_order_id,
            "OPEN",
            filled_quantity=4,
            pending_quantity=6,
            average_price=100.0,
        )
        h.step()
        b = h.bracket(bid)
        assert (b["state"], b["filled_quantity"]) == ("open", 4)
        assert h.om.sync_status(b["entry_order_id"]).status == OrderStatus.CANCELLED
        assert h.om.sync_status(b["stop_order_id"]).quantity == 4
        assert h.om.sync_status(b["target_order_id"]).quantity == 4
        assert h.paper.list_open_orders() and len(h.paper.list_open_orders()) == 2

    def test_partial_with_a_cancel_that_fails_waits_and_retries(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        entry = h.om.sync_status(h.bracket(bid)["entry_order_id"])
        h.sim.advance(
            entry.broker_order_id,
            "OPEN",
            filled_quantity=4,
            pending_quantity=6,
            average_price=100.0,
        )
        h.plan.fail("cancel_order", kex.NetworkException("timed out"), times=1)
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "entry_pending"  # not protecting while the rest rests
        h.step()
        assert h.bracket(bid)["state"] == "open"


class TestMarkToMarket:
    """#106: open positions get their last price and unrealised P&L every reconcile.

    Worked example: long 10 @ 100, last 101.5 -> unrealised (101.5 - 100) x 10 = 15."""

    def supervisor(self, h: Hostile, last):
        from executor.trade_lifecycle import TradeSupervisor

        return TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock, price=last)

    def position(self, h: Hostile):
        return h.db.execute("SELECT current_price, unrealised_pnl FROM positions").fetchone()

    def test_the_price_and_unrealised_pnl_follow_the_market(self, h):
        h.sup = self.supervisor(h, lambda s: 101.5)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        assert tuple(self.position(h)) == (101.5, 15.0)

    def test_it_updates_again_as_the_price_moves(self, h):
        px = {"v": 100.0}
        h.sup = self.supervisor(h, lambda s: px["v"])
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        px["v"] = 99.0
        h.sup.reconcile()
        assert tuple(self.position(h)) == (99.0, -10.0)

    def test_no_price_source_leaves_positions_alone(self, h):
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        assert tuple(self.position(h)) == (100.0, 0.0)  # as opened: entry price, no P&L

    def test_a_missing_price_changes_nothing(self, h):
        h.sup = self.supervisor(h, lambda s: None)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        assert tuple(self.position(h)) == (100.0, 0.0)  # as opened: entry price, no P&L

    def test_a_failing_price_source_never_breaks_trade_management(self, h):
        def boom(symbol):
            raise RuntimeError("feed down")

        h.sup = self.supervisor(h, boom)
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "open"  # the trade still advanced
