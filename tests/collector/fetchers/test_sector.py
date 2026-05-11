"""
Tests for NSE sector classification fetcher — parsing and DB writes.
No real HTTP calls.

NSE index constituent CSV format:
  Company Name, Industry, Symbol, Series, ISIN Code

Worked example (Nifty IT index):
  INFY  → Information Technology, IT - Software
  TCS   → Information Technology, IT - Software
  WIPRO → Information Technology, IT - Software
"""

import glob
import gzip
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from collector.base import PermanentFetchError
from collector.fetchers.sector import _STUB_TO_SECTOR, NseSectorFetcher, _parse_index_csv
from collector.models import DataSource, FetchResult, RawArchiveRow


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "boomer.db"))
    for f in sorted(glob.glob("migrations/*.sql")):
        with open(f) as fh:
            conn.executescript(fh.read())
    return conn


def _make_raw_row(db, body: bytes, tmp_path: Path, url: str) -> RawArchiveRow:
    chash = hashlib.sha256(body).hexdigest()
    raw_id = "sector-raw-001"
    rel_path = "sector_classifications/2024/05/01/test.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(body))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_id,
            DataSource.SECTOR_CLASSIFICATIONS.value,
            "2024-05-01T10:00:00.000000Z",
            url,
            200,
            chash,
            rel_path,
            "pending",
        ),
    )
    db.commit()
    return RawArchiveRow(
        raw_id=raw_id,
        source=DataSource.SECTOR_CLASSIFICATIONS,
        fetched_at=datetime(2024, 5, 1, 10, 0, 0),
        request_url=url,
        response_status=200,
        content_hash=chash,
        content_path=rel_path,
    )


IT_CSV = (
    b"Company Name,Industry,Symbol,Series,ISIN Code\n"
    b"Infosys Ltd,IT - Software,INFY,EQ,INE009A01021\n"
    b"Tata Consultancy Services Ltd,IT - Software,TCS,EQ,INE467B01029\n"
    b"Wipro Ltd,IT - Hardware,WIPRO,EQ,INE075A01022\n"
)

BANK_CSV = (
    b"Company Name,Industry,Symbol,Series,ISIN Code\n"
    b"HDFC Bank Ltd,Banks,HDFCBANK,EQ,INE040A01034\n"
    b"ICICI Bank Ltd,Banks,ICICIBANK,EQ,INE090A01021\n"
)


def test_stub_to_sector_mapping():
    assert _STUB_TO_SECTOR["niftyit"] == "Information Technology"
    assert _STUB_TO_SECTOR["niftybank"] == "Banks"
    assert _STUB_TO_SECTOR["niftypharma"] == "Healthcare & Pharmaceuticals"


def test_parse_it_index_csv(tmp_path):
    """IT index: 3 stocks written with sector=Information Technology."""
    db = _make_db(tmp_path)
    url = "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv"
    raw_row = _make_raw_row(db, IT_CSV, tmp_path, url)
    count = _parse_index_csv(IT_CSV, "Information Technology", raw_row, db, "v1")

    assert count == 3
    rows = db.execute(
        "SELECT symbol, sector, industry FROM sector_classifications ORDER BY symbol"
    ).fetchall()
    symbols = {r[0] for r in rows}
    assert {"INFY", "TCS", "WIPRO"} == symbols
    for r in rows:
        assert r[1] == "Information Technology"
    infy = next(r for r in rows if r[0] == "INFY")
    assert infy[2] == "IT - Software"


def test_parse_bank_index_csv(tmp_path):
    """Bank index: 2 stocks written with sector=Banks."""
    db = _make_db(tmp_path)
    url = "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv"
    raw_row = _make_raw_row(db, BANK_CSV, tmp_path, url)
    count = _parse_index_csv(BANK_CSV, "Banks", raw_row, db, "v1")

    assert count == 2
    row = db.execute("SELECT sector FROM sector_classifications WHERE symbol='HDFCBANK'").fetchone()
    assert row[0] == "Banks"


def test_parse_skips_non_eq_series(tmp_path):
    """Rows with Series other than EQ/BE should be skipped."""
    csv_body = (
        b"Company Name,Industry,Symbol,Series,ISIN Code\n"
        b"Some ETF,ETF,NIFTYETF,GS,INE000X00001\n"
        b"Real Stock,Banks,AXISBANK,EQ,INE238A01034\n"
    )
    db = _make_db(tmp_path)
    url = "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv"
    raw_row = _make_raw_row(db, csv_body, tmp_path, url)
    count = _parse_index_csv(csv_body, "Banks", raw_row, db, "v1")

    assert count == 1  # only AXISBANK (EQ), not the GS-series ETF


def test_parse_is_idempotent(tmp_path):
    """Re-parsing the same file should not create duplicates."""
    db = _make_db(tmp_path)
    url = "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv"
    raw_row = _make_raw_row(db, IT_CSV, tmp_path, url)
    _parse_index_csv(IT_CSV, "Information Technology", raw_row, db, "v1")
    _parse_index_csv(IT_CSV, "Information Technology", raw_row, db, "v1")

    count = db.execute("SELECT COUNT(*) FROM sector_classifications").fetchone()[0]
    assert count == 3  # no duplicates


def test_fetcher_parse_dispatches_by_url(tmp_path):
    """NseSectorFetcher.parse() routes to correct sector based on URL."""
    db = _make_db(tmp_path)
    fetcher = NseSectorFetcher(db, tmp_path / "raw")
    url = "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv"
    raw_row = _make_raw_row(db, IT_CSV, tmp_path, url)

    count = fetcher.parse(raw_row)
    assert count == 3
    row = db.execute("SELECT sector FROM sector_classifications WHERE symbol='TCS'").fetchone()
    assert row[0] == "Information Technology"


def test_validate_rejects_non_csv(tmp_path):
    """validate() should raise on HTML/non-CSV responses."""
    db = _make_db(tmp_path)
    fetcher = NseSectorFetcher(db, tmp_path / "raw")
    bad = FetchResult(
        source=DataSource.SECTOR_CLASSIFICATIONS,
        url="https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv",
        status_code=200,
        body=b"<!DOCTYPE html><html>error page</html>",
        content_hash="z",
        fetched_at=datetime(2024, 5, 1),
    )
    with pytest.raises(ValueError, match="doesn't look like"):
        fetcher.validate(bad)


def test_validate_404_raises_permanent(tmp_path):
    """validate() should raise PermanentFetchError on 404."""
    db = _make_db(tmp_path)
    fetcher = NseSectorFetcher(db, tmp_path / "raw")
    result = FetchResult(
        source=DataSource.SECTOR_CLASSIFICATIONS,
        url="https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
        status_code=404,
        body=b"",
        content_hash="z",
        fetched_at=datetime(2024, 5, 1),
    )
    with pytest.raises(PermanentFetchError):
        fetcher.validate(result)
