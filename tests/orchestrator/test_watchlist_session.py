"""Tests for the intraday_watchlist task and mid-session restart recovery (#37)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.brain.feature_store import FeatureStore
from src.brain.watchlist import WatchlistEntry, WatchlistStore
from src.capital.risk_config import RiskConfigStore
from src.collector.live_bars import SessionBarRecorder
from src.db.migrations import run_migrations
from src.executor.brokers.feeds.base import NIFTY_50
from src.executor.brokers.feeds.manual_feed import ManualFeed
from src.orchestrator.orchestrator import resume_market_session
from src.orchestrator.tasks import build_task_registry
from src.orchestrator.tasks_brain import _intraday_watchlist

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 19)  # Thursday
_MIGRATIONS = Path(__file__).parents[2] / "migrations"
CRORE = 10_000_000


@pytest.fixture()
def db_path(tmp_path) -> str:
    p = str(tmp_path / "b.db")
    run_migrations(p, _MIGRATIONS)
    RiskConfigStore(p).seed_defaults(DAY)
    return p


@pytest.fixture()
def alerter():
    fake = MagicMock()
    with patch("src.alerts.alerter.get_alerter", return_value=fake):
        yield fake


def _add_stock(db_path: str, symbol: str, value_cr: float, oi_change: float | None = None) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO instruments (isin, nse_symbol, company_name, series, last_refreshed)"
        " VALUES (?, ?, ?, 'EQ', '2026-11-19T00:00:00')",
        (f"INE{symbol}", symbol, symbol),
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

    def test_empty_watchlist_alerts_and_subscribes_nothing(self, db_path, alerter):
        _add_stock(db_path, "THIN", 1)
        feed = ManualFeed()
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=feed)
        assert feed.subscribed == set()
        assert "empty" in alerter.critical.call_args.args[0]

    def test_without_feed_still_persists(self, db_path, alerter):
        _add_stock(db_path, "LIQUID", 900)
        _intraday_watchlist(DAY.isoformat(), 1, db_path=db_path, feed=None)
        assert len(WatchlistStore(db_path).load(DAY)) == 1

    def test_schedule_and_retired_tasks(self):
        tasks = build_task_registry(":memory:", "/tmp/a", "/tmp/b")
        td = tasks["intraday_watchlist"]
        assert td.schedule == "10 9 * * 1-5"
        assert td.dependencies == ["morning_batch_features"]
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
