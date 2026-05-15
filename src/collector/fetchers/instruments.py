"""
Instrument master fetcher (Category C — on-demand, weekly refresh).

Fetches Kite Connect instruments CSV and populates the instruments table.
The CSV maps ISIN → NSE symbol → Kite instrument token → Fyers symbol.
All collector joins from BSE data → NSE prices → broker tokens go through this table.

URL: https://api.kite.trade/instruments (public, no auth required for CSV download).
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3

from collector.base import BaseFetcher, _fmt_dt, _now_ist
from collector.models import DataSource, FetchResult, RawArchiveRow

logger = logging.getLogger(__name__)

_KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"


class InstrumentsFetcher(BaseFetcher):
    source = DataSource.INSTRUMENTS

    def fetch_url(self, **kwargs) -> str:
        return _KITE_INSTRUMENTS_URL

    def validate(self, result: FetchResult) -> None:
        if result.status_code != 200:
            raise ValueError(f"Instruments: HTTP {result.status_code}")
        text = result.body[:500].decode("utf-8", errors="replace")
        if "instrument_token" not in text and "tradingsymbol" not in text:
            raise ValueError("Instruments: response does not look like Kite instruments CSV")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        return _parse_kite_instruments_csv(body, raw_row, self._db, self.parser_version)


def _parse_kite_instruments_csv(
    body: bytes,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    Kite instruments CSV columns:
    instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,
    instrument_type,segment,exchange,isin (may not always be present)

    We upsert rows for NSE equities (exchange=NSE, instrument_type=EQ).
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    refreshed_at = _fmt_dt(_now_ist())
    inserted = 0
    updated = 0

    for row in reader:
        exchange = (row.get("exchange") or "").strip().upper()
        instr_type = (row.get("instrument_type") or "").strip().upper()

        if exchange != "NSE" or instr_type not in ("EQ", "BE"):
            continue

        isin = (row.get("isin") or "").strip()
        symbol = (row.get("tradingsymbol") or "").strip()
        name = (row.get("name") or "").strip()

        if not symbol:
            continue
        if not isin:
            # Some rows lack ISIN; use symbol as fallback key for upsert.
            isin = f"NSE_{symbol}"

        try:
            token = int(row.get("instrument_token") or 0)
        except ValueError:
            token = None

        series = instr_type
        fyers_symbol = f"NSE:{symbol}-EQ"

        try:
            existing = db.execute("SELECT isin FROM instruments WHERE isin = ?", (isin,)).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE instruments SET
                        nse_symbol=?, company_name=?,
                        kite_instrument_token=?, kite_tradingsymbol=?,
                        fyers_symbol=?, series=?, last_refreshed=?
                    WHERE isin=?
                    """,
                    (symbol, name, token, symbol, fyers_symbol, series, refreshed_at, isin),
                )
                updated += 1
            else:
                db.execute(
                    """
                    INSERT INTO instruments
                        (isin, nse_symbol, bse_code, company_name,
                         kite_instrument_token, kite_tradingsymbol,
                         fyers_symbol, series, face_value, last_refreshed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        isin,
                        symbol,
                        None,
                        name,
                        token,
                        symbol,
                        fyers_symbol,
                        series,
                        None,
                        refreshed_at,
                    ),
                )
                inserted += 1
        except Exception as exc:
            logger.warning("Instruments: upsert failed isin=%s symbol=%s: %s", isin, symbol, exc)

    db.commit()
    logger.info("Instruments: inserted=%d updated=%d", inserted, updated)
    return inserted + updated
