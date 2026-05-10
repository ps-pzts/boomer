"""
Integration tests: Phase 1 (Capital) + Phase 2 (Collector) on the same database.

These tests verify the cross-phase contract:
  - Both migrations apply cleanly to a single DB file.
  - risk_config (Phase 1) seeds correctly and its sentiment_confidence_threshold
    is readable by Phase 2's sentiment pipeline.
  - A filing written by the collector can have sentiment applied using the
    threshold that came from risk_config.

The actual wiring (orchestrator reads threshold from risk_config and passes it
to apply_sentiment_to_filings) is a Phase 3 concern. These tests verify the
data contract that Phase 3 will rely on.
"""

import sqlite3
import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest

from capital.risk_config import RiskConfigStore
from collector.models import SentimentLabel
from collector.sentiment import SentimentPipeline, apply_sentiment_to_filings


def _apply_migrations(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    for f in ["migrations/0001_initial_schema.sql", "migrations/0002_collector_schema.sql"]:
        with open(f) as fh:
            conn.executescript(fh.read())
    conn.close()


def _insert_filing(conn: sqlite3.Connection, filing_id: str, headline: str) -> None:
    raw_id = f"raw-{filing_id}"
    conn.execute(
        "INSERT OR IGNORE INTO raw_archive "
        "(raw_id, source, fetched_at, request_url, response_status, "
        "content_hash, content_path, parse_status) "
        "VALUES (?, 'bse_filings', '2024-04-22T10:00:00Z', 'x', 200, ?, 'p', 'success')",
        (raw_id, raw_id),
    )
    conn.execute(
        "INSERT INTO filings "
        "(filing_id, raw_id, parser_version, stock_symbol, exchange, "
        "filing_date, observed_at, category, headline, is_corrected, parse_deps_met) "
        "VALUES (?, ?, 'v1', 'RELIANCE', 'NSE', '2024-04-22', "
        "'2024-04-22T10:00:00Z', 'quarterly_results', ?, 0, 1)",
        (filing_id, raw_id, headline),
    )
    conn.commit()


# ── migration compatibility ────────────────────────────────────────────────────


def test_both_migrations_apply_to_same_db(tmp_path):
    """Migrations 0001 (capital) and 0002 (collector) must coexist without conflict."""
    db_path = str(tmp_path / "boomer.db")
    _apply_migrations(db_path)

    conn = sqlite3.connect(db_path)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    # Phase 1 tables
    assert "capital_ledger" in tables
    assert "risk_config" in tables
    assert "harvest_events" in tables

    # Phase 2 tables
    assert "raw_archive" in tables
    assert "filings" in tables
    assert "prices" in tables
    assert "fo_oi_daily" in tables
    conn.close()


# ── risk_config → sentiment threshold ────────────────────────────────────────


def test_risk_config_seeds_sentiment_threshold(tmp_path):
    """Phase 1 seed_defaults writes sentiment_confidence_threshold=0.60 to risk_config."""
    db_path = str(tmp_path / "boomer.db")
    _apply_migrations(db_path)

    store = RiskConfigStore(db_path)
    cfg = store.seed_defaults(effective_from=date(2024, 4, 22))

    assert float(cfg.sentiment_confidence_threshold) == pytest.approx(0.60)


def test_sentiment_threshold_readable_from_risk_config(tmp_path):
    """
    Verify the data contract Phase 3 will use:
    read sentiment_confidence_threshold from risk_config, pass to apply_sentiment_to_filings.
    """
    db_path = str(tmp_path / "boomer.db")
    _apply_migrations(db_path)

    store = RiskConfigStore(db_path)
    store.seed_defaults(effective_from=date(2024, 4, 22))

    # Phase 3 orchestrator pattern: read threshold from config, pass to sentiment
    cfg = store.load_current()
    threshold = float(cfg.sentiment_confidence_threshold)  # 0.60

    conn = sqlite3.connect(db_path)
    _insert_filing(conn, "f-001", "Record quarterly profit, EBITDA up 40%")

    pipeline = SentimentPipeline()
    # Confidence 0.55 is below the 0.60 threshold from risk_config → must be unclassified
    pipeline._pipeline = MagicMock(return_value=[[{"label": "positive", "score": 0.55}]])

    updated = apply_sentiment_to_filings(conn, pipeline, confidence_threshold=threshold)
    assert updated == 1

    row = conn.execute(
        "SELECT sentiment_label, sentiment_confidence FROM filings WHERE filing_id='f-001'"
    ).fetchone()
    assert row[0] == SentimentLabel.UNCLASSIFIED
    assert row[1] == pytest.approx(0.55)
    conn.close()


def test_high_confidence_filing_gets_label(tmp_path):
    """Confidence above threshold stores the actual label, not unclassified."""
    db_path = str(tmp_path / "boomer.db")
    _apply_migrations(db_path)

    store = RiskConfigStore(db_path)
    store.seed_defaults(effective_from=date(2024, 4, 22))
    cfg = store.load_current()
    threshold = float(cfg.sentiment_confidence_threshold)

    conn = sqlite3.connect(db_path)
    _insert_filing(conn, "f-002", "Auditor resigns, fraud alleged at subsidiary")

    pipeline = SentimentPipeline()
    pipeline._pipeline = MagicMock(return_value=[[{"label": "negative", "score": 0.92}]])

    apply_sentiment_to_filings(conn, pipeline, confidence_threshold=threshold)

    row = conn.execute("SELECT sentiment_label FROM filings WHERE filing_id='f-002'").fetchone()
    assert row[0] == SentimentLabel.NEGATIVE
    conn.close()


def test_capital_and_filings_coexist_in_same_db(tmp_path):
    """
    Capital ledger writes and filing inserts must not interfere with each other
    when using the same database file.
    """
    db_path = str(tmp_path / "boomer.db")
    _apply_migrations(db_path)

    # Phase 1: seed capital state
    store = RiskConfigStore(db_path)
    store.seed_defaults(effective_from=date(2024, 4, 22))

    conn = sqlite3.connect(db_path)
    ledger_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO capital_ledger "
        "(ledger_id, as_of_date, total_capital, total_cash, "
        "long_term_allocated_pct, swing_allocated_pct, intraday_allocated_pct, "
        "high_water_mark, peak_date, created_at) "
        "VALUES (?, '2024-04-22', 1000000, 800000, 0.70, 0.20, 0.10, 1000000, '2024-04-22', "
        "'2024-04-22T09:00:00Z')",
        (ledger_id,),
    )
    conn.commit()

    # Phase 2: insert a filing in the same DB
    _insert_filing(conn, "f-003", "Company wins ₹500 Cr government contract")

    capital_rows = conn.execute("SELECT COUNT(*) FROM capital_ledger").fetchone()[0]
    filing_rows = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]

    assert capital_rows == 1
    assert filing_rows == 1
    conn.close()
