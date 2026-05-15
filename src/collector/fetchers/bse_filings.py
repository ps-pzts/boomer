"""
BSE corporate filings fetcher (Category B — event stream).

Polls the BSE corporate announcement API every 30 minutes during 9 AM–6 PM IST.
Rate: 1 req / 60s during business hours.
Deduplicates against already-archived content_hash.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from collector.base import BaseFetcher, _fmt_dt
from collector.models import (
    DataSource,
    Exchange,
    FetchResult,
    FilingCategory,
    ParseStatus,
    RawArchiveRow,
)

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# BSE announcements API — returns recent corporate announcements as JSON.
# &ddine=&strcat2=&subcategory=-1 = all categories, all companies.
_BSE_ANNOUNCEMENTS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C&subcategory=-1"
)


class BseFilingsFetcher(BaseFetcher):
    source = DataSource.BSE_FILINGS

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        # BSE API requires these headers to not return 403.
        self._session.headers.update(
            {
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def fetch_url(self, **kwargs) -> str:
        return _BSE_ANNOUNCEMENTS_URL

    def validate(self, result: FetchResult) -> None:
        if result.status_code != 200:
            raise ValueError(f"BSE filings: HTTP {result.status_code}")
        try:
            data = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"BSE filings: invalid JSON — {exc}") from exc
        if "Table" not in data:
            raise ValueError("BSE filings: missing 'Table' key in response")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        data = json.loads(body)
        records = data.get("Table", [])
        inserted = 0
        for rec in records:
            filing_id = str(uuid.uuid4())
            # BSE field names as of 2024 — validate against live response on first deploy.
            headline = rec.get("HEADLINE", "") or rec.get("NEWSSUB", "") or ""
            body_text = rec.get("ATTACHMENTNAME", "") or ""
            category_raw = rec.get("CATEGORYNAME", "") or ""
            filing_date_str = rec.get("NEWS_DT", "") or rec.get("DT_TM", "")
            scrip_code = str(rec.get("SCRIP_CD", "") or "")
            attachment_url = rec.get("ATTACHMENTURL", "")

            filing_date, filing_time = _parse_bse_datetime(filing_date_str)
            category = _classify_bse_category(category_raw, headline)
            # observed_at = time we fetched; filing_date = when BSE says it was filed.
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
                        scrip_code,
                        Exchange.BSE.value,
                        filing_date,
                        filing_time,
                        _fmt_dt(observed_at),
                        category.value,
                        category_raw[:200] if category_raw else None,
                        headline[:500],
                        body_text[:500] if body_text else None,
                        attachment_url or None,
                        None,  # sentiment filled by parser.py after FinBERT
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
                logger.warning("BSE filings: failed to insert record %s: %s", filing_id, exc)

        self._db.commit()
        self.mark_parsed(raw_row.raw_id, self.parser_version, ParseStatus.SUCCESS)
        return inserted


def _parse_bse_datetime(dt_str: str) -> tuple[str, str | None]:
    """Return (ISO date, ISO time | None) from BSE datetime string."""
    if not dt_str:
        return datetime.now(IST).date().isoformat(), None
    dt_str = dt_str.strip()
    for fmt in ("%d %b %Y %I:%M %p", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_str, fmt)
            has_time = "%H" in fmt or "%I" in fmt
            return dt.date().isoformat(), dt.strftime("%H:%M:%S") if has_time else None
        except ValueError:
            continue
    return dt_str[:10], None


def _classify_bse_category(bse_category: str, headline: str) -> FilingCategory:
    cat = (bse_category + " " + headline).lower()
    quarterly_kws = ("quarterly", "financial results", "q1", "q2", "q3", "q4")
    if any(k in cat for k in quarterly_kws):
        return FilingCategory.QUARTERLY_RESULTS
    if "order" in cat and ("win" in cat or "receiv" in cat or "award" in cat):
        return FilingCategory.ORDER_WIN
    if "pledge" in cat or "encumber" in cat:
        return FilingCategory.PLEDGING
    if "auditor" in cat or "statutory auditor" in cat:
        return FilingCategory.AUDITOR_CHANGE
    if "fraud" in cat or "whistle" in cat or "irregularit" in cat:
        return FilingCategory.FRAUD
    if "promoter" in cat and "acqui" in cat:
        return FilingCategory.PROMOTER_BUY
    if "promoter" in cat and ("sell" in cat or "dispos" in cat or "disinvest" in cat):
        return FilingCategory.PROMOTER_SELL
    if "bonus" in cat or "split" in cat or "dividend" in cat or "rights" in cat or "merger" in cat:
        return FilingCategory.CORPORATE_ACTION
    if "agm" in cat or "annual general" in cat or "egm" in cat:
        return FilingCategory.AGM
    return FilingCategory.OTHER
