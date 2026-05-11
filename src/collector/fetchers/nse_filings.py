"""
NSE corporate filings fetcher (Category B — event stream).

Polls NSE corporate announcement API every 30 minutes during 9 AM–6 PM IST.
Rate: 1 req / 90s. NSE requires a session cookie from the homepage — transport()
overrides the base to handle the NSE cookie dance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from collector.base import BaseFetcher, _fmt_dt, _now_utc
from collector.models import (
    DataSource,
    Exchange,
    FetchResult,
    FilingCategory,
    ParseStatus,
    RawArchiveRow,
)

logger = logging.getLogger(__name__)

_NSE_HOMEPAGE = "https://www.nseindia.com"
_NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements?index=equities&category=-1&limit=100"
)


class NseFilingsFetcher(BaseFetcher):
    source = DataSource.NSE_FILINGS
    _cookie_refreshed_at: datetime | None = None

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update(
            {
                "Referer": "https://www.nseindia.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )

    def fetch_url(self, **kwargs) -> str:
        return _NSE_ANNOUNCEMENTS_URL

    def transport(self, url: str, **kwargs) -> FetchResult:
        """NSE requires hitting the homepage first to obtain session cookies."""
        self._refresh_cookies_if_needed()
        timeout = kwargs.pop("timeout", 30)
        resp = self._session.get(url, timeout=timeout)
        resp.raise_for_status()
        body = resp.content
        import hashlib

        return FetchResult(
            source=self.source,
            url=resp.url,
            status_code=resp.status_code,
            body=body,
            content_hash=hashlib.sha256(body).hexdigest(),
            fetched_at=_now_utc(),
        )

    def validate(self, result: FetchResult) -> None:
        if result.status_code != 200:
            raise ValueError(f"NSE filings: HTTP {result.status_code}")
        try:
            data = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"NSE filings: invalid JSON — {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("NSE filings: expected JSON array at top level")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        records = json.loads(body)
        inserted = 0
        for rec in records:
            filing_id = str(uuid.uuid4())
            # NSE field names as of 2024 — verify against live response on first deploy.
            headline = rec.get("subject", "") or rec.get("desc", "") or ""
            body_text = rec.get("desc", "") or ""
            bcast_date = rec.get("bcast_date", "") or rec.get("an_dt", "")
            symbol = rec.get("symbol", "") or ""
            attachment_url = rec.get("attchmntFile", "")
            category_raw = rec.get("categoryDesc", "") or rec.get("an_type", "") or ""

            filing_date, filing_time = _parse_nse_datetime(bcast_date)
            category = _classify_nse_category(category_raw, headline)
            observed_at = raw_row.fetched_at

            try:
                self._db.execute(
                    """
                    INSERT OR IGNORE INTO filings
                        (filing_id, raw_id, parser_version, stock_symbol, exchange,
                         filing_date, filing_time, observed_at, category, subcategory,
                         headline, body_summary, attachment_url,
                         sentiment_label, sentiment_confidence, finbert_version,
                         is_corrected, corrects_filing_id,
                         depends_on_raw_id, parse_deps_met)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        filing_id,
                        raw_row.raw_id,
                        self.parser_version,
                        symbol,
                        Exchange.NSE.value,
                        filing_date,
                        filing_time,
                        _fmt_dt(observed_at),
                        category.value,
                        category_raw[:200] if category_raw else None,
                        headline[:500],
                        body_text[:500] if body_text else None,
                        attachment_url or None,
                        None,
                        None,
                        None,
                        0,
                        None,
                        None,
                        1,
                    ),
                )
                if self._db.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception as exc:
                logger.warning("NSE filings: failed to insert record %s: %s", filing_id, exc)

        self._db.commit()
        self.mark_parsed(raw_row.raw_id, self.parser_version, ParseStatus.SUCCESS)
        return inserted

    def _refresh_cookies_if_needed(self) -> None:
        """Hit NSE homepage to get a valid session cookie if stale or absent."""
        if self._cookie_refreshed_at is not None:
            age_s = (_now_utc() - self._cookie_refreshed_at).total_seconds()
            if age_s < 1800:  # reuse cookie for 30 minutes
                return
        self._session.get(_NSE_HOMEPAGE, timeout=15)
        self._cookie_refreshed_at = _now_utc()
        logger.debug("NSE session cookies refreshed")


def _parse_nse_datetime(dt_str: str) -> tuple[str, str | None]:
    if not dt_str:
        return datetime.now(UTC).date().isoformat(), None
    dt_str = dt_str.strip()
    fmts = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            dt = datetime.strptime(dt_str, fmt)
            has_time = any(tok in fmt for tok in ("%H", "%I"))
            return dt.date().isoformat(), dt.strftime("%H:%M:%S") if has_time else None
        except ValueError:
            continue
    return dt_str[:10], None


def _classify_nse_category(nse_category: str, headline: str) -> FilingCategory:
    cat = (nse_category + " " + headline).lower()
    if "result" in cat and ("quarter" in cat or "financial" in cat or "half year" in cat):
        return FilingCategory.QUARTERLY_RESULTS
    if "order" in cat and ("receiv" in cat or "win" in cat or "secur" in cat):
        return FilingCategory.ORDER_WIN
    if "pledge" in cat or "encumber" in cat:
        return FilingCategory.PLEDGING
    if "auditor" in cat:
        return FilingCategory.AUDITOR_CHANGE
    if "fraud" in cat or "irregularit" in cat:
        return FilingCategory.FRAUD
    if "insider" in cat or "acqui" in cat and "promoter" in cat:
        return FilingCategory.PROMOTER_BUY
    if "promoter" in cat and ("sell" in cat or "dispos" in cat):
        return FilingCategory.PROMOTER_SELL
    if any(k in cat for k in ("bonus", "split", "dividend", "rights issue", "amalgamat", "merger")):
        return FilingCategory.CORPORATE_ACTION
    if "agm" in cat or "annual general" in cat or "egm" in cat:
        return FilingCategory.AGM
    return FilingCategory.OTHER
