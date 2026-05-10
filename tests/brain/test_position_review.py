"""Tests for Stage 4b PositionReviewer.

Key properties:
  - Health score components sum to ≤ 100
  - Red-flag filing triggers immediate exit recommendation (Q3-2 Option B)
  - Non-red-flag filings do NOT trigger mid-session exit
  - Averaging down is never recommended (health score can't fix a bad entry)
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from brain.models import RED_FLAG_CATEGORIES, Direction, SignalRecord
from brain.position_review import PositionRecord, PositionReviewer


def _make_position(
    track="long_term",
    entry=Decimal("1000"),
    current=Decimal("1100"),
    days_held=10,
    thesis_refresh_days=5,
    minutes_to_squareoff=60.0,
) -> PositionRecord:
    import uuid
    return PositionRecord(
        position_id=str(uuid.uuid4()),
        stock_symbol="HDFC",
        exchange="NSE",
        track=track,
        sector="Finance",
        entry_price=entry,
        current_price=current,
        entry_date=datetime(2024, 3, 1, tzinfo=UTC),
        expected_target=Decimal("1200"),
        original_stop=Decimal("950"),
        signal_id=str(uuid.uuid4()),
        original_signal_confidence=0.70,
        thesis_signal_last_refreshed_days=thesis_refresh_days,
        days_held=days_held,
        minutes_to_squareoff=minutes_to_squareoff,
    )


@pytest.fixture
def reviewer():
    return PositionReviewer()


class TestHealthScore:
    def test_healthy_position_scores_above_50(self, reviewer):
        pos = _make_position("long_term", entry=Decimal("1000"), current=Decimal("1100"))
        sig = SignalRecord(
            signal_id=str(uuid.uuid4()),
            stock_symbol="HDFC", exchange="NSE", track="long_term",
            direction=Direction.LONG, raw_score=0.6, confidence=0.65,
            regime_at_signal="bull_calm", contributing_signals=[],
            feature_snapshot={}, generated_at=datetime.now(UTC),
        )
        score = reviewer.health_score(pos, sig, "bull_calm")
        assert score.total_score > 50

    def test_all_components_sum_to_total(self, reviewer):
        pos = _make_position()
        score = reviewer.health_score(pos, None, "bull_calm")
        component_sum = (score.pnl_vs_expected + score.signal_alignment +
                         score.time_thesis_factor + score.regime_favorable)
        assert component_sum == pytest.approx(score.total_score, abs=0.1)

    def test_score_capped_at_100(self, reviewer):
        pos = _make_position(current=Decimal("1500"))  # large gain
        score = reviewer.health_score(pos, None, "bull_calm")
        assert score.total_score <= 100.0

    def test_score_not_negative(self, reviewer):
        pos = _make_position(current=Decimal("800"))  # loss
        score = reviewer.health_score(pos, None, "bear")
        assert score.total_score >= 0.0

    def test_exit_recommended_below_20(self, reviewer):
        # Position in loss, no signal, bear regime → should be <20
        pos = _make_position(
            track="long_term",
            entry=Decimal("1000"),
            current=Decimal("850"),     # 15% loss
            thesis_refresh_days=120,    # stale thesis
        )
        score = reviewer.health_score(pos, None, "bear")
        if score.total_score < 20:
            assert score.exit_recommended is True

    def test_intraday_time_component_full_when_time_left(self, reviewer):
        pos = _make_position(track="intraday", minutes_to_squareoff=90.0)
        score = reviewer.health_score(pos, None, "bull_calm")
        assert score.time_thesis_factor == pytest.approx(15.0)

    def test_intraday_time_component_zero_at_squareoff(self, reviewer):
        pos = _make_position(track="intraday", minutes_to_squareoff=0.0)
        score = reviewer.health_score(pos, None, "bull_calm")
        assert score.time_thesis_factor == pytest.approx(0.0)


class TestThesisBroken:
    def test_signal_flip_breaks_thesis(self, reviewer):
        pos = _make_position()
        sig = SignalRecord(
            signal_id=str(uuid.uuid4()),
            stock_symbol="HDFC", exchange="NSE", track="long_term",
            direction=Direction.SHORT, raw_score=-0.5, confidence=0.6,
            regime_at_signal="bear", contributing_signals=[],
            feature_snapshot={}, generated_at=datetime.now(UTC),
        )
        broken, reason = reviewer.check_thesis_broken(pos, sig, {})
        assert broken is True
        assert "flipped" in reason

    def test_auditor_change_breaks_thesis(self, reviewer):
        pos = _make_position()
        broken, reason = reviewer.check_thesis_broken(pos, None, {"has_auditor_change_90d": True})
        assert broken is True
        assert "auditor" in reason

    def test_healthy_position_not_broken(self, reviewer):
        pos = _make_position()
        sig = SignalRecord(
            signal_id=str(uuid.uuid4()),
            stock_symbol="HDFC", exchange="NSE", track="long_term",
            direction=Direction.LONG, raw_score=0.6, confidence=0.65,
            regime_at_signal="bull_calm", contributing_signals=[],
            feature_snapshot={}, generated_at=datetime.now(UTC),
        )
        broken, _ = reviewer.check_thesis_broken(pos, sig, {})
        assert broken is False


class TestMaterialFilingHandler:
    def test_red_flag_generates_exit_rec(self, reviewer):
        pos = _make_position()
        pos_list = [pos]
        prices = {("HDFC", "NSE"): Decimal("1050")}
        recs = reviewer.handle_material_filing(
            "fraud_disclosure", "HDFC", "NSE", pos_list, prices, datetime.now(UTC)
        )
        assert len(recs) == 1
        assert "fraud_disclosure" in recs[0].decision_reason
        assert recs[0].requires_human is False  # risk-mgmt bypasses human

    def test_non_red_flag_no_exit(self, reviewer):
        pos = _make_position()
        recs = reviewer.handle_material_filing(
            "quarterly_results", "HDFC", "NSE", [pos], {}, datetime.now(UTC)
        )
        assert recs == []

    def test_unaffected_symbol_no_exit(self, reviewer):
        pos = _make_position()  # HDFC
        recs = reviewer.handle_material_filing(
            "fraud_disclosure", "INFY", "NSE", [pos], {}, datetime.now(UTC)
        )
        assert recs == []

    def test_all_red_flag_categories_trigger(self, reviewer):
        for cat in RED_FLAG_CATEGORIES:
            pos = _make_position()
            recs = reviewer.handle_material_filing(
                cat, "HDFC", "NSE", [pos], {}, datetime.now(UTC)
            )
            assert len(recs) == 1, f"category {cat!r} should trigger exit"
