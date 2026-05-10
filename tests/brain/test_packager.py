"""Tests for Stage 5 RecommendationPackager and RecommendationStore."""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from brain.models import (
    Direction,
    EntryStrategy,
    RecommendationOutcome,
    RecommendationStatus,
    SignalRecord,
    TradePlan,
)
from brain.packager import RecommendationPackager, RecommendationStore
from db.migrations import run_migrations

_NOW = datetime(2024, 6, 1, 8, 0, tzinfo=UTC)
_MIGRATIONS = Path(__file__).parents[2] / "migrations"


def _make_plan(track="long_term"):
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
        entry_strategy_id=EntryStrategy.LT1,
        created_at=_NOW,
    )


def _make_signal(plan):
    return SignalRecord(
        signal_id=plan.signal_id,
        stock_symbol=plan.stock_symbol,
        exchange=plan.exchange,
        track=plan.track,
        direction=Direction.LONG,
        raw_score=0.6,
        confidence=0.65,
        regime_at_signal="bull_calm",
        contributing_signals=[],
        feature_snapshot={},
        generated_at=_NOW,
    )


@pytest.fixture
def packager():
    return RecommendationPackager()


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "test.db")
    run_migrations(db, _MIGRATIONS)
    return RecommendationStore(db)


class TestPackager:
    def test_long_term_requires_human(self, packager):
        plan = _make_plan("long_term")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        assert rec.requires_human is True
        assert rec.status == RecommendationStatus.AWAITING_HUMAN

    def test_swing_does_not_require_human(self, packager):
        plan = _make_plan("swing")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=15)
        assert rec.requires_human is False
        assert rec.status == RecommendationStatus.GENERATED

    def test_apm_decide_approves_passing(self, packager):
        plan = _make_plan("swing")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        rec = packager.apm_decide(rec, circuit_check_fn=lambda r: (True, "all clear"))
        assert rec.status == RecommendationStatus.APPROVED_BY_APM

    def test_apm_decide_rejects_failing(self, packager):
        plan = _make_plan("intraday")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=5)
        rec = packager.apm_decide(
            rec, circuit_check_fn=lambda r: (False, "daily loss limit breached")
        )
        assert rec.status == RecommendationStatus.REJECTED_BY_APM
        assert "daily loss limit" in rec.decision_reason

    def test_apm_decide_raises_for_human_routed(self, packager):
        plan = _make_plan("long_term")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=5)
        with pytest.raises(ValueError):
            packager.apm_decide(rec, circuit_check_fn=lambda r: (True, ""))

    def test_apply_modification_preserves_original(self, packager):
        plan = _make_plan("long_term")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        packager.apply_modification(
            rec, Decimal("3790"), Decimal("3830"), Decimal("3680"), Decimal("4050"), 12
        )
        assert rec.operator_modified is True
        assert rec.original_params is not None
        assert rec.entry_zone_low == Decimal("3790")
        assert rec.position_size_shares == 12


_OUTCOME_DATE = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)


class TestRecommendationStore:
    def test_save_and_cooldown_approved(self, packager, store):
        plan = _make_plan("swing")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        store.save(rec)
        store.record_outcome(
            rec.recommendation_id, "TCS", "NSE", "swing",
            RecommendationOutcome.APPROVED_POSITION_OPENED,
            recorded_at=_OUTCOME_DATE,
        )
        # 7-day cooldown for swing after approved position
        remaining = store.cooldown_days_remaining("TCS", "NSE", "swing", date(2024, 6, 1))
        assert remaining == 7

    def test_no_cooldown_after_rejected_by_operator(self, packager, store):
        plan = _make_plan("long_term")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=5)
        store.save(rec)
        store.record_outcome(
            rec.recommendation_id, "TCS", "NSE", "long_term",
            RecommendationOutcome.REJECTED_BY_OPERATOR,
            recorded_at=_OUTCOME_DATE,
        )
        remaining = store.cooldown_days_remaining("TCS", "NSE", "long_term", date(2024, 6, 1))
        assert remaining == 0

    def test_cooldown_expires(self, packager, store):
        plan = _make_plan("swing")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        store.save(rec)
        store.record_outcome(
            rec.recommendation_id, "TCS", "NSE", "swing",
            RecommendationOutcome.APPROVED_POSITION_OPENED,
            recorded_at=_OUTCOME_DATE,
        )
        # After 7 days, cooldown expires
        remaining = store.cooldown_days_remaining("TCS", "NSE", "swing", date(2024, 6, 8))
        assert remaining == 0

    def test_is_in_cooldown(self, packager, store):
        plan = _make_plan("swing")
        rec = packager.package(plan, None, _make_signal(plan), position_size_shares=10)
        store.save(rec)
        store.record_outcome(
            rec.recommendation_id, "TCS", "NSE", "swing",
            RecommendationOutcome.APPROVED_POSITION_OPENED,
            recorded_at=_OUTCOME_DATE,
        )
        assert store.is_in_cooldown("TCS", "NSE", "swing", date(2024, 6, 3)) is True
        assert store.is_in_cooldown("TCS", "NSE", "swing", date(2024, 6, 9)) is False
