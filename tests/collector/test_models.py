import uuid
from datetime import datetime

from collector.models import (
    CollectionRunRow,
    DataSource,
    FetchResult,
    ParseStatus,
    RawArchiveRow,
    RunStatus,
    SentimentLabel,
)


def test_raw_archive_row_defaults():
    row = RawArchiveRow(
        source=DataSource.BSE_FILINGS,
        fetched_at=datetime(2024, 4, 22, 10, 0, 0),
        request_url="https://example.com",
        response_status=200,
        content_hash="abc123",
        content_path="bse_filings/2024/04/22/x.gz",
    )
    assert row.parse_status == ParseStatus.PENDING
    assert row.parser_version is None
    assert uuid.UUID(row.raw_id)  # valid UUID


def test_collection_run_row_defaults():
    row = CollectionRunRow(
        source=DataSource.NSE_FILINGS,
        started_at=datetime(2024, 4, 22, 2, 0, 0),
    )
    assert row.status == RunStatus.RUNNING
    assert row.records_fetched == 0
    assert row.records_new == 0
    assert uuid.UUID(row.run_id)


def test_fetch_result_fields():
    result = FetchResult(
        source=DataSource.PRICES,
        url="https://nsearchives.nseindia.com/x.csv",
        status_code=200,
        body=b"SYMBOL,CLOSE\nRELIANCE,2900",
        content_hash="deadbeef",
        fetched_at=datetime(2024, 4, 22, 18, 0, 0),
    )
    assert result.source == DataSource.PRICES
    assert result.status_code == 200


def test_sentiment_label_values():
    assert SentimentLabel.POSITIVE.value == "positive"
    assert SentimentLabel.UNCLASSIFIED.value == "unclassified"
