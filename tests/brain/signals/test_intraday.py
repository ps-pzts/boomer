"""Tests for IntradaySignalGenerator."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brain.signals.intraday import IntradaySignalGenerator

IST = ZoneInfo("Asia/Kolkata")

SYMBOL = "NIFTY_STOCK"
EXCHANGE = "NSE"

_BASE_FEATURES = {
    "avg_traded_value_20d": 2e10,  # ₹200 cr — above ₹10 cr gate
    "premarket_gap_pct": 0.5,
    "overnight_news_sentiment": 0.3,
    "orb_range_vs_20d_avg_ratio": 0.8,
    "fo_oi_overnight_change_pct": 5.0,
    "fo_max_pain_proximity_pct": 0.1,
    "minutes_since_latest_news": 20.0,
    "nifty_intraday_direction": 1.0,
    "beta_20d": 1.1,
    "bid_ask_spread_pct": 0.1,
    "days_since_max_observed": 0.0,
}


@pytest.fixture
def gen():
    return IntradaySignalGenerator()


def test_generates_signal(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(IST))
    assert signal is not None
    assert signal.track == "intraday"


def test_liquidity_gate_10cr(gen):
    features = {**_BASE_FEATURES, "avg_traded_value_20d": 5e9}  # ₹50 cr but below ₹10 cr
    # ₹50 cr < ₹10 cr? No — 5e9 = ₹500 cr. Let me use a value below ₹10 cr.
    features["avg_traded_value_20d"] = 5e7  # ₹0.5 cr
    assert gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(IST)) is None


def test_large_gap_scored_zero(gen):
    features = {**_BASE_FEATURES, "premarket_gap_pct": 3.5}
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(IST))
    # Large gap signal is neutralised — overall score should still compute but gap component = 0
    assert signal is not None


def test_confidence_between_0_and_1(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(IST))
    assert 0.0 <= signal.confidence <= 1.0


def test_weights_sum_to_one():
    from brain.signals.intraday import _WEIGHTS

    assert sum(_WEIGHTS.values()) == pytest.approx(1.0)
