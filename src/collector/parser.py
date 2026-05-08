"""
Layer 1 → Layer 2 parse dispatcher.

The parse worker reads unprocessed raw_archive rows and dispatches each
to the correct fetcher's parse() method. Sentiment inference runs after
filing rows are inserted.

Usage:
    parser = ParseWorker(db, raw_dir, fetcher_registry, sentiment_pipeline)
    parser.run_pending(limit=200)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from collector.base import _row_from_db
from collector.models import DataSource, ParseStatus
from collector.sentiment import SentimentPipeline, apply_sentiment_to_filings

logger = logging.getLogger(__name__)


class ParseWorker:
    def __init__(
        self,
        db: sqlite3.Connection,
        raw_dir: Path,
        fetcher_registry: dict,
        sentiment: SentimentPipeline | None = None,
        sentiment_threshold: float = 0.60,
    ) -> None:
        """
        fetcher_registry: dict mapping DataSource → BaseFetcher instance.
        sentiment: optional SentimentPipeline; if None, filing sentiment is skipped.
        """
        self._db = db
        self._raw_dir = raw_dir
        self._fetchers = fetcher_registry
        self._sentiment = sentiment
        self._sentiment_threshold = sentiment_threshold

    def run_pending(self, limit: int = 200) -> dict[str, int]:
        """
        Process up to `limit` pending raw_archive rows.
        Returns dict of source → rows parsed.
        """
        rows = self._db.execute(
            """
            SELECT raw_id, source, fetched_at, request_url, request_params,
                   response_status, content_hash, content_path,
                   parser_version, parsed_at, parse_status
            FROM raw_archive
            WHERE parse_status = 'pending'
            ORDER BY fetched_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        stats: dict[str, int] = {}
        for db_row in rows:
            raw_row = _row_from_db(db_row)
            source = raw_row.source
            fetcher = self._fetchers.get(source)

            if fetcher is None:
                logger.warning("ParseWorker: no fetcher registered for source=%s", source)
                self._db.execute(
                    "UPDATE raw_archive SET parse_status=? WHERE raw_id=?",
                    (ParseStatus.FAILED.value, raw_row.raw_id),
                )
                self._db.commit()
                continue

            try:
                n = fetcher.parse(raw_row)
                stats[source.value] = stats.get(source.value, 0) + n
                logger.debug(
                    "ParseWorker: parsed raw_id=%s source=%s rows=%d",
                    raw_row.raw_id, source, n,
                )
            except Exception as exc:
                logger.error(
                    "ParseWorker: parse failed raw_id=%s source=%s: %s",
                    raw_row.raw_id, source, exc,
                )
                self._db.execute(
                    "UPDATE raw_archive SET parse_status=? WHERE raw_id=?",
                    (ParseStatus.FAILED.value, raw_row.raw_id),
                )
                self._db.commit()

        # Run FinBERT sentiment on any newly inserted filing rows.
        if self._sentiment is not None:
            sentiment_count = apply_sentiment_to_filings(
                self._db,
                self._sentiment,
                confidence_threshold=self._sentiment_threshold,
            )
            if sentiment_count:
                logger.info("ParseWorker: applied sentiment to %d filings", sentiment_count)

        return stats

    def reparse_source(self, source: DataSource, limit: int = 1000) -> int:
        """
        Re-parse all raw_archive rows for a given source (e.g. after a parser upgrade).
        Marks rows as pending first, then runs run_pending.
        Returns total rows parsed.
        """
        self._db.execute(
            "UPDATE raw_archive SET parse_status='pending' WHERE source=?",
            (source.value,),
        )
        self._db.commit()
        stats = self.run_pending(limit=limit)
        return stats.get(source.value, 0)


def build_fetcher_registry(db: sqlite3.Connection, raw_dir: Path) -> dict:
    """
    Construct a DataSource → fetcher instance registry.
    Import fetchers lazily to avoid circular imports and optional dependencies.
    """
    from collector.fetchers.bse_filings import BseFilingsFetcher
    from collector.fetchers.bulk_deals import BseBulkDealsFetcher, NseBulkDealsFetcher
    from collector.fetchers.fo_oi import FoOiFetcher
    from collector.fetchers.instruments import InstrumentsFetcher
    from collector.fetchers.nse_filings import NseFilingsFetcher
    from collector.fetchers.prices import NsePricesFetcher
    from collector.fetchers.screener import ScreenerFetcher
    from collector.fetchers.shares_outstanding import SharesOutstandingFetcher

    return {
        DataSource.BSE_FILINGS: BseFilingsFetcher(db, raw_dir),
        DataSource.NSE_FILINGS: NseFilingsFetcher(db, raw_dir),
        DataSource.BSE_BULK_DEALS: BseBulkDealsFetcher(db, raw_dir),
        DataSource.NSE_BULK_DEALS: NseBulkDealsFetcher(db, raw_dir),
        DataSource.PRICES: NsePricesFetcher(db, raw_dir),
        DataSource.FO_OI: FoOiFetcher(db, raw_dir),
        DataSource.SHARES_OUTSTANDING: SharesOutstandingFetcher(db, raw_dir),
        DataSource.QUARTERLY_FINANCIALS: ScreenerFetcher(db, raw_dir),
        DataSource.INSTRUMENTS: InstrumentsFetcher(db, raw_dir),
    }
