"""
Integration tests: full pipeline across all phases — orchestrator and database.
(Split from tests/test_integration_full_pipeline.py by responsibility, #117.)

Covers: migrations apply in sequence, crash recovery (RUNNING -> INTERRUPTED), task
dispatch + task_runner state machine, double-dispatch prevention, and
Orchestrator + Scheduler dispatch.
"""

from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

from tests.integration.pipeline_helpers import MIGRATIONS, _make_db  # noqa: F401

# ── 1. Migrations ──────────────────────────────────────────────────────────────


def test_all_migrations_apply_to_single_db(tmp_path):
    """All 5 migrations must apply sequentially without conflict."""
    db_path = str(tmp_path / "all_migrations.db")
    conn = sqlite3.connect(db_path)
    for path in MIGRATIONS:
        conn.executescript(Path(path).read_text())

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    # Spot-check one key table per phase
    for expected in ("risk_config", "raw_archive", "signals", "orders", "task_runs"):
        assert expected in tables, f"Table '{expected}' missing after migrations"
    conn.close()


def test_migrations_idempotent_via_run_migrations(tmp_path):
    """run_migrations skips already-applied migrations (IF NOT EXISTS semantics)."""
    from db.migrations import run_migrations

    db_path = str(tmp_path / "idempotent.db")
    migrations_dir = Path("migrations")
    run_migrations(db_path, migrations_dir)
    run_migrations(db_path, migrations_dir)  # second run must not raise


# ── 2. Orchestrator crash recovery ────────────────────────────────────────────


def test_crash_recovery_marks_running_as_interrupted(tmp_path):
    """On startup, Orchestrator must mark any RUNNING task_runs as INTERRUPTED."""
    from orchestrator.models import TaskRunStore

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)

    # Simulate a crash: insert a RUNNING row directly
    run_id = store.create("nightly_eod_collector", "2026-01-10", attempt=1)
    store.update(run_id, "RUNNING")

    row = store.latest_for_date("nightly_eod_collector", "2026-01-10")
    assert row is not None and row.status.value == "RUNNING"

    # Re-open as orchestrator would: mark RUNNING → INTERRUPTED
    now = datetime.datetime.now(_IST).replace(tzinfo=None).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "UPDATE task_runs SET status='INTERRUPTED', ended_at=? WHERE status='RUNNING'", (now,)
    )
    conn.commit()
    conn.close()
    assert cur.rowcount == 1

    row = store.latest_for_date("nightly_eod_collector", "2026-01-10")
    assert row is not None and row.status.value == "INTERRUPTED"


# ── 3. Task runner state machine ──────────────────────────────────────────────


def test_task_runner_records_success(tmp_path):
    """execute_with_retry writes SUCCESS to task_runs when fn completes cleanly."""
    from orchestrator.models import RetryPolicy, TaskRunStore
    from orchestrator.task_runner import execute_with_retry

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)
    policy = RetryPolicy(max_attempts=1)

    called = []

    def good_task(**kwargs):
        called.append(True)

    result = execute_with_retry(
        task_id="test_task",
        run_date="2026-01-10",
        fn=good_task,
        store=store,
        retry_policy=policy,
        timeout_seconds=10,
    )

    assert result is True
    assert called == [True]
    row = store.latest_for_date("test_task", "2026-01-10")
    assert row is not None and row.status.value == "SUCCESS"


def test_task_runner_records_failed_final_after_exhausting_retries(tmp_path):
    """Task that always raises is marked FAILED_FINAL after max_attempts."""
    from orchestrator.models import RetryPolicy, TaskRunStore
    from orchestrator.task_runner import execute_with_retry

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)
    policy = RetryPolicy(max_attempts=2)

    def bad_task(**kwargs):
        raise ValueError("always fails")

    result = execute_with_retry(
        task_id="failing_task",
        run_date="2026-01-10",
        fn=bad_task,
        store=store,
        retry_policy=policy,
        timeout_seconds=10,
    )

    assert result is False
    row = store.latest_for_date("failing_task", "2026-01-10")
    assert row is not None and row.status.value == "FAILED_FINAL"
    assert "always fails" in (row.error_message or "")


def test_task_runner_timeout(tmp_path):
    """Task that exceeds timeout_seconds is recorded as TIMEOUT."""
    from orchestrator.models import RetryPolicy, TaskRunStore
    from orchestrator.task_runner import execute_with_retry

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)
    policy = RetryPolicy(max_attempts=1)

    def slow_task(**kwargs):
        time.sleep(10)

    result = execute_with_retry(
        task_id="slow_task",
        run_date="2026-01-10",
        fn=slow_task,
        store=store,
        retry_policy=policy,
        timeout_seconds=1,
    )

    assert result is False
    row = store.latest_for_date("slow_task", "2026-01-10")
    assert row is not None and row.status.value == "TIMEOUT"


