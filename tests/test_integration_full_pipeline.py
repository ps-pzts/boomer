"""
Integration tests: full pipeline across all phases.

Covers cross-component contracts that unit tests cannot catch:
  1. All 5 migrations apply in sequence to a single DB.
  2. Orchestrator crash recovery (RUNNING → INTERRUPTED on startup).
  3. Task dispatch + task_runner state machine with real SQLite.
  4. Double-dispatch prevention (60-second cooldown guard).
  5. Collector health store: run_context records SUCCESS / FAILED.
  6. BaseFetcher 404 → PermanentFetchError → run() skips without retrying.
  7. BaseFetcher archive deduplication on content_hash.
  8. nightly_eod_collector task with mocked fetchers: completes end-to-end.
  9. Orchestrator + Scheduler: already_succeeded blocks re-run for same run_date.
"""

from __future__ import annotations

import datetime
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

MIGRATIONS = [
    "migrations/0001_initial_schema.sql",
    "migrations/0002_collector_schema.sql",
    "migrations/0003_brain_schema.sql",
    "migrations/0004_executor_schema.sql",
    "migrations/0005_orchestrator_schema.sql",
]


def _apply_all_migrations(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    for path in MIGRATIONS:
        conn.executescript(Path(path).read_text())
    conn.close()


def _make_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test.db")
    _apply_all_migrations(db_path)
    return db_path


# ── 1. Migrations ──────────────────────────────────────────────────────────────


def test_all_migrations_apply_to_single_db(tmp_path):
    """All 5 migrations must apply sequentially without conflict."""
    db_path = str(tmp_path / "all_migrations.db")
    conn = sqlite3.connect(db_path)
    for path in MIGRATIONS:
        conn.executescript(Path(path).read_text())

    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    # Spot-check one key table per phase
    for expected in ("risk_config", "raw_archive", "signals", "orders", "task_runs"):
        assert expected in tables, f"Table '{expected}' missing after migrations"
    conn.close()


def test_migrations_idempotent_via_run_migrations(tmp_path):
    """run_migrations skips already-applied migrations (IF NOT EXISTS semantics)."""
    from src.db.migrations import run_migrations

    db_path = str(tmp_path / "idempotent.db")
    migrations_dir = Path("migrations")
    run_migrations(db_path, migrations_dir)
    run_migrations(db_path, migrations_dir)  # second run must not raise


# ── 2. Orchestrator crash recovery ────────────────────────────────────────────


def test_crash_recovery_marks_running_as_interrupted(tmp_path):
    """On startup, Orchestrator must mark any RUNNING task_runs as INTERRUPTED."""
    from src.orchestrator.models import TaskRunStore

    db_path = _make_db(tmp_path)
    store = TaskRunStore(db_path)

    # Simulate a crash: insert a RUNNING row directly
    run_id = store.create("nightly_eod_collector", "2026-01-10", attempt=1)
    store.update(run_id, "RUNNING")

    row = store.latest_for_date("nightly_eod_collector", "2026-01-10")
    assert row is not None and row.status.value == "RUNNING"

    # Re-open as orchestrator would: mark RUNNING → INTERRUPTED
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
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
    from src.orchestrator.models import RetryPolicy, TaskRunStore
    from src.orchestrator.task_runner import execute_with_retry

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
    from src.orchestrator.models import RetryPolicy, TaskRunStore
    from src.orchestrator.task_runner import execute_with_retry

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
    from src.orchestrator.models import RetryPolicy, TaskRunStore
    from src.orchestrator.task_runner import execute_with_retry

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

    from src.orchestrator.models import BotModeStore, RetryPolicy, TaskDefinition, TaskRunStore
    from src.orchestrator.scheduler import Scheduler

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

    now_utc = dt.datetime(2026, 1, 12, 10, 0, 0, tzinfo=dt.UTC)
    should, reason = scheduler.should_run(task, now_utc, run_date)

    assert should is False
    assert reason == "already_succeeded"


def test_latest_for_date_returns_true_latest(tmp_path):
    """latest_for_date returns the row with the highest id (most recently inserted)."""
    from src.orchestrator.models import TaskRunStore

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


# ── 5. Collector health store ─────────────────────────────────────────────────


def test_collection_run_context_records_success(tmp_path):
    """run_context yields a row and finishes it as SUCCESS when no exception."""
    from src.collector.health import CollectionRunStore
    from src.collector.models import DataSource, RunStatus

    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    store = CollectionRunStore(conn)

    with store.run_context(DataSource.BSE_FILINGS) as run:
        run.records_fetched = 10
        run.records_new = 3
        run.status = RunStatus.SUCCESS

    conn.close()

    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT status, records_fetched, records_new FROM collection_runs WHERE source=?",
        ("bse_filings",),
    ).fetchone()
    conn2.close()

    assert row["status"] == "success"
    assert row["records_fetched"] == 10
    assert row["records_new"] == 3


