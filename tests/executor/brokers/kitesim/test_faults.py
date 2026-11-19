"""Hostile mode (#87): FaultPlan makes KiteSim misbehave the way Kite does, and
the paper KiteBroker must cope. Every fault is seeded and replayable."""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor.brokers.kitesim.faults import FaultPlan
from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from tests.kitesim_kit import make_sim, paper_broker


def limit_buy(qty: int = 10, price: float = 100.0) -> OrderRequest:
    return OrderRequest(
        "KPEL", "NSE", OrderSide.BUY, OrderType.LIMIT, qty, ProductType.MIS, price=price
    )


@pytest.fixture()
def sim():
    s = make_sim()
    s.on_price("KPEL", "NSE", 101.0)  # above the 100.0 limit: the order rests
    return s


@pytest.fixture()
def broker(sim):
    return paper_broker(sim)


@pytest.fixture()
def plan(sim):
    return FaultPlan(sim, seed=7)


def kpel_qty(sim) -> int:
    rows = [p for p in sim.positions()["net"] if p["tradingsymbol"] == "KPEL"]
    return rows[0]["quantity"] if rows else 0


class TestApiFaults:
    def test_failure_before_the_effect_leaves_no_order(self, sim, broker, plan):
        plan.fail("place_order", kex.NetworkException("exchange unreachable"))
        with pytest.raises(kex.NetworkException):
            broker.place_order(limit_buy())
        assert sim.orders() == []

    def test_timeout_after_the_effect_leaves_a_ghost_order(self, sim, broker, plan):
        """Kite accepted the order but the answer was lost: the caller has no id,
        and the order is working. This is what list_open_orders (#65) exists for."""
        plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
        with pytest.raises(kex.NetworkException):
            broker.place_order(limit_buy())
        assert len(sim.orders()) == 1
        assert [o["symbol"] for o in broker.list_open_orders()] == ["KPEL"]

    def test_a_fault_fires_only_its_number_of_times(self, broker, plan):
        plan.fail("place_order", kex.GeneralException("blip"), times=2)
        for _ in range(2):
            with pytest.raises(kex.GeneralException):
                broker.place_order(limit_buy())
        assert broker.place_order(limit_buy())

    def test_match_limits_a_fault_to_some_calls(self, broker, plan):
        plan.fail(
            "place_order", kex.InputException("no"), times=None, match=lambda p: p["quantity"] > 50
        )
        assert broker.place_order(limit_buy(qty=10))
        with pytest.raises(kex.InputException):
            broker.place_order(limit_buy(qty=100))

    def test_random_failures_replay_with_the_same_seed(self):
        def run(seed: int) -> list[bool]:
            s = make_sim()
            s.on_price("KPEL", "NSE", 101.0)
            b = paper_broker(s)
            FaultPlan(s, seed=seed).fail_randomly("place_order", kex.NetworkException("x"), 0.5)
            out = []
            for _ in range(12):
                try:
                    b.place_order(limit_buy())
                    out.append(True)
                except kex.NetworkException:
                    out.append(False)
            return out

        assert run(3) == run(3)
        assert run(3) != run(4)
        assert True in run(3) and False in run(3)


class TestFillRacesCancel:
    def test_fill_wins_so_the_cancel_is_refused(self, sim, broker, plan):
        oid = broker.place_order(limit_buy(qty=10))
        plan.fill_before_cancel()
        with pytest.raises(kex.InputException, match="complete"):
            broker.cancel_order(oid)
        status = broker.get_order_status(oid)
        assert status["status"] == OrderStatus.FILLED
        assert status["filled_quantity"] == 10
        assert kpel_qty(sim) == 10

    def test_partial_fill_then_cancel_keeps_the_filled_shares(self, sim, broker, plan):
        oid = broker.place_order(limit_buy(qty=10))
        plan.fill_before_cancel(quantity=4)
        broker.cancel_order(oid)
        status = broker.get_order_status(oid)
        assert status["status"] == OrderStatus.CANCELLED
        assert status["filled_quantity"] == 4
        assert kpel_qty(sim) == 4  # 4 shares exist: a stop is needed for exactly these


