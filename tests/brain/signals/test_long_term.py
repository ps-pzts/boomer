"""Tests for LongTermSignalGenerator.

Worked example (verifiable by hand):
  regime = bull_calm
  weights = promoter:0.30, smart_money:0.20, filing_sentiment:0.15,
            earnings_quality:0.20, valuation:0.15

  promoter_holding_pct_change_90d = 1.0  → holding_score = 1.0
  promoter_open_market_buy_count_90d = 3 → buy_intensity = 1.0
  promoter_pledge_pct_current = 0        → pledge_penalty = 0.0
  promoter_score = 0.5×1 + 0.3×1 + 0.2×0 = 0.80

  smart_money_net_buy_value_90d = 50 cr → size_score = clip(50e7 / 5e9, -1, 1) = 0.10
  smart_money_buyer_count_90d = 3      → breadth_score = 1.0
  smart_money_score = 0.7×0.10 + 0.3×1.0 = 0.37

  filing_bullish_count_90d = 5, filing_bearish_count_90d = 1
  net = (5-1)/max(6,1) = 0.667
  no red flags → penalty = 0
  filing_score = 0.7×0.667 + 0.3×0 = 0.467

  revenue_growth = 20%, opm_trend = 0.1, cfo_pat_ratio = 0.9
  revenue_score = clip((20-10)/30, -1, 1) = 0.333
  margin_score = clip(0.1×5, -1, 1) = 0.5
  cfo_score = clip((0.9-0.7)/0.5, -1, 1) = 0.4
  earnings_score = 0.4×0.333 + 0.3×0.5 + 0.3×0.4 = 0.133 + 0.15 + 0.12 = 0.403

  pe_percentile_5y = 30
  valuation_score = clip((50-30)/50, -1, 1) = 0.4

  raw_score = 0.30×0.80 + 0.20×0.37 + 0.15×0.467 + 0.20×0.403 + 0.15×0.4
            = 0.240 + 0.074 + 0.070 + 0.081 + 0.060
            = 0.525
"""

from datetime import UTC, datetime

import pytest

from brain.signals.long_term import LongTermSignalGenerator

SYMBOL = "RELIANCE"
EXCHANGE = "NSE"

_BASE_FEATURES = {
    "avg_traded_value_20d": 2e10,  # ₹200 cr — above ₹5 cr gate
    "promoter_holding_pct_change_90d": 1.0,
    "promoter_open_market_buy_count_90d": 3.0,
    "promoter_pledge_pct_current": 0.0,
    "smart_money_net_buy_value_90d": 50_00_00_000.0,  # ₹50 cr
    "smart_money_buyer_count_90d": 3.0,
    "filing_bullish_count_90d": 5.0,
    "filing_bearish_count_90d": 1.0,
    "has_auditor_change_90d": 0,
    "has_pledging_increase_90d": 0,
    "revenue_growth_yoy_pct": 20.0,
    "opm_trend_4q": 0.1,
    "cfo_pat_ratio_latest": 0.9,
    "pe_percentile_5y": 30.0,
    "days_since_max_observed": 0.0,
}


@pytest.fixture
def gen():
    return LongTermSignalGenerator()


def test_full_signal_bull_calm(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(UTC))
    assert signal is not None
    assert signal.raw_score == pytest.approx(0.525, abs=0.01)
    assert signal.direction.value == "long"
    assert signal.track == "long_term"


def test_liquidity_gate_fails_below_5cr(gen):
    features = {**_BASE_FEATURES, "avg_traded_value_20d": 1e7}  # ₹0.1 cr
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(UTC))
    assert signal is None


def test_promoter_none_when_shares_outstanding_missing(gen):
    features = {**_BASE_FEATURES}
    del features["promoter_holding_pct_change_90d"]
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(UTC))
    # Signal still generated; promoter sub-signal contributes 0 (absent)
    assert signal is not None
    # raw_score is lower because promoter weight (0.30) is redistributed
    assert signal.raw_score < 0.525


def test_earnings_none_when_data_missing(gen):
    features = {**_BASE_FEATURES}
    del features["revenue_growth_yoy_pct"]
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(UTC))
    assert signal is not None


def test_red_flag_penalty_applied(gen):
    features = {**_BASE_FEATURES, "has_auditor_change_90d": 1}
    signal = gen.generate(SYMBOL, EXCHANGE, features, "bull_calm", datetime.now(UTC))
    assert signal is not None
    # Filing score should be heavily penalised; overall score lower
    normal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(UTC))
    assert signal.raw_score < normal.raw_score


def test_weights_sum_to_one_per_regime(gen):
    from brain.signals.long_term import _WEIGHTS

    for regime, weights in _WEIGHTS.items():
        total = sum(weights.values())
        assert total == pytest.approx(1.0, abs=1e-9), f"weights for {regime} sum to {total}"


def test_bear_regime_weights_promoter_heaviest(gen):
    from brain.signals.long_term import _WEIGHTS

    bear = _WEIGHTS["bear"]
    assert bear["promoter"] == max(bear.values())


def test_confidence_between_0_and_1(gen):
    signal = gen.generate(SYMBOL, EXCHANGE, _BASE_FEATURES, "bull_calm", datetime.now(UTC))
    assert signal is not None
    assert 0.0 <= signal.confidence <= 1.0
