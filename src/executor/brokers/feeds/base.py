"""MarketDataFeed abstraction and the provider-neutral Tick record.

Design: designs/intraday-live-pipeline.md §3.1. No provider payload leaks past an
adapter: everything downstream (LiveBarStore, LiveFeatureComputer, the paper broker)
only ever sees Tick.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# NIFTY 50 is always subscribed: it feeds nifty_intraday_direction and doubles as
# the heartbeat instrument for the feed health check.
NIFTY_50 = "NIFTY 50"

TickCallback = Callable[["Tick"], None]
OrderUpdateCallback = Callable[[dict], None]


class FeedMode(StrEnum):
    QUOTE = "quote"  # LTP, volume, VWAP, day OHLC
    FULL = "full"  # QUOTE + market depth (needed for bid_ask_spread_pct)


@dataclass(frozen=True)
class Tick:
    symbol: str
    exchange: str
    ltp: float
    received_at: datetime  # IST wall-clock time the tick reached us
    volume_traded: int | None = None  # cumulative day volume
    avg_traded_price: float | None = None  # exchange VWAP for the day
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    best_bid: float | None = None  # None unless FULL mode (or an index)
    best_ask: float | None = None


@dataclass(frozen=True)
class FeedCapabilities:
    has_depth: bool
    has_index: bool
    has_historical: bool  # False on Kite Personal — see design §1b


@dataclass(frozen=True)
class FeedHealth:
    connected: bool
    last_tick_at: datetime | None
    subscribed: int

    def tick_age_seconds(self, now: datetime) -> float | None:
        if self.last_tick_at is None:
            return None
        return (now - self.last_tick_at).total_seconds()

    def is_fresh(self, now: datetime, max_age_seconds: float) -> bool:
        age = self.tick_age_seconds(now)
        return self.connected and age is not None and age <= max_age_seconds


class MarketDataFeed(ABC):
    """A live price source. Thread-safety: callbacks fire on the feed's own thread."""

    @abstractmethod
    def connect(self) -> None:
        """Open the connection. Subscriptions requested earlier are applied on connect."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def reauthenticate(self) -> None:
        """Reconnect with fresh credentials (after the daily token refresh), keeping
        every subscription. Design loophole L6."""

    @abstractmethod
    def subscribe(self, symbols: list[str], exchange: str = "NSE") -> None: ...

    @abstractmethod
    def on_tick(self, callback: TickCallback) -> None: ...

    @abstractmethod
    def on_order_update(self, callback: OrderUpdateCallback) -> None: ...

    @abstractmethod
    def latest(self, symbol: str) -> Tick | None:
        """Most recent tick for symbol, any age (callers judge staleness)."""

    @abstractmethod
    def health(self) -> FeedHealth: ...

    @abstractmethod
    def capabilities(self) -> FeedCapabilities: ...
