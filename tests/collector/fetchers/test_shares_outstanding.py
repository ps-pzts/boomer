"""Tests for the shares-outstanding parser date handling (#54). No HTTP."""

import gzip
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from collector.dateparse import DateFormatError
from collector.fetchers.shares_outstanding import _parse_date, _parse_mktcap_csv
from collector.models import DataSource, RawArchiveRow

HEADER = b"SYMBOL,SERIES,DATE,ISIN,TOTAL_SHARES\n"


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for m in ("0001_initial_schema.sql", "0002_collector_schema.sql"):
        with open(f"migrations/{m}") as f:
            conn.executescript(f.read())
    return conn


def _make_raw_row(db, body: bytes, tmp_path: Path) -> RawArchiveRow:
    chash = hashlib.sha256(body).hexdigest()
    rel_path = "shares_outstanding/2024/04/22/test.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(body))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "so-raw-1",
            "shares_outstanding",
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
        raw_id="so-raw-1",
        source=DataSource.SHARES_OUTSTANDING,
        fetched_at=datetime(2024, 4, 22, 18, 30, 0),
        request_url="https://x",
        response_status=200,
        content_hash=chash,
        content_path=rel_path,
    )


def _row(date_str: str, i: int) -> bytes:
    return f"S{i},EQ,{date_str},INE{i:09d},1000000\n".encode()


def test_parse_date_formats_and_unparseable():
    assert _parse_date("22-Apr-2024") == "2024-04-22"
    assert _parse_date("2024-04-22") == "2024-04-22"
    assert _parse_date("garbage") is None  # Regression #54: used to return today
    assert _parse_date("") is None


def test_parse_inserts_rows_with_parsed_date(tmp_path):
    db = _make_db()
    body = HEADER + _row("22-Apr-2024", 1)
    raw_row = _make_raw_row(db, body, tmp_path)
    assert _parse_mktcap_csv(body, raw_row, db, "v1") == 1
    row = db.execute("SELECT trade_date, total_shares FROM shares_outstanding").fetchone()
    assert row == ("2024-04-22", 1_000_000)


def test_parse_skips_unparseable_date_row(tmp_path):
    db = _make_db()
    body = HEADER + b"".join(_row("22-Apr-2024", i) for i in range(30)) + _row("??", 99)
    raw_row = _make_raw_row(db, body, tmp_path)
    assert _parse_mktcap_csv(body, raw_row, db, "v1") == 30
    n = db.execute("SELECT COUNT(*) FROM shares_outstanding WHERE isin LIKE '%99'").fetchone()[0]
    assert n == 0


def test_parse_rejects_file_when_dates_all_fail(tmp_path):
    db = _make_db()
    body = HEADER + b"".join(_row("22.04.2024", i) for i in range(5))
    raw_row = _make_raw_row(db, body, tmp_path)
    with pytest.raises(DateFormatError):
        _parse_mktcap_csv(body, raw_row, db, "v1")
    assert db.execute("SELECT COUNT(*) FROM shares_outstanding").fetchone()[0] == 0
