"""Intraday trading universe: EQ-series F&O stocks not under ASM/GSM surveillance — #50.

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

Surveillance (ASM/GSM, brain/surveillance.py) is then subtracted. If that list is
missing or older than 3 trading days the universe is F&O-only and `surveillance_status`
says so, so the caller alerts (CRITICAL); 1-3 days old applies the last list + WARN.

Symbol matching: fo_oi_daily's underlying and `instruments.nse_symbol` are both NSE
trading symbols (so "M&M" matches "M&M"); both sides are stripped and upper-cased.
F&O underlyings that match no EQ instrument and are not a known index are returned in
`fno_unmatched` (a rename or a series change) so the caller can alert instead of
the universe shrinking silently. Rows are never matched by guessing.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from brain.surveillance import FRESH, load_surveillance, matched
from market_calendar.nse_holidays import prev_trading_day

logger = logging.getLogger(__name__)

MAX_FNO_AGE_TRADING_DAYS = 5
# Index-futures underlyings sit in fo_oi_daily as FUT rows but have no EQ instrument.
INDEX_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})


@dataclass(frozen=True)
class Universe:
    symbols: list[str]  # sorted; empty when unavailable
    equity_count: int  # EQ-series instruments considered
    fno_count: int  # F&O stock underlyings found
    fno_as_of: date | None  # trade date the F&O list comes from
    unavailable_reason: str | None  # set when the universe is empty for a data reason
    not_fno_count: int = 0  # EQ instruments with no stock futures (rejected: not_fno)
    under_surveillance: list[str] = field(default_factory=list)  # F&O EQ names removed (ASM/GSM)
    surveillance_status: str = FRESH  # fresh | stale | unavailable (brain/surveillance.py)
    surveillance_as_of: date | None = None
    surveillance_age: int | None = None
    fno_unmatched: list[str] = field(default_factory=list)  # F&O stock underlyings with no EQ row


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


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def intraday_universe(db_path: str | Path, session_date: date) -> Universe:
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        equities = {
            _norm(r[0]): r[0]
            for r in conn.execute(
                "SELECT DISTINCT nse_symbol FROM instruments"
                " WHERE series = 'EQ' AND nse_symbol IS NOT NULL"
            )
        }
    finally:
        conn.close()

    fno_raw, fno_as_of = fno_underlyings(db_path, session_date)
    fno = {_norm(s) for s in fno_raw}
    surv = load_surveillance(db_path, session_date)

    reason = None
    if fno_as_of is None:
        reason = "no_fno_data"
    elif fno_as_of < _oldest_acceptable(session_date):
        reason = f"fno_data_stale:{fno_as_of.isoformat()}"

    in_fno = {n for n in equities if n in fno}
    unmatched = sorted(fno - set(equities) - INDEX_UNDERLYINGS)
    removed = {_norm(sym) for sym, _ in matched(db_path, surv)} & in_fno
    if reason is None and not in_fno:
        reason = "no_fno_equity_match"  # fo_oi_daily and the equity master share no symbol
    symbols = [] if reason else sorted(equities[n] for n in in_fno - removed)
    excluded = sorted(equities[n] for n in removed)

    logger.info(
        "intraday_universe session=%s equities=%d fno=%d fno_as_of=%s not_fno=%d"
        " under_surveillance=%s surveillance=%s/%s age=%s fno_unmatched=%s size=%d reason=%s",
        session_date,
        len(equities),
        len(fno),
        fno_as_of,
        len(equities) - len(in_fno),
        excluded,
        surv.status,
        surv.as_of,
        surv.age_trading_days,
        unmatched,
        len(symbols),
        reason,
    )
    return Universe(
        symbols,
        len(equities),
        len(fno),
        fno_as_of,
        reason,
        not_fno_count=len(equities) - len(in_fno),
        under_surveillance=[] if reason else excluded,
        surveillance_status=surv.status,
        surveillance_as_of=surv.as_of,
        surveillance_age=surv.age_trading_days,
        fno_unmatched=unmatched,
    )
