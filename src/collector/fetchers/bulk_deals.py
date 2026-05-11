"""
Bulk and block deals fetcher (Category A — daily snapshot).

Fetches NSE and BSE bulk deal data once per day after market close (~6 PM IST).
Rate: 2 requests per day total (1 NSE + 1 BSE).

NSE: static bulk.csv updated daily — no date in URL, Last-Modified header is authoritative.
BSE: JSON API /BulkDeal_Beta/w — returns Table[] with SCRIP_CODE (not NSE symbol).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

from collector.base import BaseFetcher, PermanentFetchError, _fmt_dt
from collector.models import (
    DataSource,
    Exchange,
    FetchResult,
    RawArchiveRow,
    TransactionType,
)

logger = logging.getLogger(__name__)

# NSE: static file updated once daily — same URL every day, Last-Modified tells the date.
_NSE_BULK_DEALS_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"

# BSE: JSON API — date format YYYYMMDD, returns {"Table": [...]} JSON.
_BSE_BULK_DEALS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w"
    "?quotetype=EQ&strdate={date}&todate={date}&segment=D"
)

# Known smart-money investors tracked by name substring (case-insensitive).
# Maintained as a lightweight list — not a substitute for a proper smart-money database.
_SMART_MONEY_SUBSTRINGS = [
    "fidelity",
    "vanguard",
    "blackrock",
    "government of singapore",
    "government pension",
    "norges bank",
    "national pension",
    "sbi life",
    "hdfc life",
    "lic of india",
    "icici prudential",
    "kotak mahindra",
    "mirae asset",
    "axis mutual",
    "nippon india",
]


class NseBulkDealsFetcher(BaseFetcher):
    source = DataSource.NSE_BULK_DEALS

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        # URL is static — the same file is overwritten each trading day.
        return _NSE_BULK_DEALS_URL

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError("NSE bulk deals: 404 — file missing")
        if result.status_code != 200:
            raise ValueError(f"NSE bulk deals: HTTP {result.status_code}")
        text = result.body.decode("utf-8", errors="replace").strip()
        if len(text) < 20:
            raise ValueError("NSE bulk deals: empty response body")
        # Verify it looks like a CSV (first line should be the header)
        if "Symbol" not in text[:100] and "SYMBOL" not in text[:100]:
            raise ValueError("NSE bulk deals: response does not look like a bulk deals CSV")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        return _parse_nse_bulk_csv(body, raw_row, self._db, self.parser_version)


class BseBulkDealsFetcher(BaseFetcher):
    source = DataSource.BSE_BULK_DEALS

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update(
            {
                "Referer": "https://www.bseindia.com/markets/MarketInfo/BulkDealDataDownload.aspx",
                "Origin": "https://www.bseindia.com",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        d = trade_date or date.today()
        ds = d.strftime("%Y%m%d")
        return _BSE_BULK_DEALS_URL.format(date=ds)

    def validate(self, result: FetchResult) -> None:
        if result.status_code != 200:
            raise ValueError(f"BSE bulk deals: HTTP {result.status_code}")
        # Detect anti-bot redirect: BSE returns 200 with an HTML error page
        text = result.body.decode("utf-8", errors="replace").strip()
        if text.startswith("<!") or "error_Bse" in text or "<html" in text.lower():
            raise PermanentFetchError(
                "BSE bulk deals: got HTML error page — API blocked (anti-bot redirect)"
            )
        if len(text) < 10:
            raise PermanentFetchError("BSE bulk deals: empty response — no deals this date")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        return _parse_bse_bulk_json(body, raw_row, self._db, self.parser_version)


# ── Parsers ────────────────────────────────────────────────────────────────────


def _parse_nse_bulk_csv(
    body: bytes, raw_row: RawArchiveRow, db: sqlite3.Connection, version: str
) -> int:
    """
    NSE bulk.csv columns (as of 2026):
    Date, Symbol, Security Name, Client Name, Buy/Sell,
    Quantity Traded, Trade Price / Wght. Avg. Price, Remarks
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    inserted = 0
    for row in reader:
        symbol = (row.get("Symbol") or row.get("SYMBOL") or "").strip()
        client = (row.get("Client Name") or row.get("CLIENT NAME") or "").strip()
        tx_raw = (
            row.get("Buy/Sell") or row.get("Buy / Sell") or row.get("BUY/SELL") or ""
        ).strip().upper()
        qty_str = (
            (row.get("Quantity Traded") or row.get("QUANTITY") or "0").replace(",", "").strip()
        )
        price_str = (
            (
                row.get("Trade Price / Wght. Avg. Price")
                or row.get("Trade Price /Wt. Avg. Price")
                or row.get("PRICE")
                or "0"
            )
            .replace(",", "")
            .strip()
        )
        date_str = (row.get("Date") or row.get("DATE") or "").strip()

        if not symbol or not client:
            continue

        try:
            qty = float(qty_str)
            price = float(price_str)
        except ValueError:
            continue

        tx_type = TransactionType.BUY if "B" in tx_raw else TransactionType.SELL
        deal_date = _parse_date(date_str)
        client_norm = _normalize_client_name(client)
        deal_id = str(uuid.uuid4())

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO bulk_deals
                    (deal_id, raw_id, parser_version, stock_symbol, exchange,
                     deal_date, observed_at, client_name, client_normalized,
                     is_smart_money, transaction_type, quantity, price, value,
                     is_corrected, corrects_deal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deal_id, raw_row.raw_id, version,
                    symbol, Exchange.NSE.value,
                    deal_date, _fmt_dt(raw_row.fetched_at),
                    client, client_norm,
                    1 if _is_smart_money(client_norm) else 0,
                    tx_type.value, qty, price, qty * price,
                    0, None,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning("NSE bulk deals: insert failed symbol=%s: %s", symbol, exc)

    db.commit()
    return inserted


def _parse_bse_bulk_json(
    body: bytes, raw_row: RawArchiveRow, db: sqlite3.Connection, version: str
) -> int:
    """
    BSE BulkDeal_Beta JSON schema:
    {"Table": [{"DEAL_DATE": "DD/MM/YYYY", "SCRIP_CODE": int, "ScripName": str,
                "CLIENT_NAME": str, "TRANSACTION_TYPE": "B"|"S",
                "QUANTITY": float, "PRICE": float, "SENDTOWEBSITE": "..."}]}

    Note: BSE uses SCRIP_CODE (numeric) as the identifier, stored as stock_symbol.
    """
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        logger.warning("BSE bulk deals: JSON decode failed: %s", exc)
        return 0

    rows = data.get("Table", [])
    inserted = 0

    for row in rows:
        scrip_code = str(row.get("SCRIP_CODE", "")).strip()
        client = (row.get("CLIENT_NAME") or "").strip()
        tx_raw = (row.get("TRANSACTION_TYPE") or "B").strip().upper()
        date_str = (row.get("DEAL_DATE") or "").strip()

        if not scrip_code or not client:
            continue

        try:
            qty = float(row.get("QUANTITY") or 0)
            price = float(row.get("PRICE") or 0)
        except (ValueError, TypeError):
            continue

        tx_type = TransactionType.BUY if tx_raw == "B" else TransactionType.SELL
        deal_date = _parse_date(date_str)
        client_norm = _normalize_client_name(client)
        deal_id = str(uuid.uuid4())

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO bulk_deals
                    (deal_id, raw_id, parser_version, stock_symbol, exchange,
                     deal_date, observed_at, client_name, client_normalized,
                     is_smart_money, transaction_type, quantity, price, value,
                     is_corrected, corrects_deal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deal_id, raw_row.raw_id, version,
                    scrip_code, Exchange.BSE.value,
                    deal_date, _fmt_dt(raw_row.fetched_at),
                    client, client_norm,
                    1 if _is_smart_money(client_norm) else 0,
                    tx_type.value, qty, price, qty * price,
                    0, None,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning("BSE bulk deals: insert failed scrip=%s: %s", scrip_code, exc)

    db.commit()
    return inserted


def _parse_date(s: str) -> str:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def _normalize_client_name(name: str) -> str:
    return " ".join(name.upper().split())


def _is_smart_money(client_norm: str) -> bool:
    low = client_norm.lower()
    return any(sub in low for sub in _SMART_MONEY_SUBSTRINGS)
