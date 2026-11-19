"""Tests for brain/universe.py — F&O-only intraday universe (#50)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from brain.universe import MAX_FNO_AGE_TRADING_DAYS, fno_underlyings, intraday_universe
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[2] / "migrations"
SESSION = date(2026, 11, 19)  # Thursday


@pytest.fixture()
def db(tmp_path) -> str:
    p = str(tmp_path / "u.db")
    run_migrations(p, _MIGRATIONS)
    return p


def _equity(db: str, symbol: str, series: str = "EQ") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO instruments (isin, nse_symbol, company_name, series, last_refreshed)"
        " VALUES (?, ?, ?, ?, '2026-11-19T00:00:00')",
        (f"INE{symbol}{series}", symbol, symbol, series),
    )
    conn.commit()
    conn.close()


def _fo(db: str, symbol: str, trade_date: str, instrument_type: str = "FUT") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fo_oi_daily (record_id, raw_id, parser_version, underlying_symbol,"
        " instrument_type, expiry_date, strike_price, trade_date, observed_at, open_interest)"
        " VALUES (?, 'raw', 'v1', ?, ?, '2026-11-26', ?, ?, ?, 1000)",
        (
            f"{symbol}-{trade_date}-{instrument_type}",
            symbol,
            instrument_type,
            None if instrument_type == "FUT" else 1000.0,
            trade_date,
            f"{trade_date}T18:00:00",
        ),
    )
    conn.commit()
    conn.close()


class TestFnoUnderlyings:
    def test_latest_trade_date_strictly_before_session(self, db):
        _fo(db, "OLD", "2026-11-17")
        _fo(db, "INFY", "2026-11-18")
        _fo(db, "TCS", "2026-11-18")
        _fo(db, "FUTURE", "2026-11-19")  # same-day file would be lookahead
        symbols, as_of = fno_underlyings(db, SESSION)
        assert symbols == {"INFY", "TCS"}
        assert as_of == date(2026, 11, 18)

    def test_options_only_rows_ignored(self, db):
        _fo(db, "INFY", "2026-11-18", instrument_type="CE")
        assert fno_underlyings(db, SESSION) == (set(), None)

    def test_empty_table(self, db):
        assert fno_underlyings(db, SESSION) == (set(), None)


class TestIntradayUniverse:
    def test_eq_and_fno_intersection(self, db):
        for sym in ("INFY", "TCS", "CASHONLY"):
            _equity(db, sym)
        _equity(db, "TFT", series="BE")
        for sym in ("INFY", "TCS", "TFT", "NIFTY", "BANKNIFTY"):
            _fo(db, sym, "2026-11-18")
        u = intraday_universe(db, SESSION)
        # CASHONLY: no futures. TFT: BE series. NIFTY/BANKNIFTY: index futures, no equity.
        assert u.symbols == ["INFY", "TCS"]
        assert (u.equity_count, u.fno_count, u.fno_as_of) == (3, 5, date(2026, 11, 18))
        assert u.unavailable_reason is None

    def test_no_fno_data_fails_closed(self, db):
        _equity(db, "INFY")
        u = intraday_universe(db, SESSION)
        assert (u.symbols, u.unavailable_reason) == ([], "no_fno_data")

    def test_stale_fno_data_fails_closed(self, db):
        """Older than MAX_FNO_AGE_TRADING_DAYS trading days → empty, never unfiltered."""
        assert MAX_FNO_AGE_TRADING_DAYS == 5
        _equity(db, "INFY")
        _fo(db, "INFY", "2026-11-11")  # 6 trading days before Thu 19 Nov
        u = intraday_universe(db, SESSION)
        assert u.symbols == []
        assert u.unavailable_reason == "fno_data_stale:2026-11-11"

    def test_exactly_max_age_is_accepted(self, db):
        _equity(db, "INFY")
        _fo(db, "INFY", "2026-11-12")  # 5 trading days before Thu 19 Nov
        assert intraday_universe(db, SESSION).symbols == ["INFY"]

    def test_age_counts_trading_days_not_calendar_days(self, db):
        _equity(db, "INFY")
        _fo(db, "INFY", "2026-11-13")  # Friday
        # Fri 20 Nov: 19, 18, 17, 16, 13 -> exactly 5 trading days back -> accepted
        assert intraday_universe(db, date(2026, 11, 20)).symbols == ["INFY"]
        # Mon 23 Nov: 20, 19, 18, 17, 16 -> 13th is 6 trading days back -> stale
        # (10 calendar days, but the weekend doesn't count either way)
        assert intraday_universe(db, date(2026, 11, 23)).unavailable_reason == (
            "fno_data_stale:2026-11-13"
        )
