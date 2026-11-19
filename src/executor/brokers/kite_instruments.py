"""Per-exchange instrument-token cache shared by KiteBroker and KiteFeed.

Kite's instrument dump is REST (available on the Personal plan) and does not
change intraday, so it is loaded once per exchange per session.
"""

from __future__ import annotations

from collections.abc import Callable

InstrumentLoader = Callable[[str], list[dict]]  # exchange -> kite.instruments(exchange)


class InstrumentCache:
    def __init__(self, loader: InstrumentLoader) -> None:
        self._loader = loader
        self._by_exchange: dict[str, dict[str, int]] = {}

    def token(self, symbol: str, exchange: str) -> int:
        cache = self._by_exchange.get(exchange)
        if cache is None:
            cache = {
                inst["tradingsymbol"]: int(inst["instrument_token"])
                for inst in self._loader(exchange)
            }
            self._by_exchange[exchange] = cache
        token = cache.get(symbol)
        if token is None:
            raise ValueError(f"Instrument not found: {exchange}:{symbol}")
        return token

    def clear(self) -> None:
        self._by_exchange.clear()
