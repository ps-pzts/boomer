"""
Daily OHLCV prices fetcher (Category A — daily snapshot).

Primary source: NSE CM bhavcopy (free, authoritative).
The full bhavcopy ZIP for all equities is fetched once per day after market close.
Broker API (Kite) is used as cross-check for same-day EOD, not as primary.

SQLite only holds a 30-day rolling window; older rows pruned after confirming parquet has them.

URL format (as of 2025): BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
Column format: TckrSymb, SctySrs, TradDt, OpnPric, HghPric, LwPric, ClsPric,
               TtlTradgVol, TtlTrfVal (in ₹, not lacs)
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

# NSE CM bhavcopy — new URL format (post-2025 redesign).
# Date format in filename: YYYYMMDD  e.g. 20260511
_NSE_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
)


class NsePricesFetcher(BaseFetcher):
    source = DataSource.PRICES

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        d = trade_date or date.today()
        return _NSE_BHAVCOPY_URL.format(date=d.strftime("%Y%m%d"))

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError("NSE prices: 404 — likely non-trading day")
        if result.status_code != 200:
            raise ValueError(f"NSE prices: HTTP {result.status_code}")
        # Bhavcopy is delivered as a ZIP; verify magic bytes.
        if result.body[:2] != b"PK":
            raise ValueError("NSE prices: response is not a ZIP file (unexpected format)")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        if body[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                body = zf.read(zf.namelist()[0])
        return _parse_nse_bhavcopy_csv(body, raw_row, self._db, self.parser_version)


# ── CSV parser ─────────────────────────────────────────────────────────────────


def _parse_nse_bhavcopy_csv(
    body: bytes,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    NSE CM bhavcopy columns (BhavCopy_NSE_CM variant, post-2025):
    TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb, SctySrs,
    OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, TtlTradgVol, TtlTrfVal, ...

    Only EQ and BE series included.
    TtlTrfVal is in rupees (not lacs — the old format used TURNOVER_LACS).
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    inserted = 0

    for row in reader:
        series = (row.get("SctySrs") or "").strip()
        if series not in ("EQ", "BE", "SM", "ST"):
            continue

        symbol = (row.get("TckrSymb") or "").strip()
        if not symbol:
            continue

        date_str = (row.get("TradDt") or row.get("BizDt") or "").strip()
        trade_date = _parse_date(date_str)

        try:
            open_ = float((row.get("OpnPric") or "0").replace(",", ""))
            high = float((row.get("HghPric") or "0").replace(",", ""))
            low = float((row.get("LwPric") or "0").replace(",", ""))
            close = float((row.get("ClsPric") or row.get("LastPric") or "0").replace(",", ""))
            volume = int(float((row.get("TtlTradgVol") or "0").replace(",", "")))
            value_traded = float((row.get("TtlTrfVal") or "0").replace(",", ""))  # already in ₹
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
    from datetime import timedelta

    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    cursor = db.execute("DELETE FROM prices WHERE trade_date < ?", (cutoff,))
    db.commit()
    return cursor.rowcount


def _parse_date(s: str) -> str:
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()
