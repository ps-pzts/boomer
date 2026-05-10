from __future__ import annotations

from collections.abc import Callable

from executor.brokers.base import Broker
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

    Simulates fills using the live Kite tick feed (price_source) so that
    paper trades experience real market prices without real money at risk.

    PaperBroker.authenticate() delegates to price_source.authenticate().
    Paper trading fails if the Kite session is invalid — the price feed
    depends on a valid Kite session even when no real orders are placed.
    """

    def __init__(self, price_source: Broker, initial_cash: float = 100_000.0) -> None:
        self._price_source = price_source
        self._mock = MockBroker(initial_cash=initial_cash)
        self._ltp: dict[str, float] = {}

    @property
    def broker_id(self) -> BrokerName:
        return BrokerName.PAPER

    def authenticate(self) -> None:
        self._price_source.authenticate()

    def register_tick(self, symbol: str, ltp: float) -> None:
        """Called by price_source's on_tick callback to feed live prices."""
        self._ltp[symbol] = ltp
        bar = PriceBar(symbol=symbol, date="", open=ltp, high=ltp, low=ltp, close=ltp, volume=0)
        self._mock.set_price_bar(bar)

    def place_order(self, request: OrderRequest) -> str:
        return self._mock.place_order(request)

    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        return self._mock.modify_order(broker_order_id, changes)

    def cancel_order(self, broker_order_id: str) -> None:
        self._mock.cancel_order(broker_order_id)

    def get_order_status(self, broker_order_id: str) -> dict:
        return self._mock.get_order_status(broker_order_id)

    def list_positions(self) -> list[BrokerPosition]:
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
        # Delegate tick subscription to the real price source (KiteBroker)
        def _relay(sym: str, ltp: float) -> None:
            self.register_tick(sym, ltp)
            callback(sym, ltp)

        self._price_source.on_tick(symbols, _relay)

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
        return self._ltp.get(symbol)