class TestModifyPending:
    def test_pending_modify_reads_as_no_change_until_settled(self, sim, broker, plan):
        oid = broker.place_order(limit_buy())
        plan.modify_pending()
        broker.modify_order(oid, {"price": 99.0})
        assert sim.orders()[0]["status"] == "MODIFY PENDING"
        assert broker.get_order_status(oid)["status"] is None  # in flight: unchanged (#73)
        plan.settle()
        assert sim.orders()[0]["status"] == "OPEN"
        assert broker.get_order_status(oid)["status"] == OrderStatus.PENDING


class TestPostbackFaults:
    def _collect(self, broker) -> list[dict]:
        seen: list[dict] = []
        broker.on_order_update(seen.append)
        return seen

    def test_held_postbacks_arrive_late_and_in_order(self, sim, broker, plan):
        seen = self._collect(broker)
        plan.hold_postbacks()
        oid = broker.place_order(limit_buy())
        broker.cancel_order(oid)
        assert seen == []
        assert plan.release_postbacks() == 2
        assert [p["status"] for p in seen] == ["OPEN", "CANCELLED"]

    def test_postbacks_can_arrive_out_of_order(self, broker, plan):
        seen = self._collect(broker)
        plan.hold_postbacks()
        broker.cancel_order(broker.place_order(limit_buy()))
        plan.release_postbacks("reverse")
        assert [p["status"] for p in seen] == ["CANCELLED", "OPEN"]

    def test_shuffle_is_seeded(self, sim):
        def run(seed: int) -> list[str]:
            s = make_sim()
            s.on_price("KPEL", "NSE", 101.0)
            b = paper_broker(s)
            seen: list[dict] = []
            b.on_order_update(seen.append)
            fp = FaultPlan(s, seed=seed)
            fp.hold_postbacks()
            for _ in range(6):
                b.place_order(limit_buy())
            fp.release_postbacks("shuffle")
            return [p["order_id"] for p in seen]

        assert run(1) == run(1)

    def test_duplicated_postbacks_arrive_twice(self, broker, plan):
        seen = self._collect(broker)
        plan.duplicate_postbacks(2)
        broker.place_order(limit_buy())
        assert len(seen) == 2 and seen[0] == seen[1]

    def test_dropped_postbacks_never_arrive_but_the_book_is_right(self, broker, plan):
        seen = self._collect(broker)
        plan.drop_postbacks()
        oid = broker.place_order(limit_buy())
        assert seen == []
        assert broker.get_order_status(oid)["status"] == OrderStatus.PENDING

    def test_unknown_release_order_is_an_error(self, plan):
        with pytest.raises(ValueError):
            plan.release_postbacks("sideways")


class TestGapThroughStop:
    def test_a_stop_gapped_through_fills_at_the_gap_price(self, sim, broker):
        """Long 10 @ 100, stop-market sell at 98. The next tick is 90: the stop fills
        at 90, not 98. Loss is 10 x 10 = 100, five times the 20 the stop promised."""
        sim.on_price("KPEL", "NSE", 100.0)
        broker.place_order(
            OrderRequest("KPEL", "NSE", OrderSide.BUY, OrderType.MARKET, 10, ProductType.MIS)
        )
        stop = broker.place_order(
            OrderRequest(
                "KPEL", "NSE", OrderSide.SELL, OrderType.SL, 10, ProductType.MIS, trigger_price=98.0
            )
        )
        sim.on_price("KPEL", "NSE", 90.0)
        status = broker.get_order_status(stop)
        assert status["status"] == OrderStatus.FILLED
        assert status["average_fill_price"] == 90.0
        assert kpel_qty(sim) == 0
