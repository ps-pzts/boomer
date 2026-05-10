"""Tests for the task_runner context manager and execute_with_retry."""
import time
from pathlib import Path
from unittest.mock import patch

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"

from src.orchestrator.models import RetryPolicy, TaskRunStore, TaskStatus
from src.orchestrator.task_runner import execute_with_retry, run_task


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations
    run_migrations(str(path), MIGRATIONS_DIR)
    return path


class TestRunTaskContextManager:
    def test_success_path_writes_success(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        with run_task("t1", "2026-05-10", store, timeout_seconds=10):
            pass
        row = store.latest_for_date("t1", "2026-05-10")
        assert row is not None
        assert row.status == TaskStatus.SUCCESS

    def test_exception_writes_failed(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        with (
            pytest.raises(ValueError, match="deliberate"),
            run_task("t2", "2026-05-10", store, timeout_seconds=10),
        ):
            raise ValueError("deliberate")
        row = store.latest_for_date("t2", "2026-05-10")
        assert row is not None
        assert row.status == TaskStatus.FAILED
        assert "deliberate" in (row.error_message or "")

    def test_timeout_writes_timeout_status(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        from src.orchestrator.task_runner import TimeoutError as TaskTimeout
        with (
            pytest.raises(TaskTimeout),
            run_task("t3", "2026-05-10", store, timeout_seconds=1),
        ):
            time.sleep(5)  # will be interrupted by SIGALRM after 1s
        row = store.latest_for_date("t3", "2026-05-10")
        assert row is not None
        assert row.status == TaskStatus.TIMEOUT

    def test_manual_override_flagged_in_row(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        with run_task("t4", "2026-05-10", store, timeout_seconds=10, manual_override=True):
            pass
        row = store.latest_for_date("t4", "2026-05-10")
        assert row is not None
        assert row.manual_override is True


class TestExecuteWithRetry:
    def test_success_returns_true(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        calls = []

        def fn(run_date: str, run_id: int) -> None:
            calls.append(run_date)

        result = execute_with_retry(
            "task_ok", "2026-05-10", fn, store,
            RetryPolicy(max_attempts=1), timeout_seconds=10,
        )
        assert result is True
        assert len(calls) == 1

    def test_failure_retries_and_returns_false(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        calls = []

        def fn(run_date: str, run_id: int) -> None:
            calls.append(1)
            raise RuntimeError("always fails")

        with patch("src.orchestrator.task_runner.time.sleep"):  # don't actually sleep
            result = execute_with_retry(
                "task_fail", "2026-05-10", fn, store,
                RetryPolicy(max_attempts=3, backoff_seconds=[0, 0]), timeout_seconds=10,
            )
        assert result is False
        assert len(calls) == 3

    def test_succeeds_on_second_attempt(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)
        attempts = []

        def fn(run_date: str, run_id: int) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first attempt fails")

        with patch("src.orchestrator.task_runner.time.sleep"):
            result = execute_with_retry(
                "task_retry", "2026-05-10", fn, store,
                RetryPolicy(max_attempts=2, backoff_seconds=[0]), timeout_seconds=10,
            )
        assert result is True
        assert len(attempts) == 2

    def test_final_failure_marks_failed_final(self, db_path: Path) -> None:
        store = TaskRunStore(db_path)

        def fn(run_date: str, run_id: int) -> None:
            raise RuntimeError("done")

        with patch("src.orchestrator.task_runner.time.sleep"):
            execute_with_retry(
                "task_ff", "2026-05-10", fn, store,
                RetryPolicy(max_attempts=2, backoff_seconds=[0]), timeout_seconds=10,
            )
        row = store.latest_for_date("task_ff", "2026-05-10")
        assert row is not None
        assert row.status == TaskStatus.FAILED_FINAL
