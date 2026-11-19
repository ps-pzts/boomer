"""Intraday trading universe: EQ-series stocks that have stock futures (F&O) — #50.

Why F&O only: these ~180-200 stocks must meet exchange eligibility criteria
(market cap, liquidity, delivery), have no daily price bands, deep order books
and heavy arbitrage, so they are the hardest to manipulate. A turnover floor
alone can't protect against pump-and-dump: manipulation *creates* volume.

Source: `fo_oi_daily`, already filled nightly from the NSE F&O bhavcopy. A
symbol is in F&O if it has a `FUT` row on the latest trade date strictly before
the session date (point in time: the nightly run loads the previous session's
file, so a row dated on or after the session would be lookahead). Index futures
(NIFTY, BANKNIFTY, ...) drop out because no EQ instrument carries those names.

Fail closed: if the newest F&O data is older than MAX_FNO_AGE_TRADING_DAYS
(F&O membership changes rarely, but a long collector outage must not be
ignored), the universe is empty and the caller alerts. It never falls back to
the unfiltered equity universe.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from market_calendar.nse_holidays import prev_trading_day

logger = logging.getLogger(__name__)

MAX_FNO_AGE_TRADING_DAYS = 5


@dataclass(frozen=True)
class Universe:
    symbols: list[str]  # sorted; empty when unavailable
    equity_count: int  # EQ-series instruments considered
    fno_count: int  # F&O stock underlyings found
    fno_as_of: date | None  # trade date the F&O list comes from
    unavailable_reason: str | None  # set when the universe is empty for a data reason


def _oldest_acceptable(session_date: date) -> date:
    d = session_date
    for _ in range(MAX_FNO_AGE_TRADING_DAYS):
        d = prev_trading_day(d)
    return d


def fno_underlyings(db_path: str | Path, session_date: date) -> tuple[set[str], date | None]:
    """Stock-futures underlyings on the latest trade date before `session_date`."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM fo_oi_daily"
            " WHERE instrument_type = 'FUT' AND trade_date < ?",
            (session_date.isoformat(),),
        ).fetchone()
        if not row or not row[0]:
            return set(), None
        latest = row[0]
        symbols = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT underlying_symbol FROM fo_oi_daily"
                " WHERE instrument_type = 'FUT' AND trade_date = ?",
                (latest,),
            )
        }
    finally:
        conn.close()
    return symbols, date.fromisoformat(latest)


def intraday_universe(db_path: str | Path, session_date: date) -> Universe:
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        equities = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT nse_symbol FROM instruments"
                " WHERE series = 'EQ' AND nse_symbol IS NOT NULL"
            )
        }
    finally:
        conn.close()

    fno, fno_as_of = fno_underlyings(db_path, session_date)
    reason = None
    if fno_as_of is None:
        reason = "no_fno_data"
    elif fno_as_of < _oldest_acceptable(session_date):
        reason = f"fno_data_stale:{fno_as_of.isoformat()}"
    symbols = [] if reason else sorted(equities & fno)

    logger.info(
        "intraday_universe session=%s equities=%d fno=%d fno_as_of=%s size=%d reason=%s",
        session_date,
        len(equities),
        len(fno),
        fno_as_of,
        len(symbols),
        reason,
    )
    return Universe(symbols, len(equities), len(fno), fno_as_of, reason)
