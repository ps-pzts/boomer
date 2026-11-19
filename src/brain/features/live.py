"""Live (intraday) features computed at signal time from ticks and minute bars.

Design: designs/intraday-live-pipeline.md §3.3-3.4. Every function here is
pure: same inputs, same output, no I/O except latest_news_at().

These values are NEVER written to the `features` table. That table is keyed by
date, so a 14:00 value would be visible to any same-day query made "as of"
09:30, which is a lookahead bug (CLAUDE.md §9, design L10). They live in memory
for one cycle and are persisted per signal in signals.feature_snapshot.

compute_live_features() omits keys whose value is unknown instead of setting
them to None: the classifier does float(features.get(k, default)), which would
raise on an explicit None.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from executor.brokers.feeds.base import Tick

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
ORB_MINUTES = 15  # opening range = bars 09:15 .. 09:29
NIFTY_DEADBAND_PCT = 0.10  # |move| <= 0.10% vs prev close counts as flat


class _Bar(Protocol):
    minute: datetime
    high: float
    low: float


def _open_at(now: datetime) -> datetime:
    return now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)


def premarket_gap_pct(tick: Tick | None) -> float | None:
    """(day_open - prev_close) / prev_close x 100. Needs the 09:15 open print."""
    if tick is None or tick.day_open is None or tick.prev_close is None:
        return None
    return (tick.day_open - tick.prev_close) / tick.prev_close * 100.0


def opening_range(
    bars: Sequence[_Bar], coverage_start: datetime | None, now: datetime
) -> tuple[float, float] | None:
    """(high, low) of the 09:15-09:29 bars, or None if it can't be trusted yet.

    None when: the window hasn't closed (now < 09:30); recording started after
    09:15:59, e.g. a mid-session restart (L7), so early bars may be missing; or
    the symbol never traded in the window.
    """
    start = _open_at(now)
    end = start + timedelta(minutes=ORB_MINUTES)
    if now < end:
        return None
    if coverage_start is None or coverage_start >= start + timedelta(minutes=1):
        return None
    window = [b for b in bars if start <= b.minute < end]
    if not window:
        return None
    return max(b.high for b in window), min(b.low for b in window)


def orb_range_vs_atr(orb: tuple[float, float] | None, atr_14d: float | None) -> float | None:
    """(orb_high - orb_low) / atr_14d. Design Q4: ATR, not a 20-day ORB average."""
    if orb is None or atr_14d is None or atr_14d <= 0:
        return None
    high, low = orb
    return (high - low) / atr_14d


def price_vs_open_pct(tick: Tick | None) -> float | None:
    if tick is None or tick.day_open is None:
        return None
    return (tick.ltp - tick.day_open) / tick.day_open * 100.0


def nifty_intraday_direction(
    index_tick: Tick | None, deadband_pct: float = NIFTY_DEADBAND_PCT
) -> int | None:
    """+1 / -1 if NIFTY 50 is more than deadband_pct above / below prev close, else 0."""
    if index_tick is None or index_tick.prev_close is None:
        return None
    move = (index_tick.ltp - index_tick.prev_close) / index_tick.prev_close * 100.0
    if move > deadband_pct:
        return 1
    if move < -deadband_pct:
        return -1
    return 0


def bid_ask_spread_pct(tick: Tick | None) -> float | None:
    """(ask - bid) / mid x 100 from the best depth level; None without depth."""
    if tick is None or tick.best_bid is None or tick.best_ask is None:
        return None
    if tick.best_ask < tick.best_bid:
        return None  # crossed book snapshot: not a usable quote
    mid = (tick.best_bid + tick.best_ask) / 2.0
    return (tick.best_ask - tick.best_bid) / mid * 100.0


def minutes_since_market_open(now: datetime) -> float:
    return max(0.0, (now - _open_at(now)).total_seconds() / 60.0)


def minutes_since(event_at: datetime | None, now: datetime) -> float | None:
    if event_at is None:
        return None
    return max(0.0, (now - event_at).total_seconds() / 60.0)


def latest_news_at(db_path: str | Path, symbol: str, exchange: str = "NSE") -> datetime | None:
    """Most recent filings.observed_at for symbol, as an aware IST datetime.

    observed_at is written by collector.base._fmt_dt as IST wall-clock time with a
    trailing "Z" that does NOT mean UTC (design L13, verified 2026-11-19). The
    suffix is stripped and the value is read as IST.
    """
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = conn.execute(
            "SELECT MAX(observed_at) FROM filings WHERE stock_symbol = ? AND exchange = ?",
            (symbol, exchange),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0].rstrip("Z")).replace(tzinfo=IST)


def compute_live_features(
    tick: Tick | None,
    index_tick: Tick | None,
    bars: Sequence[_Bar],
    coverage_start: datetime | None,
    batch_features: dict[str, Any],
    latest_news: datetime | None,
    now: datetime,
) -> dict[str, float]:
    """All live features for one symbol at `now`. Unknown values are omitted."""
    orb = opening_range(bars, coverage_start, now)
    atr = batch_features.get("atr_14d")
    values: dict[str, float | int | None] = {
        "premarket_gap_pct": premarket_gap_pct(tick),
        "orb_high": orb[0] if orb else None,
        "orb_low": orb[1] if orb else None,
        "orb_range_vs_atr": orb_range_vs_atr(orb, float(atr) if atr is not None else None),
        "vwap_current": tick.avg_traded_price if tick else None,
        "ltp": tick.ltp if tick else None,
        "price_vs_open_pct": price_vs_open_pct(tick),
        "nifty_intraday_direction": nifty_intraday_direction(index_tick),
        "bid_ask_spread_pct": bid_ask_spread_pct(tick),
        "minutes_since_latest_news": minutes_since(latest_news, now),
        "minutes_since_market_open": minutes_since_market_open(now),
    }
    return {k: float(v) for k, v in values.items() if v is not None}
