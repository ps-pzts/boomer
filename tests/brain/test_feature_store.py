"""Tests for brain.feature_store — point-in-time query correctness is the key property."""
from datetime import date
from pathlib import Path

import pytest

from brain.feature_store import FeatureStore
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[2] / "migrations"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    run_migrations(str(p), _MIGRATIONS)
    return str(p)


@pytest.fixture
def fs(db_path):
    return FeatureStore(db_path)


def test_write_and_read_feature(fs):
    d = date(2024, 1, 15)
    fs.write_feature("RELIANCE", "NSE", "atr_14d", 25.5, d, d)
    val = fs.get_feature_as_of("RELIANCE", "NSE", "atr_14d", d)
    assert val == pytest.approx(25.5)


def test_point_in_time_past_date_excluded(fs):
    # Feature valid from Jan 15, observed Jan 15
    # Query on Jan 14 must return None (valid_from > query date)
    d = date(2024, 1, 15)
    fs.write_feature("RELIANCE", "NSE", "atr_14d", 25.5, d, d)
    val = fs.get_feature_as_of("RELIANCE", "NSE", "atr_14d", date(2024, 1, 14))
    assert val is None


def test_source_max_observed_at_blocks_future_data(fs):
    # Feature valid_from Jan 10, but source was observed Jan 20 (future data)
    # Query on Jan 15 must return None (source_max_observed_at > query date)
    fs.write_feature("RELIANCE", "NSE", "atr_14d", 30.0,
                     valid_from=date(2024, 1, 10),
                     source_max_observed_at=date(2024, 1, 20))
    val = fs.get_feature_as_of("RELIANCE", "NSE", "atr_14d", date(2024, 1, 15))
    assert val is None


def test_supersede_on_same_valid_from(fs):
    d = date(2024, 2, 1)
    fs.write_feature("TCS", "NSE", "pe_percentile_5y", 60.0, d, d)
    fs.write_feature("TCS", "NSE", "pe_percentile_5y", 55.0, d, d)
    # Only the latest write should be current
    val = fs.get_feature_as_of("TCS", "NSE", "pe_percentile_5y", d)
    assert val == pytest.approx(55.0)


def test_get_features_as_of_returns_dict(fs):
    d = date(2024, 3, 1)
    fs.write_feature("INFY", "NSE", "atr_14d", 12.0, d, d)
    fs.write_feature("INFY", "NSE", "avg_traded_value_20d", 1e10, d, d)
    result = fs.get_features_as_of("INFY", "NSE", d)
    assert "atr_14d" in result
    assert "avg_traded_value_20d" in result
    assert result["atr_14d"] == pytest.approx(12.0)


def test_get_latest_feature_ignores_dates(fs):
    fs.write_feature("WIPRO", "NSE", "atr_14d", 8.0, date(2024, 1, 1), date(2024, 1, 1))
    fs.write_feature("WIPRO", "NSE", "atr_14d", 9.5, date(2024, 3, 1), date(2024, 3, 1))
    val = fs.get_latest_feature("WIPRO", "NSE", "atr_14d")
    assert val == pytest.approx(9.5)


def test_list_symbols_with_feature(fs):
    d = date(2024, 4, 1)
    fs.write_feature("HDFC", "NSE", "avg_traded_value_20d", 1e9, d, d)
    fs.write_feature("ICICI", "NSE", "avg_traded_value_20d", 2e9, d, d)
    syms = fs.list_symbols_with_feature("avg_traded_value_20d", d)
    assert "HDFC" in syms
    assert "ICICI" in syms


def test_unknown_symbol_returns_none(fs):
    val = fs.get_feature_as_of("UNKNOWN", "NSE", "atr_14d", date(2024, 1, 1))
    assert val is None
