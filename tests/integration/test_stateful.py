"""Hypothesis stateful run of the whole supervisor on the hostile broker (#88).

Hypothesis explores sequences of ticks, entries, clock jumps, broker faults, postback
disorder and restarts, checks every invariant after each step, and SHRINKS a failing
sequence to the shortest one. Faults behind the open bugs (#97, #98, #99, #101) are
left out; the seeded days in test_random_days.py cover the same ground by seed.

Deterministic by default (`derandomize`), so CI never flakes; to explore, run
    HYPOTHESIS_SEED=$RANDOM BOOMER_STATEFUL_EXAMPLES=300 pytest tests/integration/test_stateful.py
A failure prints the shrunk steps; replay them as a scenario test.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import kiteconnect.exceptions as kex
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule

from tests.integration.harness import Hostile
from tests.integration.randomday import MAX_OPEN, SYMBOLS, _req
from tests.integration.test_scenarios_more import restarted

EXAMPLES = int(os.environ.get("BOOMER_STATEFUL_EXAMPLES", "25"))
SEED = os.environ.get("HYPOTHESIS_SEED")
# BOOMER_KNOWN_BUGS=1 adds the faults behind #97/#98: Hypothesis should find and shrink them.
KNOWN = ["cancel_fail", "ghost_place"] if os.environ.get("BOOMER_KNOWN_BUGS") else []


class Day(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.h = Hostile(Path(tempfile.mkdtemp()), seed=0)
        self.ltp = dict.fromkeys(SYMBOLS, 100.0)
        for s in SYMBOLS:
            self.h.tick(self.ltp[s], sym=s)

    def settle(self) -> None:
        h = self.h
        h.sup.reconcile()
        h.sup.reconcile()
        with h.plan.suspended():
            h.sup.reconcile()
            h.sup.reconcile()
        h.check()

    @rule(sym=st.sampled_from(SYMBOLS), move=st.sampled_from([-6, -3, -1, -0.5, 0.5, 1, 3, 6]))
    def tick(self, sym, move):
        self.ltp[sym] = round(max(80.0, min(120.0, self.ltp[sym] + move)) * 2) / 2
        self.h.tick(self.ltp[sym], sym=sym)
        self.settle()

    @precondition(lambda self: self.h.sup.active_count() < MAX_OPEN)
    @rule(sym=st.sampled_from(SYMBOLS))
    def open_trade(self, sym):
        with contextlib.suppress(Exception):  # a refused entry is a valid outcome
            self.h.sup.open_trade(_req(sym, round(self.ltp[sym] * 2) / 2))
        self.settle()

    @rule(minutes=st.sampled_from([1, 5, 31]))
    def clock(self, minutes):
        self.h.clock.now += timedelta(minutes=minutes)
        self.settle()

    @rule(
        kind=st.sampled_from(
            ["orders_down", "refuse_entry", "cancel_race", "modify_pending", *KNOWN]
        ),
        times=st.integers(1, 2),
    )
    def fault(self, kind, times):
        plan = self.h.plan
        if kind == "orders_down":
            plan.fail("orders", kex.NetworkException("down"), times=times)
        elif kind == "refuse_entry":
            plan.fail(
                "place_order",
                kex.InputException("refused"),
                times=1,
                match=lambda p: p["order_type"] == "LIMIT",
            )
        elif kind == "cancel_fail":  # #98
            plan.fail("cancel_order", kex.NetworkException("timed out"), times=1)
        elif kind == "ghost_place":  # #97
            plan.fail("place_order", kex.NetworkException("timed out"), times=1, after_effect=True)
        elif kind == "cancel_race":
            plan.fill_before_cancel(times=1)
        else:
            plan.modify_pending(times=1)
        self.settle()

    @rule(mode=st.sampled_from(["hold", "fifo", "reverse", "dup", "drop", "normal"]))
    def postbacks(self, mode):
        plan = self.h.plan
        if mode == "hold":
            plan.hold_postbacks()
        elif mode in ("fifo", "reverse"):
            plan.release_postbacks(mode)
        elif mode == "dup":
            plan.duplicate_postbacks(2)
        elif mode == "drop":
            plan.drop_postbacks()
        else:
            plan._copies, plan._drop = 1, None
        self.settle()

    @rule()
    def restart(self):
        self.h.sup = restarted(self.h)
        self.settle()

    def teardown(self) -> None:
        h = self.h
        h.plan.release_postbacks()
        h.sup.square_off_all()
        for _ in range(4):
            h.sup.reconcile()
        h.check(end_of_day=True)


TestStateful = Day.TestCase
TestStateful.settings = settings(
    max_examples=EXAMPLES,
    stateful_step_count=20,
    deadline=None,
    derandomize=SEED is None,
    database=None,
    suppress_health_check=list(HealthCheck),
)
