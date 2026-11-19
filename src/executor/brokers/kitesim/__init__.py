"""KiteSim: Zerodha Kite simulated in-process, so paper runs the real KiteBroker (#86).

Paper trading is `KiteBroker(kite=KiteSim(...), feed=SimFeed(feed, sim),
broker_name=BrokerName.PAPER)`: the same translation layer as live (status
mapping, partial fills, modify/cancel, postbacks, Kite's exceptions), with
KiteSim answering instead of Zerodha. The backtester drives the same pair with
bars (`on_bar`).

    sim = KiteSim(instruments=loader_or_book, cash=..., clock=..., config=KiteSimConfig())
    sim.on_price("INFY", "NSE", 1503.4, ts=...)   # live tick (a jump)
    sim.on_bar(bar)                               # open -> near extreme -> far extreme -> close
    sim.end_of_day()                              # DAY orders expire; close becomes reference
    state = sim.to_state(); sim.load_state(state) # JSON-safe, versioned

Lifecycle, in Kite's strings: LIMIT/MARKET -> OPEN -> (OPEN, part filled) ->
COMPLETE; SL/SL-M -> TRIGGER PENDING -> OPEN (triggered) -> COMPLETE; cancel ->
CANCELLED; RMS refusals -> REJECTED with Kite's status_message; bad input ->
kiteconnect.exceptions.InputException. Rules: kitesim/rules.py.

Determinism: time comes only from the injected clock (or a tick's own
timestamp), randomness only from `random.Random(config.seed)`.

Hostile-mode hooks (#87; no faults are implemented here):
  * `sim.interceptor = f(call, params, proceed)` sees every API call (one
    dispatcher: `SimEngine._dispatch`); it may delay, duplicate, reorder, raise a
    kiteconnect exception, or rewrite the result of `proceed()`.
  * every broker-side order change goes through `SimEngine._advance`, and
    `sim.advance(order_id, status, **fields)` forces one (fills included).
  * every postback goes through `SimEngine._emit` into an outbox that
    `_deliver` flushes outside the lock once the outermost call returns.
"""

from executor.brokers.kitesim.config import KiteSimConfig, bar_path
from executor.brokers.kitesim.feed import SimFeed
from executor.brokers.kitesim.instruments import InstrumentBook
from executor.brokers.kitesim.sim import KiteSim

__all__ = ["InstrumentBook", "KiteSim", "KiteSimConfig", "SimFeed", "bar_path"]
