"""Stage 5 — Recommendation packager and APM gate.

Routing rules (design doc):
  - Long-term: requires_human=True → status = awaiting_human
  - Swing/intraday: APM auto-decides via circuit-breaker check tree
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from brain.models import (
    EntryPlan,
    Recommendation,
    RecommendationOutcome,
    RecommendationStatus,
    SignalRecord,
    TradePlan,
    cooldown_days_for,
)
from capital.models import Track


class RecommendationPackager:
    """Converts a Stage 3 TradePlan + Stage 3.5 EntryPlan into a Recommendation."""

    def package(
        self,
        plan: TradePlan,
        entry_plan: EntryPlan | None,
        signal: SignalRecord,
        position_size_shares: int,
        portfolio_impact: dict[str, Any] | None = None,
    ) -> Recommendation:
        """Package a trade plan as a recommendation with correct routing."""
        requires_human = plan.track == Track.LONG_TERM

        entry_low = entry_plan.entry_price if entry_plan else plan.entry_zone_low
        entry_high = entry_plan.entry_price if entry_plan else plan.entry_zone_high

        return Recommendation(
            recommendation_id=str(uuid.uuid4()),
            plan_id=plan.plan_id,
            signal_id=plan.signal_id,
            stock_symbol=plan.stock_symbol,
            exchange=plan.exchange,
            track=plan.track,
            direction=plan.direction,
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            stop_loss_price=plan.stop_loss_price,
            target_price=plan.target_price,
            position_size_shares=position_size_shares,
            entry_strategy_id=entry_plan.strategy if entry_plan else plan.entry_strategy_id,
            requires_human=requires_human,
            status=(
                RecommendationStatus.AWAITING_HUMAN if requires_human
                else RecommendationStatus.GENERATED
            ),
            decision_reason=None,
            operator_modified=False,
            original_params=None,
            portfolio_impact=portfolio_impact,
            generated_at=datetime.now(UTC),
            intent=plan.track,
        )

    def apm_decide(
        self,
        recommendation: Recommendation,
        circuit_check_fn: Callable[[Recommendation], tuple[bool, str]],
    ) -> Recommendation:
        """Run APM circuit-breaker check tree for swing/intraday recommendations.

        Args:
            circuit_check_fn: callable that returns (passed, reason).
        """
        if recommendation.requires_human:
            raise ValueError("apm_decide called on a human-routed recommendation")

        passed, reason = circuit_check_fn(recommendation)
        recommendation.status = (
            RecommendationStatus.APPROVED_BY_APM if passed
            else RecommendationStatus.REJECTED_BY_APM
        )
        recommendation.decision_reason = reason if not passed else "all_checks_passed"
        recommendation.decided_at = datetime.now(UTC)
        return recommendation

    def validate_operator_modification(
        self,
        recommendation: Recommendation,
        new_entry_low: Decimal,
        new_entry_high: Decimal,
        new_stop: Decimal,
        new_target: Decimal,
        new_shares: int,
        revalidate_fn: Callable[[Decimal, Decimal, Decimal, Decimal, int], tuple[bool, list[str]]],
    ) -> tuple[bool, list[str]]:
        """Re-run Stage 3 and Stage 4 gates on operator-modified parameters.

        Loophole 8: modifications must pass re-validation before confirm is allowed.
        Returns (valid, failing_checks). If valid, caller may call apply_modification().
        """
        return revalidate_fn(new_entry_low, new_entry_high, new_stop, new_target, new_shares)

    def apply_modification(
        self,
        recommendation: Recommendation,
        new_entry_low: Decimal,
        new_entry_high: Decimal,
        new_stop: Decimal,
        new_target: Decimal,
        new_shares: int,
    ) -> Recommendation:
        """Apply operator modification after successful revalidation."""
        original = {
            "entry_zone_low": str(recommendation.entry_zone_low),
            "entry_zone_high": str(recommendation.entry_zone_high),
            "stop_loss_price": str(recommendation.stop_loss_price),
            "target_price": str(recommendation.target_price),
            "position_size_shares": recommendation.position_size_shares,
        }
        recommendation.original_params = original
        recommendation.operator_modified = True
        recommendation.entry_zone_low = new_entry_low
        recommendation.entry_zone_high = new_entry_high
        recommendation.stop_loss_price = new_stop
        recommendation.target_price = new_target
        recommendation.position_size_shares = new_shares
        return recommendation


class RecommendationStore:
    """Persists recommendations and outcomes; handles cooldown lookups."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def save(self, rec: Recommendation) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO recommendations (
                    recommendation_id, plan_id, signal_id,
                    stock_symbol, exchange, track, direction,
                    entry_zone_low, entry_zone_high, stop_loss_price, target_price,
                    position_size_shares, entry_strategy_id,
                    requires_human, status, decision_reason,
                    operator_modified, original_params, portfolio_impact,
                    generated_at, decided_at, queued_at, submitted_at,
                    filled_at, closed_at, outcome_recorded_at,
                    realised_pnl, actual_hold_days, intent
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    rec.recommendation_id, rec.plan_id, rec.signal_id,
                    rec.stock_symbol, rec.exchange, rec.track, rec.direction,
                    str(rec.entry_zone_low), str(rec.entry_zone_high),
                    str(rec.stop_loss_price), str(rec.target_price),
                    rec.position_size_shares,
                    rec.entry_strategy_id.value if rec.entry_strategy_id else None,
                    1 if rec.requires_human else 0,
                    rec.status.value, rec.decision_reason,
                    1 if rec.operator_modified else 0,
                    json.dumps(rec.original_params) if rec.original_params else None,
                    json.dumps(rec.portfolio_impact) if rec.portfolio_impact else None,
                    rec.generated_at.isoformat(),
                    rec.decided_at.isoformat() if rec.decided_at else None,
                    rec.queued_at.isoformat() if rec.queued_at else None,
                    rec.submitted_at.isoformat() if rec.submitted_at else None,
                    rec.filled_at.isoformat() if rec.filled_at else None,
                    rec.closed_at.isoformat() if rec.closed_at else None,
                    rec.outcome_recorded_at.isoformat() if rec.outcome_recorded_at else None,
                    str(rec.realised_pnl) if rec.realised_pnl is not None else None,
                    rec.actual_hold_days, rec.intent,
                ),
            )

    def record_outcome(
        self,
        recommendation_id: str,
        stock_symbol: str,
        exchange: str,
        track: str,
        outcome: RecommendationOutcome,
        recorded_at: datetime | None = None,
    ) -> None:
        now = (recorded_at or datetime.now(UTC)).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO recommendation_outcomes
                    (outcome_id, recommendation_id,
                     stock_symbol, exchange, track, outcome, recorded_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (str(uuid.uuid4()), recommendation_id,
                 stock_symbol, exchange, track, outcome.value, now),
            )

    def cooldown_days_remaining(
        self,
        stock_symbol: str,
        exchange: str,
        track: str,
        as_of: date,
    ) -> int:
        """Return remaining cooldown days for a stock+track, 0 if no cooldown active."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT outcome, recorded_at FROM recommendation_outcomes
                WHERE stock_symbol = ? AND exchange = ? AND track = ?
                  AND DATE(recorded_at) <= DATE(?)
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (stock_symbol, exchange, track, as_of.isoformat()),
            ).fetchone()

        if row is None:
            return 0

        outcome = RecommendationOutcome(row["outcome"])
        cd = cooldown_days_for(outcome, track)
        if cd == 0:
            return 0

        recorded = date.fromisoformat(row["recorded_at"][:10])
        days_elapsed = (as_of - recorded).days
        remaining = cd - days_elapsed
        return max(0, remaining)

    def is_in_cooldown(
        self,
        stock_symbol: str,
        exchange: str,
        track: str,
        as_of: date,
    ) -> bool:
        return self.cooldown_days_remaining(stock_symbol, exchange, track, as_of) > 0
