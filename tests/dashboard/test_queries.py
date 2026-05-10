"""Tests for dashboard read-only queries against a populated test DB."""
import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"

from src.dashboard.queries import (
    get_capital_view,
    get_recent_task_runs,
    get_today_snapshot,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations
    run_migrations(str(path), MIGRATIONS_DIR)
    return path


class TestGetTodaySnapshot:
    def test_returns_auto_mode_by_default(self, db_path: Path) -> None:
        snap = get_today_snapshot(str(db_path), "2026-05-11")
        assert snap.bot_mode == "auto"

    def test_reflects_mode_change(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE bot_mode SET mode='paused' WHERE id=1")
        conn.commit()
        conn.close()
        snap = get_today_snapshot(str(db_path), "2026-05-11")
        assert snap.bot_mode == "paused"

    def test_zero_counts_on_empty_db(self, db_path: Path) -> None:
        snap = get_today_snapshot(str(db_path), "2026-05-11")
        assert snap.signals_generated == 0
        assert snap.trades_placed == 0
        assert snap.positions_opened == 0
        assert snap.approvals_waiting == 0
        assert snap.missed_critical_alerts == 0

    def test_missed_critical_count(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO critical_notification_failures"
            " (title, body, failed_at, acknowledged) VALUES (?,?,?,0)",
            ("Test", "Body", "2026-05-11T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        snap = get_today_snapshot(str(db_path), "2026-05-11")
        assert snap.missed_critical_alerts == 1

    def test_acknowledged_critical_not_counted(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO critical_notification_failures"
            " (title, body, failed_at, acknowledged) VALUES (?,?,?,1)",
            ("Acked", "Body", "2026-05-11T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        snap = get_today_snapshot(str(db_path), "2026-05-11")
        assert snap.missed_critical_alerts == 0


class TestGetCapitalView:
    def test_returns_zeros_on_empty_db(self, db_path: Path) -> None:
        view = get_capital_view(str(db_path))
        assert view.total_capital == 0.0
        assert view.hwm == 0.0
        assert view.drawdown_pct == 0.0

    def test_reads_latest_ledger_row(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO capital_ledger
               (ledger_id, as_of_date, total_capital, total_cash,
                long_term_allocated_pct, swing_allocated_pct, intraday_allocated_pct,
                long_term_deployed, swing_deployed, intraday_deployed,
                high_water_mark, eod_drawdown_pct, consecutive_loss_days,
                peak_date, created_at)
               VALUES ('lid1', '2026-05-11', 1000000, 500000,
                       70, 20, 10,
                       500000, 150000, 80000,
                       1100000, 9.09, 0,
                       '2026-05-01', '2026-05-11T10:00:00Z')"""
        )
        conn.commit()
        conn.close()
        view = get_capital_view(str(db_path))
        assert view.total_capital == 1_000_000.0
        assert view.hwm == 1_100_000.0
        # drawdown = (1.1M - 1M) / 1.1M * 100 ≈ 9.09%
        assert abs(view.drawdown_pct - 9.09) < 0.1

    def test_no_negative_drawdown_when_at_hwm(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO capital_ledger
               (ledger_id, as_of_date, total_capital, total_cash,
                long_term_allocated_pct, swing_allocated_pct, intraday_allocated_pct,
                long_term_deployed, swing_deployed, intraday_deployed,
                high_water_mark, eod_drawdown_pct, consecutive_loss_days,
                peak_date, created_at)
               VALUES ('lid2', '2026-05-11', 1000000, 1000000,
                       70, 20, 10, 0, 0, 0,
                       1000000, 0.0, 0,
                       '2026-05-11', '2026-05-11T10:00:00Z')"""
        )
        conn.commit()
        conn.close()
        view = get_capital_view(str(db_path))
        assert view.drawdown_pct == 0.0


class TestGetRecentTaskRuns:
    def test_empty_on_fresh_db(self, db_path: Path) -> None:
        runs = get_recent_task_runs(str(db_path), hours=24)
        assert runs == []

    def test_returns_task_runs(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO task_runs"
            " (task_id, run_date, status, started_at, attempt, manual_override)"
            " VALUES ('nightly_eod_collector', '2026-05-11', 'SUCCESS', datetime('now'), 1, 0)"
        )
        conn.commit()
        conn.close()
        runs = get_recent_task_runs(str(db_path), hours=24)
        assert len(runs) == 1
        assert runs[0].task_id == "nightly_eod_collector"
        assert runs[0].status == "SUCCESS"

    def test_old_runs_excluded(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO task_runs"
            " (task_id, run_date, status, started_at, attempt, manual_override)"
            " VALUES ('old_task', '2026-01-01', 'SUCCESS', '2026-01-01T00:00:00Z', 1, 0)"
        )
        conn.commit()
        conn.close()
        runs = get_recent_task_runs(str(db_path), hours=24)
        assert all(r.task_id != "old_task" for r in runs)
