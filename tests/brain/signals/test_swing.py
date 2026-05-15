"""Tests for SwingSignalGenerator."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brain.signals.swing import SwingSignalGenerator

IST = ZoneInfo("Asia/Kolkata")

SYMBOL = "TCS"
EXCHANGE = "NSE"

_BASE_FEATURES = {
    "avg_traded_value_20d": 5e9,  # ₹50 cr — above ₹2 cr gate
    "days_to_next_catalyst": 5.0,
    "technical_pattern_score": 0.7,
    "volume_zscore_5d": 1.5,
    "sector_relative_strength_20d": 0.4,
    "filing_count_7d": 2.0,
    "price_mode_classifier": 1.0,  # momentum mode
    "days_since_max_observed": 0.0,
}


@pytest.fixture
def gen():
    return SwingSignalGenerator()


def test_generates_signal(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(IST))
    assert signal is not None
    assert signal.track == "swing"
    assert signal.direction.value in ("long", "neutral")


def test_liquidity_gate_2cr(gen):
    features = {**_BASE_FEATURES, "avg_traded_value_20d": 1e7}  # below ₹2 cr
    assert gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(IST)) is None


def test_confidence_between_0_and_1(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(IST))
    assert 0.0 <= signal.confidence <= 1.0


def test_all_data_missing_returns_none(gen):
    features = {"avg_traded_value_20d": 5e9, "days_since_max_observed": 0.0}
    # All sub-signals missing except liquidity and freshness
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(IST))
    # Should still generate; missing sub-signals excluded gracefully
    assert signal is not None


def test_weights_sum_to_one():
    from brain.signals.swing import _WEIGHTS

    assert sum(_WEIGHTS.values()) == pytest.approx(1.0)
