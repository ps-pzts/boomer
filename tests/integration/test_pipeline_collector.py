"""
Integration tests: full pipeline across all phases — collector.
(Split from tests/test_integration_full_pipeline.py by responsibility, #117.)

Covers: collector health store (run_context SUCCESS / FAILED), BaseFetcher 404 skip
without retry, archive deduplication on content_hash, and nightly_eod_collector
end-to-end with mocked fetchers.
"""

from __future__ import annotations

import hashlib
import sqlite3
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")

from tests.integration.pipeline_helpers import MIGRATIONS, _make_db  # noqa: F401

# ── 5. Collector health store ─────────────────────────────────────────────────


def test_collection_run_context_records_success(tmp_path):
    """run_context yields a row and finishes it as SUCCESS when no exception."""
    from collector.health import CollectionRunStore
    from collector.models import DataSource, RunStatus

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
    from collector.health import CollectionRunStore
    from collector.models import DataSource

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
    from collector.base import BaseFetcher, PermanentFetchError
    from collector.models import DataSource, FetchResult

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
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from collector.models import FetchResult

            body = b"Not Found"
            return FetchResult(
                source=self.source,
                url=url,
                status_code=404,
                body=body,
                content_hash=hashlib.sha256(body).hexdigest(),
                fetched_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
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
    from collector.base import BaseFetcher
    from collector.models import DataSource, FetchResult

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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = FetchResult(
        source=DataSource.PRICES,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
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

    from collector.base import BaseFetcher
    from collector.models import DataSource, FetchResult

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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = FetchResult(
        source=DataSource.FO_OI,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
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
    from collector.models import DataSource

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

    with patch("collector.parser.build_fetcher_registry", return_value=mock_registry):
        from orchestrator.tasks_collector import _nightly_eod_collector

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

    from collector.models import DataSource

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

    with patch("collector.parser.build_fetcher_registry", return_value=mock_registry):
        from orchestrator.tasks_collector import _nightly_eod_collector

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
