"""
Quarterly financials fetcher — Screener.in HTML scrape (Category C — on-demand).

Triggered within 48 hours of a quarterly_results filing appearing in the filings table.
Rate: 1 request per 5 seconds, 2–6 AM IST only (off-peak).
Uses pd.read_html() to parse the Quarterly Results and Cash Flow tables.

Screener.in ToS: permits personal use; prohibits commercial redistribution.
This usage (personal algorithmic trading research) is personal use.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from collector.base import BaseFetcher, _fmt_dt
from collector.models import DataSource, FetchResult, ParseStatus, RawArchiveRow

logger = logging.getLogger(__name__)

_SCREENER_URL = "https://www.screener.in/company/{symbol}/consolidated/"
_SCREENER_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"


class ScreenerFetcher(BaseFetcher):
    source = DataSource.QUARTERLY_FINANCIALS

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update(
            {
                "Referer": "https://www.screener.in/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def fetch_url(self, symbol: str = "", consolidated: bool = True, **kwargs) -> str:
        if consolidated:
            return _SCREENER_URL.format(symbol=symbol.upper())
        return _SCREENER_URL_STANDALONE.format(symbol=symbol.upper())

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise ValueError("Screener: 404 — company not found on Screener.in")
        if result.status_code != 200:
            raise ValueError(f"Screener: HTTP {result.status_code}")
        html = result.body.decode("utf-8", errors="replace")
        if "Quarterly Results" not in html and "quarterly" not in html.lower():
            raise ValueError("Screener: response does not contain Quarterly Results table")

    def parse(self, raw_row: RawArchiveRow) -> int:
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError(
                "pandas is required for Screener parsing: pip install pandas lxml"
            ) from exc

        body = self.load_raw_body(raw_row.content_path)
        html = body.decode("utf-8", errors="replace")

        # Derive symbol from request_url recorded in raw_archive.
        url = raw_row.request_url
        symbol = _extract_symbol_from_url(url)
        results_date = _find_results_announcement_date(self._db, symbol)

        tables = pd.read_html(html)
        qr_table = _find_quarterly_results_table(tables)
        cf_table = _find_cash_flow_table(tables)

        if qr_table is None:
            logger.warning("Screener: no quarterly results table found for %s", symbol)
            self.mark_parsed(raw_row.raw_id, self.parser_version, ParseStatus.FAILED)
            return 0

        inserted = _insert_quarterly_rows(
            qr_table, cf_table, symbol, raw_row, results_date, self._db, self.parser_version
        )
        self.mark_parsed(raw_row.raw_id, self.parser_version, ParseStatus.SUCCESS)
        return inserted


# ── helpers ────────────────────────────────────────────────────────────────────


def _extract_symbol_from_url(url: str) -> str:
    parts = [p for p in url.split("/") if p]
    try:
        idx = parts.index("company")
        return parts[idx + 1].upper()
    except (ValueError, IndexError):
        return ""


def _find_results_announcement_date(db: sqlite3.Connection, symbol: str) -> datetime | None:
    """Return observed_at for the most recent quarterly_results filing for this symbol."""
    row = db.execute(
        """
        SELECT observed_at FROM filings
        WHERE stock_symbol = ? AND category = 'quarterly_results'
        ORDER BY filing_date DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row:
        return datetime.fromisoformat(row[0].rstrip("Z"))
    return None


def _find_quarterly_results_table(tables):
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if any("sales" in c or "revenue" in c or "net sales" in c for c in cols):
            return df
        period_kws = ("mar", "jun", "sep", "dec")
        if len(df.columns) >= 4 and any(k in c for k in period_kws for c in cols):
            return df
    return None


def _find_cash_flow_table(tables):
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if any("cash" in c or "operating" in c for c in cols):
            first_col = str(df.iloc[:, 0] if len(df.columns) > 0 else "").lower()
            if "cash" in first_col or "operating" in first_col:
                return df
    return None


def _insert_quarterly_rows(qr_table, cf_table, symbol, raw_row, results_date, db, version) -> int:
    import pandas as pd  # noqa: F401

    inserted = 0

    # Screener.in quarterly table: rows = metrics, cols = periods.
    # Transpose so each column is a period.
    try:
        df = qr_table.set_index(qr_table.columns[0]).T
        df.index.name = "period"
    except Exception as exc:
        logger.warning("Screener: failed to transpose quarterly table: %s", exc)
        return 0

    for period_label, row in df.iterrows():
        period_end = _parse_screener_period(str(period_label))
        if not period_end:
            continue

        fin_id = str(uuid.uuid4())
        revenue = _safe_float(row, ["Sales", "Net Sales", "Revenue", "Total Revenue"])
        op_profit = _safe_float(row, ["Operating Profit", "EBITDA", "EBIT"])
        opm_pct = _safe_float(row, ["OPM %", "OPM%", "Operating Profit Margin"])
        pat = _safe_float(row, ["Net Profit", "PAT", "Profit after Tax"])

        # Cash from operations from the CF table if available.
        cfo = None
        if cf_table is not None:
            import contextlib

            with contextlib.suppress(Exception):
                cfo = _extract_cfo_for_period(cf_table, str(period_label))

        # observed_at = announcement date + 2 hours (point-in-time correct).
        # If no announcement date found, fall back to scraping time.
        if results_date:
            from datetime import timedelta

            observed_at = results_date + timedelta(hours=2)
        else:
            observed_at = raw_row.fetched_at

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO quarterly_financials
                    (fin_id, stock_symbol, exchange, period_end, period_type,
                     revenue, operating_profit, opm_pct, pat, cfo,
                     source_url, scraped_at, observed_at, raw_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fin_id,
                    symbol,
                    "NSE",
                    period_end,
                    "Q",
                    revenue,
                    op_profit,
                    opm_pct,
                    pat,
                    cfo,
                    raw_row.request_url,
                    _fmt_dt(raw_row.fetched_at),
                    _fmt_dt(observed_at),
                    raw_row.raw_id,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning(
                "Screener: insert failed symbol=%s period=%s: %s", symbol, period_end, exc
            )

    db.commit()
    return inserted


def _parse_screener_period(label: str) -> str | None:
    """Convert Screener period label (e.g. 'Mar 2024') to ISO date (last day of quarter)."""
    label = label.strip()
    quarter_end = {"Mar": "03-31", "Jun": "06-30", "Sep": "09-30", "Dec": "12-31"}
    for mon, day in quarter_end.items():
        if mon in label:
            parts = label.split()
            for p in parts:
                if p.isdigit() and len(p) == 4:
                    return f"{p}-{day}"
    return None


def _safe_float(row, keys: list[str]) -> float | None:
    for key in keys:
        for col in row.index:
            if key.lower() in str(col).lower():
                try:
                    val = str(row[col]).replace(",", "").replace("%", "").strip()
                    return float(val)
                except (ValueError, TypeError):
                    pass
    return None


def _extract_cfo_for_period(cf_table, period_label: str) -> float | None:
    try:
        df = cf_table.set_index(cf_table.columns[0]).T
        if period_label in df.index:
            row = df.loc[period_label]
            return _safe_float(row, ["Cash from Operating", "Operating Activities", "CFO"])
    except Exception:
        pass
    return None
