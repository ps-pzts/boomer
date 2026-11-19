"""Shared fixtures for the backtester simulation tests."""

from __future__ import annotations

import sqlite3
from datetime import date

from backtester.models import BacktestConfig
from executor.models import PriceBar


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0012_events.sql") as f:  # order transitions emit events (#56)
        db.executescript(f.read())
    return db


def _bar(symbol: str, d: str, *, o=100, h=102, lo=98, c=100, vol=100_000) -> PriceBar:
    return PriceBar(symbol=symbol, date=d, open=o, high=h, low=lo, close=c, volume=vol)


def _flat_price_loader(symbol: str, from_date: str, to_date: str) -> list[PriceBar]:
    """Flat ₹100 bar for any symbol/date."""
    return [_bar(symbol, from_date)]


def _empty_feature_loader(symbol: str, as_of_date: str) -> dict:
    return {}


def _make_config(**overrides) -> BacktestConfig:
    defaults = dict(
        name="test",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 12),  # ~8 trading days (excludes Jan 6-7 weekend)
        initial_capital=100_000.0,
        tracks=["intraday"],
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)
