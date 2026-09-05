"""Tests for Stage 3.5 entry timing classifier."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brain.entry_timing import IntradayClassifier
from brain.models import Direction, EntryStrategy, TradePlan

IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2024, 5, 1, 7, 0, tzinfo=IST)


def _make_plan(track="intraday"):
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
