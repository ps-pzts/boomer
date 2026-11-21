"""Execution wiring for the session (#63): src/orchestrator/execution.py + session task."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from capital.state import CapitalStateManager
from db.migrations import run_migrations
from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from orchestrator.execution import FeedLtp, build_session
from orchestrator.tasks_executor import _pre_market_executor_setup
from orchestrator.tasks_session import _session_tick

IST = ZoneInfo("Asia/Kolkata")
MIGRATIONS = Path(__file__).parents[2] / "migrations"
DAY = datetime.date(2026, 11, 19)


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


def _built(db, mode="paper", feed="new"):
    s = build_session(db, mode, ManualFeed() if feed == "new" else feed, brokers=[])
    s._ensure_execution(DAY)
    return s


def test_paper_session_has_paper_execution(tmp_path):
    s = build_session(_db(tmp_path), "paper", ManualFeed(), brokers=[])
    assert s.supervisor is None  # lazy (#74): built on the first tick
    s._ensure_execution(DAY)
    assert s._sup is not None
    assert str(s._broker.broker_id) == "paper"


def test_no_feed_fails_closed(tmp_path):
    s = _built(_db(tmp_path), feed=None)
    assert s._sup is None and s._broker is None


def test_no_capital_ledger_fails_closed_then_builds_once_it_exists(tmp_path):
    """#74: day one on a fresh DB: the ledger appears at 08:45, execution follows."""
    db = _db(tmp_path, capital=False)
    s = _built(db)
    assert s._sup is None
    CapitalStateManager(db).initialise(Decimal("1000000"), datetime.date(2026, 11, 18))
    s._ensure_execution(DAY)
    assert s._sup is not None
    first = s._sup
    s._ensure_execution(DAY)
    assert s._sup is first  # built once, never replaced mid-session


def test_live_without_kite_fails_closed(tmp_path):
    s = _built(_db(tmp_path), mode="live")
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
    monkeypatch.setattr("executor.auto_login.refresh_all_broker_tokens", lambda: {})
    session = MagicMock()
    _pre_market_executor_setup(run_date="2026-11-19", run_id=1, db_path=":memory:", session=session)
    session.ensure.assert_called_once_with(datetime.date(2026, 11, 19))


class TestEmergencyWatcher:
    """Orchestrator.check_emergency: the 5 s poll that runs the session at once (#67)."""

    def _orc(self, tmp_path, session):
        from orchestrator.orchestrator import Orchestrator

        return Orchestrator(_db(tmp_path), str(tmp_path), str(tmp_path), session=session)

    def _at(self, hh, mm, day=19):
        return datetime.datetime(2026, 11, day, hh, mm, tzinfo=IST)

    def test_runs_session_on_emergency_stop(self, tmp_path):
        from orchestrator.models import BotMode

        session = MagicMock()
        orc = self._orc(tmp_path, session)
        assert orc.check_emergency(self._at(11, 0)) is False  # auto: nothing
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(11, 0)) is True
        session.tick.assert_called_once()

    def test_only_in_market_window_on_trading_days(self, tmp_path):
        from orchestrator.models import BotMode

        session = MagicMock()
        orc = self._orc(tmp_path, session)
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(8, 59)) is False
        assert orc.check_emergency(self._at(16, 0)) is False
        assert orc.check_emergency(self._at(11, 0, day=21)) is False  # Saturday
        session.tick.assert_not_called()

    def test_failure_is_swallowed(self, tmp_path):
        from orchestrator.models import BotMode

        session = MagicMock()
        session.tick.side_effect = RuntimeError("broker down")
        orc = self._orc(tmp_path, session)
        orc._mode_store.set_mode(BotMode.EMERGENCY_STOP, changed_by="operator")
        assert orc.check_emergency(self._at(11, 0)) is False  # watcher keeps running


class TestDecisionCycleWiring:
    def test_built_for_a_paper_session(self, tmp_path):
        from orchestrator.execution import build_decision_cycle

        db = _db(tmp_path)
        feed = ManualFeed()
        s = build_session(db, "paper", feed, brokers=[])
        cycle = build_decision_cycle(db, s, feed, bar_recorder=None)
        assert type(cycle).__name__ == "DecisionCycle"
        assert cycle._session is s

    def test_none_without_execution(self, tmp_path):
        from orchestrator.execution import build_decision_cycle

        db = _db(tmp_path)
        s = build_session(db, "paper", ManualFeed(), brokers=[])
        assert build_decision_cycle(db, s, None, None) is None
        assert build_decision_cycle(db, None, ManualFeed(), None) is None

    def test_task_runs_the_cycle(self):
        from orchestrator.tasks_executor import _intraday_cycle

        cycle = MagicMock()
        cycle.run.return_value = MagicMock(skipped=None, opened=["b1"], rejected={})
        _intraday_cycle(run_date="2026-11-19", run_id=1, cycle=cycle)
        cycle.run.assert_called_once_with()
        _intraday_cycle(run_date="2026-11-19", run_id=1, cycle=None)  # no-op

    def test_registry_passes_the_cycle(self):
        from orchestrator.tasks import build_task_registry

        cycle = MagicMock()
        cycle.run.return_value = MagicMock(skipped="x", opened=[], rejected={})
        reg = build_task_registry(":memory:", "/tmp/a", "/tmp/b", cycle=cycle)
        reg["intraday_cycle"].fn(run_date="2026-11-19", run_id=1)
        cycle.run.assert_called_once()


class TestBootstrap:
    """#74: a fresh database must be able to trade."""

    def test_risk_config_seeded_once(self, tmp_path):
        from orchestrator.bootstrap import seed_risk_config

        db = _db(tmp_path)
        day = datetime.date(2026, 11, 19)
        assert seed_risk_config(db, day) is True
        assert seed_risk_config(db, day) is False  # never overwritten
        from capital.risk_config import RiskConfigStore

        assert RiskConfigStore(db).load_current().intraday_consecutive_loss_count == 3

    def test_capital_from_broker_cash(self, tmp_path):
        from orchestrator.bootstrap import bootstrap_capital

        db = _db(tmp_path, capital=False)
        kite = MagicMock(broker_id="kite")
        kite.get_funds.return_value = MagicMock(available_cash=250000.0)
        alerts = []
        assert bootstrap_capital(
            db, [kite], datetime.date(2026, 11, 19), lambda t, b: alerts.append(t)
        )
        ledger = CapitalStateManager(db).latest_ledger()
        assert (ledger.total_capital, str(ledger.as_of_date)) == (Decimal("250000"), "2026-11-18")
        assert alerts == ["Capital ledger initialised"]

    def test_existing_ledger_untouched(self, tmp_path):
        from orchestrator.bootstrap import bootstrap_capital

        db = _db(tmp_path)  # ₹10 L ledger
        kite = MagicMock(broker_id="kite")
        assert bootstrap_capital(db, [kite], datetime.date(2026, 11, 19)) is True
        kite.get_funds.assert_not_called()

    def test_no_cash_fails_closed(self, tmp_path):
        from orchestrator.bootstrap import bootstrap_capital

        db = _db(tmp_path, capital=False)
        kite = MagicMock(broker_id="kite")
        kite.get_funds.side_effect = RuntimeError("TokenException: expired")
        alerts = []
        assert not bootstrap_capital(
            db, [kite], datetime.date(2026, 11, 19), lambda t, b: alerts.append(t)
        )
        assert CapitalStateManager(db).latest_ledger() is None
        assert alerts == ["No capital ledger"]
