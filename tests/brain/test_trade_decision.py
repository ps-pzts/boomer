"""Tests for Stage 3 TradePlanGenerator.

Worked example (verifiable by hand):
  stock: RELIANCE, track: intraday
  current_price = 2500, atr_14d = 50, bucket_capital = 100_000
  signal.confidence = 0.70, live_backtest_ratio_intraday = 0.70

  k = ATR_K[intraday] = 1.5
  stop = 2500 - 1.5×50 = 2425

  rr_min = MIN_RR[intraday] = 1.5
  stop_distance = 2500 - 2425 = 75
  target = 2500 + 1.5×75 = 2612.5

  RR = 112.5/75 = 1.5 ≥ 1.5 → pass

  p_win = 0.70 × 0.70 = 0.49; p_loss = 0.51
  cost = 2500 × 0.0030 = 7.50
  reward_after_costs = 112.5 - 7.50 = 105
  risk_after_costs = 75 + 7.50 = 82.50
  EV = 0.49×105 - 0.51×82.50 = 51.45 - 42.075 = 9.375 > 0 → pass

  risk_pct = 0.005 (intraday default)
  risk_rupees = 100_000 × 0.005 = 500
  shares = floor(500 / 75) = 6 ≥ 1 → pass

  Target-too-close: reward=112.5 ≥ 0.5×50=25 → pass
  Decision: proceed
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from brain.models import Direction, SkipReason
from brain.trade_decision import TradePlanGenerator
from capital.models import RiskConfig

IST = ZoneInfo("Asia/Kolkata")
GENERATED_AT = datetime(2024, 4, 1, 7, 0, tzinfo=IST)


def _make_risk_config() -> RiskConfig:
    from datetime import date

    return RiskConfig(
        config_id="test",
        version=1,
        effective_from=date(2024, 1, 1),
        risk_per_intraday_trade_pct=Decimal("0.005"),
        intraday_daily_loss_limit_pct=Decimal("0.020"),
        portfolio_daily_loss_limit_pct=Decimal("0.020"),
        portfolio_weekly_loss_limit_pct=Decimal("0.040"),
        portfolio_max_drawdown_pct=Decimal("0.080"),
        single_stock_cap_pct=Decimal("0.050"),
        sector_cap_pct=Decimal("0.250"),
        correlation_cluster_cap_pct=Decimal("0.350"),
        intraday_consecutive_loss_count=3,
        nifty_intraday_pause_pct=Decimal("0.030"),
        live_backtest_ratio_intraday=Decimal("0.70"),
        sentiment_confidence_threshold=Decimal("0.60"),
        min_stock_price=Decimal("100"),
        min_avg_daily_volume=500_000,
        min_avg_daily_turnover_cr=Decimal("5"),
    )


def _make_signal(track="intraday", confidence=0.70):
    import uuid

    from brain.models import ContributingSignal, SignalRecord

    return SignalRecord(
        signal_id=str(uuid.uuid4()),
        stock_symbol="RELIANCE",
        exchange="NSE",
        track=track,
        direction=Direction.LONG,
        raw_score=0.7,
        confidence=confidence,
        regime_at_signal="bull_calm",
        contributing_signals=[ContributingSignal("premarket_gap", 0.30, 0.8, 0.24)],
        feature_snapshot={},
        generated_at=GENERATED_AT,
    )


@pytest.fixture
def gen():
    return TradePlanGenerator()


@pytest.fixture
def rc():
    return _make_risk_config()


def test_worked_example(gen, rc):
    """Exact numerical verification from docstring."""
    signal = _make_signal("intraday", confidence=0.70)
    plan = gen.generate(
        signal,
        current_price=Decimal("2500"),
        atr_14d=Decimal("50"),
        bucket_capital=Decimal("100000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "proceed"
    assert plan.stop_loss_price == pytest.approx(Decimal("2425"), abs=Decimal("1"))
    assert plan.target_price == pytest.approx(Decimal("2612.5"), abs=Decimal("1"))
    assert plan.reward_to_risk == pytest.approx(Decimal("1.5"), abs=Decimal("0.05"))
    assert plan.expected_value_per_share > 0


def test_ev_negative_when_cost_swamps_tiny_atr(gen, rc):
    # Near-zero ATR → reward = rr_min × k × atr ≈ 0.0225
    # Round-trip cost = 100 × 0.30% = 0.30 >> reward → EV negative
    signal = _make_signal("intraday", confidence=0.8)
    plan = gen.generate(
        signal,
        current_price=Decimal("100"),
        atr_14d=Decimal("0.01"),
        bucket_capital=Decimal("10000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.EV_NEGATIVE


def test_ev_negative_skips(gen, rc):
    # Very low confidence → EV negative
    signal = _make_signal("intraday", confidence=0.10)
    plan = gen.generate(
        signal,
        current_price=Decimal("1000"),
        atr_14d=Decimal("30"),
        bucket_capital=Decimal("10000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.EV_NEGATIVE


def test_position_too_small_skips(gen, rc):
    # Tiny bucket capital + large ATR → shares < 1
    signal = _make_signal("intraday", confidence=0.70)
    plan = gen.generate(
        signal,
        current_price=Decimal("5000"),
        atr_14d=Decimal("200"),  # stop = 5000 - 1.5×200 = 4700; risk=300
        bucket_capital=Decimal("100"),  # risk_rupees = 100×0.005=0.5; shares = floor(0.5/300) = 0
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.POSITION_TOO_SMALL


def test_proceeds_stores_signal_id(gen, rc):
    signal = _make_signal("intraday", confidence=0.75)
    plan = gen.generate(
        signal,
        current_price=Decimal("500"),
        atr_14d=Decimal("10"),
        bucket_capital=Decimal("200000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "proceed"
    assert plan.signal_id == signal.signal_id


def test_price_too_low_skips(gen, rc):
    """CMP below min_stock_price (₹100) must be rejected before ATR math."""
    signal = _make_signal("intraday", confidence=0.80)
    signal = signal.__class__(
        **{
            **signal.__dict__,
            "feature_snapshot": {
                "price_close": 40,
                "avg_daily_volume_20d": 2_000_000,
                "avg_traded_value_20d": 80_000_000,
            },
        }
    )
    plan = gen.generate(
        signal,
        current_price=Decimal("40"),
        atr_14d=Decimal("1"),
        bucket_capital=Decimal("200000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.PRICE_TOO_LOW


def test_volume_too_low_skips(gen, rc):
    """Avg daily volume below 5,00,000 shares triggers liquidity gate."""
    signal = _make_signal("intraday", confidence=0.80)
    signal = signal.__class__(
        **{
            **signal.__dict__,
            "feature_snapshot": {
                "price_close": 500,
                "avg_daily_volume_20d": 100_000,
                "avg_traded_value_20d": 50_000_000,
            },
        }
    )
    plan = gen.generate(
        signal,
        current_price=Decimal("500"),
        atr_14d=Decimal("10"),
        bucket_capital=Decimal("200000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.LIQUIDITY_GATE


def test_turnover_too_low_skips(gen, rc):
    """Avg daily turnover below ₹5 crore triggers liquidity gate (market-cap proxy)."""
    signal = _make_signal("intraday", confidence=0.80)
    # avg_traded_value_20d = 3 crore = 30_000_000 (below 5 crore threshold)
    signal = signal.__class__(
        **{
            **signal.__dict__,
            "feature_snapshot": {
                "price_close": 500,
                "avg_daily_volume_20d": 600_000,
                "avg_traded_value_20d": 30_000_000,
            },
        }
    )
    plan = gen.generate(
        signal,
        current_price=Decimal("500"),
        atr_14d=Decimal("10"),
        bucket_capital=Decimal("200000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "skip"
    assert plan.skip_reason == SkipReason.LIQUIDITY_GATE


def test_all_filters_pass_proceeds(gen, rc):
    """Verify a stock clearing all three quality filters reaches the EV gate."""
    signal = _make_signal("intraday", confidence=0.80)
    # price=500 > 100, avg_vol=1M > 500k, turnover=50Cr > 5Cr
    signal = signal.__class__(
        **{
            **signal.__dict__,
            "feature_snapshot": {
                "price_close": 500,
                "avg_daily_volume_20d": 1_000_000,
                "avg_traded_value_20d": 500_000_000,
            },
        }
    )
    plan = gen.generate(
        signal,
        current_price=Decimal("500"),
        atr_14d=Decimal("10"),
        bucket_capital=Decimal("200000"),
        risk_config=rc,
        generated_at=GENERATED_AT,
    )
    assert plan.decision == "proceed"
