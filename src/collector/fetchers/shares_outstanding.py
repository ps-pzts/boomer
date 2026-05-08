"""
Shares outstanding fetcher (Category A — daily snapshot).

Source: NSE CM Bhavcopy with Market Cap (TOTAL_SHARES column).
Fetched once per day. Required by Stage 0 to compute promoter_holding_pct.

VERIFY BEFORE DEPLOYING:
  - Exact NSE URL and filename pattern for the "CM Bhavcopy with Market Cap" file.
  - Column name for total issued shares (assumed TOTAL_SHARES — confirm from actual file).
  - Whether the file contains total issued capital or derives it from market cap / close price.
  Run: download one file manually from NSE archives and inspect headers before coding the parser.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

from collector.base import BaseFetcher, _fmt_dt
from collector.models import DataSource, FetchResult, RawArchiveRow

logger = logging.getLogger(__name__)

# VERIFY: This URL pattern is a best-guess based on NSE archive structure.
# NSE publishes a "Bhavcopy with Delivery Data" and a "Market Capitalisation" file separately.
# The market cap file at this path includes total issued capital (TOTAL_SHARES).
# Confirm exact URL by downloading from: https://www.nseindia.com/market-data/equity-archives
_NSE_MKTCAP_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_mktcap_{date}.csv"
)

# VERIFY: expected column name for total shares in the NSE market cap bhavcopy.
_TOTAL_SHARES_COL = "TOTAL_SHARES"  # confirm from actual downloaded file


class SharesOutstandingFetcher(BaseFetcher):
    source = DataSource.SHARES_OUTSTANDING

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        d = trade_date or date.today()
        return _NSE_MKTCAP_BHAVCOPY_URL.format(date=d.strftime("%d%b%Y").upper())

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise ValueError("Shares outstanding: 404 — non-trading day or URL pattern wrong")
        if result.status_code != 200:
            raise ValueError(f"Shares outstanding: HTTP {result.status_code}")
        # Accept CSV or ZIP; check we have some content.
        if len(result.body) < 100:
            raise ValueError("Shares outstanding: response body too small")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        if body[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
                body = zf.read(csv_name)
        return _parse_mktcap_csv(body, raw_row, self._db, self.parser_version)


def _parse_mktcap_csv(
    body: bytes,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    Assumed columns (VERIFY against actual NSE file):
    SYMBOL, SERIES, DATE, ISIN, TOTAL_SHARES, ...

    If the file does not have TOTAL_SHARES but has MARKET_CAP and CLOSE_PRICE,
    compute: total_shares = market_cap / close_price.
    Update this parser once the actual file is inspected.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = {f.strip().upper() for f in (reader.fieldnames or [])}

    has_total_shares = _TOTAL_SHARES_COL in fieldnames
    has_mktcap = "MARKET_CAP" in fieldnames or "MKTCAP" in fieldnames
    has_close = "CLOSE_PRICE" in fieldnames or "CLOSE" in fieldnames

    if not has_total_shares and not (has_mktcap and has_close):
        logger.error(
            "Shares outstanding: cannot find TOTAL_SHARES or MARKET_CAP+CLOSE_PRICE columns. "
            "Available columns: %s. VERIFY URL and column names against actual NSE file.",
            list(fieldnames)[:20],
        )
        return 0

    inserted = 0
    for row in reader:
        series = (row.get("SERIES") or "").strip()
        if series not in ("EQ", "BE", ""):
            continue

        symbol = (row.get("SYMBOL") or "").strip()
        isin = (row.get("ISIN") or "").strip()
        date_str = (row.get("DATE") or row.get("DATE1") or "").strip()
        trade_date = _parse_date(date_str)

        if not symbol or not isin:
            continue

        try:
            if has_total_shares:
                total_shares_str = (row.get(_TOTAL_SHARES_COL) or "0").replace(",", "")
                total_shares = int(float(total_shares_str))
            else:
                mktcap_col = "MARKET_CAP" if "MARKET_CAP" in (row or {}) else "MKTCAP"
                close_col = "CLOSE_PRICE" if "CLOSE_PRICE" in (row or {}) else "CLOSE"
                mktcap = float((row.get(mktcap_col) or "0").replace(",", ""))
                close = float((row.get(close_col) or "0").replace(",", ""))
                if close <= 0:
                    continue
                total_shares = int(mktcap / close)
        except (ValueError, TypeError):
            continue

        if total_shares <= 0:
            continue

        try:
            db.execute(
                """
                INSERT OR REPLACE INTO shares_outstanding
                    (isin, stock_symbol, exchange, trade_date, total_shares, observed_at, raw_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    isin, symbol, "NSE", trade_date, total_shares,
                    _fmt_dt(raw_row.fetched_at), raw_row.raw_id,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning("Shares outstanding: insert failed isin=%s: %s", isin, exc)

    db.commit()
    return inserted


def _parse_date(s: str) -> str:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d%b%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()
