"""Trailing stop (#71): pure rule + end to end on the paper broker.

Numbers from the executor/trailing.py docstring: fill 100, stop 98 (R = 2), ATR 2,
target 104.
"""

from __future__ import annotations

import json

import kiteconnect
import pytest

from executor.models import OrderStatus
from executor.trade_lifecycle import TradeSupervisor
from executor.trailing import MAX_MOVES, next_stop
from tests.executor.test_trade_lifecycle import Harness, _req


def _next(high, current=98.0, ltp=None, moves=0):
    return next_stop(
        entry=100.0,
        initial_stop=98.0,
        current_stop=current,
        high_water=high,
        atr=2.0,
        ltp=ltp if ltp is not None else high,
        moves=moves,
    )


class TestRule:
    def test_nothing_below_1r(self):
        assert _next(101.95).new_stop is None and _next(101.95).rule == "below_1r"

    def test_breakeven_at_1r(self):
        m = _next(102.0)
        assert (m.new_stop, m.rule) == (100.0, "breakeven")

    def test_trail_one_atr_below_the_high(self):
        m = _next(103.5, current=100.0)
        assert (m.new_stop, m.rule) == (101.5, "trail")

    def test_small_steps_skipped_past_breakeven(self):
        assert _next(103.6, current=101.5).rule == "step_too_small"

    def test_never_lowered(self):
        assert _next(102.5, current=101.5).rule == "not_higher"

    def test_never_at_or_above_the_market(self):
        assert _next(103.5, current=100.0, ltp=101.4).rule == "at_or_above_market"

    def test_rounded_down_to_the_tick(self):
        assert _next(103.53, current=100.0).new_stop == 101.5  # 101.53 -> 101.50

    def test_modify_budget(self):
        assert _next(103.5, current=100.0, moves=MAX_MOVES).rule == "max_moves"

    @pytest.mark.parametrize("stop", [100.0, 101.0])
    def test_invalid(self, stop):
        assert (
            next_stop(
                entry=100.0,
                initial_stop=stop,
                current_stop=stop,
                high_water=110,
                atr=2,
                ltp=110,
                moves=0,
            ).rule
            == "invalid_inputs"
        )


@pytest.fixture()
def h(tmp_path) -> Harness:
    h = Harness(tmp_path)
    h.sup = TradeSupervisor(
        h.db, h.om, h.pm, h.alerter, clock=h.clock, price=lambda s: h.feed.latest(s).ltp
    )
    return h


def _open(h: Harness) -> str:
    h.tick(100.0)
    bid = h.sup.open_trade(_req())
    h.sup.reconcile()
    assert h.bracket(bid)["state"] == "open"
    return bid


def _stop_trigger(h: Harness, bid: str) -> float:
    return h.om.sync_status(h.bracket(bid)["stop_order_id"]).trigger_price


class TestEndToEnd:
    def test_breakeven_then_trail_then_stopped_in_profit(self, h):
        bid = _open(h)
        h.tick(102.0)
        h.sup.reconcile()
        assert (_stop_trigger(h, bid), h.bracket(bid)["trail_stop"]) == (100.0, 100.0)
        h.tick(103.5)
        h.sup.reconcile()
        assert _stop_trigger(h, bid) == 101.5
        h.tick(102.6)  # pulls back: the stop never comes down
        h.sup.reconcile()
        assert _stop_trigger(h, bid) == 101.5
        h.tick(101.4)  # through the trailed stop
        h.sup.reconcile()
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["stop_moves"]) == ("closed", "stop_hit", 2)
        assert b["realised_pnl"] > 0  # the same path with a fixed stop loses 20

    def test_each_move_is_a_recorded_transition(self, h):
        bid = _open(h)
        h.tick(102.0)
        h.sup.reconcile()
        row = h.db.execute(
            "SELECT payload FROM events WHERE entity_id=? AND name='stop_raised'", (bid,)
        ).fetchone()[0]
        p = json.loads(row)
        assert (p["from_state"], p["to_state"], p["rule"]) == ("open", "open", "breakeven")
        assert (p["inputs"]["from"], p["inputs"]["to"]) == (98.0, 100.0)

    def test_high_water_persists_without_a_move(self, h):
        bid = _open(h)
        h.tick(101.0)
        h.sup.reconcile()
        assert h.bracket(bid)["high_water"] == 101.0
        assert h.bracket(bid)["trail_stop"] is None

    def test_refused_modify_keeps_the_old_stop(self, h, monkeypatch):
        bid = _open(h)

        def refuse(order_id, trigger):
            raise ValueError("modify rejected")

        monkeypatch.setattr(h.om, "modify_trigger", refuse)
        h.tick(102.0)
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["trail_stop"], b["high_water"]) == ("open", None, 102.0)
        assert _stop_trigger(h, bid) == 98.0

    def test_target_still_wins(self, h):
        bid = _open(h)
        h.tick(104.2)
        h.sup.reconcile()
        h.sup.reconcile()
        assert h.bracket(bid)["exit_reason"] == "target_hit"


class TestModifyTrigger:
    def test_not_resting_raises(self, h):
        bid = _open(h)
        b = h.bracket(bid)
        h.om.cancel(b["stop_order_id"])
        with pytest.raises(Exception, match="not resting"):
            h.om.modify_trigger(b["stop_order_id"], 99.0)

    def test_paper_refuses_wrong_side_like_kite(self, h):
        bid = _open(h)
        with pytest.raises(kiteconnect.exceptions.InputException, match="stoploss sell"):
            h.om.modify_trigger(h.bracket(bid)["stop_order_id"], 100.5)  # above LTP 100
        stop = h.om.sync_status(h.bracket(bid)["stop_order_id"])
        assert (stop.status, stop.trigger_price) == (OrderStatus.PENDING, 98.0)
