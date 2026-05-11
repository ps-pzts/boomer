"""
NSE sector classification fetcher (Category B — weekly refresh).

Fetches NSE sectoral index constituent CSVs. Each index file maps
stock symbols to their industry. One HTTP request per sector index;
approximately 16 requests total, run weekly (sector changes are rare).

NSE index constituent CSV format (as of 2025):
  Company Name, Industry, Symbol, Series, ISIN Code

URL pattern:
  https://nsearchives.nseindia.com/content/indices/ind_{stub}list.csv

Each file is archived independently so the parse worker can process them
one at a time. The broad sector label comes from which index was fetched;
the granular industry comes from the CSV's "Industry" column.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
import sqlite3

from collector.base import BaseFetcher, PermanentFetchError, _fmt_dt
from collector.models import DataSource, FetchResult, RawArchiveRow

logger = logging.getLogger(__name__)

_BASE_URL = "https://nsearchives.nseindia.com/content/indices/ind_{stub}list.csv"

# (filename_stub, broad_sector_label)
# Stubs verified against NSE archives — 404s are skipped gracefully.
_SECTOR_INDICES: list[tuple[str, str]] = [
    ("niftybank",           "Banks"),
    ("niftyit",             "Information Technology"),
    ("niftypharma",         "Healthcare & Pharmaceuticals"),
    ("niftyauto",           "Automobile"),
    ("niftyfmcg",           "FMCG"),
    ("niftymetal",          "Metals & Mining"),
    ("niftyrealty",         "Real Estate"),
    ("niftyenergy",         "Energy"),
    ("niftyinfra",          "Infrastructure"),
    ("niftymedia",          "Media & Entertainment"),
    ("niftypsubank",          "PSU Banks"),
    ("niftyhealthcare",       "Healthcare & Pharmaceuticals"),
    ("niftyoilgas",           "Oil & Gas"),
    ("niftyconsumerdurables", "Consumer Durables"),
]

# Reverse lookup: URL substring → sector (used in parse())
_STUB_TO_SECTOR: dict[str, str] = {stub: sector for stub, sector in _SECTOR_INDICES}


class NseSectorFetcher(BaseFetcher):
    """Fetches NSE sectoral index constituent CSVs to populate sector_classifications."""

    source = DataSource.SECTOR_CLASSIFICATIONS

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    # ── override run() to handle multiple index files ─────────────────────

    def run(self, trade_date: date | None = None, **kwargs) -> RawArchiveRow | None:
        """Fetch all sector index CSVs; archive each independently."""
        run_date = (trade_date or date.today()).isoformat()
        last_raw_row: RawArchiveRow | None = None

        for stub, _sector in _SECTOR_INDICES:
            url = _BASE_URL.format(stub=stub)
            try:
                result = self.transport(url)
                self.validate(result)
                raw_row = self.archive(result)
                if raw_row is not None:
                    last_raw_row = raw_row
                    logger.info("sector archived stub=%s date=%s", stub, run_date)
            except PermanentFetchError as exc:
                logger.info("sector fetch skipped stub=%s: %s", stub, exc)
            except Exception as exc:
                logger.warning("sector fetch failed stub=%s: %s", stub, exc)
            time.sleep(1)  # polite rate limiting between index requests

        return last_raw_row

    # ── required abstract methods ─────────────────────────────────────────

    def fetch_url(self, **kwargs) -> str:
        # Not called directly (run() is overridden) but required by ABC.
        return _BASE_URL.format(stub="niftybank")

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError(f"NSE sector index: 404 for {result.url}")
        if result.status_code != 200:
            raise ValueError(f"NSE sector index: HTTP {result.status_code} for {result.url}")
        text = result.body.decode("utf-8", errors="replace").strip()
        if len(text) < 30:
            raise PermanentFetchError(f"NSE sector index: empty body for {result.url}")
        if "Symbol" not in text[:200] and "SYMBOL" not in text[:200]:
            raise ValueError(f"NSE sector index: response doesn't look like index CSV: {result.url}")

    def parse(self, raw_row: RawArchiveRow) -> int:
        """Identify which sector index this raw_row is for and write classifications."""
        url = raw_row.request_url
        sector = None
        for stub, label in _SECTOR_INDICES:
            if stub in url:
                sector = label
                break
        if sector is None:
            logger.warning("sector parse: cannot determine sector from url=%s", url)
            return 0

        body = self.load_raw_body(raw_row.content_path)
        return _parse_index_csv(body, sector, raw_row, self._db, self.parser_version)


# ── Parser ────────────────────────────────────────────────────────────────────


def _parse_index_csv(
    body: bytes,
    sector: str,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    NSE index constituent CSV:
      Company Name, Industry, Symbol, Series, ISIN Code

    Writes one row per symbol into sector_classifications.
    Uses INSERT OR REPLACE so re-runs update stale entries.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    now_str = _fmt_dt(datetime.now(UTC))
    run_date = raw_row.fetched_at.date().isoformat()
    inserted = 0

    for row in reader:
        symbol = (row.get("Symbol") or row.get("SYMBOL") or "").strip()
        industry = (row.get("Industry") or row.get("INDUSTRY") or "").strip()
        series = (row.get("Series") or row.get("SERIES") or "").strip()

        if not symbol or series not in ("EQ", "BE", ""):
            continue

        try:
            db.execute(
                """
                INSERT OR REPLACE INTO sector_classifications
                    (symbol, exchange, sector, industry, source, effective_from, updated_at)
                VALUES (?, 'NSE', ?, ?, 'NSE', ?, ?)
                """,
                (symbol, sector, industry or None, run_date, now_str),
            )
            inserted += 1
        except Exception as exc:
            logger.warning("sector insert failed symbol=%s: %s", symbol, exc)

    db.commit()
    logger.info("sector parse sector=%s symbols=%d", sector, inserted)
    return inserted
