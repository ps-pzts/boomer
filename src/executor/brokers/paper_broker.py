from __future__ import annotations

import threading
from collections.abc import Callable

from executor.brokers.base import Broker
from executor.brokers.feeds.base import MarketDataFeed, Tick
from executor.brokers.mock_broker import MockBroker
from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    GttRequest,
    OrderRequest,
    PriceBar,
)


class PaperBroker(Broker):
    """
    Paper trading broker.

    Simulates fills against the live market-data feed (design §3.1, §5) so
    paper trades see real prices without real money at risk. Every tick on the
    feed reaches the simulator, whether or not anyone called on_tick().

    The feed's lifecycle (connect, reauthenticate after the daily token refresh)
    is owned by the orchestrator; authenticate() here only checks a feed exists.

    Threading: fills happen on the feed thread, orders come from task threads, so
    every touch of the simulator holds one lock. With a `book` (#64) the whole
    book is persisted after each change and restored at construction.
    """

    def __init__(
        self,
        price_source: MarketDataFeed,
        initial_cash: float = 100_000.0,
        book: object | None = None,
    ) -> None:
        self._price_source = price_source
        self._mock = MockBroker(initial_cash=initial_cash)
        self._ltp: dict[str, float] = {}
        self._lock = threading.RLock()
        self._book = book
        if book is not None:
            self._restore(book.load())  # type: ignore[attr-defined]
            self._mock.on_order_update(lambda _o: self._save())  # fills, triggers
        price_source.on_tick(self._on_feed_tick)

    def _restore(self, state: dict | None) -> None:
        if not state:
            return
        self._mock._cash = state["cash"]
        self._mock._orders = state["orders"]
        self._mock._positions = state["positions"]

    def _save(self) -> None:
        if self._book is None:
            return
        with self._lock:
            state = {
                "cash": self._mock._cash,
                "orders": self._mock._orders,
                "positions": self._mock._positions,
            }
            self._book.save(state)  # type: ignore[attr-defined]

    def _on_feed_tick(self, tick: Tick) -> None:
        self.register_tick(tick.symbol, tick.ltp)

    @property
    def broker_id(self) -> BrokerName:
        return BrokerName.PAPER

    def authenticate(self) -> None:
        # Nothing to log in to: orders are simulated. Prices come from the feed.
        return None

    def register_tick(self, symbol: str, ltp: float) -> None:
        """Called by price_source's on_tick callback to feed live prices."""
        self._ltp[symbol] = ltp
        bar = PriceBar(symbol=symbol, date="", open=ltp, high=ltp, low=ltp, close=ltp, volume=0)
        with self._lock:
            self._mock.set_price_bar(bar)

    def place_order(self, request: OrderRequest) -> str:
        with self._lock:
            oid = self._mock.place_order(request)
            self._save()
            return oid

    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        with self._lock:
            oid = self._mock.modify_order(broker_order_id, changes)
            self._save()
            return oid

    def cancel_order(self, broker_order_id: str) -> None:
        with self._lock:
            self._mock.cancel_order(broker_order_id)
            self._save()

    def get_order_status(self, broker_order_id: str) -> dict:
        with self._lock:
            return dict(self._mock.get_order_status(broker_order_id))

    def list_positions(self) -> list[BrokerPosition]:
        with self._lock:
            return self._mock.list_positions()

    def list_holdings(self) -> list[BrokerPosition]:
        return self._mock.list_holdings()

    def get_funds(self) -> BrokerFunds:
        funds = self._mock.get_funds()
        return BrokerFunds(
            available_cash=funds.available_cash,
            used_margin=funds.used_margin,
            broker_id=BrokerName.PAPER,
        )

    def on_order_update(self, callback: Callable[[dict], None]) -> None:
        self._mock.on_order_update(callback)

    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        # The simulator already receives every feed tick (see __init__); this only
        # subscribes the symbols and relays them to the caller.
        wanted = set(symbols)
        self._price_source.subscribe(symbols)
        self._price_source.on_tick(
            lambda t: callback(t.symbol, t.ltp) if t.symbol in wanted else None
        )

    def place_gtt(self, request: GttRequest) -> str:
        return self._mock.place_gtt(request)

    def modify_gtt(self, broker_gtt_id: str, changes: dict) -> None:
        self._mock.modify_gtt(broker_gtt_id, changes)

    def cancel_gtt(self, broker_gtt_id: str) -> None:
        self._mock.cancel_gtt(broker_gtt_id)

    def get_gtt(self, broker_gtt_id: str) -> dict:
        return self._mock.get_gtt(broker_gtt_id)

    def list_gtts(self) -> list[dict]:
        return self._mock.list_gtts()

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        if symbol in self._ltp:
            return self._ltp[symbol]
        tick = self._price_source.latest(symbol)
        return tick.ltp if tick else None
