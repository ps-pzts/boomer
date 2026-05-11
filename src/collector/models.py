from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class DataSource(StrEnum):
    BSE_FILINGS = "bse_filings"
    NSE_FILINGS = "nse_filings"
    BSE_BULK_DEALS = "bse_bulk_deals"
    NSE_BULK_DEALS = "nse_bulk_deals"
    PRICES = "prices"
    FO_OI = "fo_oi"
    SHARES_OUTSTANDING = "shares_outstanding"
    QUARTERLY_FINANCIALS = "quarterly_financials"
    INSTRUMENTS = "instruments"
    INDEX_DATA = "index_data"
    MINUTE_BARS = "minute_bars"
    SECTOR_CLASSIFICATIONS = "sector_classifications"


class ParseStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class FilingCategory(StrEnum):
    QUARTERLY_RESULTS = "quarterly_results"
    ORDER_WIN = "order_win"
    PLEDGING = "pledging"
    AUDITOR_CHANGE = "auditor_change"
    FRAUD = "fraud"
    PROMOTER_BUY = "promoter_buy"
    PROMOTER_SELL = "promoter_sell"
    CORPORATE_ACTION = "corporate_action"
    AGM = "agm"
    OTHER = "other"


class SentimentLabel(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNCLASSIFIED = "unclassified"


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class TransactionType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TransactionMode(StrEnum):
    OPEN_MARKET = "open_market"
    PREFERENTIAL = "preferential"
    PLEDGED = "pledged"
    RELEASED_PLEDGE = "released_pledge"
    OTHER = "other"


class InstrumentType(StrEnum):
    CE = "CE"
    PE = "PE"
    FUT = "FUT"


@dataclass
class RawArchiveRow:
    source: DataSource
    fetched_at: datetime  # UTC
    request_url: str
    response_status: int
    content_hash: str  # SHA-256 hex
    content_path: str  # path to gzipped payload
    request_params: str | None = None  # JSON string
    raw_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parser_version: str | None = None
    parsed_at: datetime | None = None
    parse_status: ParseStatus = ParseStatus.PENDING


@dataclass
class FetchResult:
    """Returned by BaseFetcher.fetch(); carries raw payload + metadata before archiving."""

    source: DataSource
    url: str
    status_code: int
    body: bytes
    content_hash: str
    fetched_at: datetime  # UTC
    params: dict | None = None


@dataclass
class CollectionRunRow:
    source: DataSource
    started_at: datetime  # UTC
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ended_at: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    records_fetched: int = 0
    records_new: int = 0
    error_message: str | None = None
    retry_count: int = 0
