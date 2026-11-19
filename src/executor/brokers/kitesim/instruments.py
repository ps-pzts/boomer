"""Instrument rows for KiteSim: the same dump KiteBroker live loads (#86).

`InstrumentBook(loader)` wraps `loader(exchange) -> rows` (live: the real
KiteBroker's `kite.instruments(exchange)`; tests: the fixture CSV). With
`synthesize_unknown=True` a symbol missing from the dump (or a dump that can't be
loaded) gets a stand-in row on the default tick instead of being refused.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_TICK = 0.05


class InstrumentBook:
    def __init__(
        self,
        loader: Callable[[str], list[dict]] | None = None,
        synthesize_unknown: bool = False,
    ) -> None:
        self._loader = loader
        self._synth = synthesize_unknown or loader is None
        self._rows: dict[str, dict[str, dict]] = {}

    def rows(self, exchange: str) -> list[dict]:
        return [dict(r) for r in self._load(exchange).values()]

    def row(self, symbol: str, exchange: str) -> dict | None:
        found = self._load(exchange).get(symbol)
        if found is not None:
            return found
        return synthetic_row(symbol, exchange) if self._synth else None

    def _load(self, exchange: str) -> dict[str, dict]:
        cached = self._rows.get(exchange)
        if cached is not None:
            return cached
        rows: list[dict] = []
        if self._loader is not None:
            try:
                rows = list(self._loader(exchange))
            except Exception as exc:  # no dump: unknown symbols fall back (if allowed)
                logger.warning(
                    "kitesim_instruments_unavailable exchange=%s error=%s", exchange, exc
                )
                self._synth = True
                return {}
        cached = {r["tradingsymbol"]: r for r in rows}
        self._rows[exchange] = cached
        return cached


def synthetic_row(symbol: str, exchange: str) -> dict:
    """A dump-shaped row for an instrument the dump doesn't list."""
    token = zlib.crc32(f"{exchange}:{symbol}".encode()) & 0x7FFFFFFF
    return {
        "instrument_token": token,
        "exchange_token": str(token >> 8),
        "tradingsymbol": symbol,
        "name": symbol,
        "last_price": 0.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": DEFAULT_TICK,
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": exchange,
        "exchange": exchange,
    }
