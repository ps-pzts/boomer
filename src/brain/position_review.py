"""Stage 4b — Position review.

Evaluates existing positions for exit signals and computes health scores.
Also implements Q3-2 Option B: immediate re-evaluation for red-flag filings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from brain.models import (
    RED_FLAG_CATEGORIES,
    Direction,
    PositionHealthScore,
    Recommendation,
    RecommendationStatus,
    SignalRecord,
)

IST = ZoneInfo("Asia/Kolkata")

# Confidence threshold below which the signal is considered dead
SIGNAL_DEAD_CONFIDENCE = 0.4
SIGNAL_DEAD_CONSECUTIVE_DAYS = 3

# Drawdown threshold: position in drawdown for >50% of expected hold with no progress
DRAWDOWN_STALE_HOLD_FRACTION = 0.5


@dataclass(frozen=True)
class PositionRecord:
    position_id: str
    stock_symbol: str
    exchange: str
    track: str
    sector: str
    entry_price: Decimal
    current_price: Decimal
    entry_date: datetime
    expected_target: Decimal
    original_stop: Decimal
    signal_id: str  # source signal
    original_signal_confidence: float  # at entry
    # Unused since the swing/long_term tracks were removed — kept for now in case
    # a future track needs thesis-freshness/hold-duration inputs again.
    thesis_signal_last_refreshed_days: int = 0
    days_held: int = 0
    # Minutes until 15:15 forced exit — drives health_score()'s time/thesis factor.
    minutes_to_squareoff: float = 60.0


def _pnl_pct(pos: PositionRecord) -> float:
    if pos.entry_price <= 0:
        return 0.0
    return float((pos.current_price - pos.entry_price) / pos.entry_price * 100)


class PositionReviewer:
    """Evaluates health and recommends exits for open positions (Stage 4b)."""

    def health_score(
        self,
        position: PositionRecord,
        current_signal: SignalRecord | None,
        current_regime: str,
        favourable_regimes: frozenset[str] | None = None,
    ) -> PositionHealthScore:
        """Compute 0–100 health score.

        Components (design doc):
          - P&L vs expected at this point: 40%
          - Signal still firing direction-aligned: 30%
          - Time/thesis factor (track-specific): 15%
          - Regime still favourable: 15%
        """
        pnl = _pnl_pct(position)

        # P&L component (40 pts): full points if at or above midpoint of target
        if position.entry_price > 0:
            target_pct = float(
                (position.expected_target - position.entry_price) / position.entry_price * 100
            )
        else:
            target_pct = 10.0
        pnl_ratio = pnl / target_pct if target_pct > 0 else 0.0
        pnl_component = min(40.0, max(0.0, pnl_ratio * 40.0))

        # Signal alignment component (30 pts)
        if current_signal is None or current_signal.confidence < SIGNAL_DEAD_CONFIDENCE:
            signal_component = 0.0
        elif current_signal.direction == Direction.LONG:
            signal_component = 30.0 * current_signal.confidence
        else:
            signal_component = 0.0

        # Time/thesis factor (15 pts): linear penalty toward forced square-off.
        # Full points at >60 min left, 0 at <=0 min.
        mins_left = position.minutes_to_squareoff
        time_component = max(0.0, min(15.0, (mins_left / 60.0) * 15.0))

        # Regime component (15 pts)
        fav = favourable_regimes or frozenset({"bull_calm", "bull_volatile"})
        regime_component = 15.0 if current_regime in fav else 0.0

        total = pnl_component + signal_component + time_component + regime_component

        exit_recommended = total < 20.0
        exit_reason: str | None = None
        if exit_recommended:
            exit_reason = "health_score_critical"

        return PositionHealthScore(
            position_id=position.position_id,
            stock_symbol=position.stock_symbol,
            track=position.track,
            total_score=round(total, 1),
            pnl_vs_expected=round(pnl_component, 1),
            signal_alignment=round(signal_component, 1),
            time_thesis_factor=round(time_component, 1),
            regime_favorable=round(regime_component, 1),
            exit_recommended=exit_recommended,
            exit_reason=exit_reason,
            scored_at=datetime.now(IST).replace(tzinfo=None),
        )

    def check_thesis_broken(
        self,
        position: PositionRecord,
        current_signal: SignalRecord | None,
        features: dict[str, Any],
    ) -> tuple[bool, str]:
        """Return (broken, reason) based on Stage 4b thesis-broken triggers."""
        if current_signal and current_signal.direction == Direction.SHORT:
            return True, "signal_flipped_short"

        if current_signal and current_signal.confidence < SIGNAL_DEAD_CONFIDENCE:
            return True, f"confidence_below_threshold ({current_signal.confidence:.2f})"

        auditor_change = bool(features.get("has_auditor_change_90d", False))
        pledging_increase = bool(features.get("has_pledging_increase_90d", False))
        if auditor_change:
            return True, "auditor_change_detected"
        if pledging_increase:
            return True, "pledging_increase_detected"

        return False, ""

    def handle_material_filing(
        self,
        filing_category: str,
        filing_symbol: str,
        filing_exchange: str,
        open_positions: list[PositionRecord],
        current_prices: dict[tuple[str, str], Decimal],
        filed_at: datetime,
    ) -> list[Recommendation]:
        """Q3-2 Option B: immediate Stage 4b exit re-evaluation for red-flag filings.

        Only affects positions in the filed ticker. New entries are NOT generated here.
        """
        if filing_category.lower() not in RED_FLAG_CATEGORIES:
            return []

        affected = [
            p
            for p in open_positions
            if p.stock_symbol == filing_symbol and p.exchange == filing_exchange
        ]
        if not affected:
            return []

        recs: list[Recommendation] = []
        for pos in affected:
            ltp = current_prices.get((pos.stock_symbol, pos.exchange))
            if ltp is None:
                ltp = pos.current_price

            rec = Recommendation(
                recommendation_id=str(uuid.uuid4()),
                plan_id="",  # no new plan; this is an exit recommendation
                signal_id=pos.signal_id,
                stock_symbol=pos.stock_symbol,
                exchange=pos.exchange,
                track=pos.track,
                direction=Direction.NEUTRAL,  # exit signal
                entry_zone_low=ltp,
                entry_zone_high=ltp,
                stop_loss_price=Decimal("0"),
                target_price=Decimal("0"),
                position_size_shares=0,
                entry_strategy_id=None,
                status=RecommendationStatus.GENERATED,
                decision_reason=f"red_flag_filing:{filing_category}",
                operator_modified=False,
                original_params=None,
                portfolio_impact=None,
                generated_at=filed_at,
                intent=pos.track,
            )
            recs.append(rec)

        return recs
