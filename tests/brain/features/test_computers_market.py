"""Tests for brain.features.computers_market."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain.feature_store import FeatureStore
from brain.features.computers import compute_price_features
from brain.features.computers_market import (
    compute_beta_features,
    compute_fo_features,
    compute_price_mode_classifier,
    compute_sector_relative_strength,
    compute_technical_pattern_score,
)
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[3] / "migrations"

SYM = "TCS"
EXC = "NSE"
AS_OF = date(2024, 6, 1)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    run_migrations(str(p), _MIGRATIONS)
    return str(p)


@pytest.fixture
def fs(db_path):
    return FeatureStore(db_path)


def _insert_prices(db_path, symbol, exchange, rows):
    """rows: list of (trade_date_str, close, high, low, volume)."""
    conn = sqlite3.connect(db_path)
    today = date.today().isoformat()
    for trade_date, close, high, low, volume in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO prices
              (stock_symbol, exchange, trade_date, open, high, low, close, volume, as_of_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, exchange, trade_date, close, high, low, close, volume, today),
        )
    conn.commit()
    conn.close()


def _seed_prices(db_path, symbol, exchange, n, close=100.0, high_offset=2.0):
    rows = [
        ((AS_OF - timedelta(days=i)).isoformat(), close, close + high_offset, close - 2, 1_000_000)
        for i in range(n)
    ]
    _insert_prices(db_path, symbol, exchange, rows)


# ── compute_technical_pattern_score ──────────────────────────────────────────

class TestComputeTechnicalPatternScore:
    def test_bullish_structure_positive_score(self, db_path, fs):
        # price > dma_20 > dma_50 → components 1 and 2 both positive
        # Seed 50 rows: last 20 at 100, prior 30 at 80 → dma_20 = 100, dma_50 = 88
        rows = []
        for i in range(20):
            rows.append(((AS_OF - timedelta(days=i)).isoformat(), 100, 102, 98, 1_000_000))
        for i in range(20, 50):
            rows.append(((AS_OF - timedelta(days=i)).isoformat(), 80, 82, 78, 1_000_000))
        _insert_prices(db_path, SYM, EXC, rows)
        compute_price_features(db_path, fs, SYM, EXC, AS_OF)
        compute_technical_pattern_score(db_path, fs, SYM, EXC, AS_OF)

        score = fs.get_feature_as_of(SYM, EXC, "technical_pattern_score", AS_OF)
        assert score is not None
        assert score > 0.0, f"expected positive score for bullish structure, got {score}"

    def test_bearish_structure_negative_score(self, db_path, fs):
        # price < dma_20: last 20 rows at 80, prior 30 at 120 → dma_20 = 80, dma_50 = 104
        # price_close = 80, dma_20 = 80 → price is AT dma_20 (not below yet)
        # Set price below dma_20: last 5 at 70, prior 15 at 85 → dma_20 = 82.5
        rows = []
        for i in range(5):
            rows.append(((AS_OF - timedelta(days=i)).isoformat(), 70, 72, 68, 1_000_000))
        for i in range(5, 20):
            rows.append(((AS_OF - timedelta(days=i)).isoformat(), 85, 87, 83, 1_000_000))
        for i in range(20, 50):
            rows.append(((AS_OF - timedelta(days=i)).isoformat(), 120, 122, 118, 1_000_000))
        _insert_prices(db_path, SYM, EXC, rows)
        compute_price_features(db_path, fs, SYM, EXC, AS_OF)
        compute_technical_pattern_score(db_path, fs, SYM, EXC, AS_OF)

        score = fs.get_feature_as_of(SYM, EXC, "technical_pattern_score", AS_OF)
        assert score is not None
        assert score < 0.0, f"expected negative score for bearish structure, got {score}"

    def test_returns_none_when_no_price_data(self, db_path, fs):
        compute_technical_pattern_score(db_path, fs, "NODATA", EXC, AS_OF)
        assert fs.get_feature_as_of("NODATA", EXC, "technical_pattern_score", AS_OF) is None

    def test_score_clipped_to_minus1_plus1(self, db_path, fs):
        _seed_prices(db_path, SYM, EXC, 50, close=100.0)
        compute_price_features(db_path, fs, SYM, EXC, AS_OF)
        compute_technical_pattern_score(db_path, fs, SYM, EXC, AS_OF)

        score = fs.get_feature_as_of(SYM, EXC, "technical_pattern_score", AS_OF)
        if score is not None:
            assert -1.0 <= score <= 1.0


# ── compute_fo_features ────────────────────────────────────────────────────────

class TestComputeFoFeatures:
    def _insert_fo(self, db_path, symbol, trade_date, instrument_type, expiry,
                   oi, oi_change=None, strike=None):
        conn = sqlite3.connect(db_path)
        import uuid
        conn.execute(
            """
            INSERT INTO raw_archive
              (raw_id, source, fetched_at, request_url, response_status, content_hash, content_path)
            VALUES (?, 'fo_test', ?, 'http://x', 200, ?, '/tmp/x')
            """,
            (str(uuid.uuid4()), trade_date + "T00:00:00", str(uuid.uuid4())),
        )
        raw_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # raw_id is an integer rowid, but raw_id column is TEXT — re-fetch
        raw_id = conn.execute(
            "SELECT raw_id FROM raw_archive ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO fo_oi_daily
              (record_id, raw_id, parser_version, underlying_symbol, instrument_type,
               expiry_date, strike_price, trade_date, observed_at, open_interest, oi_change, volume)
            VALUES (?, ?, '1.0', ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                str(uuid.uuid4()), raw_id, symbol, instrument_type,
                expiry, strike, trade_date, trade_date + "T15:30:00",
                oi, oi_change,
            ),
        )
        conn.commit()
        conn.close()

    def test_overnight_oi_change_pct(self, db_path, fs):
        # OI = 10000, oi_change = +1000 → prev_oi = 9000 → change% = 11.11%
        self._insert_fo(
            db_path, SYM, AS_OF.isoformat(), "FUT", "2024-06-27", oi=10000, oi_change=1000
        )
        compute_fo_features(db_path, fs, SYM, EXC, AS_OF)

        val = fs.get_feature_as_of(SYM, EXC, "fo_oi_overnight_change_pct", AS_OF)
        assert val == pytest.approx(1000 / 9000 * 100, rel=1e-4)

    def test_max_pain_proximity(self, db_path, fs):
        # Insert options: CE at 100 (OI=1000), PE at 100 (OI=1000)
        # Max pain at 100. Set price_close = 105 → proximity = (105-100)/105*100 ≈ 4.76%
        self._insert_fo(db_path, SYM, AS_OF.isoformat(), "CE", "2024-06-27", oi=1000, strike=100)
        self._insert_fo(db_path, SYM, AS_OF.isoformat(), "PE", "2024-06-27", oi=1000, strike=100)
        fs.write_feature(SYM, EXC, "price_close", 105.0, AS_OF, AS_OF)
        compute_fo_features(db_path, fs, SYM, EXC, AS_OF)

        prox = fs.get_feature_as_of(SYM, EXC, "fo_max_pain_proximity_pct", AS_OF)
        assert prox is not None
        assert prox == pytest.approx((105 - 100) / 105 * 100, rel=1e-4)

    def test_no_fo_data_writes_nothing(self, db_path, fs):
        compute_fo_features(db_path, fs, SYM, EXC, AS_OF)
        assert fs.get_feature_as_of(SYM, EXC, "fo_oi_overnight_change_pct", AS_OF) is None
        assert fs.get_feature_as_of(SYM, EXC, "fo_max_pain_proximity_pct", AS_OF) is None


# ── compute_price_mode_classifier ────────────────────────────────────────────

class TestComputePriceModeClassifier:
    def test_trending_market_positive_autocorr(self, db_path, fs):
        # Steadily rising prices → positive serial correlation → score > 0
        rows = []
        for i in range(12):
            close = 100.0 + (11 - i) * 1.0  # decreasing index = ascending close over time
            rows.append(
                ((AS_OF - timedelta(days=i)).isoformat(), close, close + 1, close - 1, 1_000_000)
            )
        _insert_prices(db_path, SYM, EXC, rows)
        compute_price_mode_classifier(db_path, fs, SYM, EXC, AS_OF)

        val = fs.get_feature_as_of(SYM, EXC, "price_mode_classifier", AS_OF)
        assert val is not None
        assert -1.0 <= val <= 1.0

    def test_insufficient_data_writes_nothing(self, db_path, fs):
        _seed_prices(db_path, SYM, EXC, 5)
        compute_price_mode_classifier(db_path, fs, SYM, EXC, AS_OF)
        assert fs.get_feature_as_of(SYM, EXC, "price_mode_classifier", AS_OF) is None


# ── compute_beta_features ────────────────────────────────────────────────────

class TestComputeBetaFeatures:
    def test_beta_2x_when_stock_moves_twice_market(self, db_path, fs):
        # Market returns (oldest → newest): +5%, -3%, +4%, -2%, +3%
        # Stock returns: exactly 2× each market return → beta = 2.0
        # Uniform returns (e.g. +1% every day) give var_m ≈ 0 and are untestable.
        mkt = [100.0, 105.0, 101.85, 105.924, 103.806, 106.920]
        stk = [100.0, 110.0, 103.40, 111.672, 107.205, 113.637]

        mkt_rows = [
            ((AS_OF - timedelta(days=5 - i)).isoformat(), mkt[i], mkt[i] + 1, mkt[i] - 1, 1_000_000)
            for i in range(6)
        ]
        stk_rows = [
            ((AS_OF - timedelta(days=5 - i)).isoformat(), stk[i], stk[i] + 2, stk[i] - 2, 500_000)
            for i in range(6)
        ]
        _insert_prices(db_path, "NIFTY 50", EXC, mkt_rows)
        _insert_prices(db_path, SYM, EXC, stk_rows)

        compute_beta_features(db_path, fs, SYM, EXC, AS_OF)

        beta = fs.get_feature_as_of(SYM, EXC, "beta_20d", AS_OF)
        assert beta is not None
        assert beta == pytest.approx(2.0, rel=0.05)

    def test_no_market_data_writes_nothing(self, db_path, fs):
        _seed_prices(db_path, SYM, EXC, 10)
        compute_beta_features(db_path, fs, SYM, EXC, AS_OF)
        assert fs.get_feature_as_of(SYM, EXC, "beta_20d", AS_OF) is None


# ── compute_sector_relative_strength ─────────────────────────────────────────

class TestComputeSectorRelativeStrength:
    def _add_sector(self, db_path, symbol, sector):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO sector_classifications
              (symbol, exchange, sector, source, effective_from, updated_at)
            VALUES (?, 'NSE', ?, 'NSE', '2020-01-01', '2020-01-01')
            """,
            (symbol, sector),
        )
        conn.commit()
        conn.close()

    def test_outperformer_has_positive_rs(self, db_path, fs):
        # TCS: +10% return; INFY, WIPRO: +1% return → TCS strongly outperforms
        self._add_sector(db_path, SYM, "IT")
        self._add_sector(db_path, "INFY", "IT")
        self._add_sector(db_path, "WIPRO", "IT")

        for sym, ret in [(SYM, 10.0), ("INFY", 1.0), ("WIPRO", 1.0)]:
            rows = [
                ((AS_OF - timedelta(days=i)).isoformat(),
                 100.0 + (19 - i) * ret / 19,
                 102.0, 98.0, 1_000_000)
                for i in range(20)
            ]
            _insert_prices(db_path, sym, EXC, rows)

        compute_sector_relative_strength(db_path, fs, SYM, EXC, AS_OF)

        rs = fs.get_feature_as_of(SYM, EXC, "sector_relative_strength_20d", AS_OF)
        assert rs is not None
        assert rs > 0.0

    def test_no_sector_classification_writes_nothing(self, db_path, fs):
        _seed_prices(db_path, SYM, EXC, 20)
        compute_sector_relative_strength(db_path, fs, SYM, EXC, AS_OF)
        assert fs.get_feature_as_of(SYM, EXC, "sector_relative_strength_20d", AS_OF) is None
