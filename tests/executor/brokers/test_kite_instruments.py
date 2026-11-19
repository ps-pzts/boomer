"""Tests for executor/brokers/kite_instruments.py."""

from __future__ import annotations

import pytest

from executor.brokers.kite_instruments import InstrumentCache


def test_loads_once_per_exchange_and_caches():
    calls: list[str] = []

    def loader(exchange: str) -> list[dict]:
        calls.append(exchange)
        return [{"tradingsymbol": "KPEL", "instrument_token": 111 if exchange == "NSE" else 222}]

    cache = InstrumentCache(loader)
    assert cache.token("KPEL", "NSE") == 111
    assert cache.token("KPEL", "NSE") == 111
    assert cache.token("KPEL", "BSE") == 222  # per exchange, not global (#23)
    assert calls == ["NSE", "BSE"]


def test_unknown_symbol_raises_value_error():
    cache = InstrumentCache(lambda ex: [])
    with pytest.raises(ValueError, match="NSE:NOPE"):
        cache.token("NOPE", "NSE")


def test_clear_forces_reload():
    calls: list[str] = []

    def loader(exchange: str) -> list[dict]:
        calls.append(exchange)
        return [{"tradingsymbol": "KPEL", "instrument_token": "111"}]  # SDK may give str

    cache = InstrumentCache(loader)
    assert cache.token("KPEL", "NSE") == 111
    cache.clear()
    cache.token("KPEL", "NSE")
    assert calls == ["NSE", "NSE"]
