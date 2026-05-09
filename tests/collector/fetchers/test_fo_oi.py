"""
Tests for F&O OI fetcher — CSV parsing (old and new format), instrument type mapping.

Worked numerical example:
  RELIANCE FUT, expiry 25-Apr-2024, OI=50000, OI_change=2000, volume=15000, settle=2920
  Expected: 1 row in fo_oi_daily with open_interest=50000, oi_change=2000
"""

import gzip
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from collector.fetchers.fo_oi import (
    FoOiFetcher,
    _map_instrument_type,
    _parse_date,
    _parse_fo_bhavcopy_csv,
)
from collector.models import DataSource, InstrumentType, RawArchiveRow


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _make_raw_row(db, body: bytes, tmp_path: Path) -> RawArchiveRow:
    chash = hashlib.sha256(body).hexdigest()
    raw_id = "fo-raw-001"
    rel_path = "fo_oi/2024/04/22/test.gz"
    abs_path = tmp_path / "raw" / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(gzip.compress(body))
    db.execute(
        "INSERT INTO raw_archive (raw_id, source, fetched_at, request_url, "
        "response_status, content_hash, content_path, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (raw_id, "fo_oi", "2024-04-22T18:30:00.000000Z", "https://nsearchives.nseindia.com/x",
         200, chash, rel_path, "pending"),
    )
    db.commit()
    return RawArchiveRow(
        raw_id=raw_id, source=DataSource.FO_OI,
        fetched_at=datetime(2024, 4, 22, 18, 30, 0),
        request_url="https://nsearchives.nseindia.com/x",
        response_status=200, content_hash=chash, content_path=rel_path,
    )


# Old bhavcopy format
FO_OLD_CSV = (
    b"SYMBOL,INSTRUMENT,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
    b"SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"
    b"RELIANCE,FUTSTK,25-Apr-2024,0,XX,2900,2960,2890,2920,"
    b"2920,15000,4380000,50000,2000,22-Apr-2024\n"
    b"NIFTY,FUTIDX,25-Apr-2024,0,XX,22000,22200,21900,22100,"
    b"22100,30000,66300000,180000,5000,22-Apr-2024\n"
    b"RELIANCE,OPTSTK,25-Apr-2024,2900,CE,40,50,35,45,"
    b"45,5000,22500,20000,1000,22-Apr-2024\n"
    b"RELIANCE,OPTSTK,25-Apr-2024,2900,PE,30,38,25,35,"
    b"35,4000,14000,18000,-500,22-Apr-2024\n"
)


def test_instrument_type_mapping_futures():
    assert _map_instrument_type("FUTSTK") == InstrumentType.FUT
    assert _map_instrument_type("FUTIDX") == InstrumentType.FUT


def test_instrument_type_mapping_options():
    assert _map_instrument_type("CE") == InstrumentType.CE
    assert _map_instrument_type("PE") == InstrumentType.PE


def test_instrument_type_mapping_unknown():
    assert _map_instrument_type("UNKNOWN") is None


def test_parse_date_formats():
    assert _parse_date("25-Apr-2024") == "2024-04-25"
    assert _parse_date("2024-04-25") == "2024-04-25"


def test_parse_old_format_numerical_example(tmp_path):
    """
    Worked example: RELIANCE FUTSTK
      OI = 50_000, OI_change = 2_000, volume = 15_000, settle_price = 2920
    """
    db = _make_db()
    raw_row = _make_raw_row(db, FO_OLD_CSV, tmp_path)
    count = _parse_fo_bhavcopy_csv(FO_OLD_CSV, raw_row, db, "v1")

    assert count >= 3  # FUT + CE + PE for RELIANCE + NIFTY FUT
    reliance_fut = db.execute(
        "SELECT open_interest, oi_change, volume, close_price, instrument_type "
        "FROM fo_oi_daily WHERE underlying_symbol='RELIANCE' AND instrument_type='FUT'"
    ).fetchone()
    assert reliance_fut is not None
    oi, oi_chg, vol, close, instr = reliance_fut
    assert oi == 50_000
    assert oi_chg == 2_000
    assert vol == 15_000
    assert close == pytest.approx(2920.0)
    assert instr == "FUT"


def test_parse_old_format_ce_pe(tmp_path):
    db = _make_db()
    raw_row = _make_raw_row(db, FO_OLD_CSV, tmp_path)
    _parse_fo_bhavcopy_csv(FO_OLD_CSV, raw_row, db, "v1")

    ce = db.execute(
        "SELECT strike_price, open_interest FROM fo_oi_daily "
        "WHERE underlying_symbol='RELIANCE' AND instrument_type='CE'"
    ).fetchone()
    assert ce is not None
    assert ce[0] == pytest.approx(2900.0)
    assert ce[1] == 20_000

    pe = db.execute(
        "SELECT oi_change FROM fo_oi_daily "
        "WHERE underlying_symbol='RELIANCE' AND instrument_type='PE'"
    ).fetchone()
    assert pe is not None
    assert pe[0] == -500


def test_validate_requires_zip(tmp_path):
    db = _make_db()
    fetcher = FoOiFetcher(db, tmp_path / "raw")
    from collector.models import FetchResult
    bad = FetchResult(
        source=DataSource.FO_OI, url="x", status_code=200,
        body=b"Not a zip file here",
        content_hash="z", fetched_at=datetime(2024, 4, 22),
    )
    with pytest.raises(ValueError, match="ZIP"):
        fetcher.validate(bad)


def test_validate_404_raises(tmp_path):
    db = _make_db()
    fetcher = FoOiFetcher(db, tmp_path / "raw")
    from collector.models import FetchResult
    bad = FetchResult(
        source=DataSource.FO_OI, url="x", status_code=404,
        body=b"Not Found", content_hash="z",
        fetched_at=datetime(2024, 4, 22),
    )
    with pytest.raises(Exception, match="404"):
        fetcher.validate(bad)
