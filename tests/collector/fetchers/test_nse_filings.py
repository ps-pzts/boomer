"""Tests for NSE filings datetime parsing and date-failure handling (#54). No HTTP."""

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from collector.dateparse import DateFormatError
from collector.fetchers.nse_filings import NseFilingsFetcher, _parse_nse_datetime
from collector.models import DataSource, RawArchiveRow


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for m in ("0001_initial_schema.sql", "0002_collector_schema.sql"):
        with open(f"migrations/{m}") as f:
            conn.executescript(f.read())
    return conn


def _make_raw_row(db, content: bytes, tmp_path: Path) -> RawArchiveRow:
    chash = hashlib.sha256(content).hexdigest()
    rel_path = "nse_filings/2024/04/22/test.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(content))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "nse-raw-1",
            "nse_filings",
            "2024-04-22T18:30:00.000000Z",
            "https://x",
            200,
            chash,
            rel_path,
            "pending",
        ),
    )
    db.commit()
    return RawArchiveRow(
        raw_id="nse-raw-1",
        source=DataSource.NSE_FILINGS,
        fetched_at=datetime(2024, 4, 22, 18, 30, 0),
        request_url="https://x",
        response_status=200,
        content_hash=chash,
        content_path=rel_path,
    )


def _rec(bcast: str, symbol: str) -> dict:
    return {"symbol": symbol, "subject": "Outcome of board meeting", "bcast_date": bcast}


def test_parse_nse_datetime_with_time():
    assert _parse_nse_datetime("22-Apr-2024 15:30:00") == ("2024-04-22", "15:30:00")


def test_parse_nse_datetime_date_only():
    assert _parse_nse_datetime("22-Apr-2024") == ("2024-04-22", None)


@pytest.mark.parametrize("bad", ["", "garbage", "22.04.2024"])
def test_parse_nse_datetime_unparseable_is_none(bad):
    """Regression #54: empty returned today; unparseable returned the first 10 chars."""
    assert _parse_nse_datetime(bad) is None


def test_parse_skips_unparseable_and_counts(tmp_path):
    db = _make_db()
    recs = [_rec("22-Apr-2024 15:30:00", f"S{i}") for i in range(30)] + [_rec("", "BAD")]
    raw_row = _make_raw_row(db, json.dumps(recs).encode(), tmp_path)
    assert NseFilingsFetcher(db, tmp_path / "raw").parse(raw_row) == 30
    assert db.execute("SELECT COUNT(*) FROM filings WHERE stock_symbol='BAD'").fetchone()[0] == 0


def test_parse_rejects_file_with_widespread_bad_dates(tmp_path):
    db = _make_db()
    recs = [_rec("22.04.2024", f"S{i}") for i in range(4)]
    raw_row = _make_raw_row(db, json.dumps(recs).encode(), tmp_path)
    with pytest.raises(DateFormatError):
        NseFilingsFetcher(db, tmp_path / "raw").parse(raw_row)
    assert db.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0
