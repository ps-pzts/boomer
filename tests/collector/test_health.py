import sqlite3

import pytest

from collector.health import CollectionRunStore
from collector.models import DataSource, RunStatus


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


@pytest.fixture
def store():
    return CollectionRunStore(_make_db())


def test_start_creates_running_row(store):
    row = store.start(DataSource.BSE_FILINGS)
    assert row.status == RunStatus.RUNNING
    assert row.records_fetched == 0


def test_finish_updates_row(store):
    row = store.start(DataSource.NSE_BULK_DEALS)
    row.status = RunStatus.SUCCESS
    row.records_fetched = 42
    row.records_new = 10
    store.finish(row)

    latest = store.latest(DataSource.NSE_BULK_DEALS)
    assert latest is not None
    assert latest.status == RunStatus.SUCCESS
    assert latest.records_fetched == 42
    assert latest.records_new == 10
    assert latest.ended_at is not None


def test_latest_returns_most_recent(store):
    r1 = store.start(DataSource.PRICES)
    r1.status = RunStatus.FAILED
    store.finish(r1)

    r2 = store.start(DataSource.PRICES)
    r2.status = RunStatus.SUCCESS
    store.finish(r2)

    latest = store.latest(DataSource.PRICES)
    assert latest.run_id == r2.run_id


def test_latest_returns_none_for_unknown_source(store):
    assert store.latest(DataSource.FO_OI) is None


def test_recent_failures_returns_failed_runs(store):
    for i in range(3):
        r = store.start(DataSource.BSE_FILINGS)
        r.status = RunStatus.FAILED
        r.error_message = f"error {i}"
        store.finish(r)
    # one success — should not appear
    r = store.start(DataSource.BSE_FILINGS)
    r.status = RunStatus.SUCCESS
    store.finish(r)

    failures = store.recent_failures(DataSource.BSE_FILINGS)
    assert len(failures) == 3
    assert all(f.status == RunStatus.FAILED for f in failures)


def test_run_context_sets_failed_on_exception(store):
    with pytest.raises(RuntimeError), store.run_context(DataSource.NSE_FILINGS):
        raise RuntimeError("simulated failure")

    latest = store.latest(DataSource.NSE_FILINGS)
    assert latest.status == RunStatus.FAILED
    assert "simulated failure" in latest.error_message


def test_run_context_normal_completion(store):
    with store.run_context(DataSource.INSTRUMENTS) as run:
        run.status = RunStatus.SUCCESS
        run.records_fetched = 500
        run.records_new = 10

    latest = store.latest(DataSource.INSTRUMENTS)
    assert latest.status == RunStatus.SUCCESS
    assert latest.records_fetched == 500
