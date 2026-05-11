"""
Tests for bulk deals fetcher — parsing, smart money detection, dedup.
No real HTTP calls.

NSE: static bulk.csv (CSV format, same URL every day).
BSE: /BulkDeal_Beta/w returns JSON {"Table": [...]}.

Worked numerical example (verified by hand):
  NSE row: Symbol=RELIANCE, Client=LIC OF INDIA, Buy/Sell=B, Qty=1000000, Price=2900.50
  Expected: value=2900500000.0, is_smart_money=1, tx_type=BUY

  BSE row: SCRIP_CODE=500325, Client=GOVERNMENT OF SINGAPORE, B, Qty=200000, Price=2901.00
  Expected: value=580200000.0, is_smart_money=1, tx_type=BUY
"""

import gzip
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from collector.fetchers.bulk_deals import (
    NseBulkDealsFetcher,
    _is_smart_money,
    _normalize_client_name,
    _parse_bse_bulk_json,
    _parse_date,
    _parse_nse_bulk_csv,
)
from collector.models import DataSource, RawArchiveRow


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _make_raw_row(
    db, body: bytes, tmp_path: Path, source=DataSource.NSE_BULK_DEALS
) -> RawArchiveRow:
    chash = hashlib.sha256(body).hexdigest()
    raw_id = "bulk-raw-001"
    rel_path = f"{source.value}/2024/04/22/test.gz"
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
            "2024-04-22T02:00:00.000000Z",
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
        source=source,
        fetched_at=datetime(2024, 4, 22, 2, 0, 0),
        request_url="https://nsearchives.nseindia.com/x",
        response_status=200,
        content_hash=chash,
        content_path=rel_path,
    )


NSE_CSV = (
    b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
    b"Trade Price / Wght. Avg. Price,Remarks\n"
    b'22/04/2024,RELIANCE,RELIANCE INDUSTRIES LTD,LIC OF INDIA,BUY,"1,000,000","2,900.50",-\n'
    b'22/04/2024,INFY,INFOSYS LTD,JANE SMITH PARTNERS,SELL,"500,000","1,500.00",-\n'
)

# BSE now returns JSON from /BulkDeal_Beta/w endpoint
BSE_JSON = b"""{
  "Table": [
    {"DEAL_DATE": "22/04/2024", "SCRIP_CODE": 500325, "ScripName": "RELIANCE INDUSTRIES",
     "CLIENT_NAME": "GOVERNMENT OF SINGAPORE", "TRANSACTION_TYPE": "B",
     "QUANTITY": 200000.0, "PRICE": 2901.00, "SENDTOWEBSITE": "2024-04-22T00:00:00"},
    {"DEAL_DATE": "22/04/2024", "SCRIP_CODE": 500010, "ScripName": "HDFC BANK",
     "CLIENT_NAME": "UNKNOWN TRADER", "TRANSACTION_TYPE": "S",
     "QUANTITY": 50000.0, "PRICE": 1700.00, "SENDTOWEBSITE": "2024-04-22T00:00:00"}
  ]
}"""


def test_smart_money_lic(tmp_path):
    assert _is_smart_money("LIC OF INDIA") is True


def test_smart_money_government_singapore(tmp_path):
    assert _is_smart_money("GOVERNMENT OF SINGAPORE") is True


def test_smart_money_unknown_entity(tmp_path):
    assert _is_smart_money("RANDOM PRIVATE INVESTOR") is False


def test_normalize_client_name():
    assert _normalize_client_name("  lic of india  ") == "LIC OF INDIA"
    assert _normalize_client_name("Jane  Smith") == "JANE SMITH"


def test_parse_date_dmy_slash():
    assert _parse_date("22/04/2024") == "2024-04-22"


def test_parse_date_dmy_dash():
    assert _parse_date("22-04-2024") == "2024-04-22"


def test_parse_date_iso():
    assert _parse_date("2024-04-22") == "2024-04-22"


def test_nse_bulk_csv_numerical_example(tmp_path):
    """
    Worked example (verifiable by hand):
      RELIANCE, LIC OF INDIA, BUY, 1_000_000 shares @ ₹2900.50
      Expected value = 1_000_000 × 2_900.50 = ₹2_900_500_000
    """
    db = _make_db()
    raw_row = _make_raw_row(db, NSE_CSV, tmp_path)
    count = _parse_nse_bulk_csv(NSE_CSV, raw_row, db, "v1")

    assert count == 2
    reliance = db.execute(
        "SELECT quantity, price, value, is_smart_money, transaction_type "
        "FROM bulk_deals WHERE stock_symbol='RELIANCE'"
    ).fetchone()
    assert reliance is not None
    qty, price, value, smart, tx = reliance
    assert qty == 1_000_000
    assert price == pytest.approx(2900.50)
    assert value == pytest.approx(2_900_500_000.0)
    assert smart == 1  # LIC is smart money
    assert tx == "BUY"


def test_nse_bulk_csv_sell_non_smart_money(tmp_path):
    db = _make_db()
    raw_row = _make_raw_row(db, NSE_CSV, tmp_path)
    _parse_nse_bulk_csv(NSE_CSV, raw_row, db, "v1")

    infy = db.execute(
        "SELECT is_smart_money, transaction_type FROM bulk_deals WHERE stock_symbol='INFY'"
    ).fetchone()
    assert infy[0] == 0  # JANE SMITH not smart money
    assert infy[1] == "SELL"


def test_bse_bulk_json_numerical_example(tmp_path):
    """
    Worked example (BSE /BulkDeal_Beta/w JSON format):
      SCRIP_CODE=500325, GOVERNMENT OF SINGAPORE, BUY, 200_000 @ ₹2901.00
      Expected value = 200_000 × 2_901.00 = ₹580_200_000
    """
    db = _make_db()
    raw_row = _make_raw_row(db, BSE_JSON, tmp_path, source=DataSource.BSE_BULK_DEALS)
    count = _parse_bse_bulk_json(BSE_JSON, raw_row, db, "v1")

    assert count == 2
    row = db.execute(
        "SELECT quantity, price, value, is_smart_money, exchange "
        "FROM bulk_deals WHERE stock_symbol='500325'"
    ).fetchone()
    assert row is not None
    qty, price, value, smart, exchange = row
    assert qty == 200_000
    assert price == pytest.approx(2901.00)
    assert value == pytest.approx(580_200_000.0)
    assert smart == 1
    assert exchange == "BSE"


def test_nse_parse_skips_empty_csv(tmp_path):
    db = _make_db()
    raw_row = _make_raw_row(db, b"Date,Symbol\n", tmp_path)
    count = _parse_nse_bulk_csv(b"Date,Symbol\n", raw_row, db, "v1")
    assert count == 0


def test_nse_validate_empty_body_raises(tmp_path):
    db = _make_db()
    fetcher = NseBulkDealsFetcher(db, tmp_path / "raw")
    from collector.models import FetchResult

    bad = FetchResult(
        source=DataSource.NSE_BULK_DEALS,
        url="x",
        status_code=200,
        body=b"  ",
        content_hash="z",
        fetched_at=datetime(2024, 4, 22),
    )
    with pytest.raises(ValueError, match="empty"):
        fetcher.validate(bad)
