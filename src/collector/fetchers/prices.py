"""
Daily OHLCV prices fetcher (Category A — daily snapshot).

Primary source: NSE CM bhavcopy (free, authoritative).
The full bhavcopy CSV for all equities is fetched once per day after market close.
Broker API (Kite) is used as cross-check for same-day EOD, not as primary.

SQLite only holds a 30-day rolling window; older rows pruned after confirming parquet has them.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

from collector.base import BaseFetcher, PermanentFetchError
from collector.models import DataSource, Exchange, FetchResult, RawArchiveRow

logger = logging.getLogger(__name__)

# NSE CM bhavcopy — full equity bhavcopy with delivery data.
# URL pattern verified against NSE archives (nsearchives.nseindia.com).
# Date format in filename: DDMMMYYYY  e.g. 22APR2024
_NSE_BHAVCOPY_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
# Fallback URL pattern (older archives use a zip):
_NSE_BHAVCOPY_ZIP_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{date}bhav.csv.zip"


class NsePricesFetcher(BaseFetcher):
    source = DataSource.PRICES

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        d = trade_date or date.today()
        return _NSE_BHAVCOPY_URL.format(date=d.strftime("%d%b%Y").upper())

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError("NSE prices: 404 — likely non-trading day")
        if result.status_code != 200:
            raise ValueError(f"NSE prices: HTTP {result.status_code}")
        # Expect CSV content: should contain SYMBOL,SERIES,OPEN,...
        text = result.body[:500].decode("utf-8", errors="replace")
        if "SYMBOL" not in text and "symbol" not in text:
            raise ValueError("NSE prices: response does not look like a bhavcopy CSV")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        # Some NSE bhavcopy downloads are zipped; try to detect.
        if body[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                name = zf.namelist()[0]
                body = zf.read(name)
        return _parse_nse_bhavcopy_csv(body, raw_row, self._db, self.parser_version)


# ── CSV parser ─────────────────────────────────────────────────────────────────


def _parse_nse_bhavcopy_csv(
    body: bytes,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    NSE CM bhavcopy columns (sec_bhavdata_full variant, 2024):
    SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,
    CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER

    Only EQ and BE series included.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    inserted = 0

    for row in reader:
        series = (row.get("SERIES") or "").strip()
        if series not in ("EQ", "BE", "SM", "ST"):
            continue

        symbol = (row.get("SYMBOL") or "").strip()
        if not symbol:
            continue

        date_str = (row.get("DATE1") or row.get("TIMESTAMP") or "").strip()
        trade_date = _parse_date(date_str)

        try:
            open_ = float((row.get("OPEN_PRICE") or "0").replace(",", ""))
            high = float((row.get("HIGH_PRICE") or "0").replace(",", ""))
            low = float((row.get("LOW_PRICE") or "0").replace(",", ""))
            close = float((row.get("CLOSE_PRICE") or row.get("LAST_PRICE") or "0").replace(",", ""))
            volume = int(float((row.get("TTL_TRD_QNTY") or "0").replace(",", "")))
            turnover_lacs = float((row.get("TURNOVER_LACS") or "0").replace(",", ""))
            value_traded = turnover_lacs * 100_000  # lacs → ₹
        except (ValueError, TypeError):
            continue

        if close <= 0:
            continue

        try:
            db.execute(
                """
                INSERT OR REPLACE INTO prices
                    (stock_symbol, exchange, trade_date, open, high, low, close,
                     volume, value_traded, is_adjusted, adjustment_factor, as_of_date, raw_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    Exchange.NSE.value,
                    trade_date,
                    open_,
                    high,
                    low,
                    close,
                    volume,
                    value_traded,
                    0,
                    1.0,
                    trade_date,
                    raw_row.raw_id,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning(
                "NSE prices: insert failed symbol=%s date=%s: %s", symbol, trade_date, exc
            )

    db.commit()
    return inserted


def prune_old_prices(db: sqlite3.Connection, keep_days: int = 30) -> int:
    """
    Remove price rows older than keep_days from SQLite.
    Call only after confirming those rows exist in the parquet lake.
    Returns count of pruned rows.
    """
    cutoff = date.today()
    from datetime import timedelta

    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    cursor = db.execute("DELETE FROM prices WHERE trade_date < ?", (cutoff,))
    db.commit()
    return cursor.rowcount


def _parse_date(s: str) -> str:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()