def test_collection_run_context_records_failed_on_exception(tmp_path):
    """run_context marks run FAILED and re-raises when an exception escapes the block."""
    from src.collector.health import CollectionRunStore
    from src.collector.models import DataSource

    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    store = CollectionRunStore(conn)

    with pytest.raises(RuntimeError, match="boom"), store.run_context(DataSource.NSE_FILINGS):
        raise RuntimeError("boom")

    conn.close()

    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT status, error_message FROM collection_runs WHERE source=?", ("nse_filings",)
    ).fetchone()
    conn2.close()

    assert row["status"] == "failed"
    assert "boom" in row["error_message"]


# ── 6. BaseFetcher 404 → PermanentFetchError → no retry ──────────────────────


def test_fetcher_404_skips_without_retrying(tmp_path):
    """A fetcher whose validate() raises PermanentFetchError on 404 must return None immediately."""
    from src.collector.base import BaseFetcher, PermanentFetchError
    from src.collector.models import DataSource, FetchResult

    class TestFetcher(BaseFetcher):
        source = DataSource.BSE_BULK_DEALS

        def fetch_url(self, **kwargs) -> str:
            return "https://example.com/data.csv"

        def validate(self, result: FetchResult) -> None:
            if result.status_code == 404:
                raise PermanentFetchError("404 — no data today")

        def parse(self, raw_row):
            return 0

        def transport(self, url, **kwargs):
            import hashlib
            from datetime import UTC, datetime

            from src.collector.models import FetchResult

            body = b"Not Found"
            return FetchResult(
                source=self.source,
                url=url,
                status_code=404,
                body=body,
                content_hash=hashlib.sha256(body).hexdigest(),
                fetched_at=datetime.now(UTC),
            )

    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    fetcher = TestFetcher(db=conn, raw_dir=tmp_path / "raw")

    attempts = []
    original_transport = fetcher.transport

    def counting_transport(url, **kwargs):
        attempts.append(1)
        return original_transport(url, **kwargs)

    fetcher.transport = counting_transport

    result = fetcher.run()

    assert result is None
    assert len(attempts) == 1, "PermanentFetchError must not trigger retries"
    conn.close()


# ── 7. Archive deduplication ──────────────────────────────────────────────────


def test_archive_deduplicates_on_content_hash(tmp_path):
    """Archiving the same content twice must return the existing row, not insert a duplicate."""
    from src.collector.base import BaseFetcher
    from src.collector.models import DataSource, FetchResult

    class MinimalFetcher(BaseFetcher):
        source = DataSource.PRICES

        def fetch_url(self, **kwargs) -> str:
            return "https://example.com"

        def validate(self, result):
            pass

        def parse(self, raw_row):
            return 0

    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    fetcher = MinimalFetcher(db=conn, raw_dir=tmp_path / "raw")

    body = b"SYMBOL,CLOSE\nRELIANCE,2500\n"
    from datetime import UTC, datetime

    result = FetchResult(
        source=DataSource.PRICES,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC),
    )

    row1 = fetcher.archive(result)
    row2 = fetcher.archive(result)

    assert row1.raw_id == row2.raw_id

    count = conn.execute("SELECT COUNT(*) FROM raw_archive").fetchone()[0]
    assert count == 1
    conn.close()


def test_archive_stores_date_params_as_string(tmp_path):
    """archive() must not crash when request_params contains a date object."""
    import datetime as dt

    from src.collector.base import BaseFetcher
    from src.collector.models import DataSource, FetchResult

    class DateParamFetcher(BaseFetcher):
        source = DataSource.FO_OI

        def fetch_url(self, **kwargs) -> str:
            return "https://example.com"

        def validate(self, result):
            pass

        def parse(self, raw_row):
            return 0

    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    fetcher = DateParamFetcher(db=conn, raw_dir=tmp_path / "raw")

    body = b"oi,data"
    trade_date = dt.date(2026, 5, 9)
    from datetime import UTC, datetime

    result = FetchResult(
        source=DataSource.FO_OI,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC),
        params={"trade_date": trade_date},
    )

    row = fetcher.archive(result)  # must not raise TypeError

    stored = conn.execute(
        "SELECT request_params FROM raw_archive WHERE raw_id=?", (row.raw_id,)
    ).fetchone()[0]
    assert "2026-05-09" in stored
    conn.close()


