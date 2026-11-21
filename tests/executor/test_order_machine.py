"""Order machine on the core: every status change is recorded (#56)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from executor.brokers.kitesim import KiteSim
from executor.models import (
    ALLOWED_TRANSITIONS,
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    StateMachineError,
)
from executor.order_machine import ORDER_SPEC
from executor.order_manager import OrderManager
from tests.kitesim_kit import make_sim, paper_broker


def _om() -> tuple[OrderManager, KiteSim, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    for f in ("0001_initial_schema", "0004_executor_schema", "0012_events"):
        with open(f"migrations/{f}.sql") as fh:
            db.executescript(fh.read())
    sim = make_sim()
    return OrderManager(db, {BrokerName.KITE: paper_broker(sim)}, ltp_cache={}), sim, db


def _req(order_type=OrderType.LIMIT, price=99.0) -> OrderRequest:
    return OrderRequest(
        symbol="INFY",
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=10,
        product=ProductType.MIS,
        price=price,
        idempotency_key="k1",
    )


def _history(db, order_id) -> list[tuple]:
    rows = db.execute(
        "SELECT name, payload FROM events WHERE entity_type='order' AND entity_id=?"
        " ORDER BY event_id",
        (order_id,),
    ).fetchall()
    return [(json.loads(p)["from_state"], json.loads(p)["to_state"], name) for name, p in rows]


class TestSpec:
    def test_spec_mirrors_allowed_transitions_exactly(self):
        for src, dsts in ALLOWED_TRANSITIONS.items():
            for dst in OrderStatus:
                found = ORDER_SPEC.find(src.value, dst.value)
                assert (found is not None) == (dst in dsts), (src, dst)

    def test_finished_statuses_are_terminal_and_never_reopened(self):
        """#100: FILLED/CANCELLED/REJECTED/ERROR are final; no EXPIRED state exists."""
        assert ORDER_SPEC.terminal == {"filled", "cancelled", "rejected", "error"}
        assert "expired" not in {s.value for s in OrderStatus}
        for status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert ALLOWED_TRANSITIONS[status] == frozenset()


class TestHistory:
    def test_full_lifecycle_recorded_in_order(self):
        om, sim, db = _om()
        sim.on_price("INFY", "NSE", 100.0)
        oid = om.submit(_req(), track="other")  # resting LIMIT 99 below 100
        sim.on_price("INFY", "NSE", 98.5)
        om.sync_status(oid)
        assert _history(db, oid) == [
            (None, "created", "created"),
            ("created", "submitting", "submitting"),
            ("submitting", "pending", "pending"),
            ("pending", "filled", "filled"),
        ]
        rule = json.loads(
            db.execute(
                "SELECT payload FROM events WHERE entity_id=? AND name='filled'", (oid,)
            ).fetchone()[0]
        )["rule"]
        assert rule == "broker_status_sync"

    def test_events_carry_order_and_symbol_correlation(self):
        om, _, db = _om()
        oid = om.submit(_req(), track="other")
        rows = db.execute(
            "SELECT DISTINCT order_id, symbol, source FROM events WHERE entity_id=?", (oid,)
        ).fetchall()
        assert [tuple(r) for r in rows] == [(oid, "INFY", "order")]

    def test_illegal_transition_still_raises_state_machine_error(self):
        om, _, db = _om()
        oid = om.submit(_req(), track="other")
        before = _history(db, oid)
        with pytest.raises(StateMachineError, match="pending → created"):
            om._transition(oid, OrderStatus.CREATED)
        assert _history(db, oid) == before  # nothing recorded for an illegal move
        assert om._load_order(oid).status == OrderStatus.PENDING

    def test_cancel_recorded(self):
        om, _, db = _om()
        oid = om.submit(_req(), track="other")
        om.cancel(oid)
        assert _history(db, oid)[-1] == ("pending", "cancelled", "cancelled")
