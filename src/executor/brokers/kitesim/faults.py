"""Hostile mode for KiteSim (#87): seeded, replayable broker misbehaviour.

Real Kite fails in ways a happy-path simulator never does. A FaultPlan installs
those failures on a KiteSim through its three hooks (API interceptor, order
`advance`, postback gate) so the supervisor and reconciliation code is tested
against them. Nothing here is random unless asked: with the same `seed` and the
same calls, a plan does the same thing, so any failure it finds can be replayed.

    plan = FaultPlan(sim, seed=7)
    plan.fail("place_order", kex.NetworkException("timed out"), after_effect=True)
    plan.fill_before_cancel()          # the fill beats the cancel to the exchange
    plan.modify_pending()              # modify lands, order sits in MODIFY PENDING
    plan.hold_postbacks()              # postbacks queue up ...
    plan.release_postbacks("reverse")  # ... then arrive out of order
    plan.duplicate_postbacks(2)        # every postback delivered twice
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from executor.brokers.kitesim.sim import KiteSim


@dataclass
class _Rule:
    call: str
    action: Callable[[dict, Callable[..., object]], object]
    remaining: int | None  # None = every matching call
    match: Callable[[dict], bool] | None = None


@dataclass
class FaultPlan:
    sim: KiteSim
    seed: int = 0
    log: list[tuple[str, dict]] = field(default_factory=list)  # what fired, for assertions

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._rules: list[_Rule] = []
        self._held: list[dict] = []
        self._holding = False
        self._copies = 1
        self._deliver = self.sim._deliver_now
        self._drop: Callable[[dict], bool] | None = None
        self._unsettled: dict[str, str] = {}  # order_id -> status to return to
        self.sim.interceptor = self._intercept
        self.sim.postback_gate = self._gate

    # ── API faults ────────────────────────────────────────────────────────────

    def fail(
        self,
        call: str,
        exc: Exception,
        *,
        times: int | None = 1,
        after_effect: bool = False,
        match: Callable[[dict], bool] | None = None,
    ) -> None:
        """Raise `exc` from `call`. With after_effect the call HAS happened at Kite
        and only the answer is lost (a timeout after an accepted order)."""

        def action(params: dict, proceed: Callable[..., object]) -> object:
            if after_effect:
                proceed()
            raise exc

        self._rules.append(_Rule(call, action, times, match))

    def fail_randomly(self, call: str, exc: Exception, rate: float) -> None:
        """Raise `exc` on a seeded fraction `rate` of `call`s."""

        def action(params: dict, proceed: Callable[..., object]) -> object:
            if self._rng.random() < rate:
                raise exc
            return proceed()

        self._rules.append(_Rule(call, action, None))

    def fill_before_cancel(self, quantity: int | None = None, *, times: int | None = 1) -> None:
        """A fill reaches the exchange before the cancel does. `quantity` None fills
        the order; fewer shares leave it part-filled and the cancel then succeeds."""

        def action(params: dict, proceed: Callable[..., object]) -> object:
            o = self.sim._order(params["order_id"])
            qty = (
                o["pending_quantity"] if quantity is None else min(quantity, o["pending_quantity"])
            )
            price = o["price"] or self.sim._ltp.get(f"{o['exchange']}:{o['tradingsymbol']}", 0.0)
            filled = o["filled_quantity"] + qty
            done = filled >= o["quantity"]
            self.sim._advance(
                o,
                "COMPLETE" if done else "OPEN",
                filled_quantity=filled,
                pending_quantity=o["quantity"] - filled,
                average_price=price,
            )
            self.log.append(("fill_before_cancel", dict(params)))
            return proceed()

        self._rules.append(_Rule("cancel_order", action, times))

    def modify_pending(self, *, times: int | None = 1) -> None:
        """A modify is accepted but the order sits in MODIFY PENDING until settle()."""

        def action(params: dict, proceed: Callable[..., object]) -> object:
            order_id = str(params["order_id"])
            before = self.sim._order(order_id)["status"]
            result = proceed()
            self.sim._advance(self.sim._order(order_id), "MODIFY PENDING")
            self._unsettled[order_id] = before
            self.log.append(("modify_pending", dict(params)))
            return result

        self._rules.append(_Rule("modify_order", action, times))

    def settle(self) -> None:
        """Exchange confirms every pending modify."""
        for order_id, status in list(self._unsettled.items()):
            self.sim.advance(order_id, status)
        self._unsettled.clear()

    def _intercept(self, call: str, params: dict, proceed: Callable[..., object]) -> object:
        for rule in self._rules:
            if rule.call != call or rule.remaining == 0:
                continue
            if rule.match is not None and not rule.match(params):
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
            self.log.append((f"fault:{call}", dict(params)))
            return rule.action(params, proceed)
        return proceed()

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """No API faults inside the block (an observer reading ground truth, not the
        flaky wire). Rules keep their counters; postback faults are unaffected."""
        saved, self.sim.interceptor = self.sim.interceptor, None
        try:
            yield
        finally:
            self.sim.interceptor = saved

    # ── postback faults ───────────────────────────────────────────────────────

    def hold_postbacks(self) -> None:
        """Queue postbacks instead of delivering them (a slow WebSocket)."""
        self._holding = True

    def release_postbacks(self, order: str = "fifo") -> int:
        """Deliver the queue: "fifo", "reverse" or a seeded "shuffle"."""
        self._holding = False
        queue, self._held = self._held, []
        if order == "reverse":
            queue.reverse()
        elif order == "shuffle":
            self._rng.shuffle(queue)
        elif order != "fifo":
            raise ValueError(f"unknown release order {order!r}")
        for payload in queue:
            self._send(payload)
        return len(queue)

    def duplicate_postbacks(self, copies: int = 2) -> None:
        """Deliver every postback `copies` times (Kite resends)."""
        self._copies = copies

    def drop_postbacks(self, match: Callable[[dict], bool] | None = None) -> None:
        """Lose postbacks (all, or those `match` selects): the WebSocket missed them."""
        self._drop = match or (lambda payload: True)

    def _gate(self, payload: dict, deliver: Callable[[dict], None]) -> None:
        if self._drop is not None and self._drop(payload):
            self.log.append(("postback_dropped", {"order_id": payload.get("order_id")}))
            return
        if self._holding:
            self._held.append(payload)
            return
        self._send(payload)

    def _send(self, payload: dict) -> None:
        for _ in range(self._copies):
            self._deliver(payload)
