"""Integration harness (#88): the real Order and Trade machines and the real paper
KiteBroker on KiteSim, a fake clock, and a FaultPlan to make the broker misbehave.

    h = Hostile(tmp_path)
    h.plan.fill_before_cancel()
    h.check()          # every cross-machine invariant, raises AssertionError listing breaches

Invariants (checked after every step of a scenario):
  I1  an OPEN trade has a resting stop at the broker
  I2  trades' net quantity per symbol == the broker's net position
  I3  no resting order at the broker that no trade tracks
  I5  a resting stop's trigger never moves down (CLAUDE.md §10)
  I6  every long position is covered by resting stops of at least its size,
      unless its trade is protecting or exiting (the Q5 'never without a stop' rule)
  I4  at end of day: broker flat, nothing resting, every trade terminal
"""

from __future__ import annotations

from pathlib import Path

from executor.brokers.kitesim.faults import FaultPlan
from executor.models import OrderStatus
from tests.executor.test_trade_lifecycle import Harness

_TERMINAL = ("closed", "cancelled")
_LIVE_ORDER_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.TRIGGERED.value,
    OrderStatus.PARTIAL.value,
    OrderStatus.SUBMITTING.value,
)


class Hostile(Harness):
    def __init__(self, tmp_path: Path, seed: int = 0) -> None:
        super().__init__(tmp_path)
        self.plan = FaultPlan(self.paper._kite, seed=seed)
        self.sim = self.paper._kite
        self._stop_high: dict[str, float] = {}

    def wire_postbacks(self) -> None:
        """Order updates reach OrderManager the way the live WebSocket's do."""
        self.paper.on_order_update(self.om.handle_broker_update)

    def step(self) -> None:
        self.sup.reconcile()
        self.check()

    # ── invariants ────────────────────────────────────────────────────────────

    def breaches(self, end_of_day: bool = False) -> list[str]:
        with self.plan.suspended():  # the observer sees ground truth, not the faulty wire
            return self._breaches(end_of_day)

    def _breaches(self, end_of_day: bool) -> list[str]:
        out: list[str] = []
        for _, symbol, leg, _, resting in self.sup.open_legs():
            if leg == "stop" and not resting:
                out.append(f"I1 open trade in {symbol} has no resting stop")
        broker_net = {p.symbol: p.quantity for p in self.paper.list_positions() if p.quantity}
        tracked = self.sup.net_positions()
        if broker_net != tracked:
            out.append(f"I2 broker net {broker_net} != trades' net {tracked}")
        known = {
            r[0]
            for r in self.db.execute(
                "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL "
                f"AND status IN ({','.join('?' * len(_LIVE_ORDER_STATUSES))})",
                _LIVE_ORDER_STATUSES,
            )
        }
        for o in self.paper.list_open_orders():
            if o["broker_order_id"] not in known:
                out.append(f"I3 untracked resting order {o}")
        for o in self.sim.orders():
            if o["order_type"] in ("SL", "SL-M") and o["status"] in ("TRIGGER PENDING", "OPEN"):
                trig = float(o["trigger_price"])
                seen = self._stop_high.get(o["order_id"], trig)  # per order: trailing modifies it
                if trig < seen:
                    out.append(
                        f"I5 stop {o['order_id']} for {o['tradingsymbol']} lowered {seen} -> {trig}"
                    )
                self._stop_high[o["order_id"]] = max(seen, trig)
        transitional = {
            r[0]
            for r in self.db.execute(
                "SELECT symbol FROM intraday_brackets WHERE state IN ('protecting','exiting')"
            )
        }
        for symbol, qty in broker_net.items():
            cover = sum(
                o["pending_quantity"]
                for o in self.sim.orders()
                if o["tradingsymbol"] == symbol
                and o["order_type"] in ("SL", "SL-M")
                and o["transaction_type"] == "SELL"
                and o["status"] in ("TRIGGER PENDING", "OPEN", "MODIFY PENDING")
            )
            if qty > 0 and cover < qty and symbol not in transitional:
                out.append(f"I6 {symbol}: long {qty} but resting stops cover {cover}")
        if end_of_day:
            if broker_net:
                out.append(f"I4 broker not flat: {broker_net}")
            if self.paper.list_open_orders():
                out.append("I4 orders still resting at the broker")
            live = self.db.execute(
                "SELECT COUNT(*) FROM intraday_brackets WHERE state NOT IN ('closed','cancelled')"
            ).fetchone()[0]
            if live:
                out.append(f"I4 {live} trades not terminal")
        return out

    def check(self, end_of_day: bool = False) -> None:
        found = self.breaches(end_of_day)
        assert not found, "invariants broken:\n  " + "\n  ".join(found)


def attach_book(h: Hostile) -> None:
    """Persist KiteSim into the paper book after every change (what paper mode does)."""
    from executor.brokers.paper_book import PaperBook

    book = PaperBook(h.db.execute("PRAGMA database_list").fetchone()[2], h.clock.now.date())
    h.sim.on_change = book.save


def process_restart(h: Hostile) -> None:
    """Kill the process and boot a new one: a new KiteSim restored from the paper book, a
    new feed, OrderManager, PositionManager and supervisor on the same database."""
    from executor.brokers.feeds.manual_feed import ManualFeed
    from executor.brokers.kitesim.paper import build_paper_broker
    from executor.brokers.paper_book import PaperBook
    from executor.models import BrokerName
    from executor.order_manager import OrderManager
    from executor.position_manager import PositionManager
    from executor.trade_lifecycle import TradeSupervisor
    from tests.kitesim_kit import fixture_instruments

    book = PaperBook(h.db.execute("PRAGMA database_list").fetchone()[2], h.clock.now.date())
    h.feed = ManualFeed()
    h.paper = build_paper_broker(
        h.feed, 1_000_000, instruments=fixture_instruments(), book=book, clock=h.clock
    )
    h.sim = h.paper._kite
    h.plan = FaultPlan(h.sim, seed=0)
    h.om = OrderManager(h.db, {BrokerName.KITE: h.paper}, ltp_cache={}, clock=h.clock)
    h.pm = PositionManager(h.db, order_manager=h.om)
    h.sup = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
