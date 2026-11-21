"""Tests for brain/surveillance.py and the surveillance part of the universe (#50 part 2).

Worked example (session Thursday 2026-11-19, EQ ∩ F&O = 5 names):
    F&O stock underlyings on 2026-11-18:  AAA BBB CCC DDD EEE  (+ NIFTY index future)
    EQ master:                            AAA BBB CCC DDD EEE ZZZ (ZZZ has no futures)
    ASM list: BBB                          GSM list: DDD
    universe = {AAA..EEE} - {BBB, DDD}  = [AAA, CCC, EEE]
    not_fno = 6 EQ - 5 in F&O = 1 (ZZZ); under_surveillance = 2 (BBB, DDD).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from brain.surveillance import (
    FRESH,
    STALE,
    UNAVAILABLE,
    blocked_symbols,
    load_surveillance,
    trading_age,
)
from brain.universe import intraday_universe
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[2] / "migrations"
SESSION = date(2026, 11, 19)  # Thursday


@pytest.fixture()
def db(tmp_path) -> str:
    p = str(tmp_path / "s.db")
    run_migrations(p, _MIGRATIONS)
    return p


def _equity(db: str, symbol: str, isin: str | None = None, series: str = "EQ") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO instruments (isin, nse_symbol, company_name, series, last_refreshed)"
        " VALUES (?, ?, ?, ?, '2026-11-19T00:00:00')",
        (isin or f"INE{symbol}{series}", symbol, symbol, series),
    )
    conn.commit()
    conn.close()


def _fo(db: str, symbol: str, trade_date: str = "2026-11-18") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fo_oi_daily (record_id, raw_id, parser_version, underlying_symbol,"
        " instrument_type, expiry_date, trade_date, observed_at, open_interest)"
        " VALUES (?, 'raw', 'v1', ?, 'FUT', '2026-11-26', ?, ?, 1000)",
        (f"{symbol}-{trade_date}", symbol, trade_date, f"{trade_date}T18:00:00"),
    )
    conn.commit()
    conn.close()


def _surv(db: str, list_type: str, symbol: str, list_date: str, isin: str | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO surveillance_list (list_date, list_type, symbol, isin, observed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (list_date, list_type, symbol, isin, f"{list_date}T07:30:00"),
    )
    conn.commit()
    conn.close()


def _fresh_lists(db: str, asm: tuple[str, ...] = ("BBB",), gsm: tuple[str, ...] = ("DDD",)):
    for s in asm:
        _surv(db, "ASM", s, "2026-11-19")
    for s in gsm:
        _surv(db, "GSM", s, "2026-11-19")


class TestTradingAge:
    def test_same_day_is_zero(self):
        assert trading_age(SESSION, SESSION) == 0

    def test_weekend_does_not_count(self):
        # Fri list, Mon session: one trading day (the weekend is skipped).
        assert trading_age(date(2026, 11, 20), date(2026, 11, 23)) == 1

    def test_non_trading_list_day_counts_from_next_session(self):
        assert trading_age(date(2026, 11, 21), date(2026, 11, 23)) == 1  # Sat list, Mon session

    def test_capped(self):
        assert trading_age(date(2020, 1, 1), SESSION, cap=7) == 7


class TestLoadSurveillance:
    def test_fresh_unions_asm_and_gsm(self, db):
        _fresh_lists(db)
        snap = load_surveillance(db, SESSION)
        assert snap.status == FRESH
        assert snap.symbols == {"BBB", "DDD"}
        assert snap.age_trading_days == 0

    def test_never_fetched_is_unavailable(self, db):
        snap = load_surveillance(db, SESSION)
        assert (snap.status, snap.symbols) == (UNAVAILABLE, frozenset())
        assert snap.missing_types == ("ASM", "GSM")

    def test_one_list_missing_is_unavailable(self, db):
        _surv(db, "ASM", "BBB", "2026-11-19")
        snap = load_surveillance(db, SESSION)
        assert snap.status == UNAVAILABLE
        assert snap.missing_types == ("GSM",)

    @pytest.mark.parametrize(
        ("list_date", "status", "applied"),
        [
            ("2026-11-18", STALE, True),  # 1 trading day
            ("2026-11-16", STALE, True),  # 3 trading days (18, 17, 16): the limit
            ("2026-11-13", UNAVAILABLE, False),  # 4 trading days: too old
        ],
    )
    def test_staleness_boundaries(self, db, list_date, status, applied):
        _surv(db, "ASM", "BBB", list_date)
        _surv(db, "GSM", "DDD", list_date)
        snap = load_surveillance(db, SESSION)
        assert snap.status == status
        assert (snap.symbols == {"BBB", "DDD"}) == applied  # unavailable list is not applied

    def test_older_of_the_two_types_decides(self, db):
        _surv(db, "ASM", "BBB", "2026-11-19")
        _surv(db, "GSM", "DDD", "2026-11-17")
        snap = load_surveillance(db, SESSION)
        assert (snap.status, snap.as_of, snap.age_trading_days) == (STALE, date(2026, 11, 17), 2)

    def test_latest_list_replaces_older_one(self, db):
        _surv(db, "ASM", "OLDER", "2026-11-18")  # delisted from ASM since
        _surv(db, "ASM", "BBB", "2026-11-19")
        _surv(db, "GSM", "DDD", "2026-11-19")
        assert load_surveillance(db, SESSION).symbols == {"BBB", "DDD"}

    def test_future_dated_list_is_ignored_lookahead(self, db):
        _surv(db, "ASM", "BBB", "2026-11-20")
        _surv(db, "GSM", "DDD", "2026-11-20")
        assert load_surveillance(db, SESSION).status == UNAVAILABLE


class TestBlockedSymbols:
    def test_matches_equity_symbols_case_insensitively(self, db):
        _equity(db, "BBB")
        _equity(db, "CCC")
        _fresh_lists(db, asm=("bbb ",))
        assert blocked_symbols(db, SESSION) == {"BBB"}

    def test_matches_by_isin_when_symbol_was_renamed(self, db):
        _equity(db, "NEWNAME", isin="INE000A01010")
        _fresh_lists(db, asm=("OLDNAME",), gsm=("DDD",))
        _surv(db, "ASM", "OLDNAME2", "2026-11-19", isin="INE000A01010")
        assert blocked_symbols(db, SESSION) == {"NEWNAME"}

    def test_series_other_than_eq_ignored(self, db):
        _equity(db, "BBB", series="BE")
        _fresh_lists(db)
        assert blocked_symbols(db, SESSION) == set()

    def test_unavailable_list_blocks_nothing(self, db):
        _equity(db, "BBB")
        assert blocked_symbols(db, SESSION) == set()


class TestUniverseWithSurveillance:
    def _setup(self, db):
        for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "ZZZ"):
            _equity(db, s)
        for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "NIFTY"):
            _fo(db, s)

    def test_worked_example(self, db):
        self._setup(db)
        _fresh_lists(db)
        u = intraday_universe(db, SESSION)
        assert u.symbols == ["AAA", "CCC", "EEE"]
        assert u.not_fno_count == 1  # ZZZ
        assert u.under_surveillance == ["BBB", "DDD"]
        assert u.surveillance_status == FRESH
        assert u.fno_unmatched == []  # NIFTY is a known index, not a mismatch

    def test_stale_list_still_applied(self, db):
        self._setup(db)
        _surv(db, "ASM", "BBB", "2026-11-17")
        _surv(db, "GSM", "DDD", "2026-11-17")
        u = intraday_universe(db, SESSION)
        assert u.symbols == ["AAA", "CCC", "EEE"]
        assert (u.surveillance_status, u.surveillance_age) == (STALE, 2)

    def test_expired_or_missing_list_is_fno_only_and_flagged(self, db):
        self._setup(db)
        u = intraday_universe(db, SESSION)  # never fetched
        assert u.symbols == ["AAA", "BBB", "CCC", "DDD", "EEE"]
        assert u.surveillance_status == UNAVAILABLE
        assert u.under_surveillance == []

    def test_expired_list_is_fno_only(self, db):
        self._setup(db)
        _surv(db, "ASM", "BBB", "2026-11-12")
        _surv(db, "GSM", "DDD", "2026-11-12")
        u = intraday_universe(db, SESSION)
        assert "BBB" in u.symbols and u.surveillance_status == UNAVAILABLE
        assert u.surveillance_age == 5

    def test_symbol_with_ampersand_matches(self, db):
        """M&M is the same string in the F&O bhavcopy and the equity master."""
        _equity(db, "M&M")
        _fo(db, "M&M")
        _fresh_lists(db, asm=("X",), gsm=("Y",))
        assert intraday_universe(db, SESSION).symbols == ["M&M"]

    def test_renamed_ticker_is_reported_not_silently_lost(self, db):
        _equity(db, "AAA")
        _fo(db, "AAA")
        _fo(db, "OLDTICK")  # F&O still lists the old name; the equity master has the new one
        _equity(db, "NEWTICK")
        _fresh_lists(db)
        u = intraday_universe(db, SESSION)
        assert u.symbols == ["AAA"]
        assert u.fno_unmatched == ["OLDTICK"]

    def test_no_symbol_overlap_fails_closed(self, db):
        _equity(db, "AAA")
        _fo(db, "ZZZ")
        u = intraday_universe(db, SESSION)
        assert (u.symbols, u.unavailable_reason) == ([], "no_fno_equity_match")

    def test_empty_equity_master_fails_closed(self, db):
        _fo(db, "AAA")
        assert intraday_universe(db, SESSION).unavailable_reason == "no_fno_equity_match"

    def test_surveillance_symbol_outside_fno_counted_only_as_not_fno(self, db):
        self._setup(db)
        _fresh_lists(db, asm=("ZZZ",), gsm=("DDD",))  # ZZZ is already not_fno
        u = intraday_universe(db, SESSION)
        assert u.under_surveillance == ["DDD"]
        assert u.not_fno_count == 1
