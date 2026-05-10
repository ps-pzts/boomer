"""Tests for orchestrator models: BotModeStore, TaskRunStore, RetryPolicy, is_trading_day."""
import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"

from src.orchestrator.models import (
    BotMode,
    BotModeStore,
    RetryPolicy,
    TaskRunStore,
    TaskStatus,
    is_trading_day,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations
    run_migrations(str(path), MIGRATIONS_DIR)
    return path


# ─── BotModeStore ─────────────────────────────────────────────────────────────

class TestBotModeStore:
    def test_default_mode_is_auto(self, db_path: Path) -> None:
        store = BotModeStore(db_path)
        assert store.current_mode() == BotMode.AUTO

    def test_set_mode_paused(self, db_path: Path) -> None:
        store = BotModeStore(db_path)
        store.set_mode(BotMode.PAUSED, changed_by="operator", reason="traveling")
        assert store.current_mode() == BotMode.PAUSED

    def test_set_mode_emergency_stop(self, db_path: Path) -> None:
        store = BotModeStore(db_path)
        store.set_mode(BotMode.EMERGENCY_STOP)
        assert store.current_mode() == BotMode.EMERGENCY_STOP

    def test_mode_change_logged_in_audit(self, db_path: Path) -> None:
        store = BotModeStore(db_path)
        store.set_mode(BotMode.PAUSED, reason="test")
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM bot_mode_log").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][1] == "auto"  # old_mode
        assert rows[0][2] == "paused"  # new_mode

    def test_multiple_mode_changes_all_logged(self, db_path: Path) -> None:
        store = BotModeStore(db_path)
        store.set_mode(BotMode.PAUSED)
        store.set_mode(BotMode.AUTO)
        store.set_mode(BotMode.EMERGENCY_STOP)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM bot_mode_log").fetchone()[0]
        conn.close()
        assert count == 3


# ─── TaskRunStore ─────────────────────────────────────────────────────────────

class TestTaskRunStore:
    def test_create_returns_id(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("test_task", "2026-05-10")
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_create_sets_running_status(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("test_task", "2026-05-10")
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        assert row[0] == "RUNNING"

    def test_update_to_success(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("test_task", "2026-05-10")
        store.update(run_id, TaskStatus.SUCCESS)
        latest = store.latest_for_date("test_task", "2026-05-10")
        assert latest is not None
        assert latest.status == TaskStatus.SUCCESS

    def test_update_to_failed_with_message(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("test_task", "2026-05-10")
        store.update(run_id, TaskStatus.FAILED, error_message="boom", error_traceback="tb_here")
        latest = store.latest_for_date("test_task", "2026-05-10")
        assert latest is not None
        assert latest.status == TaskStatus.FAILED
        assert latest.error_message == "boom"
        assert latest.error_traceback == "tb_here"

    def test_latest_for_date_returns_none_when_missing(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        assert store.latest_for_date("nonexistent_task", "2026-01-01") is None

    def test_latest_for_date_returns_highest_attempt(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        store.create("test_task", "2026-05-10", attempt=1)
        store.create("test_task", "2026-05-10", attempt=2)
        latest = store.latest_for_date("test_task", "2026-05-10")
        assert latest is not None
        assert latest.attempt == 2

    def test_count_running(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        assert store.count_running() == 0
        store.create("task_a", "2026-05-10")
        store.create("task_b", "2026-05-10")
        assert store.count_running() == 2

    def test_count_running_excludes_finished(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        run_id = store.create("task_a", "2026-05-10")
        store.update(run_id, TaskStatus.SUCCESS)
        assert store.count_running() == 0

    def test_manual_override_flag(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        store.create("test_task", "2026-05-10", manual_override=True)
        latest = store.latest_for_date("test_task", "2026-05-10")
        assert latest is not None
        assert latest.manual_override is True


# ─── RetryPolicy ──────────────────────────────────────────────────────────────

class TestRetryPolicy:
    def test_first_attempt_zero_delay(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff_seconds=[60, 120])
        assert policy.delay_for_attempt(1) == 0

    def test_second_attempt_uses_first_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff_seconds=[60, 120])
        assert policy.delay_for_attempt(2) == 60

    def test_third_attempt_uses_second_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff_seconds=[60, 120])
        assert policy.delay_for_attempt(3) == 120

    def test_beyond_backoff_list_uses_last(self) -> None:
        policy = RetryPolicy(max_attempts=5, backoff_seconds=[30])
        assert policy.delay_for_attempt(4) == 30
        assert policy.delay_for_attempt(5) == 30

    def test_single_attempt_no_backoff(self) -> None:
        policy = RetryPolicy(max_attempts=1)
        assert policy.delay_for_attempt(1) == 0


# ─── is_trading_day ───────────────────────────────────────────────────────────

class TestIsTradingDay:
    def test_weekday_default_is_trading(self, db_path: Path) -> None:
        # Monday 2026-05-04 not in holiday table
        assert is_trading_day(db_path, "2026-05-04") is True

    def test_saturday_is_not_trading(self, db_path: Path) -> None:
        # 2026-05-09 is a Saturday
        assert is_trading_day(db_path, "2026-05-09") is False

    def test_sunday_is_not_trading(self, db_path: Path) -> None:
        assert is_trading_day(db_path, "2026-05-10") is False

    def test_republic_day_is_holiday(self, db_path: Path) -> None:
        # 2026-01-26 seeded in migration as holiday; it's a Monday
        assert is_trading_day(db_path, "2026-01-26") is False

    def test_holi_is_holiday(self, db_path: Path) -> None:
        assert is_trading_day(db_path, "2026-03-25") is False
