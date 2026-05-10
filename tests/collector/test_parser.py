"""
Tests for ParseWorker — dispatching pending raw_archive rows to fetchers,
handling unknown sources, running sentiment after parse.
"""

import gzip
import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, patch

from collector.models import DataSource
from collector.parser import ParseWorker


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _insert_pending_raw(db, tmp_path, source: DataSource, body: bytes) -> str:
    chash = hashlib.sha256(body).hexdigest()
    raw_id = f"raw-{chash[:8]}"
    rel_path = f"{source.value}/2024/04/22/{raw_id}.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(body))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_id,
            source.value,
            "2024-04-22T10:00:00Z",
            "https://example.com",
            200,
            chash,
            rel_path,
            "pending",
        ),
    )
    db.commit()
    return raw_id


def _make_mock_fetcher(parse_return: int = 3) -> MagicMock:
    m = MagicMock()
    m.parse.return_value = parse_return
    return m


def test_run_pending_dispatches_to_fetcher(tmp_path):
    db = _make_db()
    body = json.dumps({"Table": []}).encode()
    _insert_pending_raw(db, tmp_path, DataSource.BSE_FILINGS, body)

    mock_fetcher = _make_mock_fetcher(5)
    registry = {DataSource.BSE_FILINGS: mock_fetcher}
    worker = ParseWorker(db, tmp_path / "raw", registry)

    stats = worker.run_pending()
    assert stats.get("bse_filings") == 5
    mock_fetcher.parse.assert_called_once()


def test_run_pending_unknown_source_marks_failed(tmp_path):
    db = _make_db()
    body = b"some data"
    raw_id = _insert_pending_raw(db, tmp_path, DataSource.BSE_FILINGS, body)

    # Empty registry — no fetcher for this source.
    worker = ParseWorker(db, tmp_path / "raw", fetcher_registry={})
    worker.run_pending()

    status = db.execute(
        "SELECT parse_status FROM raw_archive WHERE raw_id=?", (raw_id,)
    ).fetchone()[0]
    assert status == "failed"


def test_run_pending_parse_error_marks_failed(tmp_path):
    db = _make_db()
    body = b"bad data"
    raw_id = _insert_pending_raw(db, tmp_path, DataSource.NSE_FILINGS, body)

    mock_fetcher = MagicMock()
    mock_fetcher.parse.side_effect = ValueError("parse exploded")
    registry = {DataSource.NSE_FILINGS: mock_fetcher}

    worker = ParseWorker(db, tmp_path / "raw", registry)
    worker.run_pending()

    status = db.execute(
        "SELECT parse_status FROM raw_archive WHERE raw_id=?", (raw_id,)
    ).fetchone()[0]
    assert status == "failed"


def test_run_pending_respects_limit(tmp_path):
    db = _make_db()
    for i in range(5):
        _insert_pending_raw(db, tmp_path, DataSource.BSE_FILINGS, f"body {i}".encode())

    mock_fetcher = _make_mock_fetcher(1)
    registry = {DataSource.BSE_FILINGS: mock_fetcher}
    worker = ParseWorker(db, tmp_path / "raw", registry)

    worker.run_pending(limit=2)
    assert mock_fetcher.parse.call_count == 2


def test_run_pending_calls_sentiment_after_parse(tmp_path):
    db = _make_db()
    body = json.dumps({"Table": []}).encode()
    _insert_pending_raw(db, tmp_path, DataSource.BSE_FILINGS, body)

    mock_fetcher = _make_mock_fetcher(0)
    mock_sentiment = MagicMock()

    with patch("collector.parser.apply_sentiment_to_filings", return_value=2) as mock_sent:
        registry = {DataSource.BSE_FILINGS: mock_fetcher}
        worker = ParseWorker(db, tmp_path / "raw", registry, sentiment=mock_sentiment)
        worker.run_pending()
        mock_sent.assert_called_once_with(db, mock_sentiment, confidence_threshold=0.60)


def test_run_pending_skips_sentiment_if_none(tmp_path):
    db = _make_db()
    body = b"x"
    _insert_pending_raw(db, tmp_path, DataSource.BSE_FILINGS, body)

    mock_fetcher = _make_mock_fetcher(0)
    with patch("collector.parser.apply_sentiment_to_filings") as mock_sent:
        worker = ParseWorker(
            db, tmp_path / "raw", {DataSource.BSE_FILINGS: mock_fetcher}, sentiment=None
        )
        worker.run_pending()
        mock_sent.assert_not_called()


def test_reparse_source_resets_and_parses(tmp_path):
    db = _make_db()
    body = b"data"
    raw_id = _insert_pending_raw(db, tmp_path, DataSource.PRICES, body)

    # Manually mark as success first.
    db.execute("UPDATE raw_archive SET parse_status='success' WHERE raw_id=?", (raw_id,))
    db.commit()

    mock_fetcher = _make_mock_fetcher(7)
    registry = {DataSource.PRICES: mock_fetcher}
    worker = ParseWorker(db, tmp_path / "raw", registry)
    total = worker.reparse_source(DataSource.PRICES)

    assert total == 7
    mock_fetcher.parse.assert_called_once()