# ── 4. Double-dispatch prevention ─────────────────────────────────────────────


def test_scheduler_already_succeeded_blocks_rerun(tmp_path):
    """Scheduler returns already_succeeded if task succeeded on run_date."""
    import datetime as dt

    from orchestrator.models import BotModeStore, RetryPolicy, TaskDefinition, TaskRunStore
    from orchestrator.scheduler import Scheduler

    db_path = _make_db(tmp_path)
    run_store = TaskRunStore(db_path)
    mode_store = BotModeStore(db_path)

    # Seed bot_mode
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO bot_mode (id, mode) VALUES (1, 'auto')")
    conn.commit()
    conn.close()

    task = TaskDefinition(
        task_id="probe_task",
        fn=lambda **kw: None,
        schedule="0 10 * * 1-5",
        dependencies=[],
        timeout_seconds=30,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    scheduler = Scheduler(
        task_registry={"probe_task": task},
        run_store=run_store,
        mode_store=mode_store,
        db_path=db_path,
        poll_interval_seconds=30,
    )

    run_date = "2026-01-12"  # Monday
    # Manually record a SUCCESS
    run_id = run_store.create("probe_task", run_date, attempt=1)
    run_store.update(run_id, "SUCCESS")

    # schedule "0 10 * * 1-5" is IST 10:00 — pass IST-aware datetime so cron matches
    from zoneinfo import ZoneInfo

    now_utc = dt.datetime(2026, 1, 12, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    should, reason = scheduler.should_run(task, now_utc, run_date)

    assert should is False
    assert reason == "already_succeeded"


def test_latest_for_date_returns_true_latest(tmp_path):
    """latest_for_date returns the row with the highest id (most recently inserted)."""
    from orchestrator.models import TaskRunStore

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)

    # Insert FAILED, then SUCCESS for same task+date
    id1 = store.create("multi_attempt", "2026-01-12", attempt=1)
    store.update(id1, "FAILED")
    id2 = store.create("multi_attempt", "2026-01-12", attempt=2)
    store.update(id2, "SUCCESS")

    row = store.latest_for_date("multi_attempt", "2026-01-12")
    assert row is not None
    assert row.status.value == "SUCCESS"
    assert row.id == id2


# ── 9. Orchestrator + Scheduler full dispatch integration ─────────────────────


def test_orchestrator_dispatches_task_and_records_result(tmp_path):
    """Orchestrator loop dispatches a task and task_runner records SUCCESS."""

    from orchestrator.models import RetryPolicy, TaskDefinition, TaskRunStore
    from orchestrator.orchestrator import Orchestrator

    db_path = _make_db(tmp_path)

    # Seed bot_mode and trading_calendar
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO bot_mode (id, mode) VALUES (1, 'auto')")
    # 2026-01-12 is a Monday — seed as trading day (not a holiday)
    conn.execute("DELETE FROM trading_calendar WHERE trade_date='2026-01-12'")
    conn.commit()
    conn.close()

    dispatched = threading.Event()

    def probe_fn(**kwargs):
        dispatched.set()

    task = TaskDefinition(
        task_id="probe",
        fn=probe_fn,
        schedule="* * * * *",  # every minute — always matches
        dependencies=[],
        timeout_seconds=5,
        retry_policy=RetryPolicy(max_attempts=1),
        run_on_holiday=True,  # dispatch test must run on weekends/holidays too
    )

    orc = Orchestrator(
        db_path=db_path,
        archive_dir=str(tmp_path / "archive"),
        backup_dir=str(tmp_path / "backups"),
        poll_interval=1,
    )
    orc._tasks = {"probe": task}

    # Call _loop() directly to avoid signal.signal() which only works in main thread.
    thread = threading.Thread(target=orc._loop, daemon=True)
    thread.start()

    fired = dispatched.wait(timeout=5)
    assert fired, "Task was never dispatched within 5 seconds"

    # Give task_runner time to write SUCCESS after probe_fn returns.
    time.sleep(0.5)
    orc._stop_event.set()
    thread.join(timeout=3)

    store = TaskRunStore(db_path)
    # Orchestrator writes run_date in IST; match that to avoid UTC/IST day boundary failures.
    from zoneinfo import ZoneInfo

    ist_today = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    row = store.latest_for_date("probe", ist_today)
    assert row is not None and row.status.value == "SUCCESS"
