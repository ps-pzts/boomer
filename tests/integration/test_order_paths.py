"""Order machine paths through OrderManager -> real KiteBroker -> KiteSim (#88).

Drives every transition a live Kite order can really take, one scenario each:
  created -> submitting -> pending -> triggered -> partial -> filled / cancelled
  pending -> rejected / cancelled / filled, submitting -> error.
Spec transitions no code path can emit are listed in tests/integration/spec_only.py.
"""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from tests.integration.harness import Hostile


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def sl_limit_sell(qty: int = 10) -> OrderRequest:
    """Stop-limit SELL: triggers at 98, then rests as a LIMIT at 97.5 (below = not marketable
    until the price comes back up to 97.5)."""
    return OrderRequest(
        "INFY",
        "NSE",
        OrderSide.SELL,
        OrderType.SL_LIMIT,
        qty,
        ProductType.MIS,
        price=97.5,
        trigger_price=98.0,
        idempotency_key="sl-1",
    )


def status(h: Hostile, oid: str) -> OrderStatus:
    return h.om.sync_status(oid).status


def place_triggered_stop(h: Hostile) -> str:
    h.tick(100.0)
    oid = h.om.submit(sl_limit_sell(), "intraday")
    assert status(h, oid) == OrderStatus.PENDING  # TRIGGER PENDING: waiting, not fired
    h.tick(97.0)  # trigger fires; a SELL limit at 97.5 does not fill at 97.0, so it rests
    return oid


class TestStopLimitLifecycle:
    def test_trigger_then_partial_then_filled(self, h):
        oid = place_triggered_stop(h)
        assert status(h, oid) == OrderStatus.TRIGGERED
        broker_id = h.om.sync_status(oid).broker_order_id
        h.sim.advance(broker_id, "OPEN", filled_quantity=4, pending_quantity=6, average_price=97.5)
        assert status(h, oid) == OrderStatus.PARTIAL
        h.tick(98.0)  # price recovers through the limit: the rest fills
        done = h.om.sync_status(oid)
        assert (done.status, done.filled_quantity) == (OrderStatus.FILLED, 10)

    def test_trigger_then_fully_filled(self, h):
        oid = place_triggered_stop(h)
        assert status(h, oid) == OrderStatus.TRIGGERED
        h.tick(98.0)
        assert status(h, oid) == OrderStatus.FILLED

    def test_cancel_after_trigger(self, h):
        oid = place_triggered_stop(h)
        assert status(h, oid) == OrderStatus.TRIGGERED
        assert h.om.cancel(oid).status == OrderStatus.CANCELLED

    def test_cancel_after_partial_keeps_the_filled_quantity(self, h):
        oid = place_triggered_stop(h)
        broker_id = h.om.sync_status(oid).broker_order_id
        h.sim.advance(broker_id, "OPEN", filled_quantity=4, pending_quantity=6, average_price=97.5)
        assert status(h, oid) == OrderStatus.PARTIAL
        rec = h.om.cancel(oid)
        assert (rec.status, rec.filled_quantity) == (OrderStatus.CANCELLED, 4)


class TestPlainOrders:
    def _limit(self, h: Hostile) -> str:
        h.tick(101.0)
        return h.om.submit(
            OrderRequest(
                "INFY", "NSE", OrderSide.BUY, OrderType.LIMIT, 10, ProductType.MIS, price=100.0,
                idempotency_key="l-1",
            ),
            "intraday",
        )  # fmt: skip

    def test_pending_to_rejected_by_rms_after_acceptance(self, h):
        oid = self._limit(h)
        broker_id = h.om.sync_status(oid).broker_order_id
        h.sim.advance(broker_id, "REJECTED", status_message="RMS:Margin exceeded")
        rec = h.om.sync_status(oid)
        assert rec.status == OrderStatus.REJECTED

    def test_partial_to_filled(self, h):
        oid = self._limit(h)
        broker_id = h.om.sync_status(oid).broker_order_id
        h.sim.advance(broker_id, "OPEN", filled_quantity=3, pending_quantity=7, average_price=100.0)
        assert status(h, oid) == OrderStatus.PARTIAL
        h.tick(99.0)
        assert status(h, oid) == OrderStatus.FILLED

    def test_day_end_cancels_a_working_order(self, h):
        """Kite's exchange cancels DAY orders at close; it never says EXPIRED (see spec_only)."""
        oid = self._limit(h)
        h.sim.end_of_day()
        rec = h.om.sync_status(oid)
        assert rec.status == OrderStatus.CANCELLED

    def test_broker_refusal_at_placement_is_recorded_as_error(self, h):
        h.tick(101.0)
        h.plan.fail("place_order", kex.InputException("bad"), times=1)
        with pytest.raises(kex.InputException):
            h.om.submit(
                OrderRequest(
                    "INFY", "NSE", OrderSide.BUY, OrderType.LIMIT, 10, ProductType.MIS,
                    price=100.0, idempotency_key="l-2",
                ),
                "intraday",
            )  # fmt: skip
        row = h.db.execute("SELECT status FROM orders").fetchone()
        assert row[0] == OrderStatus.ERROR.value
