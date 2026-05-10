"""Tests for the Scheduler: should_run logic, dependency checking, intraday disable."""
import datetime
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"

from src.orchestrator.models import (
    BotMode,
    BotModeStore,
    RetryPolicy,
    TaskDefinition,
    TaskRunStore,
    TaskStatus,
)
from src.orchestrator.scheduler import Scheduler, cron_matches, dependency_met


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations
    run_migrations(str(path), MIGRATIONS_DIR)
    return path


def _make_task(task_id: str, schedule: str, deps: list[str] | None = None,
               run_on_holiday: bool = False, trailing: bool = False) -> TaskDefinition:
    return TaskDefinition(
        task_id=task_id,
        fn=lambda **kw: None,
        schedule=schedule,
        dependencies=deps or [],
        timeout_seconds=60,
        retry_policy=RetryPolicy(max_attempts=1),
        run_on_holiday=run_on_holiday,
        trailing_stop_task=trailing,
    )


# ─── cron_matches ─────────────────────────────────────────────────────────────

class TestCronMatches:
    def test_every_minute_matches(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 7, 0, 0, tzinfo=datetime.UTC)
        assert cron_matches("* * * * *", dt) is True

    def test_specific_hour_minute_matches(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 7, 0, 0, tzinfo=datetime.UTC)
        assert cron_matches("0 7 * * *", dt) is True

    def test_specific_hour_minute_no_match(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 7, 1, 0, tzinfo=datetime.UTC)
        assert cron_matches("0 7 * * *", dt) is False

    def test_every_30_min_matches_at_00(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 9, 0, 0, tzinfo=datetime.UTC)
        assert cron_matches("*/30 * * * *", dt) is True

    def test_every_30_min_matches_at_30(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 9, 30, 0, tzinfo=datetime.UTC)
        assert cron_matches("*/30 * * * *", dt) is True

    def test_every_30_min_no_match_at_15(self) -> None:
        dt = datetime.datetime(2026, 5, 10, 9, 15, 0, tzinfo=datetime.UTC)
        assert cron_matches("*/30 * * * *", dt) is False

    def test_weekday_range_monday(self) -> None:
        mon = datetime.datetime(2026, 5, 11, 6, 30, tzinfo=datetime.UTC)  # Monday
        assert cron_matches("30 6 * * 1-5", mon) is True

    def test_weekday_range_saturday_no_match(self) -> None:
        sat = datetime.datetime(2026, 5, 9, 6, 30, tzinfo=datetime.UTC)  # Saturday
        assert cron_matches("30 6 * * 1-5", sat) is False


# ─── Scheduler.should_run ─────────────────────────────────────────────────────

