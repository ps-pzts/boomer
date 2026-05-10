"""Tests for brain.models — enums, cooldown table, and RED_FLAG_CATEGORIES."""
from brain.models import (
    RED_FLAG_CATEGORIES,
    RecommendationOutcome,
    cooldown_days_for,
)


def test_cooldown_approved_position_opened_swing():
    # Design doc Loophole 3: approved + position opened → 7 days swing cooldown
    assert cooldown_days_for(RecommendationOutcome.APPROVED_POSITION_OPENED, "swing") == 7


def test_cooldown_approved_position_opened_long_term():
    assert cooldown_days_for(RecommendationOutcome.APPROVED_POSITION_OPENED, "long_term") == 30


def test_cooldown_rejected_by_operator_is_zero():
    # Rejected = immediate reset; operator disagrees with signal, not signal validity
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_OPERATOR, "swing") == 0
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_OPERATOR, "long_term") == 0


def test_cooldown_expired_swing():
    assert cooldown_days_for(RecommendationOutcome.EXPIRED, "swing") == 3


def test_cooldown_expired_long_term():
    assert cooldown_days_for(RecommendationOutcome.EXPIRED, "long_term") == 7


def test_cooldown_rejected_by_apm():
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_APM, "swing") == 2
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_APM, "long_term") == 7


def test_red_flag_categories_contains_expected():
    assert "fraud_disclosure" in RED_FLAG_CATEGORIES
    assert "auditor_change" in RED_FLAG_CATEGORIES
    assert "pledging_increase" in RED_FLAG_CATEGORIES
    assert "promoter_large_sell" in RED_FLAG_CATEGORIES


def test_red_flag_categories_is_frozenset():
    assert isinstance(RED_FLAG_CATEGORIES, frozenset)
