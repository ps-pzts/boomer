"""Tests for _nightly_health_check in tasks_maintenance.

Covers:
- All-clear path: sends ✅ report with integrity/vacuum/disk details
- Stuck RUNNING task detection
- DB integrity failure surfaced as issue
- No Telegram configured: checks still run, no send attempted
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    from src.db.migrations import run_migrations

    run_migrations(path, MIGRATIONS_DIR)
    return path


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _insert_task_run(db: str, task_id: str, status: str, started_at: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_runs (task_id, run_date, status, started_at, attempt)"
        " VALUES (?, '2026-05-24', ?, ?, 1)",
        (task_id, status, started_at),
    )
    conn.commit()
    conn.close()


# ─── Tests ────────────────────────────────────────────────────────────────────


class TestNightlyHealthCheckAllClear:
    def test_sends_all_clear_telegram(self, db_path: str) -> None:
        """Happy path: fresh DB, no stuck tasks, plenty of disk → ✅ message."""
        with (
            patch("alerts.telegram.send_telegram", return_value=True) as mock_send,
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}),
        ):
            from src.orchestrator.tasks_maintenance import _nightly_health_check

            _nightly_health_check(run_date="2026-05-24", run_id=1, db_path=db_path)

        mock_send.assert_called_once()
        token, chat_id, msg = mock_send.call_args[0]
        assert token == "tok"
        assert chat_id == "123"
        assert "all clear" in msg
        assert "integrity: ok" in msg
        assert "Stuck tasks: none" in msg

    def test_no_send_when_telegram_not_configured(self, db_path: str) -> None:
        """If env vars are absent, checks still run but send is never called."""
        _skip = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
        env = {k: v for k, v in os.environ.items() if k not in _skip}
        with (
            patch("alerts.telegram.send_telegram", return_value=True) as mock_send,
            patch.dict(os.environ, env, clear=True),
        ):
            from src.orchestrator.tasks_maintenance import _nightly_health_check

            _nightly_health_check(run_date="2026-05-24", run_id=1, db_path=db_path)

        mock_send.assert_not_called()


class TestNightlyHealthCheckStuckTasks:
    def test_stuck_running_task_reported(self, db_path: str) -> None:
        """A RUNNING task started > 1 h ago appears as an issue in the Telegram message."""
        # started_at well in the past so it's definitely > 1 h before now
        _insert_task_run(db_path, "nightly_eod_collector", "RUNNING", "2026-05-24T00:00:00")

        with (
            patch("alerts.telegram.send_telegram", return_value=True) as mock_send,
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}),
        ):
            from src.orchestrator.tasks_maintenance import _nightly_health_check

            _nightly_health_check(run_date="2026-05-24", run_id=1, db_path=db_path)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][2]
        assert "issues found" in msg
        assert "nightly_eod_collector" in msg

    def test_recent_running_task_not_flagged(self, db_path: str) -> None:
        """A RUNNING task started < 1 h ago is not flagged as stuck."""
        # Use a future timestamp so it's definitely within the 1-h window
        _insert_task_run(db_path, "morning_heartbeat", "RUNNING", "2099-01-01T01:44:00")

        with (
            patch("alerts.telegram.send_telegram", return_value=True) as mock_send,
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}),
        ):
            from src.orchestrator.tasks_maintenance import _nightly_health_check

            _nightly_health_check(run_date="2026-05-24", run_id=1, db_path=db_path)

        msg = mock_send.call_args[0][2]
        assert "all clear" in msg
        assert "morning_heartbeat" not in msg


class TestNightlyHealthCheckDiskSpace:
    def test_low_disk_reported_as_issue(self, db_path: str) -> None:
        """If disk_usage reports < 20 % free, the issue is included in the report."""
        import shutil

        # Fake a disk that is 5% free
        from collections import namedtuple

        _DiskUsage = namedtuple("usage", ["total", "used", "free"])
        fake_usage = _DiskUsage(total=100 * 1024 ** 3, used=95 * 1024 ** 3, free=5 * 1024 ** 3)

        with (
            patch("shutil.disk_usage", return_value=fake_usage),
            patch("alerts.telegram.send_telegram", return_value=True) as mock_send,
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}),
        ):
            from src.orchestrator.tasks_maintenance import _nightly_health_check

            _nightly_health_check(run_date="2026-05-24", run_id=1, db_path=db_path)

        msg = mock_send.call_args[0][2]
        assert "issues found" in msg
        assert "Low disk space" in msg
        assert "5.0%" in msg
