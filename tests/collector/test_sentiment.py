"""
Tests for FinBERT sentiment module.
All inference calls are mocked — no model is loaded in tests.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from collector.models import SentimentLabel
from collector.sentiment import (
    SentimentPipeline,
    _build_inference_text,
    _map_label,
    apply_sentiment_to_filings,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    with open("migrations/0001_initial_schema.sql") as f:
        conn.executescript(f.read())
    with open("migrations/0002_collector_schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _insert_filing(db, filing_id: str, headline: str, body: str = ""):
    raw_id = f"raw-{filing_id}"
    db.execute(
        """
        INSERT OR IGNORE INTO raw_archive (raw_id, source, fetched_at, request_url,
            response_status, content_hash, content_path, parse_status)
        VALUES (?, 'bse_filings', '2024-04-22T10:00:00Z', 'x', 200, ?, 'p', 'success')
        """,
        (raw_id, raw_id),
    )
    db.execute(
        """
        INSERT INTO filings
            (filing_id, raw_id, parser_version, stock_symbol, exchange,
             filing_date, observed_at, category, headline, body_summary,
             is_corrected, parse_deps_met)
        VALUES (?, ?, 'v1', 'RELIANCE', 'NSE',
                '2024-04-22', '2024-04-22T10:00:00Z', 'other', ?, ?, 0, 1)
        """,
        (filing_id, raw_id, headline, body),
    )
    db.commit()


# ── _build_inference_text ──────────────────────────────────────────────────────


def test_build_inference_text_combines_headline_and_body():
    text = _build_inference_text("Strong Q4 results", "Revenue grew 20% YoY")
    assert "Strong Q4 results" in text
    assert "Revenue grew 20%" in text


def test_build_inference_text_truncates_long_body():
    long_body = "x" * 1000
    text = _build_inference_text("Headline", long_body)
    assert len(text) <= 600


def test_build_inference_text_empty_body():
    text = _build_inference_text("Only a headline", "")
    assert text == "Only a headline"


# ── _map_label ────────────────────────────────────────────────────────────────


def test_map_label_positive():
    assert _map_label("positive") == SentimentLabel.POSITIVE


def test_map_label_negative():
    assert _map_label("negative") == SentimentLabel.NEGATIVE


def test_map_label_neutral():
    assert _map_label("neutral") == SentimentLabel.NEUTRAL


def test_map_label_unknown_becomes_unclassified():
    assert _map_label("UNKNOWN") == SentimentLabel.UNCLASSIFIED


# ── SentimentPipeline.infer (mocked) ──────────────────────────────────────────


def test_infer_returns_correct_labels():
    pipeline = SentimentPipeline()
    mock_hf = MagicMock()
    mock_hf.return_value = [
        [{"label": "positive", "score": 0.92}],
        [{"label": "negative", "score": 0.85}],
    ]
    pipeline._pipeline = mock_hf

    results = pipeline.infer(["Company beats earnings", "Major fraud discovered"])
    assert results[0] == (SentimentLabel.POSITIVE, pytest.approx(0.92))
    assert results[1] == (SentimentLabel.NEGATIVE, pytest.approx(0.85))


def test_infer_empty_input():
    pipeline = SentimentPipeline()
    assert pipeline.infer([]) == []


def test_infer_handles_batch_failure():
    """If inference raises, batch falls back to UNCLASSIFIED rather than crashing."""
    pipeline = SentimentPipeline()
    mock_hf = MagicMock(side_effect=RuntimeError("GPU OOM"))
    pipeline._pipeline = mock_hf

    results = pipeline.infer(["some text"])
    assert len(results) == 1
    assert results[0][0] == SentimentLabel.UNCLASSIFIED


# ── apply_sentiment_to_filings ────────────────────────────────────────────────


def test_apply_sentiment_updates_null_rows():
    db = _make_db()
    _insert_filing(db, "f1", "Strong quarterly profit beat", "EBITDA margin expanded by 300bps")
    _insert_filing(db, "f2", "Auditor resigns citing fraud", "Company under regulatory scrutiny")

    pipeline = SentimentPipeline()
    # Both filings are in one batch call; HuggingFace returns one list per input.
    pipeline._pipeline = MagicMock(
        return_value=[
            [{"label": "positive", "score": 0.91}],
            [{"label": "negative", "score": 0.88}],
        ]
    )

    updated = apply_sentiment_to_filings(db, pipeline, confidence_threshold=0.60)
    assert updated == 2

    f1 = db.execute(
        "SELECT sentiment_label, sentiment_confidence FROM filings WHERE filing_id='f1'"
    ).fetchone()
    assert f1[0] == "positive"
    assert f1[1] == pytest.approx(0.91)

    f2 = db.execute("SELECT sentiment_label FROM filings WHERE filing_id='f2'").fetchone()
    assert f2[0] == "negative"


def test_apply_sentiment_low_confidence_stored_as_unclassified():
    db = _make_db()
    _insert_filing(db, "f3", "Board meeting outcome", "")

    pipeline = SentimentPipeline()
    # Score 0.45 — below threshold 0.60 → should become unclassified
    pipeline._pipeline = MagicMock(return_value=[[{"label": "neutral", "score": 0.45}]])

    apply_sentiment_to_filings(db, pipeline, confidence_threshold=0.60)

    row = db.execute(
        "SELECT sentiment_label, sentiment_confidence FROM filings WHERE filing_id='f3'"
    ).fetchone()
    assert row[0] == "unclassified"
    assert row[1] == pytest.approx(0.45)


def test_apply_sentiment_skips_already_labelled_rows():
    db = _make_db()
    _insert_filing(db, "f4", "Already labelled", "")
    db.execute(
        "UPDATE filings SET sentiment_label='positive', "
        "sentiment_confidence=0.9 WHERE filing_id='f4'"
    )
    db.commit()

    pipeline = SentimentPipeline()
    mock_hf = MagicMock()
    pipeline._pipeline = mock_hf

    updated = apply_sentiment_to_filings(db, pipeline)
    assert updated == 0
    mock_hf.assert_not_called()


def test_apply_sentiment_no_rows():
    db = _make_db()
    pipeline = SentimentPipeline()
    pipeline._pipeline = MagicMock()

    updated = apply_sentiment_to_filings(db, pipeline)
    assert updated == 0
