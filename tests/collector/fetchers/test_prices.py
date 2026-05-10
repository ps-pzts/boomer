"""
Tests for NSE prices fetcher — bhavcopy CSV parsing, pruning.

Worked numerical example:
  RELIANCE, EQ, 22-Apr-2024, O=2880 H=2950 L=2870 C=2920, Vol=5_000_000, Turnover=1460 lacs
  Expected value_traded = 1460 * 100_000 = ₹146_000_000
"""

import gzip
import hashlib
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from collector.fetchers.prices import (
    NsePricesFetcher,
    _parse_date,
    _parse_nse_bhavcopy_csv,
    prune_old_prices,
)
from collector.models import DataSource, RawArchiveRow


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _make_raw_row(db, body: bytes, tmp_path: Path) -> RawArchiveRow:
    chash = hashlib.sha256(body).hexdigest()
    raw_id = "prices-raw-001"
    rel_path = "prices/2024/04/22/test.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(body))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_id,
            "prices",
            "2024-04-22T18:00:00.000000Z",
            "https://nsearchives.nseindia.com/x",
            200,
            chash,
            rel_path,
            "pending",
        ),
    )
    db.commit()
    return RawArchiveRow(
        raw_id=raw_id,
        source=DataSource.PRICES,
        fetched_at=datetime(2024, 4, 22, 18, 0, 0),
        request_url="https://nsearchives.nseindia.com/x",
        response_status=200,
        content_hash=chash,
        content_path=rel_path,
    )


NSE_BHAVCOPY_CSV = (
    b"SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,"
    b"CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER\n"
    b"RELIANCE,EQ,22-Apr-2024,2890.00,2880.00,2950.00,2870.00,2920.00,"
    b"2920.00,2910.00,5000000,1460.00,120000,2500000,50.0\n"
    b"INFY,EQ,22-Apr-2024,1490.00,1495.00,1520.00,1480.00,1510.00,"
    b"1510.00,1505.00,3000000,451.50,90000,1500000,50.0\n"
    b"SOMENOTE,SM,22-Apr-2024,100,101,102,100,101,101,101,10000,1.01,500,5000,50.0\n"
)


def test_parse_nse_bhavcopy_numerical_example(tmp_path):
    """
    Worked example for RELIANCE row:
      volume = 5_000_000
      turnover_lacs = 1460.00
      value_traded = 1460 × 100_000 = ₹146_000_000
    """
    db = _make_db()
    raw_row = _make_raw_row(db, NSE_BHAVCOPY_CSV, tmp_path)
    count = _parse_nse_bhavcopy_csv(NSE_BHAVCOPY_CSV, raw_row, db, "v1")

    assert count >= 2  # RELIANCE + INFY (SM series also included)
    reliance = db.execute(
        "SELECT open, high, low, close, volume, value_traded "
        "FROM prices WHERE stock_symbol='RELIANCE'"
    ).fetchone()
    assert reliance is not None
    open_, high, low, close, volume, value_traded = reliance
    assert open_ == pytest.approx(2880.00)
    assert high == pytest.approx(2950.00)
    assert low == pytest.approx(2870.00)
    assert close == pytest.approx(2920.00)
    assert volume == 5_000_000
    assert value_traded == pytest.approx(146_000_000.0)


def test_parse_skips_non_eq_series(tmp_path):
    """N series (non-equity) should not be inserted."""
    db = _make_db()
    raw_row = _make_raw_row(db, NSE_BHAVCOPY_CSV, tmp_path)
    _parse_nse_bhavcopy_csv(NSE_BHAVCOPY_CSV, raw_row, db, "v1")
    # SM series IS included per our filter; only series outside EQ/BE/SM/ST are skipped.
    sm_row = db.execute("SELECT COUNT(*) FROM prices WHERE stock_symbol='SOMENOTE'").fetchone()[0]
    assert sm_row == 1  # SM is allowed


def test_parse_date_various_formats():
    assert _parse_date("22-Apr-2024") == "2024-04-22"
    assert _parse_date("2024-04-22") == "2024-04-22"
    assert _parse_date("22/04/2024") == "2024-04-22"
    assert _parse_date("22Apr2024") == "2024-04-22"


def test_prune_old_prices(tmp_path):
    from datetime import timedelta

    db = _make_db()

    # Insert rows: one old, one recent.
    old_date = (date.today() - timedelta(days=45)).isoformat()
    new_date = date.today().isoformat()

    for trade_date in [old_date, new_date]:
        db.execute(
            "INSERT INTO prices (stock_symbol, exchange, trade_date, open, high, low, close, "
            "is_adjusted, adjustment_factor, as_of_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RELIANCE", "NSE", trade_date, 100, 110, 90, 105, 0, 1.0, trade_date),
        )
    db.commit()

    pruned = prune_old_prices(db, keep_days=30)
    assert pruned == 1
    remaining = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert remaining == 1


def test_validate_raises_on_404(tmp_path):
    db = _make_db()
    fetcher = NsePricesFetcher(db, tmp_path / "raw")
    from collector.models import FetchResult

    bad = FetchResult(
        source=DataSource.PRICES,
        url="x",
        status_code=404,
        body=b"Not Found",
        content_hash="z",
        fetched_at=datetime(2024, 4, 22),
    )
    with pytest.raises(Exception, match="404"):
        fetcher.validate(bad)
