"""Tests for brain.features.computers — new functions added to the existing module."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain.feature_store import FeatureStore
from brain.features.computers import (
    compute_catalyst_proximity_features,
    compute_filing_count_features,
    compute_price_features,
)
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[3] / "migrations"

SYM = "TCS"
EXC = "NSE"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    run_migrations(str(p), _MIGRATIONS)
    return str(p)


@pytest.fixture
def fs(db_path):
    return FeatureStore(db_path)


def _insert_prices(db_path: str, symbol: str, exchange: str, rows: list[tuple]):
    """Insert (trade_date, open, high, low, close, volume) tuples into prices."""
    conn = sqlite3.connect(db_path)
    today = date.today().isoformat()
    for trade_date, open_, high, low, close, volume in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO prices
              (stock_symbol, exchange, trade_date, open, high, low, close, volume, as_of_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, exchange, trade_date, open_, high, low, close, volume, today),
        )
    conn.commit()
    conn.close()


# ── compute_price_features ────────────────────────────────────────────────────

class TestComputePriceFeatures:
    def _seed(self, db_path, n: int, base_close: float = 100.0):
        """Insert n consecutive daily rows ending today, all with uniform values."""
        today = date(2024, 6, 1)
        rows = []
        for i in range(n):
            d = (today - timedelta(days=i)).isoformat()
            rows.append((d, base_close, base_close + 2, base_close - 2, base_close, 1_000_000))
        _insert_prices(db_path, SYM, EXC, rows)
        return today

    def test_writes_price_close_and_liquidity(self, db_path, fs):
        as_of = self._seed(db_path, 20)
        compute_price_features(db_path, fs, SYM, EXC, as_of)

        assert fs.get_feature_as_of(SYM, EXC, "price_close", as_of) == pytest.approx(100.0)
        # avg_traded_value_20d = close * volume = 100 * 1_000_000 = 1e8
        assert fs.get_feature_as_of(SYM, EXC, "avg_traded_value_20d", as_of) == pytest.approx(1e8)

    def test_atr14_uniform_highs_lows(self, db_path, fs):
        as_of = self._seed(db_path, 20)
        compute_price_features(db_path, fs, SYM, EXC, as_of)

        # ATR-14 = mean of (high - low) over 14 days = (102 - 98) = 4.0 per bar
        assert fs.get_feature_as_of(SYM, EXC, "atr_14d", as_of) == pytest.approx(4.0)

    def test_dma20_written_when_20_rows_available(self, db_path, fs):
        as_of = self._seed(db_path, 20, base_close=200.0)
        compute_price_features(db_path, fs, SYM, EXC, as_of)

        # All closes are 200.0 → DMA-20 = 200.0
        assert fs.get_feature_as_of(SYM, EXC, "dma_20", as_of) == pytest.approx(200.0)

    def test_high_20d_is_max_high(self, db_path, fs):
        today = date(2024, 6, 1)
        # Insert 20 rows; last row (oldest) has a spike high of 999
        rows = []
        for i in range(19):
            d = (today - timedelta(days=i)).isoformat()
            rows.append((d, 100, 102, 98, 100, 1_000_000))
        rows.append(((today - timedelta(days=19)).isoformat(), 100, 999, 98, 100, 1_000_000))
        _insert_prices(db_path, SYM, EXC, rows)
        compute_price_features(db_path, fs, SYM, EXC, today)

        # high_20d should be 999 (the spike)
        val = fs.get_feature_as_of(SYM, EXC, "high_20d", today)
        assert val == pytest.approx(999.0)

    def test_high_20d_is_max_high_corrected(self, db_path, fs):
        today = date(2024, 6, 1)
        rows = []
        for i in range(19):
            d = (today - timedelta(days=i)).isoformat()
            rows.append((d, 100, 102, 98, 100, 1_000_000))
        rows.append(((today - timedelta(days=19)).isoformat(), 100, 999, 98, 100, 1_000_000))
        _insert_prices(db_path, SYM, EXC, rows)
        compute_price_features(db_path, fs, SYM, EXC, today)

        val = fs.get_feature_as_of(SYM, EXC, "high_20d", today)
        assert val == pytest.approx(999.0)

    def test_dma50_not_written_when_fewer_than_50_rows(self, db_path, fs):
        as_of = self._seed(db_path, 20)
        compute_price_features(db_path, fs, SYM, EXC, as_of)

        assert fs.get_feature_as_of(SYM, EXC, "dma_50", as_of) is None

    def test_no_rows_does_not_raise(self, db_path, fs):
        compute_price_features(db_path, fs, "UNKNOWN", EXC, date(2024, 6, 1))
        assert fs.get_feature_as_of("UNKNOWN", EXC, "price_close", date(2024, 6, 1)) is None


# ── compute_filing_count_features ─────────────────────────────────────────────

class TestComputeFilingCountFeatures:
    def _insert_filing(self, db_path, symbol, exchange, observed_at_utc):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO raw_archive
              (raw_id, source, fetched_at, request_url, response_status, content_hash, content_path)
            VALUES (?, 'test', ?, 'http://x', 200, 'abc', '/tmp/x')
            """,
            (observed_at_utc, observed_at_utc),
        )
        conn.execute(
            """
            INSERT INTO filings
              (filing_id, raw_id, parser_version, stock_symbol, exchange,
               filing_date, observed_at, category, headline, sentiment_label,
               sentiment_confidence)
            VALUES (?, ?, '1.0', ?, ?, ?, ?, 'other', 'Test filing',
                    'neutral', 0.5)
            """,
            (
                observed_at_utc + "_f", observed_at_utc, symbol, exchange,
                observed_at_utc[:10], observed_at_utc,
            ),
        )
        conn.commit()
        conn.close()

    def test_counts_filings_within_7_days(self, db_path, fs):
        as_of = date(2024, 6, 1)
        self._insert_filing(db_path, SYM, EXC, "2024-05-28T10:00:00")  # 4 days ago — inside
        self._insert_filing(db_path, SYM, EXC, "2024-05-20T10:00:00")  # 12 days ago — outside
        compute_filing_count_features(db_path, fs, SYM, EXC, as_of)

        val = fs.get_feature_as_of(SYM, EXC, "filing_count_7d", as_of)
        assert val == pytest.approx(1.0)

    def test_zero_when_no_filings(self, db_path, fs):
        as_of = date(2024, 6, 1)
        compute_filing_count_features(db_path, fs, SYM, EXC, as_of)

        val = fs.get_feature_as_of(SYM, EXC, "filing_count_7d", as_of)
        assert val == pytest.approx(0.0)


