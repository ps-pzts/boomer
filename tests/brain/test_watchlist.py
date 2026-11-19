"""Tests for brain/watchlist.py — daily intraday watchlist selection and storage."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from brain.watchlist import (
    WatchlistEntry,
    WatchlistStore,
    eligibility_reason,
    select_watchlist,
)
from db.migrations import run_migrations

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 9, 10, tzinfo=IST)
_MIGRATIONS = Path(__file__).parents[2] / "migrations"

RISK = SimpleNamespace(
    min_stock_price=Decimal("100"),
    min_avg_daily_volume=500_000,
    min_avg_daily_turnover_cr=Decimal("10.0"),  # #38
)
CRORE = 10_000_000


def _features(price=500.0, volume=1_000_000, value_cr=50.0, atr=10.0, oi_change=None):
    f = {
        "price_close": price,
        "avg_daily_volume_20d": volume,
        "avg_traded_value_20d": value_cr * CRORE,
        "atr_14d": atr,
    }
    if oi_change is not None:
        f["fo_oi_overnight_change_pct"] = oi_change  # drives a LONG/SHORT batch score
    return f


class TestEligibility:
    def test_eligible(self):
        assert eligibility_reason(_features(), RISK) is None

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"price": 99.95}, "price_below_min"),
            ({"volume": 499_999}, "volume_below_min"),
            ({"value_cr": 9.99}, "turnover_below_min"),
            ({"atr": 0.0}, "no_atr"),
        ],
    )
    def test_each_floor(self, overrides, reason):
        assert eligibility_reason(_features(**overrides), RISK) == reason

    def test_boundary_values_are_eligible(self):
        assert (
            eligibility_reason(_features(price=100.0, volume=500_000, value_cr=10.0), RISK) is None
        )

    def test_missing_features_rejected(self):
        assert eligibility_reason({}, RISK) == "price_below_min"
        f = _features()
        del f["atr_14d"]
        assert eligibility_reason(f, RISK) == "no_atr"


class TestSelectWatchlist:
    def test_longs_ranked_by_score_then_filled_by_liquidity(self):
        universe = [
            ("A", "NSE", _features(value_cr=20, oi_change=2.0)),  # long, weaker
            ("B", "NSE", _features(value_cr=30, oi_change=8.0)),  # long, strongest
            ("C", "NSE", _features(value_cr=900, oi_change=-5.0)),  # short: fill only
            ("D", "NSE", _features(value_cr=400)),  # no F&O: neutral, fill
            ("E", "NSE", _features(value_cr=100)),  # neutral, fill
        ]
        got = select_watchlist(universe, RISK, "bull_calm", NOW, size=4)
        assert [(e.stock_symbol, e.rank, e.selection_reason) for e in got] == [
            ("B", 1, "batch_score"),
            ("A", 2, "batch_score"),
            ("C", 3, "liquidity_fill"),
            ("D", 4, "liquidity_fill"),
        ]
        assert got[0].batch_direction == "long" and got[0].batch_raw_score > 0

    def test_size_caps_longs(self):
        universe = [(f"S{i}", "NSE", _features(oi_change=float(i + 1))) for i in range(5)]
        got = select_watchlist(universe, RISK, "bull_calm", NOW, size=3)
        assert [e.stock_symbol for e in got] == ["S4", "S3", "S2"]
        assert {e.selection_reason for e in got} == {"batch_score"}

    def test_fewer_than_size_when_universe_small(self):
        got = select_watchlist([("A", "NSE", _features())], RISK, "bull_calm", NOW, size=50)
        assert len(got) == 1

    def test_ineligible_names_excluded_by_the_one_floor(self):
        """#38: ₹7 cr used to pass risk_config (5 cr) but be dropped by the generator's
        hidden 10 cr gate. Now risk_config's 10 cr floor is the only one."""
        universe = [
            ("CHEAP", "NSE", _features(price=50.0)),
            ("THIN", "NSE", _features(value_cr=7.0)),
            ("OK", "NSE", _features(value_cr=10.0)),
        ]
        got = select_watchlist(universe, RISK, "bull_calm", NOW)
        assert [e.stock_symbol for e in got] == ["OK"]

    def test_empty_universe(self):
        assert select_watchlist([], RISK, "bull_calm", NOW) == []

    def test_default_size_is_fifty(self):
        universe = [(f"S{i:03d}", "NSE", _features(value_cr=20 + i)) for i in range(80)]
        assert len(select_watchlist(universe, RISK, "bull_calm", NOW)) == 50


class TestWatchlistStore:
    @pytest.fixture()
    def store(self, tmp_path):
        db = tmp_path / "w.db"
        run_migrations(str(db), _MIGRATIONS)
        return WatchlistStore(db)

    def _entry(self, sym, rank, reason="batch_score"):
        return WatchlistEntry(sym, "NSE", rank, reason, 0.4, "long", 2.0e8)

    def test_save_and_load_in_rank_order(self, store):
        day = date(2026, 11, 19)
        store.save(day, [self._entry("B", 2, "liquidity_fill"), self._entry("A", 1)])
        assert [(e.stock_symbol, e.rank) for e in store.load(day)] == [("A", 1), ("B", 2)]

    def test_rerun_same_day_replaces(self, store):
        day = date(2026, 11, 19)
        store.save(day, [self._entry("A", 1), self._entry("B", 2)])
        store.save(day, [self._entry("C", 1)])
        assert [e.stock_symbol for e in store.load(day)] == ["C"]

    def test_days_are_independent(self, store):
        store.save(date(2026, 11, 18), [self._entry("OLD", 1)])
        store.save(date(2026, 11, 19), [self._entry("NEW", 1)])
        assert [e.stock_symbol for e in store.load(date(2026, 11, 18))] == ["OLD"]
        assert store.load(date(2026, 11, 20)) == []
