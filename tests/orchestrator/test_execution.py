"""Execution wiring for the session (#63): src/orchestrator/execution.py + session task."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.capital.state import CapitalStateManager
from src.db.migrations import run_migrations
from src.executor.brokers.feeds.base import Tick
from src.executor.brokers.feeds.manual_feed import ManualFeed
from src.orchestrator.execution import FeedLtp, build_session
from src.orchestrator.tasks_executor import _pre_market_executor_setup
from src.orchestrator.tasks_session import _session_tick

IST = ZoneInfo("Asia/Kolkata")
MIGRATIONS = Path(__file__).parents[2] / "migrations"


def _db(tmp_path: Path, capital: bool = True) -> str:
    db = str(tmp_path / "x.db")
    run_migrations(db, MIGRATIONS)
    if capital:
        CapitalStateManager(db).initialise(Decimal("1000000"), datetime.date(2026, 11, 18))
    return db


def test_feed_ltp_reads_latest_tick():
    feed = ManualFeed()
    ltp = FeedLtp(feed)
    assert ltp.get("INFY") is None
    feed.push(Tick("INFY", "NSE", 1500.5, datetime.datetime.now(IST)))
    assert ltp.get("INFY") == 1500.5


def test_paper_session_has_paper_execution(tmp_path):
    s = build_session(_db(tmp_path), "paper", ManualFeed(), brokers=[])
    assert s._sup is not None
    assert str(s._broker.broker_id) == "paper"


def test_no_feed_fails_closed(tmp_path):
    s = build_session(_db(tmp_path), "paper", None, brokers=[])
    assert s._sup is None and s._broker is None


def test_no_capital_ledger_fails_closed(tmp_path):
    s = build_session(_db(tmp_path, capital=False), "paper", ManualFeed(), brokers=[])
    assert s._sup is None


def test_live_without_kite_fails_closed(tmp_path):
    s = build_session(_db(tmp_path), "live", ManualFeed(), brokers=[])
    assert s._sup is None


def test_connection_usable_from_another_thread(tmp_path):
    """Tasks run on their own threads; the shared connection must allow it."""
    import threading

    s = build_session(_db(tmp_path), "paper", ManualFeed(), brokers=[])
    errors = []

    def use():
        try:
            s.ensure(datetime.date(2026, 11, 19))
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert errors == []


def test_session_tick_task_calls_tick():
    session = MagicMock()
    _session_tick(run_date="2026-11-19", run_id=1, session=session)
    session.tick.assert_called_once()
    _session_tick(run_date="2026-11-19", run_id=1, session=None)  # no-op, no raise


def test_setup_task_creates_todays_session(monkeypatch):
    monkeypatch.setattr("src.executor.auto_login.refresh_all_broker_tokens", lambda: {})
    session = MagicMock()
    _pre_market_executor_setup(run_date="2026-11-19", run_id=1, db_path=":memory:", session=session)
    session.ensure.assert_called_once_with(datetime.date(2026, 11, 19))


class TestEmergencyWatcher:
    """Orchestrator.check_emergency: the 5 s poll that runs the session at once (#67)."""

    def _orc(self, tmp_path, session):
        from src.orchestrator.orchestrator import Orchestrator

        return Orchestrator(_db(tmp_path), str(tmp_path), str(tmp_path), session=session)

    def _at(self, hh, mm, day=19):
        return datetime.datetime(2026, 11, day, hh, mm, tzinfo=IST)

    def test_runs_session_on_emergency_stop(self, tmp_path):
        from src.orchestrator.models import BotMode

        session = MagicMock()
        orc = self._orc(tmp_path, session)
        assert orc.check_emergency(self._at(11, 0)) is False  # auto: nothing
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(11, 0)) is True
        session.tick.assert_called_once()

    def test_only_in_market_window_on_trading_days(self, tmp_path):
        from src.orchestrator.models import BotMode

        session = MagicMock()
        orc = self._orc(tmp_path, session)
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(8, 59)) is False
        assert orc.check_emergency(self._at(16, 0)) is False
        assert orc.check_emergency(self._at(11, 0, day=21)) is False  # Saturday
        session.tick.assert_not_called()

    def test_failure_is_swallowed(self, tmp_path):
        from src.orchestrator.models import BotMode

        session = MagicMock()
        session.tick.side_effect = RuntimeError("broker down")
        orc = self._orc(tmp_path, session)
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(11, 0)) is False  # watcher keeps running
