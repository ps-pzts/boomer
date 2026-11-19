"""A seeded random day against the hostile broker (#88).

`play(h, seed, steps)` draws actions from random.Random(seed): the same seed gives
the same day, so a failing seed is a reproduction. Faults that trigger the open bugs
#97/#98/#99 are excluded unless `known_bugs=True`, so the run guards everything else
and flips on the day they are fixed.
"""

from __future__ import annotations

import contextlib
import random
from datetime import timedelta

import kiteconnect.exceptions as kex

from executor.trade_lifecycle import TradeRequest
from tests.integration.harness import Hostile, attach_book, process_restart

SYMBOLS = ("INFY", "KPEL")
MAX_OPEN = 3  # decision_cycle.MAX_OPEN_TRADES; the cap lives in the cycle, not the supervisor


def _req(sym: str, entry: float) -> TradeRequest:
    return TradeRequest(sym, "NSE", 10, entry, entry - 2.0, entry + 4.0, atr=2.0)


def play(h: Hostile, seed: int, steps: int = 40, known_bugs: bool = False) -> None:
    rng = random.Random(seed)
    attach_book(h)  # restarts rebuild KiteSim from the paper book
    ltp = {s: 100.0 for s in SYMBOLS}
    for s in SYMBOLS:
        h.tick(ltp[s], sym=s)
    for _ in range(steps):
        action = rng.choice(
            ["tick", "tick", "tick", "open", "clock", "reconcile", "fault", "postbacks", "restart"]
        )
        sym = rng.choice(SYMBOLS)
        if action == "tick":
            ltp[sym] = (
                round(
                    max(80.0, min(120.0, ltp[sym] + rng.choice([-3, -1, -0.5, 0.5, 1, 3, 6]))) * 2
                )
                / 2
            )
            h.tick(ltp[sym], sym=sym)
        elif action == "open" and h.sup.active_count() < MAX_OPEN:
            with contextlib.suppress(Exception):  # a refused entry is a valid outcome
                h.sup.open_trade(_req(sym, round(ltp[sym] * 2) / 2))
        elif action == "clock":
            h.clock.now += timedelta(minutes=rng.choice([1, 5, 31]))
        elif action == "fault":
            _fault(h, rng, known_bugs)
        elif action == "postbacks":
            _postbacks(h, rng)
        elif action == "restart":
            if rng.random() < 0.5:
                from tests.integration.test_scenarios_more import restarted

                h.sup = restarted(h)  # supervisor only: the broker process kept running
            else:
                process_restart(h)  # everything: KiteSim restored from the paper book
        h.sup.reconcile()
        h.sup.reconcile()
        with h.plan.suspended():  # the wire recovers: the system must heal to a safe state
            h.sup.reconcile()
            h.sup.reconcile()
        h.check()
    h.plan.release_postbacks()
    for s in SYMBOLS:  # the feed keeps ticking up to 15:30, so market exits can fill
        h.tick(ltp[s], sym=s)
    h.sup.square_off_all()
    for _ in range(4):
        h.sup.reconcile()
    h.check(end_of_day=True)


def _fault(h: Hostile, rng: random.Random, known_bugs: bool) -> None:
    kind = rng.choice(["orders_down", "place_fail", "cancel_race", "modify_pending", "cancel_fail"])
    if kind == "orders_down":
        h.plan.fail("orders", kex.NetworkException("down"), times=rng.randint(1, 2))
    elif kind == "place_fail":  # definite refusal: no order exists afterwards
        entries_only = None if known_bugs else (lambda p: p["order_type"] == "LIMIT")  # #101
        h.plan.fail("place_order", kex.InputException("refused"), times=1, match=entries_only)
    elif kind == "cancel_race":
        h.plan.fill_before_cancel(times=1)
    elif kind == "modify_pending":
        h.plan.modify_pending(times=1)
    elif kind == "cancel_fail" and known_bugs:  # #98
        h.plan.fail("cancel_order", kex.NetworkException("timed out"), times=1)


def _postbacks(h: Hostile, rng: random.Random) -> None:
    mode = rng.choice(["hold", "release", "reverse", "dup", "drop", "normal"])
    if mode == "hold":
        h.plan.hold_postbacks()
    elif mode in ("release", "reverse"):
        h.plan.release_postbacks("fifo" if mode == "release" else "reverse")
    elif mode == "dup":
        h.plan.duplicate_postbacks(2)
    elif mode == "drop":
        h.plan.drop_postbacks()
    else:
        h.plan._copies, h.plan._drop = 1, None
