"""Tests for the intraday_watchlist task and mid-session restart recovery (#37)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from brain.feature_store import FeatureStore
from brain.watchlist import WatchlistEntry, WatchlistStore
from capital.risk_config import RiskConfigStore
from collector.live_bars import SessionBarRecorder
from db.migrations import run_migrations
from executor.brokers.feeds.base import NIFTY_50
from executor.brokers.feeds.manual_feed import ManualFeed
from orchestrator.orchestrator import resume_market_session
from orchestrator.tasks import build_task_registry
from orchestrator.tasks_brain import _intraday_watchlist

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 19)  # Thursday
_MIGRATIONS = Path(__file__).parents[2] / "migrations"
CRORE = 10_000_000


@pytest.fixture()
def db_path(tmp_path) -> str:
    p = str(tmp_path / "b.db")
    run_migrations(p, _MIGRATIONS)
    RiskConfigStore(p).seed_defaults(DAY)
    _add_surveillance(p)  # a fresh list by default; tests of the degraded paths delete it
    return p


@pytest.fixture()
def alerter():
    fake = MagicMock()
    with patch("alerts.alerter.get_alerter", return_value=fake):
        yield fake


def _add_fno(db_path: str, symbol: str, trade_date: str = "2026-11-18") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fo_oi_daily (record_id, raw_id, parser_version, underlying_symbol,"
        " instrument_type, expiry_date, trade_date, observed_at, open_interest)"
        " VALUES (?, 'raw', 'v1', ?, 'FUT', '2026-11-26', ?, ?, 1000)",
        (f"{symbol}-{trade_date}", symbol, trade_date, f"{trade_date}T18:00:00"),
    )
    conn.commit()
    conn.close()


def _add_surveillance(db_path: str, *symbols: str, list_date: str = "2026-11-19") -> None:
    """A fresh ASM list holding `symbols` and one placeholder in each list."""
    conn = sqlite3.connect(db_path)
    rows = [("ASM", s) for s in symbols] + [("ASM", "UNRELATED1"), ("GSM", "UNRELATED")]
    conn.executemany(
        "INSERT OR IGNORE INTO surveillance_list (list_date, list_type, symbol, observed_at)"
        " VALUES (?, ?, ?, ?)",
        [(list_date, t, s, f"{list_date}T07:30:00") for t, s in rows],
    )
    conn.commit()
    conn.close()


def _add_stock(
    db_path: str,
    symbol: str,
    value_cr: float,
    oi_change: float | None = None,
    series: str = "EQ",
    fno: bool = True,
) -> None:
    if fno:
        _add_fno(db_path, symbol)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO instruments (isin, nse_symbol, company_name, series, last_refreshed)"
        " VALUES (?, ?, ?, ?, '2026-11-19T00:00:00')",
        (f"INE{symbol}", symbol, symbol, series),
    )
    conn.commit()
    conn.close()
    fs = FeatureStore(db_path)
    values = {
        "price_close": 500.0,
        "avg_daily_volume_20d": 1_000_000,
        "avg_traded_value_20d": value_cr * CRORE,
        "atr_14d": 10.0,
    }
    if oi_change is not None:
        values["fo_oi_overnight_change_pct"] = oi_change
    for name, value in values.items():
        fs.write_feature(symbol, "NSE", name, value, DAY, DAY)


class TestIntradayWatchlistTask:
    def test_selects_persists_and_subscribes(self, db_path, alerter):
        _add_stock(db_path, "LONGY", 50, oi_change=6.0)
        _add_stock(db_path, "LIQUID", 900)
        _add_stock(db_path, "THIN", 1)  # below the turnover floor
        feed = ManualFeed()

        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=feed)

        saved = WatchlistStore(db_path).load(DAY)
        assert [(e.stock_symbol, e.selection_reason) for e in saved] == [
            ("LONGY", "batch_score"),
            ("LIQUID", "liquidity_fill"),
        ]
        assert feed.subscribed == {"LONGY", "LIQUID"}
        alerter.critical.assert_not_called()

    def test_be_series_never_in_watchlist(self, db_path, alerter):
        """Regression #49: BE (trade-for-trade) stocks can't be traded intraday."""
        _add_stock(db_path, "TFT", 900, oi_change=9.0, series="BE")
        _add_stock(db_path, "LIQUID", 900)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert [e.stock_symbol for e in WatchlistStore(db_path).load(DAY)] == ["LIQUID"]

    def test_empty_watchlist_alerts_and_subscribes_nothing(self, db_path, alerter):
        _add_stock(db_path, "THIN", 1)
        feed = ManualFeed()
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=feed)
        assert feed.subscribed == set()
        assert "empty" in alerter.critical.call_args.args[0]

    def test_non_fno_stock_never_in_watchlist(self, db_path, alerter):
        """#50: only stocks with stock futures are tradable intraday."""
        _add_stock(db_path, "CASHONLY", 900, oi_change=9.0, fno=False)
        _add_stock(db_path, "LIQUID", 900)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert [e.stock_symbol for e in WatchlistStore(db_path).load(DAY)] == ["LIQUID"]

    def test_no_fno_data_fails_closed_with_critical(self, db_path, alerter):
        _add_stock(db_path, "LIQUID", 900, fno=False)
        feed = ManualFeed()
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=feed)
        assert WatchlistStore(db_path).load(DAY) == []
        assert feed.subscribed == set()
        assert "No F&O universe" in alerter.critical.call_args.args[0]

    def test_asm_gsm_stock_never_in_watchlist_and_counted(self, db_path, alerter, caplog):
        """#50: F&O stock under ASM is excluded from the watchlist; both reasons counted."""
        import logging

        _add_surveillance(db_path, "PUMPED", list_date="2026-11-19")
        _add_stock(db_path, "PUMPED", 900, oi_change=9.0)
        _add_stock(db_path, "CASHONLY", 900, fno=False)
        _add_stock(db_path, "LIQUID", 800)
        with caplog.at_level(logging.INFO):
            _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert [e.stock_symbol for e in WatchlistStore(db_path).load(DAY)] == ["LIQUID"]
        line = next(r.message for r in caplog.records if "watchlist_selected" in r.message)
        assert "'not_fno': 1" in line and "'under_surveillance': 1" in line
        alerter.critical.assert_not_called()
        alerter.warn.assert_not_called()

    def test_missing_surveillance_list_trades_fno_only_with_critical(self, db_path, alerter):
        sqlite3.connect(db_path).execute("DELETE FROM surveillance_list").connection.commit()
        _add_stock(db_path, "LIQUID", 900)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert [e.stock_symbol for e in WatchlistStore(db_path).load(DAY)] == ["LIQUID"]
        titles = [c.args[0] for c in alerter.critical.call_args_list]
        assert titles == ["ASM/GSM list unavailable: trading F&O-only"]

    def test_stale_surveillance_list_warns_and_still_filters(self, db_path, alerter):
        sqlite3.connect(db_path).execute("DELETE FROM surveillance_list").connection.commit()
        _add_surveillance(db_path, "PUMPED", list_date="2026-11-17")  # 2 trading days old
        _add_stock(db_path, "PUMPED", 900)
        _add_stock(db_path, "LIQUID", 800)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert [e.stock_symbol for e in WatchlistStore(db_path).load(DAY)] == ["LIQUID"]
        assert alerter.warn.call_args.args[0] == "ASM/GSM list is stale"
        alerter.critical.assert_not_called()

    def test_renamed_fno_underlying_warns(self, db_path, alerter):
        _add_stock(db_path, "LIQUID", 900)
        _add_fno(db_path, "OLDNAME")  # F&O knows a symbol the equity master doesn't
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert alerter.warn.call_args.args[0] == "F&O underlyings with no equity match"

    def test_morning_features_computed_only_for_the_universe(self, db_path, alerter):
        from orchestrator.tasks_brain import _morning_batch_features

        _add_surveillance(db_path, "PUMPED")
        _add_stock(db_path, "PUMPED", 900)
        _add_stock(db_path, "LIQUID", 900)
        _add_stock(db_path, "CASHONLY", 900, fno=False)
        with patch("brain.features.runner.run_track_computers") as run:
            _morning_batch_features(DAY.isoformat(), 1, db_path=db_path)
        assert [c.args[3] for c in run.call_args_list] == ["LIQUID"]

    def test_without_feed_still_persists(self, db_path, alerter):
        _add_stock(db_path, "LIQUID", 900)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert len(WatchlistStore(db_path).load(DAY)) == 1

    def test_schedule_and_retired_tasks(self):
        tasks = build_task_registry(":memory:", "/tmp/a", "/tmp/b")
        td = tasks["intraday_watchlist"]
        assert td.schedule == "10 9 * * 1-5"
        assert td.dependencies == ["morning_batch_features"]
        refresh = tasks["surveillance_refresh"]
        assert refresh.repeats and refresh.schedule == "25,55 9-14 * * 1-5"
        # Retired in #37: they produced recommendations nothing executes since #26.
        assert "morning_batch_signals" not in tasks
        assert "morning_batch_recommendations" not in tasks


