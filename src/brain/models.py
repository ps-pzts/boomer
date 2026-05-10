from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class RecommendationStatus(StrEnum):
    GENERATED = "generated"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED_BY_APM = "approved_by_apm"
    REJECTED_BY_APM = "rejected_by_apm"
    QUEUED_FOR_EXECUTION = "queued_for_execution"
    SUBMITTED_TO_BROKER = "submitted_to_broker"
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    REJECTED_BY_BROKER = "rejected_by_broker"
    POSITION_OPEN = "position_open"
    POSITION_CLOSED = "position_closed"
    OUTCOME_RECORDED = "outcome_recorded"


class RecommendationOutcome(StrEnum):
    """Outcome category used for cooldown table lookups (Loophole 3)."""

    APPROVED_POSITION_OPENED = "approved_position_opened"
    REJECTED_BY_OPERATOR = "rejected_by_operator"
    EXPIRED = "expired"
    REJECTED_BY_APM = "rejected_by_apm"


class EntryStrategy(StrEnum):
    LT1 = "LT1"  # staged accumulation
    LT2 = "LT2"  # DMA pullback
    LT3 = "LT3"  # valuation-anchored
    SW1 = "SW1"  # breakout with volume
    SW2 = "SW2"  # pullback to support
    SW3 = "SW3"  # catalyst event
    ID1 = "ID1"  # opening range breakout
    ID2 = "ID2"  # VWAP pullback
    ID3 = "ID3"  # gap fade/ride


class SkipReason(StrEnum):
    RR_TOO_LOW = "RR_too_low"
    EV_NEGATIVE = "EV_negative"
    POSITION_TOO_SMALL = "position_too_small"
    LIQUIDITY_GATE = "liquidity_gate"
    TARGET_TOO_CLOSE = "target_too_close"
    DATA_UNAVAILABLE = "data_unavailable"


# Filing categories that trigger immediate Stage 4b re-evaluation (Q3-2 Option B)
RED_FLAG_CATEGORIES: frozenset[str] = frozenset(
    {
        "fraud_disclosure",
        "auditor_change",
        "pledging_increase",
        "promoter_large_sell",
        "regulatory_action",
        "going_concern",
    }
)


@dataclass(frozen=True)
class ContributingSignal:
    name: str
    weight: float
    value: float
    contribution: float  # weight × value


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    stock_symbol: str
    exchange: str
    track: str  # Track enum value
    direction: Direction
    raw_score: float  # -1.0 to +1.0
    confidence: float  # 0.0 to 1.0
    regime_at_signal: str  # Regime enum value
    contributing_signals: list[ContributingSignal]
    feature_snapshot: dict[str, Any]  # exact feature values used
    generated_at: datetime
    generator_version: str = "1.0"


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    signal_id: str
    stock_symbol: str
    exchange: str
    track: str
    direction: Direction
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    expected_reward_per_share: Decimal
    expected_risk_per_share: Decimal
    reward_to_risk: Decimal
    expected_value_per_share: Decimal
    decision: str  # "proceed" | "skip"
    skip_reason: SkipReason | None
    entry_strategy_id: EntryStrategy | None
    created_at: datetime


@dataclass(frozen=True)
class EntryPlan:
    """Output of Stage 3.5 — refined entry parameters for a single tranche or order."""

    strategy: EntryStrategy
    entry_price: Decimal  # limit or stop-buy price
    validity_days: int
    tranche_fraction: Decimal  # 1.0 for single entry, <1.0 for staged
    tranche_index: int = 1  # 1-based
    total_tranches: int = 1
    notes: str = ""


@dataclass
class Recommendation:
    recommendation_id: str
    plan_id: str
    signal_id: str
    stock_symbol: str
    exchange: str
    track: str
    direction: Direction
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    position_size_shares: int
    entry_strategy_id: EntryStrategy | None
    requires_human: bool
    status: RecommendationStatus
    decision_reason: str | None
    operator_modified: bool
    original_params: dict[str, Any] | None
    portfolio_impact: dict[str, Any] | None
    generated_at: datetime
    decided_at: datetime | None = None
    queued_at: datetime | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    closed_at: datetime | None = None
    outcome_recorded_at: datetime | None = None
    realised_pnl: Decimal | None = None
    actual_hold_days: int | None = None
    intent: str | None = None


@dataclass(frozen=True)
class PositionHealthScore:
    """Stage 4b health score, 0–100."""

    position_id: str
    stock_symbol: str
    track: str
    total_score: float  # 0–100
    pnl_vs_expected: float  # 40% weight component (0–40)
    signal_alignment: float  # 30% weight component (0–30)
    time_thesis_factor: float  # 15% weight component (0–15)
    regime_favorable: float  # 15% weight component (0–15)
    exit_recommended: bool
    exit_reason: str | None
    scored_at: datetime


# Cooldown days per outcome and track (Loophole 3)
COOLDOWN_DAYS: dict[RecommendationOutcome, dict[str, int]] = {
    RecommendationOutcome.APPROVED_POSITION_OPENED: {
        "swing": 7,
        "long_term": 30,
        "intraday": 1,
    },
    RecommendationOutcome.REJECTED_BY_OPERATOR: {
        "swing": 0,
        "long_term": 0,
        "intraday": 0,
    },
    RecommendationOutcome.EXPIRED: {
        "swing": 3,
        "long_term": 7,
        "intraday": 0,
    },
    RecommendationOutcome.REJECTED_BY_APM: {
        "swing": 2,
        "long_term": 7,
        "intraday": 0,
    },
}


def cooldown_days_for(outcome: RecommendationOutcome, track: str) -> int:
    return COOLDOWN_DAYS[outcome].get(track, 0)
