"""
Tests for BaseFetcher: archive dedup, backoff behaviour, content path logic.
Uses a concrete stub subclass — no real HTTP calls.
"""

import gzip
import hashlib
import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from collector.base import BaseFetcher
from collector.models import DataSource, FetchResult, ParseStatus, RawArchiveRow

# ── fixtures ──────────────────────────────────────────────────────────────────


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


class _StubFetcher(BaseFetcher):
    source = DataSource.BSE_FILINGS

    def fetch_url(self, **kwargs) -> str:
        return "https://example.com/filings"

    def validate(self, result: FetchResult) -> None:
        if result.status_code != 200:
            raise ValueError("bad status")

    def parse(self, raw_row: RawArchiveRow) -> int:
        return 0


@pytest.fixture
def fetcher(tmp_path):
    db = _make_db()
    return _StubFetcher(db, tmp_path / "raw")


# ── archive tests ─────────────────────────────────────────────────────────────


def test_archive_writes_gzipped_file(fetcher, tmp_path):
    body = b"Hello World"
    result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    row = fetcher.archive(result)

    assert row.raw_id
    assert row.parse_status == ParseStatus.PENDING
    gz_path = tmp_path / "raw" / row.content_path
    assert gz_path.exists()
    assert gzip.decompress(gz_path.read_bytes()) == body


def test_archive_dedup_same_hash(fetcher):
    body = b"same content"
    chash = hashlib.sha256(body).hexdigest()
    result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=chash,
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    row1 = fetcher.archive(result)
    row2 = fetcher.archive(result)  # identical hash → dedup

    assert row1.raw_id == row2.raw_id
    count = fetcher._db.execute(
        "SELECT COUNT(*) FROM raw_archive WHERE content_hash=?", (chash,)
    ).fetchone()[0]
    assert count == 1


def test_archive_different_hash_creates_new_row(fetcher):
    for i, body in enumerate([b"content v1", b"content v2"]):
        result = FetchResult(
            source=DataSource.BSE_FILINGS,
            url="https://example.com",
            status_code=200,
            body=body,
            content_hash=hashlib.sha256(body).hexdigest(),
            fetched_at=datetime(2024, 4, 22, 10, i, 0),
        )
        fetcher.archive(result)

    count = fetcher._db.execute("SELECT COUNT(*) FROM raw_archive").fetchone()[0]
    assert count == 2


def test_mark_parsed_updates_status(fetcher):
    body = b"data"
    result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    row = fetcher.archive(result)
    fetcher.mark_parsed(row.raw_id, "v1", ParseStatus.SUCCESS)

    db_row = fetcher._db.execute(
        "SELECT parse_status, parser_version FROM raw_archive WHERE raw_id=?",
        (row.raw_id,),
    ).fetchone()
    assert db_row[0] == "success"
    assert db_row[1] == "v1"


def test_load_raw_body_roundtrip(fetcher, tmp_path):
    body = b"round trip test"
    result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    row = fetcher.archive(result)
    recovered = fetcher.load_raw_body(row.content_path)
    assert recovered == body


# ── backoff / run tests ───────────────────────────────────────────────────────


def test_run_returns_none_after_exhausted_retries(fetcher):
    """When transport raises every time, run() exhausts retries and returns None."""
    with (
        patch.object(_StubFetcher, "transport", side_effect=ConnectionError("no network")),
        patch("collector.base.time.sleep"),
    ):  # don't actually sleep in tests
        result = fetcher.run()
    assert result is None


def test_run_returns_row_on_success(fetcher):
    body = b"success"
    mock_result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    with patch.object(_StubFetcher, "transport", return_value=mock_result):
        row = fetcher.run()
    assert row is not None
    assert row.source == DataSource.BSE_FILINGS


def test_run_retries_on_first_failure_then_succeeds(fetcher):
    body = b"eventual success"
    success_result = FetchResult(
        source=DataSource.BSE_FILINGS,
        url="https://example.com",
        status_code=200,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
    )
    call_count = [0]

    def flaky_transport(url, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("first attempt fails")
        return success_result

    with (
        patch.object(_StubFetcher, "transport", side_effect=flaky_transport),
        patch("collector.base.time.sleep"),
    ):
        row = fetcher.run()

    assert row is not None
    assert call_count[0] == 2
