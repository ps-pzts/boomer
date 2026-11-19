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
        self._ticks: dict[str, dict[str, float]] = {}
        self._rows: dict[str, dict[str, dict]] = {}

    def _load(self, exchange: str) -> dict[str, int]:
        cache = self._by_exchange.get(exchange)
        if cache is None:
            rows = self._loader(exchange)
            cache = {inst["tradingsymbol"]: int(inst["instrument_token"]) for inst in rows}
            self._ticks[exchange] = {
                inst["tradingsymbol"]: float(inst["tick_size"])
                for inst in rows
                if inst.get("tick_size")
            }
            self._rows[exchange] = {inst["tradingsymbol"]: inst for inst in rows}
            self._by_exchange[exchange] = cache
        return cache

    def token(self, symbol: str, exchange: str) -> int:
        token = self._load(exchange).get(symbol)
        if token is None:
            raise ValueError(f"Instrument not found: {exchange}:{symbol}")
        return token

    def tick_size(self, symbol: str, exchange: str) -> float | None:
        """The instrument's price tick from Kite's dump (#75); None if unknown."""
        self._load(exchange)
        return self._ticks.get(exchange, {}).get(symbol)

    def row(self, symbol: str, exchange: str) -> dict | None:
        """The raw dump row (for the pre-flight shape check, #85); None if unknown."""
        self._load(exchange)
        return self._rows.get(exchange, {}).get(symbol)

    def clear(self) -> None:
        self._by_exchange.clear()
        self._ticks.clear()
        self._rows.clear()
