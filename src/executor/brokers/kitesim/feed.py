"""SimFeed: the live market feed, with KiteSim's order postbacks (#86).

Paper KiteBroker takes this as its feed. Ticks, subscriptions and health are the
real feed's; every tick also drives KiteSim's matching (whether or not anyone
called on_tick); `on_order_update` registers with KiteSim, so paper order updates
reach the same callback path live postbacks use. Real-account postbacks from the
live WebSocket never reach a paper session.
"""

from __future__ import annotations

from executor.brokers.feeds.base import (
    FeedCapabilities,
    FeedHealth,
    MarketDataFeed,
    OrderUpdateCallback,
    Tick,
    TickCallback,
)
from executor.brokers.kitesim.sim import KiteSim


class SimFeed(MarketDataFeed):
    def __init__(self, feed: MarketDataFeed, sim: KiteSim) -> None:
        self._feed = feed
        self._sim = sim
        feed.on_tick(self._to_sim)

    def _to_sim(self, tick: Tick) -> None:
        self._sim.on_price(tick.symbol, tick.exchange, tick.ltp, ts=tick.received_at)

    def connect(self) -> None:
        self._feed.connect()

    def close(self) -> None:
        self._feed.close()

    def reauthenticate(self) -> None:
        self._feed.reauthenticate()

    def subscribe(self, symbols: list[str], exchange: str = "NSE") -> None:
        self._feed.subscribe(symbols, exchange)

    def on_tick(self, callback: TickCallback) -> None:
        self._feed.on_tick(callback)

    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        self._sim.subscribe_postbacks(callback)

    def latest(self, symbol: str) -> Tick | None:
        return self._feed.latest(symbol)

    def health(self) -> FeedHealth:
        return self._feed.health()

    def capabilities(self) -> FeedCapabilities:
        return self._feed.capabilities()