# ── compute_catalyst_proximity_features ───────────────────────────────────────

class TestComputeCatalystProximityFeatures:
    def _insert_action(
        self, db_path, symbol, exchange, ex_date: str, action_type: str = "dividend"
    ):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO corporate_actions
              (action_id, stock_symbol, exchange, action_type,
               announcement_date, ex_date, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ex_date, symbol, exchange, action_type, ex_date, ex_date, ex_date + "T00:00:00"),
        )
        conn.commit()
        conn.close()

    def test_writes_days_to_next_catalyst(self, db_path, fs):
        as_of = date(2024, 6, 1)
        self._insert_action(db_path, SYM, EXC, "2024-06-08")  # 7 days away

        compute_catalyst_proximity_features(db_path, fs, SYM, EXC, as_of)

        val = fs.get_feature_as_of(SYM, EXC, "days_to_next_catalyst", as_of)
        assert val == pytest.approx(7.0)

    def test_past_action_not_written(self, db_path, fs):
        as_of = date(2024, 6, 1)
        self._insert_action(db_path, SYM, EXC, "2024-05-15")  # in the past

        compute_catalyst_proximity_features(db_path, fs, SYM, EXC, as_of)

        assert fs.get_feature_as_of(SYM, EXC, "days_to_next_catalyst", as_of) is None

    def test_picks_nearest_of_multiple_actions(self, db_path, fs):
        as_of = date(2024, 6, 1)
        self._insert_action(db_path, SYM, EXC, "2024-06-20")  # 19 days
        self._insert_action(db_path, SYM, EXC, "2024-06-10")  # 9 days — nearest

        compute_catalyst_proximity_features(db_path, fs, SYM, EXC, as_of)

        val = fs.get_feature_as_of(SYM, EXC, "days_to_next_catalyst", as_of)
        assert val == pytest.approx(9.0)