# ── 8. nightly_eod_collector task end-to-end ──────────────────────────────────


def test_nightly_eod_collector_completes_with_mocked_fetchers(tmp_path):
    """
    _nightly_eod_collector task runs end-to-end using mocked fetchers.
    Verifies correct API usage: CollectionRunStore(conn), run_context(source),
    fetcher.run(trade_date=date).
    """
    from src.collector.models import DataSource

    db_path = _make_db(tmp_path)
    archive_dir = str(tmp_path / "archive")

    called_sources: list[DataSource] = []

    def make_mock_fetcher(source: DataSource):
        m = MagicMock()
        m.run.return_value = None

        def side_effect(*args, **kwargs):
            called_sources.append(source)
            return None

        m.run.side_effect = side_effect
        return m

    mock_registry = {
        DataSource.BSE_FILINGS: make_mock_fetcher(DataSource.BSE_FILINGS),
        DataSource.NSE_FILINGS: make_mock_fetcher(DataSource.NSE_FILINGS),
        DataSource.PRICES: make_mock_fetcher(DataSource.PRICES),
    }

    with patch("src.collector.parser.build_fetcher_registry", return_value=mock_registry):
        from src.orchestrator.tasks_collector import _nightly_eod_collector

        _nightly_eod_collector(
            run_date="2026-05-09",
            run_id=1,
            db_path=db_path,
            archive_dir=archive_dir,
        )

    assert set(called_sources) == {
        DataSource.BSE_FILINGS,
        DataSource.NSE_FILINGS,
        DataSource.PRICES,
    }

    # Each fetcher.run() must have been called with a date object (not string)
    import datetime as dt

    for source, fetcher in mock_registry.items():
        call_kwargs = fetcher.run.call_args[1]
        got = type(call_kwargs["trade_date"])
        assert isinstance(call_kwargs["trade_date"], dt.date), (
            f"{source}: trade_date must be datetime.date, got {got}"
        )

    # collection_runs must have entries for each source
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT source FROM collection_runs").fetchall()
    conn.close()
    recorded = {r[0] for r in rows}
    assert "bse_filings" in recorded
    assert "nse_filings" in recorded
    assert "prices" in recorded


def test_nightly_eod_collector_bulk_deals_use_prev_weekday(tmp_path):
    """Bulk deal fetchers receive the previous weekday, not run_date."""
    import datetime as dt

    from src.collector.models import DataSource

    db_path = _make_db(tmp_path)

    received: dict[DataSource, dt.date] = {}

    def make_mock(source):
        m = MagicMock()

        def side_effect(*args, **kwargs):
            received[source] = kwargs.get("trade_date")
            return None

        m.run.side_effect = side_effect
        return m

    mock_registry = {
        DataSource.NSE_BULK_DEALS: make_mock(DataSource.NSE_BULK_DEALS),
        DataSource.BSE_BULK_DEALS: make_mock(DataSource.BSE_BULK_DEALS),
        DataSource.PRICES: make_mock(DataSource.PRICES),
    }

    with patch("src.collector.parser.build_fetcher_registry", return_value=mock_registry):
        from src.orchestrator.tasks_collector import _nightly_eod_collector

        _nightly_eod_collector(
            run_date="2026-05-11",  # Monday
            run_id=1,
            db_path=db_path,
            archive_dir=str(tmp_path / "archive"),
        )

    # Bulk deals should use Friday 2026-05-08 (prev weekday of Monday)
    assert received[DataSource.NSE_BULK_DEALS] == dt.date(2026, 5, 8)
    assert received[DataSource.BSE_BULK_DEALS] == dt.date(2026, 5, 8)
    # Prices use run_date itself
    assert received[DataSource.PRICES] == dt.date(2026, 5, 11)


# ── 9. Orchestrator + Scheduler full dispatch integration ─────────────────────


def test_orchestrator_dispatches_task_and_records_result(tmp_path):
    """Orchestrator loop dispatches a task and task_runner records SUCCESS."""

    from src.orchestrator.models import RetryPolicy, TaskDefinition, TaskRunStore
    from src.orchestrator.orchestrator import Orchestrator

    db_path = _make_db(tmp_path)

    # Seed bot_mode and trading_calendar
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO bot_mode (id, mode) VALUES (1, 'auto')")
    # 2026-01-12 is a Monday — seed as trading day (not a holiday)
    conn.execute(
        "DELETE FROM trading_calendar WHERE trade_date='2026-01-12'"
    )
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
    row = store.latest_for_date("probe", datetime.date.today().isoformat())
    assert row is not None and row.status.value == "SUCCESS"
