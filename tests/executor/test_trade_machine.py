"""SM-2: the Trade machine on the core (#45). Complements test_trade_lifecycle.py
(behaviour) with: the recorded history, the explicit PROTECTING state, the spec,
and restart safety."""

from __future__ import annotations

import json

import pytest

from executor.trade_lifecycle import TradeSupervisor
from executor.trade_machine import TRADE_SPEC
from statemachine import IllegalTransition, transition
from tests.executor.test_trade_lifecycle import Harness, _filled_and_protected, _req


@pytest.fixture()
def h(tmp_path) -> Harness:
    return Harness(tmp_path)


def _history(h: Harness, bid: str) -> list[tuple[str | None, str, str]]:
    rows = h.db.execute(
        "SELECT name, payload FROM events WHERE entity_type='trade' AND entity_id=?"
        " ORDER BY event_id",
        (bid,),
    ).fetchall()
    return [(json.loads(p)["from_state"], json.loads(p)["to_state"], n) for n, p in rows]


class TestSpec:
    def test_terminal_states(self):
        assert TRADE_SPEC.terminal == {"closed", "cancelled"}

    def test_square_off_target_depends_on_state(self):
        assert TRADE_SPEC.find("entry_pending", "square_off").target == "cancelled"
        assert TRADE_SPEC.find("protecting", "square_off").target == "exiting"
        assert TRADE_SPEC.find("open", "square_off").target == "exiting"

    def test_no_shortcut_from_open_to_closed(self):
        """Every exit passes through EXITING, so the settlement is always recorded."""
        assert all(t.target != "closed" or t.source == {"exiting"} for t in TRADE_SPEC.transitions)

    def test_closed_trade_cannot_move(self, h):
        bid = _filled_and_protected(h)
        h.tick(104.2)
        h.sup.reconcile()
        with pytest.raises(IllegalTransition):
            transition(h.db, TRADE_SPEC, bid, "stop_lost", rule="x")


class TestHistory:
    def test_target_path_recorded_with_correlation(self, h):
        bid = _filled_and_protected(h)
        h.tick(104.2)
        h.sup.reconcile()
        assert _history(h, bid) == [
            (None, "entry_pending", "created"),
            ("entry_pending", "protecting", "entry_filled"),
            ("protecting", "open", "stop_confirmed"),
            ("open", "exiting", "leg_filled"),
            ("exiting", "closed", "flat"),
        ]
        rows = h.db.execute(
            "SELECT DISTINCT trade_id, symbol, source FROM events WHERE entity_id=?", (bid,)
        ).fetchall()
        assert [tuple(r) for r in rows] == [(bid, "INFY", "trade")]
        flat = h.db.execute(
            "SELECT payload FROM events WHERE entity_id=? AND name='flat'", (bid,)
        ).fetchone()[0]
        assert json.loads(flat)["inputs"]["realised_pnl"] == 40.0

    def test_stop_rejected_path_is_critical_and_explicit(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.tick(97.0)  # below the stop before it can be placed
        h.sup.reconcile()
        h.sup.reconcile()
        assert [n for _, _, n in _history(h, bid)] == [
            "created",
            "entry_filled",
            "stop_failed",
            "corrective_order",
            "flat",
        ]
        sev = h.db.execute(
            "SELECT severity FROM events WHERE entity_id=? AND name='stop_failed'", (bid,)
        ).fetchone()[0]
        assert sev == "critical"

    def test_fields_and_state_change_together(self, h):
        """advance() writes bracket fields in the same transaction as the transition."""
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        assert (b["state"], b["filled_quantity"]) == ("open", 10)
        assert b["position_id"] is not None


class TestRestartSafety:
    def _into_protecting_without_leg_ids(self, h: Harness) -> str:
        """Simulate a crash after the stop was submitted but before its id was saved."""
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        entry = h.om.sync_status(h.bracket(bid)["entry_order_id"])
        pid = h.pm.open_position(
            symbol="INFY",
            exchange="NSE",
            track="intraday",
            bucket_id="intraday",
            broker_id=entry.broker_id,
            quantity=10,
            average_entry_price=100.0,
            stop_loss_price=98.0,
            target_price=104.0,
            atr_at_entry=2.0,
            entry_order_id=entry.order_id,
        )
        h.sup._store.advance(
            h.bracket(bid), "entry_filled", rule="test", filled_quantity=10, position_id=pid
        )
        h.sup._place_leg(h.bracket(bid), "stop")  # submitted, id never saved
        return bid

    def test_restart_in_protecting_reuses_submitted_stop(self, h):
        bid = self._into_protecting_without_leg_ids(h)
        fresh = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
        fresh.reconcile()
        stops = h.db.execute(
            "SELECT COUNT(*) FROM orders WHERE idempotency_key=?", (f"{bid}-stop",)
        ).fetchone()[0]
        assert stops == 1  # no second resting stop that could later fire into a short
        b = h.bracket(bid)
        assert b["state"] == "open"
        assert h.om.sync_status(b["stop_order_id"]).idempotency_key == f"{bid}-stop"

    def test_restart_in_exiting_without_exit_order_places_one(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        for leg in ("stop_order_id", "target_order_id"):
            h.om.cancel(b[leg])
        h.sup._store.advance(b, "square_off", rule="test")  # crash before the exit order
        assert json.loads(h.bracket(bid)["exit_order_ids"]) == []
        h.tick(101.0)
        fresh = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
        fresh.reconcile()
        fresh.reconcile()
        assert h.bracket(bid)["state"] == "closed"
        assert h.paper.list_positions() == []
