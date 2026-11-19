"""In-process feed driven by push(): for tests and for replaying recorded ticks."""

from __future__ import annotations

import threading

from executor.brokers.feeds.base import (
    FeedCapabilities,
    FeedHealth,
    MarketDataFeed,
    OrderUpdateCallback,
    Tick,
    TickCallback,
)


class ManualFeed(MarketDataFeed):
    def __init__(self, has_depth: bool = True) -> None:
        self._has_depth = has_depth
        self._connected = False
        self._subscribed: set[str] = set()
        self._latest: dict[str, Tick] = {}
        self._tick_callbacks: list[TickCallback] = []
        self._order_callbacks: list[OrderUpdateCallback] = []
        self._lock = threading.Lock()

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def reauthenticate(self) -> None:
        self._connected = True

    def subscribe(self, symbols: list[str], exchange: str = "NSE") -> None:
        self._subscribed.update(symbols)

    def on_tick(self, callback: TickCallback) -> None:
        self._tick_callbacks.append(callback)

    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        self._order_callbacks.append(callback)

    def latest(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest.get(symbol)

    def health(self) -> FeedHealth:
        with self._lock:
            last = max((t.received_at for t in self._latest.values()), default=None)
        return FeedHealth(
            connected=self._connected, last_tick_at=last, subscribed=len(self._subscribed)
        )

    def capabilities(self) -> FeedCapabilities:
        return FeedCapabilities(has_depth=self._has_depth, has_index=True, has_historical=False)

    # ── Test / replay driver ─────────────────────────────────────────────────

    def push(self, tick: Tick) -> None:
        with self._lock:
            self._latest[tick.symbol] = tick
        for cb in self._tick_callbacks:
            cb(tick)

    def push_order_update(self, data: dict) -> None:
        for cb in self._order_callbacks:
            cb(data)

    @property
    def subscribed(self) -> set[str]:
        return set(self._subscribed)