class TestResumeMarketSession:
    def _at(self, h: int, m: int, d: date = DAY) -> datetime:
        return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)

    def _watchlist(self, db_path):
        WatchlistStore(db_path).save(
            DAY, [WatchlistEntry("INFY", "NSE", 1, "batch_score", 0.5, "long", 1e9)]
        )

    def test_mid_session_restart_resubscribes_and_reconnects(self, db_path, tmp_path):
        self._watchlist(db_path)
        feed = MagicMock()
        rec = SessionBarRecorder(tmp_path / "bars")
        assert resume_market_session(db_path, feed, rec, self._at(11, 0)) is True
        feed.subscribe.assert_called_once_with([NIFTY_50, "INFY"])
        feed.reauthenticate.assert_called_once()
        assert rec.store.session_date == DAY

    def test_restart_before_watchlist_built_subscribes_nifty_only(self, db_path):
        feed = MagicMock()
        assert resume_market_session(db_path, feed, None, self._at(9, 0)) is True
        feed.subscribe.assert_called_once_with([NIFTY_50])

    @pytest.mark.parametrize(("h", "m"), [(3, 0), (8, 44), (15, 30), (20, 0)])
    def test_outside_session_window_does_nothing(self, db_path, h, m):
        feed = MagicMock()
        assert resume_market_session(db_path, feed, None, self._at(h, m)) is False
        feed.reauthenticate.assert_not_called()

    def test_window_start_is_inclusive(self, db_path):
        assert resume_market_session(db_path, MagicMock(), None, self._at(8, 45)) is True

    def test_weekend_does_nothing(self, db_path):
        saturday = date(2026, 11, 21)
        feed = MagicMock()
        assert resume_market_session(db_path, feed, None, self._at(11, 0, saturday)) is False

    def test_no_feed_does_nothing(self, db_path):
        assert resume_market_session(db_path, None, None, self._at(11, 0)) is False

    def test_feed_failure_returns_false(self, db_path):
        feed = MagicMock()
        feed.reauthenticate.side_effect = RuntimeError("ws refused")
        assert resume_market_session(db_path, feed, None, self._at(11, 0)) is False
