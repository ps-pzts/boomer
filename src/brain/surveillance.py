"""NSE ASM / GSM surveillance exclusion — read side of #50 part 2.

The nightly collector writes `surveillance_list` (collector/fetchers/surveillance.py).
This module answers "which symbols are under surveillance as of this session, and can
that answer be trusted?".

Freshness is measured in trading days between the list's fetch date (IST) and the
session date, per list type; the older of ASM and GSM decides:

    age 0        FRESH        fetched today (the normal case: the collector runs 07:30)
    age 1..3     STALE        the last list still applies, the caller sends a WARN
    age > 3      UNAVAILABLE  too old to trust (or never fetched) -> the caller trades
                              F&O-only and sends a CRITICAL
    a type missing entirely -> UNAVAILABLE

The system never silently runs unfiltered: UNAVAILABLE is always surfaced. Operator
decision #50 (2026-11-19): F&O-only while the list is unavailable.

Point in time: only list_date <= session date is read. Matching is by normalised
symbol, and also by ISIN when the list carries one, so a stock renamed on one side
(equity master vs surveillance list) is still caught.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from market_calendar.nse_holidays import prev_trading_day

logger = logging.getLogger(__name__)

LIST_TYPES = ("ASM", "GSM")
MAX_STALE_TRADING_DAYS = 3  # up to here: use the last list + WARN; beyond: UNAVAILABLE

FRESH = "fresh"
STALE = "stale"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SurveillanceSnapshot:
    status: str  # FRESH | STALE | UNAVAILABLE
    as_of: date | None  # older of the ASM / GSM list dates; None if either is missing
    age_trading_days: int | None
    symbols: frozenset[str] = field(default_factory=frozenset)  # upper-case, ASM | GSM
    isins: frozenset[str] = frozenset()
    missing_types: tuple[str, ...] = ()  # list types never fetched


def trading_age(list_date: date, session_date: date, cap: int = 30) -> int:
    """Trading days from `list_date` forward to `session_date` (0 = same day).

    A list dated on a non-trading day counts from the next session: Sat list, Mon
    session -> 1. Capped so a years-old list costs a bounded loop.
    """
    d, steps = session_date, 0
    while d > list_date and steps < cap:
        d = prev_trading_day(d)
        steps += 1
    return steps


def load_surveillance(db_path: str | Path, session_date: date) -> SurveillanceSnapshot:
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        symbols: set[str] = set()
        isins: set[str] = set()
        dates: dict[str, date] = {}
        for list_type in LIST_TYPES:
            row = conn.execute(
                "SELECT MAX(list_date) FROM surveillance_list"
                " WHERE list_type = ? AND list_date <= ?",
                (list_type, session_date.isoformat()),
            ).fetchone()
            if not row or not row[0]:
                continue
            dates[list_type] = date.fromisoformat(row[0])
            for sym, isin in conn.execute(
                "SELECT symbol, isin FROM surveillance_list WHERE list_type = ? AND list_date = ?",
                (list_type, row[0]),
            ):
                symbols.add(sym.strip().upper())
                if isin:
                    isins.add(isin.strip().upper())
    finally:
        conn.close()

    missing = tuple(t for t in LIST_TYPES if t not in dates)
    if missing:
        return SurveillanceSnapshot(UNAVAILABLE, None, None, missing_types=missing)
    as_of = min(dates.values())
    age = trading_age(as_of, session_date)
    if age == 0:
        status = FRESH
    elif age <= MAX_STALE_TRADING_DAYS:
        status = STALE
    else:
        status = UNAVAILABLE
    # An unavailable list is not applied (operator decision: F&O-only), but its age is reported.
    if status == UNAVAILABLE:
        return SurveillanceSnapshot(status, as_of, age)
    return SurveillanceSnapshot(status, as_of, age, frozenset(symbols), frozenset(isins))


def blocked_symbols(db_path: str | Path, session_date: date) -> set[str]:
    """Equity symbols (as stored in `instruments`) currently under ASM/GSM.

    Used mid-session to keep a watchlist stock that has just been listed out of NEW
    entries (held positions are managed normally, never exited because of this).
    Empty when the list is unavailable: nothing is known, nothing is blocked here.
    """
    snap = load_surveillance(db_path, session_date)
    return {sym for sym, _ in matched(db_path, snap)}


def matched(db_path: str | Path, snap: SurveillanceSnapshot) -> list[tuple[str, str]]:
    """(equity symbol, matched-by 'symbol'|'isin') for each EQ instrument on the list."""
    if not snap.symbols and not snap.isins:
        return []
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        rows = conn.execute(
            "SELECT DISTINCT nse_symbol, isin FROM instruments"
            " WHERE series = 'EQ' AND nse_symbol IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for sym, isin in rows:
        if sym.strip().upper() in snap.symbols:
            out.append((sym, "symbol"))
        elif isin and isin.strip().upper() in snap.isins:
            out.append((sym, "isin"))
    return out