class TestSchedulerShouldRun:
    def _make_scheduler(self, db_path: Path) -> Scheduler:
        tasks = {
            "simple_task": _make_task("simple_task", "0 7 * * 1-5"),
            "holiday_task": _make_task("holiday_task", "* * * * *", run_on_holiday=True),
            "trailing_task": _make_task("trailing_task", "* * * * *", trailing=True),
        }
        return Scheduler(tasks, TaskRunStore(db_path), BotModeStore(db_path), str(db_path))

    def test_cron_no_match_blocks(self, db_path: Path) -> None:
        s = self._make_scheduler(db_path)
        dt = datetime.datetime(2026, 5, 11, 6, 0, tzinfo=datetime.UTC)  # 06:00 not 07:00
        ok, reason = s.should_run(s._tasks["simple_task"], dt, "2026-05-11")
        assert not ok
        assert "cron_no_match" in reason

    def test_emergency_stop_blocks_all(self, db_path: Path) -> None:
        mode_store = BotModeStore(db_path)
        mode_store.set_mode(BotMode.EMERGENCY_STOP)
        s = self._make_scheduler(db_path)
        s._mode_store = mode_store
        dt = datetime.datetime(2026, 5, 11, 7, 0, tzinfo=datetime.UTC)
        ok, reason = s.should_run(s._tasks["simple_task"], dt, "2026-05-11")
        assert not ok
        assert "emergency_stop" in reason

    def test_paused_blocks_non_trailing(self, db_path: Path) -> None:
        mode_store = BotModeStore(db_path)
        mode_store.set_mode(BotMode.PAUSED)
        s = self._make_scheduler(db_path)
        s._mode_store = mode_store
        dt = datetime.datetime(2026, 5, 11, 7, 0, tzinfo=datetime.UTC)
        ok, reason = s.should_run(s._tasks["simple_task"], dt, "2026-05-11")
        assert not ok
        assert "paused" in reason

    def test_paused_allows_trailing_stop_task(self, db_path: Path) -> None:
        mode_store = BotModeStore(db_path)
        mode_store.set_mode(BotMode.PAUSED)
        s = self._make_scheduler(db_path)
        s._mode_store = mode_store
        dt = datetime.datetime(2026, 5, 11, 9, 0, tzinfo=datetime.UTC)
        ok, _ = s.should_run(s._tasks["trailing_task"], dt, "2026-05-11")
        assert ok

    def test_holiday_blocks_non_holiday_task(self, db_path: Path) -> None:
        s = self._make_scheduler(db_path)
        dt = datetime.datetime(2026, 1, 26, 7, 0, tzinfo=datetime.UTC)  # Republic Day
        ok, reason = s.should_run(s._tasks["simple_task"], dt, "2026-01-26")
        assert not ok
        assert "holiday" in reason

    def test_holiday_allows_holiday_task(self, db_path: Path) -> None:
        s = self._make_scheduler(db_path)
        dt = datetime.datetime(2026, 1, 26, 0, 0, tzinfo=datetime.UTC)
        ok, _ = s.should_run(s._tasks["holiday_task"], dt, "2026-01-26")
        assert ok

    def test_already_succeeded_blocks(self, db_path: Path) -> None:
        run_store = TaskRunStore(db_path)
        run_id = run_store.create("simple_task", "2026-05-11")
        run_store.update(run_id, TaskStatus.SUCCESS)
        s = self._make_scheduler(db_path)
        dt = datetime.datetime(2026, 5, 11, 7, 0, tzinfo=datetime.UTC)
        ok, reason = s.should_run(s._tasks["simple_task"], dt, "2026-05-11")
        assert not ok
        assert "already_succeeded" in reason

    def test_intraday_disabled_after_3_failures(self, db_path: Path) -> None:
        tasks = {"intraday_cycle": _make_task("intraday_cycle", "* * * * *")}
        s = Scheduler(tasks, TaskRunStore(db_path), BotModeStore(db_path), str(db_path))
        s._intraday_fail_count["2026-05-11"] = 3
        dt = datetime.datetime(2026, 5, 11, 9, 30, tzinfo=datetime.UTC)
        ok, reason = s.should_run(s._tasks["intraday_cycle"], dt, "2026-05-11")
        assert not ok
        assert "disabled" in reason


# ─── dependency_met ───────────────────────────────────────────────────────────

class TestDependencyMet:
    def test_no_deps_always_passes(self, db_path: Path) -> None:
        task = _make_task("t", "* * * * *", deps=[])
        ok, _ = dependency_met(task, "2026-05-10", TaskRunStore(db_path))
        assert ok

    def test_dep_succeeded_passes(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("dep_task", "2026-05-10")
        store.update(run_id, TaskStatus.SUCCESS)
        task = _make_task("t", "* * * * *", deps=["dep_task"])
        ok, _ = dependency_met(task, "2026-05-10", store)
        assert ok

    def test_dep_not_run_fails(self, db_path: Path) -> None:
        task = _make_task("t", "* * * * *", deps=["missing_dep"])
        ok, reason = dependency_met(task, "2026-05-10", TaskRunStore(db_path))
        assert not ok
        assert "missing_dep" in reason

    def test_dep_failed_fails(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("dep_task", "2026-05-10")
        store.update(run_id, TaskStatus.FAILED)
        task = _make_task("t", "* * * * *", deps=["dep_task"])
        ok, reason = dependency_met(task, "2026-05-10", store)
        assert not ok

    def test_manual_override_bypasses_failed_dep(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("dep_task", "2026-05-10")
        store.update(run_id, TaskStatus.FAILED)
        task = _make_task("t", "* * * * *", deps=["dep_task"])
        ok, reason = dependency_met(task, "2026-05-10", store, manual_override=True)
        assert ok
        assert "manual_override" in reason
