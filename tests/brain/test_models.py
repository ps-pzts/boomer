"""Tests for brain.models — enums, cooldown table, and RED_FLAG_CATEGORIES."""

from brain.models import (
    RED_FLAG_CATEGORIES,
    RecommendationOutcome,
    cooldown_days_for,
)


def test_cooldown_approved_position_opened_intraday():
    assert cooldown_days_for(RecommendationOutcome.APPROVED_POSITION_OPENED, "intraday") == 1


def test_cooldown_rejected_by_operator_is_zero():
    # Rejected = immediate reset; operator disagrees with signal, not signal validity
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_OPERATOR, "intraday") == 0


def test_cooldown_expired_intraday():
    assert cooldown_days_for(RecommendationOutcome.EXPIRED, "intraday") == 0


def test_cooldown_rejected_by_apm():
    assert cooldown_days_for(RecommendationOutcome.REJECTED_BY_APM, "intraday") == 0


def test_cooldown_unknown_track_defaults_to_zero():
    assert cooldown_days_for(RecommendationOutcome.APPROVED_POSITION_OPENED, "unknown") == 0


def test_red_flag_categories_contains_expected():
    assert "fraud_disclosure" in RED_FLAG_CATEGORIES
    assert "auditor_change" in RED_FLAG_CATEGORIES
    assert "pledging_increase" in RED_FLAG_CATEGORIES
    assert "promoter_large_sell" in RED_FLAG_CATEGORIES


def test_red_flag_categories_is_frozenset():
    assert isinstance(RED_FLAG_CATEGORIES, frozenset)
