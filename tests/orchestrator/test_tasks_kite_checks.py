"""#85: 08:55 Kite pre-flight and 09:15 canary as scheduled tasks."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.orchestrator.models import BotModeStore, TaskRunStore
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.tasks import build_task_registry
from src.orchestrator.tasks_session import _kite_canary, _kite_preflight
from tests.kitesim_kit import SimKite

IST = ZoneInfo("Asia/Kolkata")
MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    from src.db.migrations import run_migrations

    path = str(tmp_path / "t.db")
    run_migrations(path, MIGRATIONS_DIR)
    return path


class _Alerter:
    def __init__(self) -> None:
        self.titles: list[str] = []

    def critical(self, title, body, source_task_id=None):
        self.titles.append(title)


def _rows(db_path: str) -> list[tuple]:
    c = sqlite3.connect(db_path)
    try:
        return c.execute(
            "SELECT session_date, check_name, ok, source FROM kite_daily_checks ORDER BY id"
        ).fetchall()
    finally:
        c.close()


class TestRegistry:
    def test_schedules(self):
        reg = build_task_registry(":memory:", "/tmp/a", "/tmp/b")
        pre, can = reg["kite_preflight"], reg["kite_canary"]
        assert pre.schedule == "55 8 * * 1-5"
        assert can.schedule == "15 9 * * 1-5"
        for t in (pre, can):
            assert not t.run_on_holiday and not t.repeats
            assert t.retry_policy.max_attempts == 1  # a failure needs an operator re-run
            assert t.dependencies == []

    @pytest.mark.parametrize(
        "when, run_date, expected",
        [
            (datetime.datetime(2026, 5, 11, 8, 55, tzinfo=IST), "2026-05-11", "ok"),  # Monday
            (datetime.datetime(2026, 1, 26, 8, 55, tzinfo=IST), "2026-01-26", "holiday"),
            (datetime.datetime(2026, 5, 9, 8, 55, tzinfo=IST), "2026-05-09", "holiday"),  # Sat
            (datetime.datetime(2026, 5, 11, 8, 54, tzinfo=IST), "2026-05-11", "cron_no_match"),
        ],
    )
    def test_preflight_respects_the_market_calendar(self, db_path, when, run_date, expected):
        reg = build_task_registry(db_path, "/tmp/a", "/tmp/b")
        s = Scheduler(reg, TaskRunStore(db_path), BotModeStore(db_path), db_path)
        ok, reason = s.should_run(reg["kite_preflight"], when, run_date)
        assert reason == expected and ok == (expected == "ok")

    def test_canary_not_scheduled_on_a_holiday(self, db_path):
        reg = build_task_registry(db_path, "/tmp/a", "/tmp/b")
        s = Scheduler(reg, TaskRunStore(db_path), BotModeStore(db_path), db_path)
        when = datetime.datetime(2026, 1, 26, 9, 15, tzinfo=IST)
        assert s.should_run(reg["kite_canary"], when, "2026-01-26") == (False, "holiday")


class TestTaskFunctions:
    def test_preflight_records_a_pass(self, db_path):
        alerter = _Alerter()
        _kite_preflight(
            "2026-11-19", 1, db_path=db_path, brokers=[SimKite().broker()], alerter=alerter
        )
        assert [r[1:] for r in _rows(db_path)] == [("preflight", 1, "scheduled")]
        assert alerter.titles == []

    def test_preflight_without_kite_fails_and_alerts(self, db_path):
        alerter = _Alerter()
        _kite_preflight("2026-11-19", 1, db_path=db_path, brokers=[], alerter=alerter)
        assert [r[1:3] for r in _rows(db_path)] == [("preflight", 0)]
        assert alerter.titles == ["Kite preflight FAILED — trading blocked today"]

    def test_canary_waits_for_0915_05_then_runs(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("BOOMER_KITE_RECORDINGS_DIR", str(tmp_path / "rec"))
        now = [datetime.datetime(2026, 11, 19, 9, 15, 0, tzinfo=IST)]
        slept: list[float] = []

        def sleep(s: float) -> None:
            slept.append(s)
            now[0] += datetime.timedelta(seconds=s)

        _kite_canary(
            "2026-11-19",
            1,
            db_path=db_path,
            brokers=[SimKite().broker()],
            alerter=_Alerter(),
            clock=lambda: now[0],
            sleep=sleep,
        )
        assert slept[0] == pytest.approx(5.0)
        assert [r[:3] for r in _rows(db_path)] == [("2026-11-19", "canary", 1)]
        assert (tmp_path / "rec" / "2026-11-19").is_dir()

    def test_canary_symbol_from_env(self, db_path, tmp_path, monkeypatch):
        monkeypatch.setenv("BOOMER_KITE_RECORDINGS_DIR", str(tmp_path / "rec"))
        monkeypatch.setenv("BOOMER_CANARY_SYMBOL", "TCS")
        fk = SimKite()
        at = datetime.datetime(2026, 11, 19, 9, 16, tzinfo=IST)
        _kite_canary(
            "2026-11-19",
            1,
            db_path=db_path,
            brokers=[fk.broker()],
            alerter=_Alerter(),
            clock=lambda: at,
            sleep=lambda s: None,
        )
        assert (["NSE:TCS"],) in [c["instruments"] for c in fk.sdk.called("ltp")]
