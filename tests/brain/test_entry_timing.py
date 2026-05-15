"""Tests for Stage 3.5 entry timing classifiers."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from brain.entry_timing import (
    IntradayClassifier,
    LongTermClassifier,
    SwingClassifier,
    check_stacking_gate,
)
from brain.models import Direction, EntryStrategy, TradePlan

IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2024, 5, 1, 7, 0, tzinfo=IST)


def _make_plan(track="long_term"):
    import uuid

    return TradePlan(
        plan_id=str(uuid.uuid4()),
        signal_id=str(uuid.uuid4()),
        stock_symbol="TCS",
        exchange="NSE",
        track=track,
        direction=Direction.LONG,
        entry_zone_low=Decimal("3800"),
        entry_zone_high=Decimal("3840"),
        stop_loss_price=Decimal("3700"),
        target_price=Decimal("4000"),
        expected_reward_per_share=Decimal("180"),
        expected_risk_per_share=Decimal("120"),
        reward_to_risk=Decimal("1.5"),
        expected_value_per_share=Decimal("20"),
        decision="proceed",
        skip_reason=None,
        entry_strategy_id=None,
        created_at=_NOW,
    )


class TestLongTermClassifier:
    def test_lt2_when_near_50dma(self):
        plan = _make_plan("long_term")
        features = {"dma_50": 3830.0, "dma_200": 3500.0, "atr_14d": 40.0}
        entries = LongTermClassifier().classify(plan, features)
        assert len(entries) == 1
        assert entries[0].strategy == EntryStrategy.LT2

    def test_lt1_three_tranches_default(self):
        plan = _make_plan("long_term")
        features = {"dma_50": 3500.0, "dma_200": 3200.0, "atr_14d": 40.0}
        entries = LongTermClassifier().classify(plan, features)
        assert len(entries) == 3
        assert all(e.strategy == EntryStrategy.LT1 for e in entries)
        fracs = [e.tranche_fraction for e in entries]
        assert sum(fracs) == pytest.approx(Decimal("1.0"))

    def test_lt1_validity_30_days(self):
        plan = _make_plan("long_term")
        features = {"dma_50": 3500.0, "dma_200": 3200.0, "atr_14d": 40.0}
        entries = LongTermClassifier().classify(plan, features)
        assert all(e.validity_days == 30 for e in entries)


class TestSwingClassifier:
    def test_sw3_near_catalyst(self):
        plan = _make_plan("swing")
        features = {"days_to_next_catalyst": 3.0, "volume_zscore_5d": 0.2, "atr_14d": 15.0}
        entries = SwingClassifier().classify(plan, features)
        assert entries[0].strategy == EntryStrategy.SW3
        assert entries[0].tranche_fraction == Decimal("0.50")

    def test_sw1_high_volume_breakout(self):
        plan = _make_plan("swing")
        features = {
            "days_to_next_catalyst": None,
            "high_20d": 3850.0,
            "volume_zscore_5d": 1.5,
            "dma_20": 3750.0,
            "atr_14d": 15.0,
        }
        entries = SwingClassifier().classify(plan, features)
        assert entries[0].strategy == EntryStrategy.SW1

    def test_sw2_default(self):
        plan = _make_plan("swing")
        features = {
            "days_to_next_catalyst": None,
            "volume_zscore_5d": 0.1,  # below 0.5 threshold
            "dma_20": 3750.0,
            "atr_14d": 15.0,
        }
        entries = SwingClassifier().classify(plan, features)
        assert entries[0].strategy == EntryStrategy.SW2


class TestIntradayClassifier:
    def test_id1_default_orb(self):
        plan = _make_plan("intraday")
        features = {
            "premarket_gap_pct": 0.2,
            "orb_high": 3850.0,
            "orb_low": 3810.0,
            "minutes_since_market_open": 20.0,
            "atr_14d": 20.0,
        }
        entries = IntradayClassifier().classify(plan, features)
        assert len(entries) == 1
        assert entries[0].strategy == EntryStrategy.ID1

    def test_large_gap_returns_empty(self):
        plan = _make_plan("intraday")
        features = {"premarket_gap_pct": 3.0, "atr_14d": 20.0}
        entries = IntradayClassifier().classify(plan, features)
        assert entries == []

    def test_id2_vwap_pullback(self):
        plan = _make_plan("intraday")
        features = {
            "premarket_gap_pct": 0.2,
            "vwap_current": 3820.0,
            "price_vs_open_pct": 0.8,  # ran up >0.5%
            "minutes_since_market_open": 45.0,
            "atr_14d": 20.0,
        }
        entries = IntradayClassifier().classify(plan, features)
        assert entries[0].strategy == EntryStrategy.ID2


class TestStackingGate:
    def test_all_conditions_met(self):
        ok, reason = check_stacking_gate(
            intraday_signal=None,
            swing_position_pnl_pct=2.5,
            intraday_contributing_names={"premarket_gap", "fo_signals"},
            swing_contributing_names={"promoter"},
            total_exposure_pct=3.0,
            single_stock_cap_pct=0.05,
        )
        assert ok is True

    def test_fails_swing_in_loss(self):
        ok, reason = check_stacking_gate(
            intraday_signal=None,
            swing_position_pnl_pct=0.5,  # below 1% threshold
            intraday_contributing_names={"premarket_gap"},
            swing_contributing_names={"promoter"},
            total_exposure_pct=2.0,
            single_stock_cap_pct=0.05,
        )
        assert ok is False
        assert "averaging down" in reason.lower() or "1%" in reason

    def test_fails_low_signal_independence(self):
        ok, reason = check_stacking_gate(
            intraday_signal=None,
            swing_position_pnl_pct=3.0,
            intraday_contributing_names={"promoter", "smart_money"},  # 100% overlap
            swing_contributing_names={"promoter", "smart_money"},
            total_exposure_pct=2.0,
            single_stock_cap_pct=0.05,
        )
        assert ok is False
        assert "independence" in reason.lower() or "50%" in reason

    def test_fails_concentration_cap(self):
        ok, reason = check_stacking_gate(
            intraday_signal=None,
            swing_position_pnl_pct=3.0,
            intraday_contributing_names={"premarket_gap"},
            swing_contributing_names={"promoter"},
            total_exposure_pct=6.0,  # > 5% cap
            single_stock_cap_pct=0.05,
        )
        assert ok is False
        assert "concentration" in reason.lower() or "cap" in reason.lower()
